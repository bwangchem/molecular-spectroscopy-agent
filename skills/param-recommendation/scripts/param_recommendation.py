#!/usr/bin/env python3
"""param-recommendation — recommend ORCA calculation parameters for each task by looking up the shared per-spectrum recipe table.

This is a Type-A (preprocessing) MSA skill: pure local Python, no SLURM. It sits
at pipeline stage P1-5, consuming the task_list emitted by smiles-to-xyz and
appending calculation-parameter fields to every task.

SUPPORTED / UNSUPPORTED SYSTEMS (v2.0 scope):
  Supported : closed-shell singlets only. The three system classes
              (neutral / anion / zwitterion) are ALL closed-shell singlets, so
              the spin multiplicity is hard-fixed at 1 for every task
              (functional_basis.SYSTEM_CHARGE_MULT). Charge is 0 for neutral /
              zwitterion and -1 for a mono-anion. ZORA is an orthogonal
              relativistic flag (Br / I), not a system class.
  NOT supported : open-shell systems — radicals, triplets, high-spin states and
              carbenes — are out of scope in this version (deferred to a later
              release). They are intercepted upstream: task-recognition rejects
              open-shell wording and name-to-smiles rejects odd-electron SMILES,
              so such molecules never reach this skill.

Per-task business logic (a thin table lookup — this skill knows no functional /
basis strings itself; all of them live in the skill-local _functional_basis):
  1. spec = task["target_spectra"]; classify the molecule from its SMILES via
     functional_basis.detect_system_type(smiles) -> (system_type, need_zora).
     A ValueError (empty / unparseable SMILES) fails soft, never raises.
  2. solvent_model: the task's explicit solvent_std when present, else the
     per-spectrum default (functional_basis.DEFAULT_SOLVENT; "" / gas for IR & Raman).
  3. recipe = functional_basis.recipe_for(spec, system_type, need_zora,
     solvent_model, target_nucleus); a None recipe (unknown spectrum) fails soft.
  4. copy.deepcopy(recipe) so the shared constant module's return value is never
     mutated in place; stamp solvent_model onto every step.
  5. charge, multiplicity = functional_basis.SYSTEM_CHARGE_MULT[system_type]
     (multiplicity is always 1 — closed-shell singlet).
  6. Append system_type (str) + need_zora (bool) + charge + multiplicity (int) +
     params (per-step dict); mark_success stamps params_status="success".

Input : upstream task_list JSON (--input)
Output: task_list JSON with system_type / need_zora / charge / multiplicity /
        params / params_status appended (--output)
"""
import os
import sys
import json
import argparse
import copy
import csv

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
STAGE_ID = "params"
UPSTREAM_STAGE = "xyz"
# ---- end bootstrap ----

import _functional_basis as functional_basis  # skill-local (single-consumer module)
from msa_shared.constants import solvents


def do_work(task: dict) -> dict:
    """Recommend calculation parameters for a single task.

    Append-only and side-effect free with respect to the task object: it reads
    `task["smiles"]` / `task["target_spectra"]` / `task["solvent_std"]` /
    `task["extra_params"]` but NEVER mutates an existing task field, NEVER touches
    task_id, and NEVER raises. It returns a small result dict; process_task turns
    that into the contract status fields:
      - success: {"ok": True, "system_type": <str>, "need_zora": <bool>,
                  "charge": <int>, "multiplicity": <int>, "params": <per-step dict>}
      - failure: {"ok": False, "error": <one-line reason>}

    The system class (neutral / anion / zwitterion) and the ZORA flag come from an
    RDKit reading of the SMILES (functional_basis.detect_system_type); every
    functional / dispersion / basis / calc_type / block value then comes from the
    shared recipe table (functional_basis.recipe_for). This skill hard-codes none
    of them. The solvent is the task's explicit solvent_std when present, else the
    per-spectrum default (functional_basis.DEFAULT_SOLVENT).
    """
    spec = (task.get("target_spectra") or "").strip().lower()

    smiles = (task.get("smiles") or "").strip()
    if not smiles:
        return {"ok": False, "error": "smiles is empty or missing"}

    # Classify the molecule (charge class + orthogonal ZORA flag) from its SMILES.
    try:
        system_type, need_zora = functional_basis.detect_system_type(smiles)
        contains_iodine = functional_basis.contains_iodine(smiles)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    # Resolve the solvent: an explicit task solvent wins; otherwise use the
    # per-spectrum default (which is "" / gas for IR & Raman).
    explicit = solvents.solvent_model_from_std(task.get("solvent_std"))
    default = functional_basis.DEFAULT_SOLVENT.get(spec, "")
    solvent_model = explicit if explicit else (default or None)

    extra = task.get("extra_params") or {}
    target_nucleus = extra.get("target_nucleus")

    recipe = functional_basis.recipe_for(
        spec, system_type=system_type, need_zora=need_zora,
        solvent_model=solvent_model, target_nucleus=target_nucleus,
        contains_iodine=contains_iodine,
    )
    if recipe is None:
        # Unknown spectrum type -> fail soft, never raise.
        return {"ok": False, "error": f"unsupported target_spectra: '{spec}'"}

    # Deep-copy so the shared constant module's return value is never mutated.
    params = copy.deepcopy(recipe)
    params_fallbacks = functional_basis.fallback_recipes_for(
        spec, system_type=system_type, need_zora=need_zora,
        solvent_model=solvent_model, target_nucleus=target_nucleus,
        contains_iodine=contains_iodine,
    )
    charge, multiplicity = functional_basis.SYSTEM_CHARGE_MULT[system_type]

    return {
        "ok": True,
        "system_type": system_type,
        "need_zora": need_zora,
        "charge": charge,
        "multiplicity": multiplicity,
        "params": params,
        "params_fallbacks": params_fallbacks,
    }


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
            system_type=result["system_type"],
            need_zora=result["need_zora"],
            charge=result["charge"],
            multiplicity=result["multiplicity"],
            params=result["params"],
            params_fallbacks=result["params_fallbacks"],
        )
    return mark_failed(task, STAGE_ID, result.get("error", "parameter recommendation failed"))


def _manifest_value(field, raw):
    """Normalize one manifest comparison value without accepting it as authority."""
    text = str(raw or "").strip()
    if field in ("charge", "multiplicity"):
        try:
            return int(float(text))
        except ValueError:
            return text
    if field == "needs_ZORA":
        lowered = text.lower()
        if lowered in ("true", "1", "yes", "y"):
            return True
        if lowered in ("false", "0", "no", "n"):
            return False
    return text


def _report_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_manifest_validation_report(tasks, manifest_path, report_path):
    """Write diagnostic-only manifest differences and return summary counters."""
    with open(manifest_path, encoding="utf-8-sig", newline="") as f:
        manifest_rows = {
            (row.get("mol_id") or "").strip(): row
            for row in csv.DictReader(f)
            if (row.get("mol_id") or "").strip()
        }

    unique_tasks = {}
    for task in tasks:
        mol_id = str(task.get("mol_id") or task.get("molecule_name") or "").strip()
        if mol_id and mol_id not in unique_tasks:
            unique_tasks[mol_id] = task

    differences = []
    matched = 0
    for mol_id, task in unique_tasks.items():
        row = manifest_rows.get(mol_id)
        if row is None:
            continue
        smiles = (task.get("smiles") or "").strip()
        try:
            system_type, need_zora = functional_basis.detect_system_type(smiles)
            has_i = functional_basis.contains_iodine(smiles)
            has_br = functional_basis.contains_bromine(smiles)
        except ValueError:
            continue
        charge, multiplicity = functional_basis.SYSTEM_CHARGE_MULT[system_type]
        derived = {
            "charge": charge,
            "multiplicity": multiplicity,
            "needs_ZORA": need_zora,
        }
        for field, pipeline_value in derived.items():
            raw = row.get(field, "")
            manifest_value = _manifest_value(field, raw)
            if manifest_value == pipeline_value:
                matched += 1
                continue
            expected = (
                field == "needs_ZORA" and manifest_value is True and
                pipeline_value is False and has_i and not has_br
            )
            classification = "expected_rule_change" if expected else "mismatch"
            reason = (
                "意见1由 Br-or-I 改为 Br-only"
                if expected else
                "manifest value differs from independently derived RDKit value"
            )
            differences.append({
                "mol_id": mol_id,
                "field": field,
                "manifest_value": _report_value(manifest_value),
                "pipeline_value": _report_value(pipeline_value),
                "classification": classification,
                "reason": reason,
            })

    fieldnames = [
        "mol_id", "field", "manifest_value", "pipeline_value",
        "classification", "reason",
    ]
    parent = os.path.dirname(os.path.abspath(report_path))
    os.makedirs(parent, exist_ok=True)
    with open(report_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(differences)

    return {
        "checked": len(unique_tasks),
        "matched": matched,
        "expected_rule_change": sum(
            row["classification"] == "expected_rule_change" for row in differences
        ),
        "mismatch": sum(row["classification"] == "mismatch" for row in differences),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="param-recommendation — task_list processor")
    parser.add_argument("--input", required=True, help="input task_list JSON path")
    parser.add_argument("--output", required=True, help="output task_list JSON path")
    parser.add_argument("--manifest", help="optional source manifest CSV for diagnostic comparison")
    parser.add_argument("--validation-report", help="CSV path for manifest comparison differences")
    args = parser.parse_args()

    if bool(args.manifest) != bool(args.validation_report):
        print("ERROR: --manifest and --validation-report must be supplied together", file=sys.stderr)
        sys.exit(1)

    log_startup("param-recommendation")
    ensure_deps(MSA_REQUIRES, "param-recommendation")
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

    if args.manifest:
        try:
            counts = write_manifest_validation_report(
                tasks, args.manifest, args.validation_report,
            )
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: cannot write manifest validation report: {e}", file=sys.stderr)
            sys.exit(1)
        print(
            "MANIFEST_VALIDATION: "
            f"checked={counts['checked']} matched={counts['matched']} "
            f"expected_rule_change={counts['expected_rule_change']} "
            f"mismatch={counts['mismatch']} -> {args.validation_report}",
            file=sys.stderr,
        )

    n_ok = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "success")
    n_fail = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "failed")
    n_skip = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "skipped")
    print(
        f"SUCCESS: param-recommendation processed {len(tasks)} tasks "
        f"(ok={n_ok}, failed={n_fail}, skipped={n_skip}) -> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
