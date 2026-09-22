#!/usr/bin/env python3
"""name-to-smiles — resolve each task's molecule_name into a canonical SMILES string.

This is a Type-A (preprocessing) MSA skill: pure local Python, no SLURM. It sits
at pipeline stage P1-2, consuming the task_list emitted by task-recognition and
appending SMILES fields to every task.

SMILES resolution is three-tiered, offline-first:
  0. Manifest verbatim  — when --manifest is given, a task whose molecule_name
     matches a manifest `mol_id` takes that row's SMILES exactly as published
     (source="manifest"). Highest priority, no network, and deliberately NO RDKit
     re-canonicalization: rewriting the structure would replace the published
     dataset's own notation with ours, and the whole point of a manifest run is to
     reproduce that dataset bit for bit. This is also the ONLY tier that can
     resolve the `mol_000063`-style names the manifest entry of task-recognition
     emits, since no dictionary or PubChem search knows them.
  1. Offline dictionary  — dicts/name_to_smiles.json (source="dict").
  2. PubChem live lookup — pubchempy fallback for names absent from the dict
     (source="pubchem"), with the result canonicalized through RDKit when RDKit
     is importable.

v2.1 also resolves an English DISPLAY name (`molecule_name_en`) in the same pass,
because every downstream figure title and CSV metadata header must be English
even when the user typed Chinese. Resolution order (expert-mandated):
  manifest `name` -> dictionary `english` -> PubChem `Title` -> PubChem
  `IUPACName` -> the original `molecule_name` when it is already ASCII.
The chosen name is normalized to display case (`p-nitrophenol` -> `p-Nitrophenol`)
by `_display_en`. An unresolved English name is a WARNING, never a failure — the
SMILES is what the pipeline actually computes on.

The environment variable MSA_N2S_DISABLE_NETWORK, when set to "1", forces a fully
offline run: a dictionary miss becomes a fail-soft failure and no network request
is ever made. This keeps batch behaviour deterministic (used by the golden test).

Scope gate (v2.0): after a SMILES is resolved it is screened for open-shell
character (unpaired electrons / odd electron count). The pipeline supports only
closed-shell singlets, so radicals, triplets, high-spin systems and carbenes are
rejected here as a fail-soft failure with a clear message.

Input : upstream task_list JSON (--input), optionally the same manifest CSV the
        batch was expanded from (--manifest)
Output: task_list JSON with smiles / smiles_source / molecule_name_en /
        smiles_status appended (--output)
"""
import os
import sys
import json
import csv
import argparse

# ---- msa_shared bootstrap (DO NOT MODIFY) ----
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from msa_shared.common_utils import (
    load_task_list, dump_task_list,
    mark_success, mark_failed, mark_skipped, upstream_ok, add_warning,
    log_startup, read_shared_version, ensure_deps,
)
from msa_shared import naming

MSA_CONTRACT_VERSION = "2.0"
MSA_REQUIRES = [('rdkit', 'rdkit>=2023.9'), ('pubchempy', 'pubchempy>=1.0.4', 'soft')]
STAGE_ID = "smiles"
UPSTREAM_STAGE = "recognition"
# ---- end bootstrap ----


# ---------- offline dictionary (domain data shipped inside this skill) ----------
# The dictionary is loaded relative to this script, so the skill stays self-contained.
# It maps a Chinese molecule name -> {"smiles": <RDKit canonical>, "english": ...}.
_dict_path = os.path.join(os.path.dirname(__file__), "..", "dicts", "name_to_smiles.json")
_network_flag = "MSA_N2S_DISABLE_NETWORK"

_offline_dict_cache = None


def _load_offline_dict() -> dict:
    """Load and cache the offline name->SMILES dictionary (the `molecules` map)."""
    global _offline_dict_cache
    if _offline_dict_cache is None:
        with open(_dict_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _offline_dict_cache = data.get("molecules", {}) or {}
    return _offline_dict_cache


def _network_disabled() -> bool:
    """True when the offline switch MSA_N2S_DISABLE_NETWORK=="1" is set."""
    return os.environ.get(_network_flag) == "1"


# ---------- Tier 0: the curated manifest (optional, highest priority) ----------
# A manifest run starts at task-recognition's manifest entry, whose molecule_name
# is a `mol_id`. Nothing but the manifest itself can resolve those, so the same
# file has to travel with the task_list — see this skill's SKILL.md and
# task-recognition's Integration section.
_MANIFEST_REQUIRED_COLUMNS = ("mol_id", "smiles")


def load_manifest_smiles(path: str) -> dict:
    """Load ``{mol_id: {"smiles": ..., "english": ...}}`` from a manifest CSV.

    Read as ``utf-8-sig`` because the manifest is routinely exported from a
    spreadsheet, which prepends a BOM; with plain ``utf-8`` the first column name
    silently becomes ``\\ufeffmol_id`` and every lookup misses for an invisible
    reason. Raises ValueError on a missing header or a missing required column —
    the caller turns that into exit 1, because a manifest run whose manifest could
    not be read would fail every single task one by one instead of once, loudly.

    Rows with an empty ``mol_id`` or ``smiles`` are skipped rather than rejected:
    validating the manifest is task-recognition's job (it refuses the whole batch
    over one bad row), and by the time a task_list exists those rows produced no
    task anyway.
    """
    table = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: file is empty (no header row)")
        cols = [c.strip() for c in reader.fieldnames]
        missing = [c for c in _MANIFEST_REQUIRED_COLUMNS if c not in cols]
        if missing:
            raise ValueError(
                f"{path}: header is missing required column(s) {missing}; found {cols}"
            )
        for row in reader:
            clean = {(k.strip() if k else k): (v or "").strip()
                     for k, v in row.items() if k is not None}
            mol_id = clean.get("mol_id", "")
            smiles = clean.get("smiles", "")
            if not mol_id or not smiles:
                continue
            table[mol_id] = {"smiles": smiles, "english": clean.get("name", "")}
    return table


def _canonicalize_smiles(smiles: str) -> str:
    """Canonicalize a SMILES string via RDKit.

    Degrades gracefully: if RDKit is not importable or the string cannot be
    parsed, the original SMILES is returned unchanged. Never raises.
    """
    try:
        from rdkit import Chem
        from rdkit import RDLogger
        RDLogger.DisableLog("rdApp.*")  # silence third-party stderr noise
        mol = Chem.MolFromSmiles(smiles)
        if mol is not None:
            return Chem.MolToSmiles(mol)
    except Exception:  # noqa: BLE001 — missing/broken RDKit must not break the batch
        pass
    return smiles


# ---------- English display name (v2.1) ----------
#
# Downstream figure titles and CSV metadata headers are English-only, so every
# task needs an English rendering of molecule_name. The names we source are
# lower-case common names ("p-nitrophenol"); the expert asked for display case
# ("p-Nitrophenol"), i.e. PubChem Title style: the head word is capitalized while
# locant / stereo prefixes stay lower-case.
#
# The rule itself lives in ``msa_shared.naming.display_name`` — the SAME function
# ``spectrum_style`` uses for figure titles — so the CSV metadata line and the
# figure title can never disagree about how a name is spelled. It only ever
# UPPERCASES a single letter, so acronyms ("THF", "DMSO") and PubChem Titles
# (already correctly cased) pass through untouched.
_display_en = naming.display_name


def _ascii_name(name: str) -> str:
    """The molecule_name itself when the user already typed it in Latin script."""
    text = (name or "").strip()
    return text if text and text.isascii() else ""


# v2.0 scope guard: the pipeline supports ONLY closed-shell singlets
# (param-recommendation fixes multiplicity at 1). Radicals, triplets, high-spin
# systems and carbenes are out of scope; open-shell support is deferred to a
# later version. We reject such species right after SMILES resolution — with a
# clear message — so a wrong closed-shell calculation never runs downstream.
_OPENSHELL_UNSUPPORTED = (
    "open-shell system is not supported: the current version only handles "
    "closed-shell singlets (radicals / triplets / high-spin / carbenes are out "
    "of scope)"
)


def _openshell_reason(smiles: str):
    """Return a one-line reason if `smiles` describes an open-shell species, else None.

    Two independent signals, both keyed on unpaired electrons:
      1. explicit radical electrons (RDKit ``GetNumRadicalElectrons`` > 0);
      2. an odd total electron count (heavy-atom Z + hydrogens - net formal
         charge), which necessarily means at least one unpaired electron even
         when the SMILES draws no explicit radical.
    If RDKit is unavailable or cannot parse the string we cannot judge and
    return None — the gate fails soft and never blocks a resolvable molecule.
    """
    try:
        from rdkit import Chem
        from rdkit import RDLogger
        RDLogger.DisableLog("rdApp.*")
    except Exception:  # noqa: BLE001 — no RDKit: skip the gate, never break batch
        return None
    try:
        mol = Chem.MolFromSmiles(str(smiles))
    except Exception:  # noqa: BLE001
        return None
    if mol is None:
        return None
    if any(a.GetNumRadicalElectrons() for a in mol.GetAtoms()):
        return _OPENSHELL_UNSUPPORTED
    total_electrons = (
        sum(a.GetAtomicNum() for a in mol.GetAtoms())
        + sum(a.GetTotalNumHs() for a in mol.GetAtoms())
        - Chem.GetFormalCharge(mol)
    )
    if total_electrons % 2 == 1:
        return _OPENSHELL_UNSUPPORTED
    return None


def _resolved(smiles: str, source: str, name_en: str = "") -> dict:
    """Turn a resolved SMILES into a success result, unless it is open-shell."""
    reason = _openshell_reason(smiles)
    if reason:
        return {"ok": False, "error": reason}
    return {"ok": True, "smiles": smiles, "source": source,
            "name_en": _display_en(name_en)}


# One PubChem round-trip serves both the SMILES and the English display name, so
# adding molecule_name_en costs no extra network latency.
_PUBCHEM_PROPS = ["IsomericSMILES", "Title", "IUPACName"]


def _absorb(out: dict, props: dict) -> None:
    """Merge one PubChem property record into the accumulator (first value wins)."""
    # PubChem renamed IsomericSMILES -> SMILES; accept whichever the server sent.
    for key, prop in (("smiles", "SMILES"), ("smiles", "IsomericSMILES"),
                      ("title", "Title"), ("iupac", "IUPACName")):
        if not out.get(key):
            value = (props.get(prop) or "").strip() if props.get(prop) else ""
            if value:
                out[key] = value


def _pubchem_lookup(query: str) -> dict:
    """Look up a molecule on PubChem by name.

    Returns ``{"smiles": ..., "title": ..., "iupac": ...}``; any value may be an
    empty string. Adapted from the archived pubchempy implementation: primary
    query via get_properties(name), CID-based fallback via get_compounds. May
    raise on network/HTTP errors — callers must catch.
    """
    import pubchempy as pcp
    out = {"smiles": "", "title": "", "iupac": ""}
    results = pcp.get_properties(_PUBCHEM_PROPS, query, "name")
    if results:
        _absorb(out, results[0])
    if not out["smiles"]:
        compounds = pcp.get_compounds(query, "name")
        if compounds:
            props = pcp.get_properties(_PUBCHEM_PROPS, compounds[0].cid, "cid")
            if props:
                _absorb(out, props[0])
    return out


def do_work(task: dict, manifest: dict = None) -> dict:
    """Resolve a single task's molecule_name to SMILES (and an English display name).

    Append-only and side-effect free: it reads `task["molecule_name"]` but NEVER
    mutates any existing task field and NEVER raises. It returns a small result
    dict describing the outcome; process_task turns that into the contract
    status fields:
      - success: {"ok": True, "smiles": <str>,
                  "source": "manifest"|"dict"|"pubchem",
                  "name_en": <str, "" when unresolved>}
      - failure: {"ok": False, "error": <one-line reason>}

    `manifest` is the optional Tier 0 table from ``load_manifest_smiles``; when it
    is None the behaviour is exactly what it was before Tier 0 existed.
    """
    name = (task.get("molecule_name") or "").strip()
    if not name:
        return {"ok": False, "error": "molecule_name is empty or missing"}

    # Tier 0: the curated manifest wins over everything, verbatim. No network, and
    # no canonicalization pass — the published SMILES is the authority here, not
    # our preferred spelling of the same structure.
    if manifest:
        row = manifest.get(name)
        if isinstance(row, dict) and row.get("smiles"):
            name_en = (row.get("english") or "").strip() or _ascii_name(name)
            return _resolved(row["smiles"], "manifest", name_en)

    # Tier 1: offline dictionary (authoritative primary source). A dictionary hit
    # stays fully offline, so a missing `english` falls back to the name itself
    # rather than reaching for the network.
    entry = _load_offline_dict().get(name)
    if isinstance(entry, dict) and entry.get("smiles"):
        name_en = (entry.get("english") or "").strip() or _ascii_name(name)
        return _resolved(entry["smiles"], "dict", name_en)

    # Offline switch: never touch the network; a dict miss is a fail-soft failure.
    if _network_disabled():
        return {
            "ok": False,
            "error": (
                f"'{name}' not found in offline dictionary and network is "
                f"disabled ({_network_flag}=1)"
            ),
        }

    # Tier 2: PubChem live lookup (canonicalized through RDKit when available).
    try:
        found = _pubchem_lookup(name)
    except Exception as e:  # noqa: BLE001 — network/HTTP errors become fail-soft
        return {"ok": False, "error": f"PubChem lookup failed: {type(e).__name__}: {e}"}
    if found.get("smiles"):
        # Title is the common name a chemist expects on a figure; IUPACName is
        # the systematic long form and only a last resort.
        name_en = found.get("title") or found.get("iupac") or _ascii_name(name)
        return _resolved(_canonicalize_smiles(found["smiles"]), "pubchem", name_en)
    return {"ok": False, "error": f"'{name}' not found in offline dictionary or PubChem"}


def process_task(task: dict, manifest: dict = None) -> dict:
    """Single-task orchestrator. Never raises; enforces the fail-soft contract."""
    if UPSTREAM_STAGE and not upstream_ok(task, UPSTREAM_STAGE):
        return mark_skipped(task, STAGE_ID, UPSTREAM_STAGE)
    try:
        result = do_work(task, manifest)
    except Exception as e:  # noqa: BLE001 — defensive; do_work should not raise
        return mark_failed(task, STAGE_ID, f"{type(e).__name__}: {e}")
    if not result.get("ok"):
        return mark_failed(task, STAGE_ID, result.get("error", "SMILES resolution failed"))

    name_en = result.get("name_en") or ""
    mark_success(
        task, STAGE_ID, smiles=result["smiles"], smiles_source=result["source"],
        molecule_name_en=name_en,
    )
    if not name_en:
        # Cosmetic only: figures fall back to molecule_name, the calculation is
        # unaffected — so this is a warning, never a failure.
        add_warning(
            task, STAGE_ID,
            f"no English name found for '{task.get('molecule_name')}'; "
            f"figure titles and CSV metadata will fall back to molecule_name",
        )
    return task


def main() -> None:
    parser = argparse.ArgumentParser(description="name-to-smiles — task_list processor")
    parser.add_argument("--input", required=True, help="input task_list JSON path")
    parser.add_argument("--output", required=True, help="output task_list JSON path")
    parser.add_argument(
        "--manifest", default=None,
        help="the manifest CSV this batch was expanded from; its `smiles` column "
             "becomes the highest-priority source (Tier 0). MANDATORY when the "
             "task_list came from task-recognition's manifest entry, whose "
             "molecule_name is a mol_id nothing else can resolve.",
    )
    args = parser.parse_args()

    log_startup("name-to-smiles")
    ensure_deps(MSA_REQUIRES, "name-to-smiles")
    shared_v = read_shared_version()
    if shared_v != MSA_CONTRACT_VERSION:
        print(
            f"ERROR: msa_contract_version mismatch (skill={MSA_CONTRACT_VERSION}, shared={shared_v})",
            file=sys.stderr,
        )
        sys.exit(2)

    manifest = None
    if args.manifest:
        # A manifest that cannot be read is a setup error, not a per-task
        # chemistry failure: continuing would mark every task failed and bury the
        # one real cause under thousands of identical messages.
        try:
            manifest = load_manifest_smiles(args.manifest)
        except (IOError, OSError, ValueError, UnicodeDecodeError) as e:
            print(f"ERROR: cannot load manifest from {args.manifest}: {e}",
                  file=sys.stderr)
            sys.exit(1)
        print(f"[name-to-smiles] Tier 0 manifest loaded: {len(manifest)} entries "
              f"from {args.manifest}", file=sys.stderr)

    try:
        tasks = load_task_list(args.input)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: cannot load task_list from {args.input}: {e}", file=sys.stderr)
        sys.exit(1)

    tasks = [process_task(t, manifest) for t in tasks]
    dump_task_list(tasks, args.output)

    n_ok = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "success")
    n_fail = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "failed")
    n_skip = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "skipped")
    print(
        f"SUCCESS: name-to-smiles processed {len(tasks)} tasks "
        f"(ok={n_ok}, failed={n_fail}, skipped={n_skip}) -> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
