#!/usr/bin/env python3
"""smiles-to-xyz — generate an initial 3D geometry (.xyz) from each task's SMILES using RDKit.

This is a Type-A (preprocessing) MSA skill: pure local Python, no SLURM. It sits
at pipeline stage P1-3, consuming the task_list emitted by name-to-smiles and
appending geometry fields to every task.

Per-task business logic (adapted from the archived smiles-to-3d embed logic, but
writing XYZ instead of SDF and operating on a task_list instead of a bare string):
  1. Chem.MolFromSmiles(smiles) — an unparseable SMILES yields None and fails soft.
  2. Chem.AddHs(mol)            — explicit hydrogens, so num_atoms counts them.
  3. AllChem.EmbedMolecule      — ETKDG defaults first, then a useRandomCoords
                                  fallback for stubborn molecules; still-failed
                                  embedding fails soft.
  4. AllChem.MMFFOptimizeMolecule — best-effort cleanup; its return code is ignored.
  5. Chem.MolToXYZFile(mol, path) — path always comes from naming.xyz_initial().

Input : upstream task_list JSON (--input)
Output: task_list JSON with xyz_file / num_atoms / xyz_status appended (--output)
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
STAGE_ID = "xyz"
UPSTREAM_STAGE = "smiles"
# ---- end bootstrap ----


def _embed_3d(mol):
    """Embed a 3D conformer into `mol`. Return True on success, False on failure.

    Robustness ladder: try RDKit's ETKDG defaults first; if that fails (non-zero
    return code) retry once with random starting coordinates, which recovers many
    otherwise-unembeddable molecules. Never raises.
    """
    from rdkit.Chem import AllChem

    if AllChem.EmbedMolecule(mol) == 0:
        return True
    params = AllChem.ETKDG()
    params.useRandomCoords = True
    return AllChem.EmbedMolecule(mol, params) == 0


def do_work(task: dict) -> dict:
    """Generate the initial 3D geometry for a single task.

    Append-only and side-effect free with respect to the task object: it reads
    `task["smiles"]` / `task["molecule_name"]` but NEVER mutates an existing task
    field and NEVER raises. It returns a small result dict; process_task turns
    that into the contract status fields:
      - success: {"ok": True, "xyz_file": <str>, "num_atoms": <int>}
      - failure: {"ok": False, "error": <one-line reason>}
    """
    from rdkit import Chem
    from rdkit import RDLogger
    from rdkit.Chem import AllChem  # noqa: F401 — ensures the module is importable

    RDLogger.DisableLog("rdApp.*")  # silence third-party stderr noise

    smiles = (task.get("smiles") or "").strip()
    if not smiles:
        return {"ok": False, "error": "smiles is empty or missing"}

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"ok": False, "error": f"invalid SMILES: '{smiles}'"}

    mol = Chem.AddHs(mol)

    if not _embed_3d(mol):
        return {"ok": False, "error": f"3D embedding failed for SMILES: '{smiles}'"}

    # Force-field cleanup is best effort; a non-zero/failed optimization is not fatal.
    try:
        AllChem.MMFFOptimizeMolecule(mol)
    except Exception:  # noqa: BLE001 — optimization must never break the batch
        pass

    num_atoms = int(mol.GetNumAtoms())

    xyz_path = naming.xyz_initial(task["molecule_name"])
    Chem.MolToXYZFile(mol, xyz_path)

    return {"ok": True, "xyz_file": xyz_path, "num_atoms": num_atoms}


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
            task, STAGE_ID, xyz_file=result["xyz_file"], num_atoms=result["num_atoms"]
        )
    return mark_failed(task, STAGE_ID, result.get("error", "3D geometry generation failed"))


def main() -> None:
    parser = argparse.ArgumentParser(description="smiles-to-xyz — task_list processor")
    parser.add_argument("--input", required=True, help="input task_list JSON path")
    parser.add_argument("--output", required=True, help="output task_list JSON path")
    args = parser.parse_args()

    log_startup("smiles-to-xyz")
    ensure_deps(MSA_REQUIRES, "smiles-to-xyz")
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
        f"SUCCESS: smiles-to-xyz processed {len(tasks)} tasks "
        f"(ok={n_ok}, failed={n_fail}, skipped={n_skip}) -> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
