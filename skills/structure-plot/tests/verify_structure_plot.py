#!/usr/bin/env python3
"""verify_structure_plot.py — acceptance harness owned by the main session.

Runs three gates against the outsource-produced structure-plot skill:
  1. static lint  (msa-skill-spec validator)
  2. smoke        (--help exits 0, no traceback)
  3. golden       (hand-crafted upstream task_list in, checked field-by-field)

structure-plot renders a 2D molecular depiction (PNG, via RDKit Draw + Cairo)
from each task's SMILES. The exact pixels are renderer-version dependent and
therefore NOT asserted; what IS checked: a PNG file is written at the
naming-convention path, it is non-empty and carries the PNG magic header,
upstream-failed tasks are skipped, and an invalid SMILES fails soft. The skill
is run inside a temporary working directory so its relative ./local_workspace/
output lands in throwaway space.

exit 0 = all three gates green; non-zero = at least one failure.
"""

import json
import os
import subprocess
import sys
import tempfile
from typing import List, Dict, Any


def _resolve_repo_root() -> str:
    # Self-locate the repo root from this test's own path; no env var needed.
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    if not os.path.isdir(os.path.join(root, "msa_shared")):
        sys.stderr.write(f"ERROR: repo root '{root}' has no msa_shared/.\n")
        sys.exit(3)
    return root


REPO_ROOT = _resolve_repo_root()
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "structure-plot")
ENTRY = os.path.join(SKILL_DIR, "scripts", "structure_plot.py")
SPEC_VALIDATOR = os.path.join(REPO_ROOT, "meta", "msa-skill-spec", "scripts", "validate_skill.py")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def run(cmd: List[str], env=None, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env, cwd=cwd)


def step_lint() -> Dict[str, Any]:
    r = run(["python3", SPEC_VALIDATOR, SKILL_DIR])
    return {"step": "lint", "passed": r.returncode == 0, "returncode": r.returncode,
            "stderr_tail": r.stderr.strip().splitlines()[-4:]}


def step_help() -> Dict[str, Any]:
    r = run(["python3", ENTRY, "--help"])
    passed = r.returncode == 0 and "Traceback" not in (r.stderr + r.stdout)
    return {"step": "help", "passed": passed, "returncode": r.returncode}


def _input_tasks() -> List[Dict[str, Any]]:
    def t(tid, mol, smi, spec, smiles_status="success"):
        return {
            "task_id": tid, "molecule_name": mol, "target_spectra": spec,
            "solvent_raw": "", "solvent_std": "", "extra_params": {},
            "recognition_status": "success",
            "smiles": smi, "smiles_source": "dict", "smiles_status": smiles_status,
        }
    return [
        t("task_01_苯_ir", "苯", "c1ccccc1", "ir"),
        t("task_02_甲醇_nmr", "甲醇", "CO", "nmr"),
        # upstream (smiles) failed -> must skip, no png
        t("task_03_苯_uvvis", "苯", "c1ccccc1", "uvvis", smiles_status="failed"),
        # invalid smiles but upstream success -> fail-soft
        t("task_04_坏分子_ir", "坏分子", "Q!!notvalid", "ir"),
    ]


def check(cond: bool, msg: str, failures: List[str]) -> None:
    if not cond:
        failures.append(msg)


def _is_png(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        return head == _PNG_MAGIC and os.path.getsize(path) > 0
    except Exception:  # noqa: BLE001
        return False


def step_golden() -> Dict[str, Any]:
    failures: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.json")
        out_path = os.path.join(td, "out.json")
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump({"parse_batch_id": "parse_test", "task_list": _input_tasks(),
                       "parse_warnings": [], "parse_errors": []}, f, ensure_ascii=False)

        env = dict(os.environ)
        # Run inside td so the skill's relative ./local_workspace/ lands here.
        r = run(["python3", ENTRY, "--input", in_path, "--output", out_path], env=env, cwd=td)

        if r.returncode != 0:
            return {"step": "golden", "passed": False, "n_pass": 0, "n_total": 1,
                    "fatal": f"skill exited {r.returncode}", "stderr_tail": r.stderr[-600:]}
        try:
            payload = json.load(open(out_path, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            return {"step": "golden", "passed": False, "fatal": f"cannot read output: {e}",
                    "stderr_tail": r.stderr[-600:]}

        tasks = payload["task_list"] if isinstance(payload, dict) and "task_list" in payload else payload
        by_id = {t["task_id"]: t for t in tasks}

        check(len(tasks) == 4, f"expected 4 tasks out, got {len(tasks)}", failures)

        # G1..G2 successes: PNG written on disk, valid magic, append-only
        for tid, mol in (("task_01_苯_ir", "苯"), ("task_02_甲醇_nmr", "甲醇")):
            t = by_id.get(tid, {})
            check(t.get("structure_status") == "success",
                  f"{mol}: structure_status={t.get('structure_status')} (want success)", failures)
            png_rel = t.get("structure_png")
            check(bool(png_rel), f"{mol}: empty structure_png", failures)
            if png_rel:
                abs_path = png_rel if os.path.isabs(png_rel) else os.path.join(td, png_rel)
                check(os.path.isfile(abs_path), f"{mol}: structure_png not found on disk: {png_rel}", failures)
                check(_is_png(abs_path), f"{mol}: structure_png is not a valid non-empty PNG: {png_rel}", failures)
            # append-only: upstream fields untouched
            check(t.get("molecule_name") == mol, f"{tid}: molecule_name mutated", failures)
            check(t.get("smiles_status") == "success", f"{tid}: smiles_status mutated", failures)

        # G3 upstream failed -> skipped, no png
        ts = by_id.get("task_03_苯_uvvis", {})
        check(ts.get("structure_status") == "skipped",
              f"upstream-failed task: structure_status={ts.get('structure_status')} (want skipped)", failures)
        check(not ts.get("structure_png"), "skipped task must not carry a structure_png", failures)

        # G4 invalid smiles -> failed (fail-soft), error present, batch survived
        tg = by_id.get("task_04_坏分子_ir", {})
        check(tg.get("structure_status") == "failed",
              f"invalid smiles: structure_status={tg.get('structure_status')} (want failed)", failures)
        check(bool(tg.get("structure_error")), "failed task must carry structure_error", failures)

    return {"step": "golden", "passed": not failures, "n_pass": 0 if failures else 1,
            "n_total": 1, "failures": failures}


def main() -> None:
    report = {"lint": step_lint(), "help": step_help(), "golden": step_golden()}
    ok = report["lint"]["passed"] and report["help"]["passed"] and report["golden"]["passed"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not ok:
        print("\nFAIL — see failures above", file=sys.stderr)
        sys.exit(1)
    print("\nPASS — all three gates green", file=sys.stderr)


if __name__ == "__main__":
    main()
