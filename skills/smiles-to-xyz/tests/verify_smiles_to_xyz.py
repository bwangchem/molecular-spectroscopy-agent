#!/usr/bin/env python3
"""verify_smiles_to_xyz.py — acceptance harness owned by the main session.

Runs three gates against the outsource-produced smiles-to-xyz skill:
  1. static lint  (msa-skill-spec validator)
  2. smoke        (--help exits 0, no traceback)
  3. golden       (hand-crafted upstream task_list in, checked field-by-field)

Determinism note: RDKit 3D embedding uses random coordinates, so the emitted
COORDINATES are non-deterministic and are NOT asserted. What IS deterministic
and therefore checked: the heavy-atom-plus-hydrogen count (num_atoms), the fact
that an .xyz file is written at the naming-convention path, and that the file's
first line (the XYZ atom count) equals num_atoms. The skill is run inside a
temporary working directory so its relative ./local_workspace/ output lands in
throwaway space.

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
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "smiles-to-xyz")
ENTRY = os.path.join(SKILL_DIR, "scripts", "smiles_to_xyz.py")
SPEC_VALIDATOR = os.path.join(REPO_ROOT, "meta", "msa-skill-spec", "scripts", "validate_skill.py")


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


# Deterministic atom counts (after RDKit AddHs) for the offline-covered molecules.
_EXPECTED_ATOMS = {"苯": 12, "甲醇": 6, "水": 3}


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
        t("task_03_水_raman", "水", "O", "raman"),
        # upstream (smiles) failed -> must skip, no xyz
        t("task_04_苯_uvvis", "苯", "c1ccccc1", "uvvis", smiles_status="failed"),
        # invalid smiles but upstream success -> fail-soft
        t("task_05_坏分子_ir", "坏分子", "Q!!notvalid", "ir"),
    ]


def check(cond: bool, msg: str, failures: List[str]) -> None:
    if not cond:
        failures.append(msg)


def _read_xyz_atom_count(path: str):
    """Return the integer on the first line of an XYZ file, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline().strip()
        return int(first)
    except Exception:  # noqa: BLE001
        return None


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

        check(len(tasks) == 5, f"expected 5 tasks out, got {len(tasks)}", failures)

        # G1..G3 dict-covered successes: file written, num_atoms correct, append-only
        for tid, mol in (("task_01_苯_ir", "苯"), ("task_02_甲醇_nmr", "甲醇"),
                         ("task_03_水_raman", "水")):
            t = by_id.get(tid, {})
            check(t.get("xyz_status") == "success", f"{mol}: xyz_status={t.get('xyz_status')} (want success)", failures)
            check(t.get("num_atoms") == _EXPECTED_ATOMS[mol],
                  f"{mol}: num_atoms={t.get('num_atoms')} (want {_EXPECTED_ATOMS[mol]})", failures)
            xyz_rel = t.get("xyz_file")
            check(bool(xyz_rel), f"{mol}: empty xyz_file", failures)
            if xyz_rel:
                abs_path = xyz_rel if os.path.isabs(xyz_rel) else os.path.join(td, xyz_rel)
                check(os.path.isfile(abs_path), f"{mol}: xyz_file not found on disk: {xyz_rel}", failures)
                n_in_file = _read_xyz_atom_count(abs_path)
                check(n_in_file == _EXPECTED_ATOMS[mol],
                      f"{mol}: xyz first line={n_in_file} (want {_EXPECTED_ATOMS[mol]})", failures)
            # append-only: upstream fields untouched
            check(t.get("molecule_name") == mol, f"{tid}: molecule_name mutated", failures)
            check(t.get("smiles_status") == "success", f"{tid}: smiles_status mutated", failures)

        # G4 upstream failed -> skipped, no xyz
        ts = by_id.get("task_04_苯_uvvis", {})
        check(ts.get("xyz_status") == "skipped", f"upstream-failed task: xyz_status={ts.get('xyz_status')} (want skipped)", failures)
        check(not ts.get("xyz_file"), "skipped task must not carry an xyz_file", failures)

        # G5 invalid smiles -> failed (fail-soft), error present, batch survived
        tg = by_id.get("task_05_坏分子_ir", {})
        check(tg.get("xyz_status") == "failed", f"invalid smiles: xyz_status={tg.get('xyz_status')} (want failed)", failures)
        check(bool(tg.get("xyz_error")), "failed task must carry xyz_error", failures)

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
