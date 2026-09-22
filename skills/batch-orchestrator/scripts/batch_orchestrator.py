#!/usr/bin/env python3
"""batch-orchestrator — the SPECIAL pipeline stage (P1-7) that schedules a run and rolls status up.

Unlike every other MSA skill, this stage does NOT process a molecule and appends
NO per-stage data field of its own. Per meta-skill iron rule 24 and PIPELINE.md
§1.1 its job is exactly two things:

  1. Stamp a single batch-level ``batch_id`` on every task in the run (one id for
     the whole batch), produced by ``make_batch_id()``.
  2. Roll each task's per-stage ``{stage}_status`` fields up into a single
     ``pipeline_status`` via ``rollup_pipeline_status(task)``.

It therefore has NO STAGE_ID and NO UPSTREAM_STAGE, and it never calls
mark_success / mark_failed / mark_skipped / upstream_ok. It fabricates no
``{stage}_status`` field, never rewrites an existing upstream field, and never
modifies ``task_id``. All roll-up rules and the batch-id format live in the
shared helpers — this skill implements no such logic itself.

Input : upstream task_list JSON (--input)
Output: task_list JSON with batch_id / pipeline_status appended (--output)
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
from _orchestration import make_batch_id, rollup_pipeline_status  # skill-local
from msa_shared import naming  # noqa: F401

MSA_CONTRACT_VERSION = "2.0"
MSA_REQUIRES = []  # stdlib only
# ---- end bootstrap ----


def orchestrate_task(task: dict, batch_id: str) -> dict:
    """Stamp the run-wide batch_id and the rolled-up pipeline_status on one task.

    Append-only: it writes exactly two fields (``batch_id`` and
    ``pipeline_status``) and touches nothing else. It never fabricates a
    ``{stage}_status`` field, never rewrites an upstream field, never modifies
    ``task_id``, and never raises — ``rollup_pipeline_status`` never raises.
    """
    task["batch_id"] = batch_id
    task["pipeline_status"] = rollup_pipeline_status(task)
    return task


def main() -> None:
    parser = argparse.ArgumentParser(description="batch-orchestrator — task_list scheduler / status roll-up")
    parser.add_argument("--input", required=True, help="input task_list JSON path (default: none)")
    parser.add_argument("--output", required=True, help="output task_list JSON path (default: none)")
    args = parser.parse_args()

    log_startup("batch-orchestrator")
    ensure_deps(MSA_REQUIRES, "batch-orchestrator")
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

    # One batch_id for the whole run, stamped identically on every task.
    batch_id = make_batch_id()
    tasks = [orchestrate_task(t, batch_id) for t in tasks]
    dump_task_list(tasks, args.output)

    n_ok = sum(1 for t in tasks if t.get("pipeline_status") == "ok")
    n_failed = sum(1 for t in tasks if t.get("pipeline_status") == "failed")
    n_blocked = sum(1 for t in tasks if t.get("pipeline_status") == "blocked")
    n_pending = sum(1 for t in tasks if t.get("pipeline_status") == "pending")
    print(
        f"SUCCESS: batch-orchestrator scheduled {len(tasks)} tasks as {batch_id} "
        f"(ok={n_ok}, failed={n_failed}, blocked={n_blocked}, pending={n_pending}) -> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
