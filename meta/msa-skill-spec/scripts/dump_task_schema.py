#!/usr/bin/env python3
"""dump_task_schema.py — Emit a canonical empty task object for a given stage.

Source of truth: PIPELINE.md §1.1 (mirrored here as STAGE_SCHEMA).

The mirror is not trusted: `--check` re-parses PIPELINE.md §1.1 and fails if
STAGE_SCHEMA has drifted from it (missing stage, missing required field, or a
field the contract table does not declare). Run it after editing either file.

Note: the file-path example values below (`xyz_file`, `structure_png`, `.out`,
`plot_png` / `plot_pdf`, `peaks_csv` / `spectrum_csv`) are shown in illustrative
relative `./local_workspace/...` form for readability. At runtime
`naming.workspace_dir` absolutizes them (`os.path.abspath`), so a real task_list
carries absolute paths (see PIPELINE.md §1.2).

Usage:
    python dump_task_schema.py --stage smiles
    python dump_task_schema.py --stage calc --spec ir
    python dump_task_schema.py --list       # list all known stages
    python dump_task_schema.py --stage xyz --single
    python dump_task_schema.py --check      # guard the mirror against PIPELINE.md §1.1
"""

import argparse
import json
import sys
from typing import Dict, Any

import _contract_table as ct  # spec-local

# Mirror of PIPELINE.md §1.1 append columns. When PIPELINE.md changes, update here
# and re-run `--check`, which is what proves the two are still in sync.
STAGE_SCHEMA: Dict[str, Dict[str, Any]] = {
    "recognition": {
        "producer_skill": "task-recognition",
        "upstream_stage": None,
        "append_fields": {
            "run_id": "run_20260101_120000_a1b2c3",
            "task_id": "task_01_benzene_ir",
            "molecule_name": "benzene",
            "target_spectra": "ir",
            "solvent_raw": "",
            "solvent_std": "gas",
            "extra_params": {},
        },
    },
    "smiles": {
        "producer_skill": "name-to-smiles",
        "upstream_stage": "recognition",
        "append_fields": {
            "smiles": "c1ccccc1",
            "smiles_source": "dict",
            "molecule_name_en": "benzene",
            "smiles_status": "success",
        },
    },
    "xyz": {
        "producer_skill": "smiles-to-xyz",
        "upstream_stage": "smiles",
        "append_fields": {
            "xyz_file": "./local_workspace/benzene/benzene_initial.xyz",
            "xyz_status": "success",
            "num_atoms": 12,
        },
    },
    "structure": {
        "producer_skill": "structure-plot",
        "upstream_stage": "xyz",
        "append_fields": {
            "structure_png": "./local_workspace/benzene/benzene_structure.png",
            "structure_status": "success",
        },
    },
    "params": {
        "producer_skill": "param-recommendation",
        "upstream_stage": "xyz",
        "append_fields": {
            "system_type": "neutral_light",
            "charge": 0,
            "multiplicity": 1,
            "need_zora": False,
            "params": {
                "step1": {
                    "calc_type": "opt+freq",
                    "functional": "r2SCAN-3c",
                    "basis_set": None,
                    "zora": False,
                    "solvent_model": None,
                }
            },
            "params_fallbacks": [
                {
                    "fallback_kind": "basis",
                    "changed_steps": ["step2"],
                    "params": {
                        "step1": {"calc_type": "opt", "basis_set": "def2-TZVP"},
                        "step2": {"calc_type": "freq", "basis_set": "def2-SVP"},
                    },
                }
            ],
            "params_status": "success",
        },
    },
    "time_estimate": {
        "producer_skill": "time-estimation",
        "upstream_stage": "params",
        "append_fields": {
            "time_estimate": {
                "steps": [{"name": "step1", "seconds": 300, "confidence": "medium"}],
                "total_seconds": 300,
                "confidence": "medium",
            },
            "time_estimate_status": "success",
        },
    },
    "orchestration": {
        "producer_skill": "batch-orchestrator",
        # P1-7 is the SPECIAL cross-cutting stage: per its SKILL.md it carries no
        # stage_id and gates on no upstream status field. `upstream_stage` below only
        # places it in pipeline order so a cumulative dump is possible; it is NOT an
        # `upstream_ok()` gate, and this stage appends no `{stage}_status` of its own
        # (iron rule 24).
        "upstream_stage": "time_estimate",
        "cross_cutting": True,
        "append_fields": {
            "batch_id": "batch_20260101_120500",
            "pipeline_status": "ok",
        },
    },
    "calc": {
        "producer_skill": "orca-{spec}",
        "upstream_stage": "params",
        "append_fields": {
            "out_file": "./local_workspace/benzene/benzene_ir.out",
            "calc_status": "success",
            "calc_wallclock": 293,
            "slurm_job_id": "12345",
            "calc_attempts": [
                {
                    "attempt": 0,
                    "params_source": "params",
                    "submission": "single",
                    "changed_steps": [],
                    "status": "success",
                    "failure_reason": "none",
                    "out_file": "./local_workspace/benzene/benzene_ir.out",
                    "slurm_job_id": "12345",
                }
            ],
            "calc_fallback_applied": False,
            "calc_basis_original": None,
            "calc_basis_final": None,
            "calc_basis_fallback": "none",
            "calc_fallback_steps": [],
            "calc_fallback_reason": "none",
            "calc_scf_thresh": None,
        },
    },
    "plot": {
        "producer_skill": "plot-{spec}",
        "upstream_stage": "calc",
        "append_fields": {
            "plot_png": "./local_workspace/benzene/benzene_ir_spectrum.png",
            "plot_pdf": "./local_workspace/benzene/benzene_ir_spectrum.pdf",
            "plot_status": "success",
        },
    },
    "export": {
        "producer_skill": "export-spectrum-data",
        "upstream_stage": "calc",
        "append_fields": {
            "peaks_csv": "./local_workspace/benzene/benzene_ir_peaks.csv",
            "spectrum_csv": "./local_workspace/benzene/benzene_ir_spectrum.csv",
            "export_status": "success",
        },
    },
}


def build_cumulative_task(stage: str, spec: str = "ir") -> Dict[str, Any]:
    """Compose a task object as it would look right AFTER `stage` has run."""
    # Walk up from stage to the root, collecting stages in order.
    chain = []
    cur = stage
    while cur:
        chain.append(cur)
        cur = STAGE_SCHEMA[cur]["upstream_stage"]
    chain.reverse()

    task: Dict[str, Any] = {}
    for s in chain:
        fields = json.loads(json.dumps(STAGE_SCHEMA[s]["append_fields"]))  # deep copy
        # Substitute the illustrative "ir" in paths for the requested spectrum.
        for k, v in list(fields.items()):
            if isinstance(v, str) and "ir" in v and s in ("calc", "plot", "export"):
                fields[k] = v.replace("ir", spec)
        task.update(fields)
    return task


def _row_for_mirror(producer_skill: str, idx) -> "ct.StageRow":
    """Map a STAGE_SCHEMA producer_skill onto its PIPELINE.md §1.1 row.

    STAGE_SCHEMA writes multi-spectrum producers as `orca-{spec}` / `plot-{spec}`,
    while §1.1 writes them as an explicit brace list. Both denote the same row, so a
    `{spec}` placeholder is resolved through one representative spectrum.
    """
    if producer_skill in idx:
        return idx[producer_skill]
    return idx.get(producer_skill.replace("{spec}", "ir"))


def check_mirror() -> int:
    """Fail if STAGE_SCHEMA has drifted from PIPELINE.md §1.1. Returns exit code."""
    msa_root = ct.resolve_repo_root(__file__)
    rows = ct.parse_pipeline_table(ct.pipeline_md_path(msa_root))
    idx = ct.skill_index(rows)
    findings = []

    # (a) every contract row must be mirrored here
    mirrored = set()
    for stage, meta in STAGE_SCHEMA.items():
        row = _row_for_mirror(meta["producer_skill"], idx)
        if row is None:
            findings.append(
                f"ERROR: STAGE_SCHEMA['{stage}'].producer_skill="
                f"'{meta['producer_skill']}' has no PIPELINE.md §1.1 row"
            )
            continue
        mirrored.add(row.stage_label)

        got = set(meta["append_fields"])
        extra = got - row.all_fields
        if extra:
            findings.append(
                f"ERROR: STAGE_SCHEMA['{stage}'] appends {sorted(extra)} which §1.1 row "
                f"'{row.stage_label}' does not declare"
            )
        missing = row.required - got
        if missing:
            findings.append(
                f"ERROR: STAGE_SCHEMA['{stage}'] is missing required field(s) "
                f"{sorted(missing)} declared by §1.1 row '{row.stage_label}'"
            )

    for row in rows:
        if row.stage_label not in mirrored:
            findings.append(
                f"ERROR: PIPELINE.md §1.1 row '{row.stage_label}' "
                f"(producer {row.producer_raw}) is not mirrored in STAGE_SCHEMA"
            )

    # (b) the upstream chain must terminate
    for stage, meta in STAGE_SCHEMA.items():
        seen, cur = set(), stage
        while cur:
            if cur in seen:
                findings.append(f"ERROR: upstream_stage cycle reached from '{stage}'")
                break
            seen.add(cur)
            nxt = STAGE_SCHEMA.get(cur, {}).get("upstream_stage")
            if nxt and nxt not in STAGE_SCHEMA:
                findings.append(
                    f"ERROR: STAGE_SCHEMA['{cur}'].upstream_stage='{nxt}' is unknown"
                )
                break
            cur = nxt

    if findings:
        for f in findings:
            sys.stderr.write(f + "\n")
        sys.stderr.write(
            f"\nFAIL: {len(findings)} drift finding(s) between STAGE_SCHEMA and "
            "PIPELINE.md §1.1.\n"
        )
        return 1

    print(
        f"OK: STAGE_SCHEMA mirrors all {len(rows)} PIPELINE.md §1.1 rows "
        f"({len(idx)} skill names) with no drift."
    )
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stage", help="stage id (e.g. smiles, xyz, calc, plot)")
    p.add_argument("--spec", default="ir", help="spectrum for calc/plot/export stages (default: ir)")
    p.add_argument("--list", action="store_true", help="list known stages and exit")
    p.add_argument("--single", action="store_true", help="dump only this stage's append_fields, not the cumulative task")
    p.add_argument("--check", action="store_true", help="verify STAGE_SCHEMA against PIPELINE.md §1.1 and exit")
    args = p.parse_args()

    if args.check:
        sys.exit(check_mirror())

    if args.list:
        for s, meta in STAGE_SCHEMA.items():
            print(f"{s:20s}  producer={meta['producer_skill']:25s}  upstream={meta['upstream_stage']}")
        sys.exit(0)

    if not args.stage:
        p.error("--stage is required (or use --list)")
    if args.stage not in STAGE_SCHEMA:
        p.error(f"unknown stage '{args.stage}'; use --list to see valid values")

    if args.single:
        payload = STAGE_SCHEMA[args.stage]["append_fields"]
    else:
        payload = build_cumulative_task(args.stage, args.spec)

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
