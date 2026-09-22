---
name: name-to-smiles
description: Name-to-SMILES resolver for the Molecular Spectroscopy Agent (MSA); it consumes the task_list produced by task-recognition and appends a canonical SMILES plus an English display name to every task. Resolution is three-tiered and offline-first: an optional curated manifest CSV passed via --manifest (`smiles_source="manifest"`, taken verbatim, highest priority), then the shipped dictionary (dicts/name_to_smiles.json), then a live PubChem lookup canonicalized through RDKit. The same pass resolves `molecule_name_en` (manifest `name` -> dictionary `english` -> PubChem `Title` -> PubChem `IUPACName` -> the molecule_name itself when already ASCII), normalized to display case such as `p-Nitrophenol`; this is the ONLY source of the English name every downstream figure title and CSV metadata header uses. Trigger keywords molecule_name -> SMILES, name to SMILES, resolve SMILES, English molecule name, manifest SMILES, P1-2 preprocessing stage. When the pipeline needs to convert recognized molecule names into SMILES before geometry generation, the Agent MUST load this skill BEFORE running smiles-to-xyz, and MUST pass --manifest whenever the task_list came from a manifest batch. DO NOT hand-construct the task_list, DO NOT edit dicts/name_to_smiles.json, DO NOT re-canonicalize a manifest SMILES, DO NOT hand-write molecule_name_en in a downstream skill, DO NOT rewrite any upstream field.
version: 0.3.0
msa_contract_version: 2.0
---

# name-to-smiles

## What this skill does

Name-to-SMILES resolver for the Molecular Spectroscopy Agent (MSA); it consumes
the task_list produced by task-recognition and appends a canonical SMILES to
every task. Resolution is three-tiered and offline-first: an optional curated
manifest (`--manifest`, `smiles_source="manifest"`) wins over everything, then the
shipped offline dictionary (`dicts/name_to_smiles.json`, `smiles_source="dict"`),
and only on a miss does it fall back to a live PubChem query
(`smiles_source="pubchem"`) whose result is canonicalized through RDKit when RDKit
is importable. It is a text/structure preprocessing step only: it does NOT
generate 3D geometry, recommend parameters, or schedule jobs.

### Tier 0 — the curated manifest (verbatim, highest priority)

When `--manifest` is given, a task whose `molecule_name` equals a manifest
`mol_id` takes that row's `smiles` **exactly as published**, with
`smiles_source="manifest"`. Two properties are deliberate:

- **No network.** A Tier 0 hit never reaches PubChem, regardless of
  `MSA_N2S_DISABLE_NETWORK`.
- **No RDKit re-canonicalization.** Rewriting the structure would substitute our
  preferred spelling for the published dataset's own notation, and reproducing
  that dataset is the entire reason the manifest exists. `C1=CC=CC=C1` stays
  `C1=CC=CC=C1` even though RDKit would render it `c1ccccc1`. The open-shell scope
  gate still runs — it only screens, it never rewrites.

Tier 0 is also the ONLY tier that can resolve the `mol_000063`-style names
task-recognition's manifest entry emits: no dictionary or PubChem search knows
them. **A task_list produced by that entry MUST be given the same manifest here**,
otherwise every task fails with an unresolvable name. Required columns: `mol_id`
and `smiles`; the optional `name` column supplies `molecule_name_en`.

English display name (v2.1): the same pass resolves `molecule_name_en`, the
English rendering every downstream figure title and CSV metadata header uses.
Resolution order is manifest `name` -> dictionary `english` -> PubChem `Title` ->
PubChem `IUPACName` -> the `molecule_name` itself when it is already ASCII; the
chosen name is normalized to display case (the head word is capitalized while
locant and stereo prefixes stay lower-case, so `p-nitrophenol` becomes
`p-Nitrophenol`, while acronyms such as `THF` and PubChem Titles pass through
verbatim). PubChem is queried once for SMILES and both name properties, so this
costs no extra network round-trip. An unresolved English name is a WARNING
(`smiles_warnings`), never a failure — the figure simply falls back to
`molecule_name`, and the calculation is unaffected.

The manifest `name` column matters most in a manifest batch: there
`molecule_name` is a bare `mol_000063`, so without it every figure would be titled
with an internal id instead of a real molecule name.

Scope gate (v2.0): after a SMILES is resolved it is screened for open-shell
character (explicit radical electrons or an odd total electron count). The
pipeline supports only closed-shell singlets, so radicals, triplets, high-spin
systems and carbenes are rejected here as a fail-soft failure
(`smiles_status="failed"`) with a clear message, rather than being passed
downstream to be mis-computed as a closed-shell singlet.

## Agent Behavior

**CRITICAL: Always interact in the same language the user is using.** When the
user writes in Chinese, reply in Chinese; when the user writes in English, reply
in English.

Rules for defaults / overrides:
- Defaults: DO NOT pass parameters; let the skill read them from `msa_shared/constants/`.
- Overrides: ONLY pass parameters the user explicitly specified.
- NEVER silently override a default value.

Task-source rule: this skill accepts only the task_list JSON produced by the
upstream skill (`recognition`); hand-assembling a task_list is forbidden. Both
entry points of that skill are valid producers — but when the producer was
`manifest_to_tasks.py`, the same manifest CSV MUST accompany the task_list via
`--manifest`.

Offline switch: when the environment variable `MSA_N2S_DISABLE_NETWORK` equals
`"1"`, the skill never issues any network request — a dictionary miss becomes a
fail-soft failure. Use it for deterministic, reproducible runs.

## Task Field Contract

This skill sits at the **name to SMILES (P1-2)** stage of the pipeline
(stage_id=`smiles`, upstream=`recognition`).

**Consumes (from recognition):**
- `molecule_name`: string, the user's original molecule name (may be Chinese /
  contain spaces / special characters — it is domain input data).
- `recognition_status`: string; must equal `"success"` for this task to be
  processed (otherwise the task is skipped).

**Appends (written by this skill, per PIPELINE.md §1.1 P1-2 row):**
- `smiles`: string, the resolved SMILES. RDKit-canonical for the dictionary and
  PubChem sources; **verbatim as published** for the manifest source, which is not
  re-canonicalized.
- `smiles_source`: string, one of `"manifest"` (curated manifest CSV, Tier 0),
  `"dict"` (offline dictionary) or `"pubchem"` (live fallback). This enumeration
  is closed — see PIPELINE.md §1.2.
- `molecule_name_en`: string, the English display name for figure titles and CSV
  metadata (written on success only; may be `""` when no English name could be
  resolved, in which case downstream falls back to `molecule_name`).
- `smiles_status`: string, `"success" | "failed" | "skipped"`.
- `smiles_error`: string, present only when `smiles_status == "failed"` (a
  one-line reason — a resolution miss, or an open-shell-unsupported rejection) or
  `"skipped"` (`"upstream recognition failed"`).
- `smiles_warnings`: list of strings, present only when a non-fatal diagnostic
  applies (currently: no English name could be resolved). A warning never changes
  the status — the SMILES was produced and the task proceeds.

Rewriting any upstream field is forbidden; appending fields outside this
whitelist is forbidden; modifying `task_id` is forbidden.

## Scripts

`scripts/name_to_smiles.py` — CLI entry point.

```
python scripts/name_to_smiles.py --input local_workspace/<run_id>/task_list.json --output local_workspace/<run_id>/task_list.json
python scripts/name_to_smiles.py --input local_workspace/<run_id>/task_list.json --output local_workspace/<run_id>/task_list.json --manifest manifest.csv
```

Arguments:

| Name | Required | Description |
|---|---|---|
| `--input` | yes | upstream task_list JSON path (bare list or `{task_list:[...]}`) |
| `--output` | yes | output task_list JSON path |
| `--manifest` | no (yes for a manifest batch) | the manifest CSV the batch was expanded from; its `smiles` column becomes Tier 0. A task_list produced by `manifest_to_tasks.py` MUST be paired with the SAME manifest, since its `molecule_name` values are `mol_id`s. A manifest that cannot be read (missing header or missing `mol_id` / `smiles` column) is exit 1, not a per-task failure |

Environment:

| Variable | Effect |
|---|---|
| `MSA_N2S_DISABLE_NETWORK` | when `"1"`, forces a fully offline run (no PubChem) |

A Tier 0 hit is offline either way, so `--manifest` and this switch compose: a
manifest batch runs fully offline without needing the switch at all.

All logs and the `SUCCESS:` / `ERROR:` summary lines go to stderr; the task_list
is written to the `--output` file.

## Workflow

1. Load the task_list from `--input` via `common_utils.load_task_list`
   (it accepts both a bare list and a `{task_list:[...]}` wrapper).
2. For each task, check `upstream_ok(task, "recognition")`; if it fails,
   `mark_skipped` (writes `smiles_status="skipped"`, no `smiles`).
3. Tier 0 — when `--manifest` was given, look `molecule_name` up as a `mol_id`; on
   a hit take its `smiles` verbatim with `smiles_source="manifest"` and its `name`
   as the English display name. No network, no canonicalization.
4. Tier 1 — look `molecule_name` up in `dicts/name_to_smiles.json`; on a hit,
   take its `smiles` with `smiles_source="dict"` and its `english` as the
   English display name (a dictionary hit never touches the network).
5. Tier 2 — on a dictionary miss, if `MSA_N2S_DISABLE_NETWORK != "1"`, query
   PubChem (pubchempy) once for SMILES + `Title` + `IUPACName`, canonicalize the
   SMILES through RDKit with `smiles_source="pubchem"`, and prefer `Title` over
   `IUPACName` as the English display name; if the offline switch is on, fail
   soft instead.
6. Open-shell gate — screen the resolved SMILES for unpaired electrons (explicit
   radical electrons, or an odd total electron count). If open-shell, fail soft
   with an "open-shell not supported" reason instead of returning success. This
   applies to all three tiers.
7. On success write `smiles` / `smiles_source` / `molecule_name_en` (display-cased
   by `_display_en`) via `mark_success`, and `add_warning` when no English name
   could be resolved; on any failure write `smiles_status="failed"` +
   `smiles_error` via `mark_failed` — never raise.
8. Dump the task_list to `--output` via `common_utils.dump_task_list`.

## Integration

1. task-recognition
2. name-to-smiles **(this skill)**
3. smiles-to-xyz
4. structure-plot
5. param-recommendation
6. time-estimation
7. orca-{spec}
8. plot-{spec}
9. export-spectrum-data

**name-to-smiles (this skill)** is step 2: it turns the upstream `molecule_name`
into a `smiles` string that `smiles-to-xyz` (and, transitively,
`param-recommendation`) consume to build the initial geometry.

When step 1 used its manifest entry, the manifest travels with the task_list into
this step (`--manifest`) and stops here: downstream stages read `smiles`, never the
manifest.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| ImportError: msa_shared | vendored scripts/msa_shared/ missing or bootstrap altered | run `tools/vendor_msa_shared.py sync`; keep the §4.1 self-locating block verbatim |
| exit 2 "msa_contract_version mismatch" | msa_shared/VERSION bumped, skill not synced | update the frontmatter and the `MSA_CONTRACT_VERSION` constant |
| `smiles_status="skipped"` on every task | upstream `recognition_status` is not `"success"` | fix the task-recognition stage first |
| `smiles_status="failed"` with "network is disabled" | name absent from the offline dictionary while `MSA_N2S_DISABLE_NETWORK=1` | unset the switch to allow PubChem, or add the molecule to the offline dictionary source |
| `smiles_status="failed"` with "PubChem lookup failed" | network/HTTP error reaching PubChem | retry with connectivity, or run offline against the dictionary |
| `smiles_status="failed"` with "open-shell system is not supported" | the resolved SMILES has unpaired electrons / odd electron count (radical, triplet, high-spin, carbene) | expected — this version only handles closed-shell singlets; open-shell support is deferred to a later release |
| PubChem SMILES not canonicalized | RDKit not importable in the environment | install rdkit; the skill still degrades gracefully and emits the raw SMILES |
| `molecule_name_en` is `""` and `smiles_warnings` mentions "no English name found" | the dictionary entry has no `english`, and PubChem returned neither `Title` nor `IUPACName` (PubChem does not index Chinese names, so a dictionary miss on a Chinese name resolves neither) | expected fail-soft; add the molecule to the offline dictionary source with an `english` value if the figure title matters |
| English name in the wrong case (e.g. `p-nitrophenol` on the figure) | the name was hand-written downstream instead of read from `molecule_name_en` | never hand-write the English name; read `molecule_name_en` — `_display_en` here is the single place that decides case |
| every task `failed` with "'mol_000063' not found" | a manifest batch was run without `--manifest` | pass the same manifest the task_list was expanded from; `mol_id`s are resolvable only by Tier 0 |
| exit 1 "cannot load manifest" | the file is missing, empty, or lacks the `mol_id` / `smiles` column | fix the path or the header; this is refused once rather than failing every task with the same message |
| a manifest SMILES differs from RDKit's canonical form | Tier 0 is verbatim by design | expected — the published notation is preserved; downstream stages parse it normally |
| `smiles_source` is `"dict"` when a manifest was passed | the `molecule_name` is a real name that the dictionary also knows, not a `mol_id` | expected — Tier 0 keys on `mol_id`; nothing is wrong unless the name should have been a `mol_id` |
