"""Pipeline orchestration helpers (batch-orchestrator, PIPELINE.md §1.1 P1-7).

These three symbols are consumed *only* by the batch-orchestrator skill, so they
live here as a skill-local single-consumer module rather than in the shared
package. They have no dependency on the rest of msa_shared — only the standard
library — so the skill stays self-contained.
"""

from typing import Any, Dict

# Canonical stage order across the whole pipeline. The batch-orchestrator rolls a
# task's per-stage ``{stage}_status`` fields up against this order. ``recognition``
# has no status field (it is the entry stage that creates the task), so it is not
# scanned. Auxiliary branches (``structure`` off xyz, ``time_estimate`` off params)
# need not be present for a task to be considered healthy.
PIPELINE_STAGES = (
    "smiles", "xyz", "structure", "params", "time_estimate", "calc", "plot", "export",
)


def make_batch_id(now=None) -> str:
    """Return a batch identifier ``batch_YYYYMMDD_HHMMSS`` (PIPELINE.md §0)."""
    from datetime import datetime
    dt = now or datetime.now()
    return dt.strftime("batch_%Y%m%d_%H%M%S")


def rollup_pipeline_status(task: Dict[str, Any]) -> str:
    """Roll a task's present ``{stage}_status`` fields up to one pipeline status.

    Order-independent and robust to missing stages. Vocabulary:
      - ``"failed"``  : at least one present stage status == ``"failed"``
      - ``"blocked"`` : at least one present == ``"skipped"`` and none failed
      - ``"ok"``      : >=1 stage status present and all present == ``"success"``
      - ``"pending"`` : no stage status present at all
    """
    present = [task.get(f"{s}_status") for s in PIPELINE_STAGES]
    present = [s for s in present if s]
    if any(s == "failed" for s in present):
        return "failed"
    if any(s == "skipped" for s in present):
        return "blocked"
    if present and all(s == "success" for s in present):
        return "ok"
    return "pending"
