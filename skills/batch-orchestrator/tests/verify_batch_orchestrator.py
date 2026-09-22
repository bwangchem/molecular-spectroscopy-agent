#!/usr/bin/env python3
"""verify_batch_orchestrator.py — acceptance harness owned by the main session.

Runs three gates against the outsource-produced batch-orchestrator skill:
  1. static lint  (msa-skill-spec validator)
  2. smoke        (--help exits 0, no traceback)
  3. golden       (hand-crafted mixed-status task_list in, checked field-by-field)

batch-orchestrator is the pipeline's SPECIAL stage (P1-7). Per meta-skill iron
rule 24 it appends NO per-stage data field of its own; per PIPELINE.md §1.1 it
stamps a batch-level `batch_id` on every task and rolls each task's per-stage
`{stage}_status` fields up into a single `pipeline_status`
(ok | failed | blocked | pending). It must NOT invent per-stage fields, must NOT
touch task_id, and must NOT rewrite any upstream `{stage}_status` field.

What this harness checks: every task gets the SAME non-empty batch_id with the
`batch_` prefix; pipeline_status is the correct roll-up for a healthy task, a
failed task, an upstream-skipped (blocked) task, and a never-started (pending)
task; no per-stage status field is fabricated or mutated; task_id is preserved;
and the batch is processed whole (no task dropped, fail-soft).

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
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "batch-orchestrator")
ENTRY = os.path.join(SKILL_DIR, "scripts", "batch_orchestrator.py")
SPEC_VALIDATOR = os.path.join(REPO_ROOT, "meta", "msa-skill-spec", "scripts", "validate_skill.py")

# Any per-stage status field the orchestrator must never fabricate on a task that
# did not already carry it (rule 24: appends no fields of its own).
_STAGE_STATUS_FIELDS = [
    "smiles_status", "xyz_status", "structure_status", "params_status",
    "time_estimate_status", "calc_status", "plot_status", "export_status",
]


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
    base = {"molecule_name": "苯", "target_spectra": "ir", "recognition_status": "success"}
    return [
        # G1 healthy: every present stage succeeded -> ok
        {**base, "task_id": "task_01_ok",
         "smiles_status": "success", "xyz_status": "success",
         "params_status": "success", "time_estimate_status": "success"},
        # G2 failed: a stage failed -> failed
        {**base, "task_id": "task_02_failed",
         "smiles_status": "success", "xyz_status": "failed"},
        # G3 blocked: an upstream skip, nothing failed -> blocked
        {**base, "task_id": "task_03_blocked",
         "smiles_status": "success", "params_status": "skipped"},
        # G4 pending: no stage status at all -> pending
        {**base, "task_id": "task_04_pending"},
    ]


def check(cond: bool, msg: str, failures: List[str]) -> None:
    if not cond:
        failures.append(msg)


def step_golden() -> Dict[str, Any]:
    failures: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        in_tasks = _input_tasks()
        # Snapshot the per-stage status fields each task carried on the way in, so
        # we can prove the orchestrator neither fabricated nor mutated any of them.
        before = {t["task_id"]: {k: t.get(k) for k in _STAGE_STATUS_FIELDS} for t in in_tasks}

        in_path = os.path.join(td, "in.json")
        out_path = os.path.join(td, "out.json")
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump({"parse_batch_id": "parse_test", "task_list": in_tasks,
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

        check(len(tasks) == 4, f"expected 4 tasks out, got {len(tasks)}", failures)

        # batch_id: present, non-empty, batch_-prefixed, identical across all tasks
        batch_ids = {t.get("batch_id") for t in tasks}
        check(len(batch_ids) == 1, f"batch_id not uniform across tasks: {batch_ids}", failures)
        the_id = next(iter(batch_ids)) if batch_ids else None
        check(bool(the_id), "batch_id missing/empty", failures)
        check(isinstance(the_id, str) and the_id.startswith("batch_"),
              f"batch_id not batch_-prefixed: {the_id!r}", failures)

        # pipeline_status roll-up correctness
        expected = {
            "task_01_ok": "ok",
            "task_02_failed": "failed",
            "task_03_blocked": "blocked",
            "task_04_pending": "pending",
        }
        for tid, want in expected.items():
            got = by_id.get(tid, {}).get("pipeline_status")
            check(got == want, f"{tid}: pipeline_status={got!r} (want {want!r})", failures)

        # rule 24: no per-stage status field fabricated or mutated
        for tid, snap in before.items():
            t = by_id.get(tid, {})
            for k, v in snap.items():
                check(t.get(k) == v,
                      f"{tid}: stage field {k} changed {v!r} -> {t.get(k)!r} (orchestrator must not touch it)",
                      failures)
            # task_id preserved
            check(t.get("task_id") == tid, f"{tid}: task_id mutated", failures)

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
