#!/usr/bin/env python3
"""time-estimation — append a rough walltime estimate to every task by looking up the shared per-calc_type timing power laws.

This is a Type-A (preprocessing) MSA skill: pure local Python, no SLURM. It sits
at pipeline stage P1-6, consuming the task_list emitted by param-recommendation
and appending a walltime-estimate field to every task.

Per-task business logic (a thin lookup + arithmetic step — this skill knows no
timing numbers itself; every coefficient lives in msa_shared.constants.timing):
  1. Read params = task["params"] (per-step dict) and num_atoms = task["num_atoms"].
     A missing / empty params dict fails soft, never raises.
  2. Iterate the params steps in sorted key order (step1, step2, ...). For each
     step call timing.estimate_step_seconds(step["calc_type"], num_atoms) and use
     its returned (seconds, confidence) verbatim.
  3. total_seconds = the exact int sum of the per-step seconds (not a re-rounded
     recompute); overall confidence = timing.combine_confidence([...]).
  4. Append the time_estimate object; mark_success stamps
     time_estimate_status="success".

Input : upstream task_list JSON (--input)
Output: task_list JSON with time_estimate / time_estimate_status appended (--output)
"""
import os
import sys
import json
import argparse

# ---- msa_shared bootstrap (DO NOT MODIFY) ----
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from msa_shared.common_utils import (
    load_task_list, dump_task_list,
    mark_success, mark_failed, mark_skipped, upstream_ok,
    log_startup, read_shared_version, ensure_deps,
)
from msa_shared import naming

MSA_CONTRACT_VERSION = "2.0"
MSA_REQUIRES = []  # stdlib only
STAGE_ID = "time_estimate"
UPSTREAM_STAGE = "params"
# ---- end bootstrap ----

import _timing as timing  # skill-local (single-consumer module)


def do_work(task: dict) -> dict:
    """Estimate the walltime for a single task.

    Append-only and side-effect free with respect to the task object: it reads
    `task["params"]` / `task["num_atoms"]` but NEVER mutates an existing task
    field, NEVER touches task_id, and NEVER raises. It returns a small result
    dict; process_task turns that into the contract status fields:
      - success: {"ok": True, "time_estimate": <object>}
      - failure: {"ok": False, "error": <one-line reason>}

    All seconds / confidence values come from the shared timing module
    (timing.estimate_step_seconds / timing.combine_confidence); this skill
    hard-codes no power laws, prefactors, or seconds numbers.
    """
    params = task.get("params")
    if not isinstance(params, dict) or not params:
        # Missing or empty params dict -> fail soft, never raise.
        return {"ok": False, "error": "params is missing or empty"}

    num_atoms = task.get("num_atoms")

    steps = []
    confidences = []
    # Iterate params steps in sorted key order (step1, step2, ...).
    for name in sorted(params.keys()):
        step = params[name]
        calc_type = step.get("calc_type") if isinstance(step, dict) else None
        seconds, confidence = timing.estimate_step_seconds(calc_type, num_atoms)
        steps.append({"name": name, "seconds": seconds, "confidence": confidence})
        confidences.append(confidence)

    # total_seconds is the exact int sum of the per-step seconds, not a
    # re-rounded recompute.
    total_seconds = sum(s["seconds"] for s in steps)
    overall_confidence = timing.combine_confidence(confidences)

    time_estimate = {
        "steps": steps,
        "total_seconds": total_seconds,
        "confidence": overall_confidence,
    }
    return {"ok": True, "time_estimate": time_estimate}


def process_task(task: dict) -> dict:
    """Single-task orchestrator. Never raises; enforces the fail-soft contract."""
    if UPSTREAM_STAGE and not upstream_ok(task, UPSTREAM_STAGE):
        return mark_skipped(task, STAGE_ID, UPSTREAM_STAGE)
    try:
        result = do_work(task)
    except Exception as e:  # noqa: BLE001 — defensive; do_work should not raise
        return mark_failed(task, STAGE_ID, f"{type(e).__name__}: {e}")
    if result.get("ok"):
        return mark_success(
            task, STAGE_ID,
            time_estimate=result["time_estimate"],
        )
    return mark_failed(task, STAGE_ID, result.get("error", "time estimation failed"))


def main() -> None:
    parser = argparse.ArgumentParser(description="time-estimation — task_list processor")
    parser.add_argument("--input", required=True, help="input task_list JSON path (default: none)")
    parser.add_argument("--output", required=True, help="output task_list JSON path (default: none)")
    args = parser.parse_args()

    log_startup("time-estimation")
    ensure_deps(MSA_REQUIRES, "time-estimation")
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
    print(
        f"SUCCESS: time-estimation processed {len(tasks)} tasks "
        f"(ok={n_ok}, failed={n_fail}, skipped={n_skip}) -> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
