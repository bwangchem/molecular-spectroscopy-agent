#!/usr/bin/env python3
"""structure-plot — render a 2D molecular structure image (PNG) from each task's SMILES using RDKit.

This is a Type-A (preprocessing) MSA skill: pure local Python, no SLURM. It sits
at pipeline stage P1-4, consuming the task_list emitted upstream and appending a
2D structure-image path to every task.

Per PIPELINE.md §7 the default structure visualization is 2D (RDKit Draw +
Cairo), not the legacy interactive 3Dmol.js HTML route. Per-task business logic:
  1. Chem.MolFromSmiles(smiles) — an unparseable SMILES yields None and fails
     soft (never raises, never aborts the batch).
  2. rdMolDraw2D.MolDraw2DCairo(500, 400) + PrepareAndDrawMolecule — renders the
     2D depiction (PrepareAndDrawMolecule computes 2D coordinates automatically).
  3. GetDrawingText() returns the PNG byte stream, written "wb" to the path from
     naming.structure_png(molecule_name); filenames are never hand-built.

Upstream stage is `smiles` (not `xyz`): a 2D depiction only needs the SMILES, so
the image can still be produced even if the 3D geometry stage failed, which keeps
the pipeline success rate as high as possible.

Input : upstream task_list JSON (--input)
Output: task_list JSON with structure_png / structure_status appended (--output)
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
MSA_REQUIRES = [('rdkit', 'rdkit>=2023.9')]
STAGE_ID = "structure"
UPSTREAM_STAGE = "smiles"
# ---- end bootstrap ----


def _render_png_bytes(mol) -> bytes:
    """Render a molecule to 2D PNG bytes via RDKit's Cairo backend.

    PrepareAndDrawMolecule adds 2D coordinates and standard drawing options
    automatically, so no explicit Compute2DCoords call is required.
    """
    from rdkit.Chem.Draw import rdMolDraw2D

    d = rdMolDraw2D.MolDraw2DCairo(500, 400)
    rdMolDraw2D.PrepareAndDrawMolecule(d, mol)
    d.FinishDrawing()
    return d.GetDrawingText()


def do_work(task: dict) -> dict:
    """Render the 2D structure image for a single task.

    Append-only and side-effect free with respect to the task object: it reads
    `task["smiles"]` / `task["molecule_name"]` but NEVER mutates an existing task
    field and NEVER raises. It returns a small result dict; process_task turns
    that into the contract status fields:
      - success: {"ok": True, "structure_png": <str>}
      - failure: {"ok": False, "error": <one-line reason>}
    """
    from rdkit import Chem
    from rdkit import RDLogger

    RDLogger.DisableLog("rdApp.*")  # silence third-party stderr noise

    smiles = (task.get("smiles") or "").strip()
    if not smiles:
        return {"ok": False, "error": "smiles is empty or missing"}

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"ok": False, "error": f"invalid SMILES: '{smiles}'"}

    png_bytes = _render_png_bytes(mol)
    if not png_bytes:
        return {"ok": False, "error": f"empty PNG rendered for SMILES: '{smiles}'"}

    png_path = naming.structure_png(task["molecule_name"])
    with open(png_path, "wb") as f:
        f.write(png_bytes)

    return {"ok": True, "structure_png": png_path}


def process_task(task: dict) -> dict:
    """Single-task orchestrator. Never raises; enforces the fail-soft contract."""
    if UPSTREAM_STAGE and not upstream_ok(task, UPSTREAM_STAGE):
        return mark_skipped(task, STAGE_ID, UPSTREAM_STAGE)
    try:
        result = do_work(task)
    except Exception as e:  # noqa: BLE001 — defensive; do_work should not raise
        return mark_failed(task, STAGE_ID, f"{type(e).__name__}: {e}")
    if result.get("ok"):
        return mark_success(task, STAGE_ID, structure_png=result["structure_png"])
    return mark_failed(task, STAGE_ID, result.get("error", "structure rendering failed"))


def main() -> None:
    parser = argparse.ArgumentParser(description="structure-plot — task_list processor")
    parser.add_argument("--input", required=True, help="input task_list JSON path")
    parser.add_argument("--output", required=True, help="output task_list JSON path")
    args = parser.parse_args()

    log_startup("structure-plot")
    ensure_deps(MSA_REQUIRES, "structure-plot")
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
        f"SUCCESS: structure-plot processed {len(tasks)} tasks "
        f"(ok={n_ok}, failed={n_fail}, skipped={n_skip}) -> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
