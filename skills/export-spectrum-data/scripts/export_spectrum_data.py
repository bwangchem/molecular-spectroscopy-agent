#!/usr/bin/env python3
"""export-spectrum-data — dump per-spectrum peaks + broadened-curve CSVs from an ORCA .out.

Type-D (export) MSA skill (Part-3, stage_id=export, upstream=calc). Pure local, no
SLURM. Unlike the plot-{spec} family this one skill handles ALL six spectra: it
delegates the CSV layout (metadata header, precision, which builder fact lands in
which file) to the skill-local ``_export_format`` module, which in turn delegates
every chemistry decision to ``msa_shared.orca_output.build_spectrum`` — the same
source of truth the plot skills use, so a CSV and its PNG always describe
identical peaks and curves.

Each CSV opens with ``# key: value`` metadata lines (molecule, units, broadening,
normalization, Jacobi note, TMS reference, imaginary-frequency / dark-state
diagnostics, source file), then the column header row, then data. Every spectrum
except NMR writes two files — a peaks CSV (all rows ORCA produced, unfiltered and
unformatted) and a three-column broadened-spectrum CSV (x, raw, normalized). NMR
is a stick spectrum and writes only a per-nucleus peaks CSV.

"No valid signal" is a SUCCESSFUL export, not a failure: the peaks CSV is written
in full, the spectrum CSV keeps its metadata and header with no data rows, and
the task carries ``export_signal="none"`` plus the fixed reason in
``export_message``.

Input : upstream task_list JSON (--input)
Output: task_list JSON with export_status / peaks_csv / spectrum_csv (plus
        export_signal / export_message / export_warnings where they apply, or
        export_error on failure) appended (--output)
"""
import csv
import os
import sys
import argparse

# ---- msa_shared bootstrap (DO NOT MODIFY) ----
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from msa_shared.common_utils import (
    load_task_list, dump_task_list,
    mark_success, mark_failed, mark_skipped, mark_no_signal, add_warning,
    upstream_ok, spectrum_kwargs,
    log_startup, read_shared_version, ensure_deps,
)
from msa_shared import naming
import _export_format

MSA_CONTRACT_VERSION = "2.0"
MSA_REQUIRES = [('numpy', 'numpy>=1.24')]
STAGE_ID = "export"
UPSTREAM_STAGE = "calc"
# ---- end bootstrap ----


def _write_csv(path: str, meta, header, rows) -> None:
    """Write a metadata-headed CSV: ``# key: value`` lines, then header, then rows."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        for key, value in meta:
            f.write(f"# {key}: {value}\n")
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _calculation_metadata(task: dict, builder_kwargs: dict):
    """Return stable calculation/fallback metadata for every exported CSV."""
    basis_final = task.get("calc_basis_final") or builder_kwargs.get("basis_set")
    basis_original = task.get("calc_basis_original") or basis_final
    basis_fallback = task.get("calc_basis_fallback")
    if basis_fallback not in {"none", "one_step"}:
        basis_fallback = "one_step" if basis_original != basis_final else "none"

    meta = [
        ("basis_original", basis_original or "none"),
        ("basis_final", basis_final or "none"),
        ("basis_fallback", basis_fallback),
    ]
    spec = (task.get("target_spectra") or "").strip().lower()
    system_type = (task.get("system_type") or "").strip().lower()
    if spec == "nmr" and system_type in {"anion", "zwitterion", "anion_zwitterion"}:
        scf_thresh = task.get("calc_scf_thresh")
        if not scf_thresh:
            final_params = task.get("params") or {}
            for step_name in sorted(final_params, reverse=True):
                step = final_params.get(step_name) or {}
                if step.get("scf_thresh"):
                    scf_thresh = step["scf_thresh"]
                    break
        meta.append(("scf_thresh", scf_thresh or "none"))
    meta.append(("fallback_reason", task.get("calc_fallback_reason") or "none"))

    changed_steps = task.get("calc_fallback_steps") or []
    if spec == "xanes" and "step2" in changed_steps and "step3" not in changed_steps:
        meta.append(("basis_fallback_step", "step2"))
    return meta


def export_task(task: dict) -> dict:
    """Read the .out, build every CSV artifact for the task's spectrum, write them.

    Returns the fields to merge into the task: the written paths plus the
    builder's ``has_signal`` / ``message`` / ``warnings``, which ``process_task``
    turns into the status contract. Raises on a missing .out / unknown spectrum;
    ``process_task`` converts any raise into export_status=failed.
    """
    spec = task.get("target_spectra")
    mol = task["molecule_name"]
    out_file = task.get("out_file")
    if not out_file or not os.path.isfile(out_file):
        raise FileNotFoundError(f"out_file missing or not on disk: {out_file!r}")
    with open(out_file, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    builder_kwargs = spectrum_kwargs(task)
    artifacts, result = _export_format.build_artifacts(
        spec, text, mol, os.path.basename(out_file),
        molecule_en=task.get("molecule_name_en"),
        calculation_meta=_calculation_metadata(task, builder_kwargs),
        **builder_kwargs,
    )  # raises on unknown spec

    # One variant per artifact family: NMR files carry the nucleus, XANES the
    # edge element, so two nuclei of the same molecule never overwrite each other.
    variant = naming.spec_variant(task)
    paths = {}
    for art in artifacts:
        if art["role"] == "peaks":
            path = naming.peaks_csv(mol, spec, variant=variant)
            paths["peaks_csv"] = path
        else:
            path = naming.spectrum_csv(mol, spec, variant=variant)
            paths["spectrum_csv"] = path
        _write_csv(path, art["meta"], art["columns"], art["rows"])

    paths["_has_signal"] = result["has_signal"]
    paths["_message"] = result["message"]
    paths["_warnings"] = result["warnings"]
    return paths


def process_task(task: dict) -> dict:
    """Single-task orchestrator. Never raises; enforces the fail-soft contract.

    Unlike plot-{spec} there is no spec pass-through: this one skill exports every
    spectrum, so it only gates on the upstream ``calc`` stage. A task whose ``calc``
    did not succeed is marked ``export_status="skipped"``. A run that completed but
    produced nothing to broaden is still ``export_status="success"`` (the raw peaks
    table is real data) and additionally carries ``export_signal="none"``.
    """
    if not upstream_ok(task, UPSTREAM_STAGE):
        return mark_skipped(task, STAGE_ID, UPSTREAM_STAGE)
    try:
        fields = export_task(task)
    except Exception as e:  # noqa: BLE001 — fail soft, never abort the batch
        return mark_failed(task, STAGE_ID, f"{type(e).__name__}: {e}")

    has_signal = fields.pop("_has_signal", True)
    message = fields.pop("_message", "")
    warnings = fields.pop("_warnings", [])
    if has_signal:
        mark_success(task, STAGE_ID, **fields)
    else:
        mark_no_signal(task, STAGE_ID, message, **fields)
    for w in warnings:
        add_warning(task, STAGE_ID, w)
    return task


def main() -> None:
    parser = argparse.ArgumentParser(description="export-spectrum-data — CSV exporter")
    parser.add_argument("--input", required=True, help="input task_list JSON path")
    parser.add_argument("--output", required=True, help="output task_list JSON path")
    args = parser.parse_args()

    log_startup("export-spectrum-data")
    ensure_deps(MSA_REQUIRES, "export-spectrum-data")
    shared_v = read_shared_version()
    if shared_v != MSA_CONTRACT_VERSION:
        print(
            f"ERROR: msa_contract_version mismatch (skill={MSA_CONTRACT_VERSION}, shared={shared_v})",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        tasks = load_task_list(args.input)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: cannot load task_list from {args.input}: {e}", file=sys.stderr)
        sys.exit(1)

    tasks = [process_task(t) for t in tasks]
    dump_task_list(tasks, args.output)

    n_ok = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "success")
    n_fail = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "failed")
    n_skip = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "skipped")
    # A no-signal export IS a success (the raw peaks table was written); report it
    # separately so the Agent knows some spectrum CSVs carry no data rows.
    n_nosig = sum(1 for t in tasks if t.get(f"{STAGE_ID}_signal") == "none")
    print(
        f"SUCCESS: export-spectrum-data processed {len(tasks)} tasks "
        f"(ok={n_ok} [no_signal={n_nosig}], failed={n_fail}, skipped={n_skip}) "
        f"-> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
