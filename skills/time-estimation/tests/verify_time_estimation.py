#!/usr/bin/env python3
"""verify_time_estimation.py — acceptance harness owned by the main session.

Runs three gates against the outsource-produced time-estimation skill:
  1. static lint  (msa-skill-spec validator)
  2. smoke        (--help exits 0, no traceback)
  3. golden       (hand-crafted upstream task_list in, checked field-by-field)

time-estimation (pipeline stage P1-6) consumes the params-stage task_list and
appends a `time_estimate` object ({steps:[{name,seconds,confidence}],
total_seconds, confidence}) plus `time_estimate_status` to every task. The
underlying seconds come from the skill-local `_timing.py`; its coefficients are
UNFITTED provisional heuristics — so this harness deliberately
does NOT assert exact seconds. What IS checked: the object shape, that
total_seconds equals the sum of the per-step seconds, that every params step
yields exactly one time_estimate step, a size-monotonicity invariant (a bigger
molecule costs at least as much wall-clock), skipped propagation when the params
upstream failed, and fail-soft when params is present-but-empty. Those invariants
survive any later re-fit of the timing coefficients.

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
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "time-estimation")
ENTRY = os.path.join(SKILL_DIR, "scripts", "time_estimation.py")
SPEC_VALIDATOR = os.path.join(REPO_ROOT, "meta", "msa-skill-spec", "scripts", "validate_skill.py")

_CONF_OK = {"low", "medium", "high"}


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


def _step(calc_type: str) -> Dict[str, Any]:
    # A params step as emitted by param-recommendation. Only calc_type is read by
    # time-estimation; the rest is carried for realism / append-only checks.
    return {"calc_type": calc_type, "functional": "r2SCAN-3c", "basis_set": None,
            "zora": False, "solvent_model": None}


def _task(tid: str, mol: str, spec: str, params: Dict[str, Any], num_atoms: int,
          params_status: str = "success") -> Dict[str, Any]:
    t = {
        "task_id": tid, "molecule_name": mol, "target_spectra": spec,
        "solvent_raw": "", "solvent_std": "", "extra_params": {},
        "recognition_status": "success",
        "smiles": "c1ccccc1", "smiles_source": "dict", "smiles_status": "success",
        "xyz_file": f"./local_workspace/{mol}/{mol}_initial.xyz",
        "xyz_status": "success", "num_atoms": num_atoms,
        "system_type": "neutral_light", "params": params,
        "params_status": params_status,
    }
    if params_status == "failed":
        t["params_error"] = "synthetic upstream failure"
    return t


def _input_tasks() -> List[Dict[str, Any]]:
    return [
        # G1 single-step
        _task("task_01_苯_ir", "苯", "ir", {"step1": _step("opt+freq")}, 12),
        # G2 two-step
        _task("task_02_对硝基苯酚_uvvis", "对硝基苯酚", "uvvis",
              {"step1": _step("opt"), "step2": _step("tddft")}, 16),
        # G3 three-step
        _task("task_03_蒽_fluo", "蒽", "fluo",
              {"step1": _step("gs_opt"), "step2": _step("es_opt"),
               "step3": _step("emission_tddft")}, 24),
        # G4 monotonicity pair — identical recipe, different size
        _task("task_04_small_ir", "small", "ir", {"step1": _step("opt+freq")}, 10),
        _task("task_05_big_ir", "big", "ir", {"step1": _step("opt+freq")}, 40),
        # G5 upstream failed -> skipped
        _task("task_06_苯_nmr", "苯", "nmr",
              {"step1": _step("opt"), "step2": _step("nmr")}, 12, params_status="failed"),
        # G6 params present but empty -> fail-soft
        _task("task_07_坏_ir", "坏", "ir", {}, 12),
    ]


def check(cond: bool, msg: str, failures: List[str]) -> None:
    if not cond:
        failures.append(msg)


def _check_estimate_shape(tid: str, t: Dict[str, Any], n_steps: int, failures: List[str]) -> int:
    """Validate the time_estimate object for a success task; return total_seconds."""
    check(t.get("time_estimate_status") == "success",
          f"{tid}: time_estimate_status={t.get('time_estimate_status')} (want success)", failures)
    te = t.get("time_estimate")
    if not isinstance(te, dict):
        failures.append(f"{tid}: time_estimate is not an object: {te!r}")
        return -1
    steps = te.get("steps")
    check(isinstance(steps, list) and len(steps) == n_steps,
          f"{tid}: expected {n_steps} estimate steps, got {steps!r}", failures)
    summed = 0
    if isinstance(steps, list):
        for s in steps:
            check(isinstance(s, dict), f"{tid}: step is not an object: {s!r}", failures)
            sec = s.get("seconds")
            check(isinstance(sec, int) and not isinstance(sec, bool) and sec > 0,
                  f"{tid}: step seconds not a positive int: {sec!r}", failures)
            check(bool(s.get("name")), f"{tid}: step missing name: {s!r}", failures)
            check(s.get("confidence") in _CONF_OK,
                  f"{tid}: step confidence invalid: {s.get('confidence')!r}", failures)
            if isinstance(sec, int) and not isinstance(sec, bool):
                summed += sec
    total = te.get("total_seconds")
    check(isinstance(total, int) and not isinstance(total, bool) and total > 0,
          f"{tid}: total_seconds not a positive int: {total!r}", failures)
    if isinstance(total, int) and not isinstance(total, bool):
        check(total == summed, f"{tid}: total_seconds {total} != sum of steps {summed}", failures)
    check(te.get("confidence") in _CONF_OK,
          f"{tid}: overall confidence invalid: {te.get('confidence')!r}", failures)
    # append-only: upstream fields untouched
    check(t.get("params_status") == "success", f"{tid}: params_status mutated", failures)
    check(t.get("task_id") == tid, f"{tid}: task_id mutated", failures)
    return total if isinstance(total, int) and not isinstance(total, bool) else -1


def step_golden() -> Dict[str, Any]:
    failures: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.json")
        out_path = os.path.join(td, "out.json")
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump({"parse_batch_id": "parse_test", "task_list": _input_tasks(),
                       "parse_warnings": [], "parse_errors": []}, f, ensure_ascii=False)

        env = dict(os.environ)
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

        check(len(tasks) == 7, f"expected 7 tasks out, got {len(tasks)}", failures)

        # G1..G3 success shapes with 1/2/3 steps
        _check_estimate_shape("task_01_苯_ir", by_id.get("task_01_苯_ir", {}), 1, failures)
        _check_estimate_shape("task_02_对硝基苯酚_uvvis", by_id.get("task_02_对硝基苯酚_uvvis", {}), 2, failures)
        _check_estimate_shape("task_03_蒽_fluo", by_id.get("task_03_蒽_fluo", {}), 3, failures)

        # G4 monotonicity: bigger molecule costs at least as much
        small_total = _check_estimate_shape("task_04_small_ir", by_id.get("task_04_small_ir", {}), 1, failures)
        big_total = _check_estimate_shape("task_05_big_ir", by_id.get("task_05_big_ir", {}), 1, failures)
        if small_total > 0 and big_total > 0:
            check(big_total > small_total,
                  f"monotonicity: big({big_total}) must exceed small({small_total})", failures)

        # G5 upstream params failed -> skipped, no estimate
        ts = by_id.get("task_06_苯_nmr", {})
        check(ts.get("time_estimate_status") == "skipped",
              f"upstream-failed task: time_estimate_status={ts.get('time_estimate_status')} (want skipped)", failures)
        check(not ts.get("time_estimate"), "skipped task must not carry a time_estimate", failures)

        # G6 empty params -> failed (fail-soft), error present, batch survived
        tg = by_id.get("task_07_坏_ir", {})
        check(tg.get("time_estimate_status") == "failed",
              f"empty-params task: time_estimate_status={tg.get('time_estimate_status')} (want failed)", failures)
        check(bool(tg.get("time_estimate_error")), "failed task must carry time_estimate_error", failures)

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
