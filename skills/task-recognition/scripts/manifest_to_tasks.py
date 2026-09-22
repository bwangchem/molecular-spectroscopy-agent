#!/usr/bin/env python3
"""manifest_to_tasks.py — expand a curated manifest CSV into a structured task_list JSON.

This is the SECOND entry point of the task-recognition stage (PIPELINE.md §0).
The first one, ``task_recognition.py``, parses one user's natural language. This
one takes a curated manifest CSV — one row per molecule — and expands it as
molecules x spectra, which is how a whole published dataset gets reproduced
without anyone typing 2991 sentences.

Both entries emit field-for-field identical tasks for the same molecule and the
same spectrum; the only differences are ``run_id`` (per batch) and ``task_id``
(per position). That equivalence is not maintained by hand: every id format, the
global numbering, the status stamping and the batch wrapper are IMPORTED from
``task_recognition`` (msa-skill-spec rule 36). This file only adds what is
genuinely specific to a manifest: reading the CSV, rejecting bad rows, selecting
a subset, and deciding which molecule x spectrum combinations physically exist.

Manifest columns read (per the field-authority table agreed with the data owner):
  mol_id      REQUIRED  -> molecule_name; also the file-naming stem downstream
  smiles      REQUIRED  -> bad-row validation ONLY (the field itself belongs to
                           the name-to-smiles stage, PIPELINE.md §1.1; that skill
                           reads the same manifest as its Tier 0 source)
  formula     REQUIRED  -> presence of H / C, i.e. whether 1H / 13C NMR exists
  elements    optional  -> which XANES K-edges exist (falls back to formula)
  multiplicity optional -> bad-row validation ONLY (open-shell rows are refused)
  category    optional  -> --category selection ONLY, never a chemistry decision

Columns deliberately NOT read: charge, needs_ZORA, cid, inchikey, is_core,
source, name_source, has_*. The pipeline re-derives charge and the ZORA decision
from the structure itself, so reading them here would create a second, silently
diverging source of truth. Unknown columns are ignored, not rejected.

Input : a manifest CSV (--manifest)
Output: wrapped task_list JSON to stdout (or --output), plus an optional
        skipped-combination report CSV (--skipped-report, PIPELINE.md §2.6)

CLI:
  python manifest_to_tasks.py --manifest manifest.csv --output task_list.json
  python manifest_to_tasks.py --manifest manifest.csv --spectra ir,raman --summary
  python manifest_to_tasks.py --manifest manifest.csv --shard 1/8 --run-id run_20260908_120000_ab12cd
"""
import os
import sys
import json
import argparse
import re
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
from msa_shared import naming  # noqa: F401  batch-level products are named by CLI args (§3.1)

MSA_CONTRACT_VERSION = "2.0"
# rdkit is SOFT here, and only for one bad-row check: "this SMILES does not
# parse". Every other validation is pure string work, so a host without rdkit
# still gets a usable batch — it just cannot pre-screen unparseable structures
# (the name-to-smiles stage will fail those tasks individually later).
MSA_REQUIRES = [("rdkit", "rdkit>=2023.9", "soft")]
STAGE_ID = "recognition"
UPSTREAM_STAGE = None
# ---- end bootstrap ----

# Shared batch primitives of THIS stage. Importing them (rather than restating
# the task dict, the id formats or the wrapper) is what structurally guarantees
# that both entry points of task-recognition emit the same fields — rule 36.
from task_recognition import (
    expand_task_list,
    validate_tasks,
    assign_task_ids,
    finalize_batch,
    new_parse_batch_id,
    new_run_id,
    _spectra_synonyms,
)


# ---------- 1. the spectrum vocabulary ----------
# The VOCABULARY is derived from the natural-language entry's synonym table
# rather than retyped: if a new spectrum type is ever recognized there, this
# entry accepts it on the same day and the two can never disagree about what
# "uvvis" is called. Only the listing ORDER is stated here, matching the order
# the contract documents, so that a batch's task ids read ir-first like a
# hand-written request does. A type recognized upstream but absent from the
# order list is still accepted, appended at the end.
_SPECTRA_ORDER = ("ir", "raman", "uvvis", "nmr", "fluo", "xanes")
_RECOGNIZED_SPECTRA = set(_spectra_synonyms.values())
SPECTRA_ALL = (
    tuple(s for s in _SPECTRA_ORDER if s in _RECOGNIZED_SPECTRA)
    + tuple(sorted(_RECOGNIZED_SPECTRA - set(_SPECTRA_ORDER)))
)

# NMR nuclei and XANES K-edges the pipeline supports, in canonical output order.
NMR_NUCLEI = (("1H", "H"), ("13C", "C"))
XANES_EDGES = ("C", "N", "O", "F")

REQUIRED_COLUMNS = ("mol_id", "smiles", "formula")


# ---------- 2. manifest reading ----------

def _symbols(cell):
    """Element symbols occurring in a formula or an element-list cell.

    One regex serves both columns on purpose: ``C6Cl5O``, ``C,Cl,O``, ``CClO``
    and ``['C', 'Cl', 'O']`` all yield {C, Cl, O}. Digits, separators, brackets
    and charge signs are simply not element symbols, so they fall out for free.
    """
    return set(re.findall(r"[A-Z][a-z]?", cell or ""))


def read_manifest(path):
    """Read the manifest CSV; return ``[(csv_line_no, row_dict), ...]``.

    ``utf-8-sig`` because the manifest is routinely exported from a spreadsheet,
    which prepends a BOM; with plain ``utf-8`` the first column name silently
    becomes ``\\ufeffmol_id`` and every required-column check fails for a reason
    nobody can see. Line numbers come from the reader itself so that the errors
    reported to the user point at lines they can actually open in an editor.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: file is empty (no header row)")
        cols = [c.strip() for c in reader.fieldnames]
        missing = [c for c in REQUIRED_COLUMNS if c not in cols]
        if missing:
            raise ValueError(
                f"{path}: header is missing required column(s) {missing}; "
                f"found {cols}"
            )
        rows = []
        for row in reader:
            clean = {(k.strip() if k else k): (v or "").strip()
                     for k, v in row.items() if k is not None}
            rows.append((reader.line_num, clean))
    if not rows:
        raise ValueError(f"{path}: header only, no data rows — nothing to expand")
    return rows


# ---------- 3. bad-row policy: refuse the whole batch ----------

def validate_manifest(rows, rdkit_missing):
    """Return a list of human-readable problems, each naming its CSV line.

    A manifest is a curated artifact meant to reproduce a specific dataset, so a
    single malformed row means the file is not the file we think it is. Expanding
    the other 2990 rows would produce a batch that looks complete, costs weeks of
    compute, and silently misses molecules — therefore ANY bad row aborts the
    whole run (the caller exits 1) instead of being skipped with a warning. All
    problems are collected before returning so one fix-up pass can address them
    all rather than rediscovering them one exit code at a time.
    """
    problems = []
    first_seen = {}

    parse_smiles = None
    if not rdkit_missing:
        from rdkit import Chem
        from rdkit import RDLogger
        RDLogger.DisableLog("rdApp.*")  # parse failures are reported by us, not by rdkit
        parse_smiles = Chem.MolFromSmiles

    for line_no, row in rows:
        mol_id = row.get("mol_id", "")
        smiles = row.get("smiles", "")

        if not mol_id:
            problems.append(f"line {line_no}: empty mol_id")
        elif mol_id in first_seen:
            problems.append(
                f"line {line_no}: duplicate mol_id '{mol_id}' "
                f"(first seen on line {first_seen[mol_id]})"
            )
        else:
            first_seen[mol_id] = line_no

        if not smiles:
            problems.append(f"line {line_no}: empty smiles for mol_id '{mol_id}'")

        # multiplicity is validated but never used as an input: the pipeline
        # re-derives spin from the structure. Its only job here is to refuse
        # open-shell rows early, since the whole pipeline is closed-shell only.
        mult = row.get("multiplicity", "")
        if mult and mult not in ("1", "1.0"):
            problems.append(
                f"line {line_no}: multiplicity={mult!r} for mol_id '{mol_id}' — "
                "only closed-shell singlets (multiplicity 1) are supported"
            )

        if parse_smiles is not None and smiles and parse_smiles(smiles) is None:
            problems.append(
                f"line {line_no}: rdkit cannot parse smiles {smiles!r} "
                f"for mol_id '{mol_id}'"
            )

    return problems


# ---------- 4. row selection (subsetting and sharding) ----------

def _id_sort_key(mol_id):
    """Sort by the numeric suffix of ``mol_000063``-style ids, not lexically.

    Lexical order would put mol_000100 before mol_000063's neighbours only by
    luck of zero-padding; ids without a numeric suffix sort last, deterministically.
    """
    m = re.search(r"(\d+)$", mol_id or "")
    return (0, int(m.group(1)), mol_id) if m else (1, 0, mol_id)


def _id_number(mol_id):
    m = re.search(r"(\d+)$", mol_id or "")
    return int(m.group(1)) if m else None


def parse_shard(spec):
    """Parse ``k/N`` into (k, N) with 1 <= k <= N."""
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", spec or "")
    if not m:
        raise ValueError(f"--shard must look like k/N (e.g. 1/8), got {spec!r}")
    k, n = int(m.group(1)), int(m.group(2))
    if n < 1 or k < 1 or k > n:
        raise ValueError(f"--shard k/N requires 1 <= k <= N, got {spec!r}")
    return k, n


def parse_id_range(spec):
    """Parse ``lo-hi`` (inclusive) over the numeric suffix of mol_id."""
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", spec or "")
    if not m:
        raise ValueError(f"--id-range must look like lo-hi (e.g. 1-500), got {spec!r}")
    lo, hi = int(m.group(1)), int(m.group(2))
    if lo > hi:
        raise ValueError(f"--id-range lo must not exceed hi, got {spec!r}")
    return lo, hi


def shard_members(rows, k, n):
    """The mol_id set belonging to shard ``k`` of ``n``.

    Membership is computed over the FULL manifest, ordered by numeric id, before
    any other selection is applied. That independence is the point: adding
    ``--spectra ir`` or ``--category`` must not move a molecule from shard 3 to
    shard 5, otherwise two runs of the "same" shard cover different molecules.
    """
    ordered = sorted((row.get("mol_id", "") for _, row in rows), key=_id_sort_key)
    return {mol_id for i, mol_id in enumerate(ordered) if i % n == k - 1}


def select_rows(rows, mol_ids=None, id_range=None, categories=None, shard=None):
    """Apply every selection filter; manifest order is preserved throughout."""
    members = shard_members(rows, shard[0], shard[1]) if shard else None
    out = []
    for line_no, row in rows:
        mol_id = row.get("mol_id", "")
        if mol_ids is not None and mol_id not in mol_ids:
            continue
        if id_range is not None:
            num = _id_number(mol_id)
            if num is None or not (id_range[0] <= num <= id_range[1]):
                continue
        if categories is not None and row.get("category", "") not in categories:
            continue
        if members is not None and mol_id not in members:
            continue
        out.append((line_no, row))
    return out


# ---------- 5. molecule x spectrum expansion ----------

def row_configs(row, spectra):
    """Which spectra of this molecule exist, and which cannot exist.

    Returns ``(configs, skipped)``: ``configs`` is the spectra_configs list
    ``expand_task_list`` consumes, ``skipped`` is a list of
    ``(mol_id, target_spectra, variant, reason)`` tuples for the §2.6 report.

    A combination that does not physically exist (1H NMR of a molecule with no
    hydrogen, an N K-edge of a molecule with no nitrogen) is NOT emitted as a
    task with some special status — it never enters the task_list at all, and is
    accounted for in a separate report. See PIPELINE.md §2.6 for why this cannot
    be a fourth ``{stage}_status`` value.

    Presence of H and C is judged from ``formula`` and NEVER from ``elements``:
    the elements column lists heavy atoms only, so using it would silently drop
    every 1H task in the entire batch while looking perfectly healthy.
    """
    mol_id = row.get("mol_id", "")
    present_in_formula = _symbols(row.get("formula", ""))
    # elements drives the XANES edges only; fall back to formula when the column
    # is absent, since formula is a required column and lists the same elements.
    edge_source = row.get("elements") or row.get("formula", "")
    present_edges = _symbols(edge_source)

    configs = []
    skipped = []
    for spec in spectra:
        if spec == "nmr":
            for nucleus, element in NMR_NUCLEI:
                if element in present_in_formula:
                    configs.append({"spec_std": "nmr",
                                    "extra_params": {"target_nucleus": nucleus}})
                else:
                    skipped.append((mol_id, "nmr", nucleus,
                                    f"molecule contains no {element}"))
        elif spec == "xanes":
            for element in XANES_EDGES:
                if element in present_edges:
                    configs.append({"spec_std": "xanes",
                                    "extra_params": {"target_element": element}})
                else:
                    skipped.append((mol_id, "xanes", element,
                                    f"molecule contains no {element}"))
        else:
            configs.append({"spec_std": spec, "extra_params": {}})
    return configs, skipped


def build_batch(rows, spectra, run_id=None, batch_id=None):
    """Expand selected manifest rows into the wrapped batch object.

    ``solvent_raw`` / ``solvent_std`` are left empty on purpose: a manifest
    states what molecules exist, not what solvent they were measured in, and the
    per-spectrum default belongs to the param-recommendation stage. Emitting a
    solvent here would hard-code one entry point's guess into the dataset.
    """
    batch_id = batch_id or new_parse_batch_id()
    run_id = run_id or new_run_id()

    all_tasks = []
    all_skipped = []
    global_idx = 0
    for _line_no, row in rows:
        configs, skipped = row_configs(row, spectra)
        all_skipped.extend(skipped)
        if not configs:
            continue
        # Same constructor as the natural-language entry, called per molecule so
        # that task ids run in manifest order.
        tasks = expand_task_list([row["mol_id"]], configs, "", "")
        global_idx = assign_task_ids(tasks, global_idx)
        all_tasks.extend(tasks)

    result = finalize_batch(all_tasks, [], [], batch_id, run_id)
    return result, all_skipped


def write_skipped_report(path, skipped):
    """Write the §2.6 bypass report: one row per combination that cannot exist."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mol_id", "target_spectra", "variant", "reason"])
        for rec in skipped:
            w.writerow(list(rec))


# ---------- 6. summary ----------

def generate_summary(result, skipped, n_manifest, n_selected):
    """Build a human-readable expansion summary (multi-line text)."""
    lines = [
        "===== manifest expansion =====",
        f"batch id: {result['parse_batch_id']}",
        f"run id: {result['run_id']}",
        f"manifest rows: {n_manifest}",
        f"selected rows: {n_selected}",
        f"tasks: {len(result['task_list'])}",
        f"skipped combinations: {len(skipped)}",
        "",
    ]
    if result["parse_warnings"]:
        lines.append("[warnings]")
        for w in result["parse_warnings"]:
            lines.append(f"  - {w}")
        lines.append("")
    if result["parse_errors"]:
        lines.append("[errors]")
        for e in result["parse_errors"]:
            lines.append(f"  x {e}")
        lines.append("")

    per_spec = {}
    for t in result["task_list"]:
        per_spec[t["target_spectra"]] = per_spec.get(t["target_spectra"], 0) + 1
    if per_spec:
        lines.append("[tasks per spectrum]")
        for spec in sorted(per_spec):
            lines.append(f"  {spec}: {per_spec[spec]}")
        lines.append("")

    per_reason = {}
    for _mol, spec, variant, _reason in skipped:
        key = f"{spec} {variant}".strip()
        per_reason[key] = per_reason.get(key, 0) + 1
    if per_reason:
        lines.append("[skipped per variant]")
        for key in sorted(per_reason):
            lines.append(f"  {key}: {per_reason[key]}")
    return "\n".join(lines)


# ---------- 7. CLI ----------

def _csv_list(value):
    return [v.strip() for v in value.split(",") if v.strip()]


def main():
    ap = argparse.ArgumentParser(
        description="task-recognition (manifest entry): curated manifest CSV -> "
                    "structured task_list JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--manifest", type=str, required=True,
                    help="path to the manifest CSV (columns: mol_id, smiles, formula, "
                         "[elements], [multiplicity], [category])")
    ap.add_argument("--output", type=str, default=None,
                    help="write the JSON to this path (default: stdout)")
    ap.add_argument("--skipped-report", type=str, default=None,
                    help="write the skipped-combination report CSV to this path "
                         "(PIPELINE.md §2.6)")
    ap.add_argument("--run-id", type=str, default=None,
                    help="reuse an existing run_id instead of minting a new one; "
                         "mandatory with --shard so all shards land in one run")
    ap.add_argument("--spectra", type=str, default=None,
                    help=f"comma-separated subset of {','.join(SPECTRA_ALL)} "
                         "(default: all of them)")
    ap.add_argument("--mol-ids", type=str, default=None,
                    help="comma-separated mol_id whitelist")
    ap.add_argument("--id-range", type=str, default=None,
                    help="inclusive range over the numeric suffix of mol_id, e.g. 1-500")
    ap.add_argument("--category", type=str, default=None,
                    help="comma-separated values of the manifest's category column; "
                         "selection only, never a chemistry decision")
    ap.add_argument("--shard", type=str, default=None,
                    help="process shard k of N (e.g. 1/8); membership is computed "
                         "over the full manifest, independent of every other filter")
    ap.add_argument("--summary", action="store_true",
                    help="also print a human-readable summary to stderr")
    ap.add_argument("--indent", type=int, default=2,
                    help="JSON indent (default: 2; pass 0 for compact output)")
    args = ap.parse_args()

    log_startup("task-recognition")
    soft_missing = ensure_deps(MSA_REQUIRES, "task-recognition")
    shared_v = read_shared_version()
    if shared_v != MSA_CONTRACT_VERSION:
        print(
            f"ERROR: msa_contract_version mismatch (skill={MSA_CONTRACT_VERSION}, shared={shared_v})",
            file=sys.stderr,
        )
        sys.exit(2)

    rdkit_missing = "rdkit" in soft_missing
    if rdkit_missing:
        print("WARNING: rdkit unavailable — manifest rows will NOT be checked for "
              "unparseable SMILES; every other bad-row check still applies",
              file=sys.stderr)

    # --- argument validation (all of it before any work) ---
    spectra = list(SPECTRA_ALL)
    if args.spectra:
        spectra = _csv_list(args.spectra)
        unknown = [s for s in spectra if s not in SPECTRA_ALL]
        if unknown:
            print(f"ERROR: unknown spectrum type(s) {unknown}; supported: "
                  f"{list(SPECTRA_ALL)}", file=sys.stderr)
            sys.exit(1)

    shard = None
    if args.shard:
        # A shard is one slice of ONE dataset. Minting a fresh run_id per shard
        # would scatter a single dataset across N run directories, and no
        # downstream stage could ever assemble it back, so this is refused
        # rather than warned about.
        if not args.run_id:
            print("ERROR: --shard requires --run-id, otherwise each shard would "
                  "mint its own run_id and one dataset would be split across "
                  "several run directories", file=sys.stderr)
            sys.exit(1)
        try:
            shard = parse_shard(args.shard)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    id_range = None
    if args.id_range:
        try:
            id_range = parse_id_range(args.id_range)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    mol_ids = set(_csv_list(args.mol_ids)) if args.mol_ids else None
    categories = set(_csv_list(args.category)) if args.category else None

    # --- read + validate the manifest ---
    try:
        rows = read_manifest(args.manifest)
    except (IOError, OSError, ValueError, UnicodeDecodeError) as e:
        print(f"ERROR: cannot read manifest: {e}", file=sys.stderr)
        sys.exit(1)

    problems = validate_manifest(rows, rdkit_missing)
    if problems:
        print(f"ERROR: {len(problems)} bad row(s) in {args.manifest}; the whole "
              "batch is refused so a partial dataset is never produced:",
              file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)

    if categories is not None and "category" not in rows[0][1]:
        print("ERROR: --category was given but the manifest has no 'category' column",
              file=sys.stderr)
        sys.exit(1)

    selected = select_rows(rows, mol_ids=mol_ids, id_range=id_range,
                           categories=categories, shard=shard)
    if not selected:
        print("ERROR: no manifest row survived the selection filters "
              "(--mol-ids / --id-range / --category / --shard)", file=sys.stderr)
        sys.exit(1)

    result, skipped = build_batch(selected, spectra, run_id=args.run_id)

    indent = None if args.indent == 0 else args.indent
    if args.output:
        dump_task_list(result, args.output, indent=indent if indent is not None else 2)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=indent))

    if args.skipped_report:
        write_skipped_report(args.skipped_report, skipped)

    if args.summary:
        print(generate_summary(result, skipped, len(rows), len(selected)),
              file=sys.stderr)

    print(f"SUCCESS: task-recognition expanded {len(selected)} manifest row(s) into "
          f"{len(result['task_list'])} tasks, {len(skipped)} combination(s) skipped "
          f"(batch={result['parse_batch_id']}, run={result['run_id']})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
