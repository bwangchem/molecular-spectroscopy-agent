---
name: task-recognition
description: Task-recognition sub-agent for the Molecular Spectroscopy Agent (MSA); it turns a spectroscopy-calculation request into a structured task_list JSON through TWO entry points. Entry 1 (task_recognition.py) parses one user's natural language, recognizing molecule names (three-layer strategy dictionary -> naming-rule regex -> context fallback), the target spectrum (ir/raman/uvvis/nmr/fluo/xanes, extracting the NMR target nucleus and the XANES target element), and the ORCA CPCM solvent keyword. Entry 2 (manifest_to_tasks.py) expands a curated manifest CSV as molecules x spectra for whole-dataset reproduction. Both emit field-for-field identical tasks. Trigger keywords 计算/算/跑/生成/我要/帮我 plus a molecule name plus a spectrum type (红外/拉曼/紫外/荧光/核磁/NMR/IR/Raman/UV/XANES ...), or a request to run a manifest / batch / whole dataset. When the user asks to compute or parse such a spectroscopy request, the Agent MUST load this skill BEFORE constructing any task_list. DO NOT hand-write the task_list, DO NOT convert molecule names to SMILES here, DO NOT modify constants inside msa_shared.
version: 0.2.0
msa_contract_version: 2.0
---

# task-recognition

## What this skill does

Task-recognition sub-agent for the Molecular Spectroscopy Agent (MSA); it turns a
spectroscopy-calculation request into a structured task_list JSON. It is the
**entry** skill of the pipeline: its input is a request, not an upstream
task_list. It performs recognition and formatting only; it does not convert names
to SMILES, recommend parameters, or schedule jobs. When no solvent is named,
`solvent_std` is left as an empty string and the downstream `param-recommendation`
skill fills in the default.

### Two entry points, one stage

A request arrives in one of two genuinely different shapes, so this stage has two
CLI entry scripts (PIPELINE.md §0). They share `STAGE_ID = recognition` and emit
the same fields with the same spelling:

| | `scripts/task_recognition.py` | `scripts/manifest_to_tasks.py` |
|---|---|---|
| Input | one user's natural language | a curated manifest CSV, one row per molecule |
| `molecule_name` | the user's own word (苯, benzene) | the manifest's `mol_id` (`mol_000063`) |
| Which spectra | whatever the sentence asks for | the `--spectra` subset, default all six |
| NMR nuclei | as worded; both when unstated | `1H` only if the formula has H, `13C` only if it has C |
| XANES edges | the user MUST name the edge, else the task is dropped | every edge of C/N/O/F actually present |
| Solvent | recognized from the sentence | always empty — a manifest states molecules, not measurement conditions |
| Scale | a handful of tasks | thousands; supports subsetting and sharding |
| Non-existent combinations | cannot arise (no formula is known) | reported in a separate CSV, never entered as tasks (§2.6) |

**Equivalence promise (msa-skill-spec rule 36):** for the same molecule and the
same spectrum, both entries produce a task that is identical field for field
except `run_id` (per batch) and `task_id` (per position). This is not maintained
by copying: `manifest_to_tasks.py` imports the task constructor, the id formats,
the global numbering and the batch wrapper from `task_recognition.py`. Nothing
downstream can tell which entry was used, and
`tests/verify_task_recognition.py` asserts it directly.

**Which entry to use:** natural language when a human is asking for specific
spectra of specific molecules; the manifest entry when reproducing a published
dataset, or whenever the molecule set already exists as a file. Never hand-write
a task_list to bridge the two.

Open-shell scope guard (v2.0): the pipeline supports only closed-shell singlets.
If a request's wording asks for an open-shell species — radical, triplet,
high-spin, carbene, nitrene, diradical — that request is intercepted here, emits
no task, and is reported as unsupported in `parse_errors`. This guards the
natural-language side; name-to-smiles independently guards the structural side
(odd-electron SMILES).

## Agent Behavior

**CRITICAL: Always interact in the same language the user is using.** When the
user writes in Chinese, reply in Chinese; when the user writes in English, reply
in English.

Rules for defaults / overrides:
- Defaults: DO NOT pass parameters; let the skill read them from `msa_shared/constants/`.
- Overrides: ONLY pass parameters the user explicitly specified.
- NEVER silently override a default value.

Task-source rule: this is the pipeline entry skill, so it accepts **natural
language** (via `--text` / `--input-file` / stdin) or **a manifest CSV** (via
`--manifest`), NOT an upstream task_list. It is the sole preprocessing skill that
generates task objects and `task_id` from scratch; every downstream skill consumes
the task_list this skill emits.

Entry-selection rule: pick ONE entry per batch. Do not run both and merge the
outputs — each entry mints its own `run_id`, and two `run_id`s in one task_list
would scatter the batch's artifacts across two run directories. To split a large
manifest across parallel invocations use `--shard k/N` with a shared `--run-id`,
which is the supported way to keep one dataset in one run.

## Task Field Contract

This skill sits at the **recognition** stage of the pipeline
(stage_id=`recognition`, upstream=`(user natural language)`).

**Consumes (from user natural language):**
- none — the input is a raw natural-language request string; there is no
  upstream task_list.

**Appends (written by this skill, per PIPELINE.md §1.1 recognition row):**
- `run_id`: string, `run_YYYYMMDD_HHMMSS_<6hex>`, the run-isolation namespace.
  Generated **once per invocation** and stamped identically on every task in the
  batch; all downstream `naming` path builders nest their output under it.
- `task_id`: string, `task_{NN}_{molecule_name}_{target_spectra}`, NN a
  two-digit index increasing globally within the batch.
- `molecule_name`: string, the user's original molecule name (NOT converted to
  SMILES).
- `target_spectra`: string, one of `ir | raman | uvvis | nmr | fluo | xanes`.
- `solvent_raw`: string, the user's original solvent word ("" if none given).
- `solvent_std`: string, the ORCA CPCM keyword (e.g. `CPCM(Chloroform)`);
  `""` for gas phase (气相/真空/无溶剂) AND for an unspecified solvent.
- `extra_params`: object; NMR carries `{"target_nucleus": "1H"|"13C"}`, XANES
  carries `{"target_element": "C"|"N"|"O"|"F"}`, all other spectra carry `{}`.

The output is the wrapped batch object
`{parse_batch_id, task_list, parse_warnings, parse_errors}`; `task_list` is the
list of task objects above.

Rewriting any upstream field is forbidden; appending fields outside this
whitelist is forbidden; modifying `task_id` downstream is forbidden.

## Scripts

### `scripts/task_recognition.py` — entry 1, natural language

```
python scripts/task_recognition.py --text "计算苯的红外光谱" --summary
echo "计算乙醇的核磁共振谱" | python scripts/task_recognition.py
python scripts/task_recognition.py --input-file req.txt --output local_workspace/<run_id>/task_list.json
```

**Output file naming (PIPELINE.md §3.4):** this skill creates the `run_id`. The
Agent MUST read it from the output JSON, then ensure the task_list is stored at
`local_workspace/<run_id>/task_list.json`. The simplest pattern: let the script
output to stdout, capture it, read `run_id`, then write to the scoped path. If
using `--output`, the Agent can first run with stdout to discover `run_id`, or
pipe through `jq` to extract it. **Never write to a bare generic path like
`task_list.json` or `tasks.json`** — concurrent sessions sharing the same
workspace will clobber each other.

Arguments:

| Name | Required | Description |
|---|---|---|
| `--text` | one of text/input-file/stdin | natural-language request string |
| `--input-file` | one of text/input-file/stdin | UTF-8 file with the request |
| `--output` | no | write JSON to this path (default: stdout) |
| `--summary` | no | also print a human-readable summary to stderr |
| `--indent` | no | JSON indent (default 2; pass 0 for compact output) |

stdout is the wrapped task_list JSON (consumed by downstream); all logs and the
optional `--summary` go to stderr.

### `scripts/manifest_to_tasks.py` — entry 2, curated manifest CSV

```
python scripts/manifest_to_tasks.py --manifest manifest.csv \
    --output local_workspace/<run_id>/task_list.json \
    --skipped-report local_workspace/<run_id>/skipped_combinations.csv --summary
python scripts/manifest_to_tasks.py --manifest manifest.csv --spectra ir,raman --id-range 1-500
python scripts/manifest_to_tasks.py --manifest manifest.csv --shard 1/8 --run-id run_20260908_120000_ab12cd
```

The same `run_id` rule as entry 1 applies: the script mints it (or accepts one via
`--run-id`), and the Agent MUST store the task_list under
`local_workspace/<run_id>/`. The skipped-combination report is a batch-level
product too, so it is named by CLI argument and does NOT go through `naming.*`
(PIPELINE.md §3.1); keep it next to the task_list of the same run.

Manifest columns:

| Column | Required | How it is used |
|---|---|---|
| `mol_id` | yes | becomes `molecule_name`, and thus the file-naming stem of every downstream artifact |
| `smiles` | yes | bad-row validation ONLY; the `smiles` field itself belongs to name-to-smiles (§1.1), which reads the same manifest as its Tier 0 source |
| `formula` | yes | whether H / C are present, i.e. whether `1H` / `13C` NMR physically exists |
| `elements` | no | which of the C/N/O/F K-edges exist; falls back to `formula` when absent |
| `multiplicity` | no | bad-row validation ONLY — anything but 1 is refused (closed-shell singlets only) |
| `category` | no | `--category` selection ONLY, never a chemistry decision |
| `name` | no | not read here; name-to-smiles uses it for `molecule_name_en` |

Columns deliberately NOT read: `charge`, `needs_ZORA`, `cid`, `inchikey`,
`is_core`, `source`, `name_source`, `has_*`. The pipeline re-derives charge and
the ZORA decision from the structure, so reading them here would create a second
source of truth that can silently disagree. Unknown columns are ignored, never
rejected.

**H is judged from `formula`, never from `elements`.** The `elements` column lists
heavy atoms only, so using it for the hydrogen test would drop every `1H` task in
the whole batch while every count still looked plausible.

Arguments:

| Name | Required | Description |
|---|---|---|
| `--manifest` | yes | path to the manifest CSV (read as `utf-8-sig`, so a spreadsheet BOM is harmless) |
| `--output` | no | write JSON to this path (default: stdout) |
| `--skipped-report` | no | write the §2.6 bypass report CSV (`mol_id,target_spectra,variant,reason`) |
| `--run-id` | no (yes with `--shard`) | reuse an existing `run_id` instead of minting one |
| `--spectra` | no | comma-separated subset of `ir,raman,uvvis,nmr,fluo,xanes` (default: all six) |
| `--mol-ids` | no | comma-separated `mol_id` whitelist |
| `--id-range` | no | inclusive range over the numeric suffix of `mol_id`, e.g. `1-500` |
| `--category` | no | comma-separated values of the `category` column; selection only |
| `--shard` | no | process shard `k` of `N`, e.g. `1/8` |
| `--summary` | no | also print a human-readable summary to stderr |
| `--indent` | no | JSON indent (default 2; pass 0 for compact output) |

Two hard conventions about sharding:

1. **`--shard` requires `--run-id`** (else exit 1). Each invocation would
   otherwise mint its own `run_id` and one dataset would end up split across N run
   directories, with no downstream stage able to reassemble it.
2. **Shard membership is computed over the full manifest**, ordered by the numeric
   suffix of `mol_id`, independent of every other filter. Adding `--spectra ir` or
   `--category` therefore cannot move a molecule from shard 3 to shard 5, so
   "shard 3" means the same molecules in every run.

**Bad-row policy: one bad row refuses the whole batch (exit 1).** A manifest is a
curated artifact meant to reproduce a specific dataset, so a malformed row means
the file is not the file we think it is; expanding the rest would burn weeks of
compute on a batch that looks complete but silently misses molecules. All problems
are reported together, each naming its CSV line number. The five bad-row classes:
empty `mol_id`; empty `smiles`; duplicate `mol_id` (the first-seen line is named
too); `multiplicity` other than 1; and a `smiles` rdkit cannot parse. The last one
is checked only when rdkit is installed — it is a SOFT dependency, so a host
without it prints a `WARNING:` that this single check was skipped and proceeds
with the other four rather than exiting 4.

stdout is the wrapped task_list JSON; logs, warnings and `--summary` go to stderr.

## Workflow

### Entry 1 — natural language (`task_recognition.py`)

1. Read the natural-language request from `--text` / `--input-file` / stdin.
2. Clean the text (soft punctuation -> spaces; semicolons kept as hard block delimiters).
3. Semantic chunking: hard-split on semicolons, then soft-split on the connective
   separators from `dicts/separators.json`; drop empty sub-chunks.
4. Open-shell guard: if a block's wording contains an open-shell term (自由基 /
   三重态 / 高自旋 / 卡宾 / radical / triplet / carbene …), emit no task for it and
   append an "unsupported open-shell" message to `parse_errors`; skip the block.
5. Per block: recognize the solvent first (strong token OR solvent-context only),
   then mask that solvent token out of molecule recognition.
6. Recognize molecules (three layers: dictionary -> naming-rule regex -> context
   fallback) and spectra (synonym map; NMR nucleus, XANES element).
7. Expand molecules x spectra into tasks and renumber `task_id` globally.
8. Validate (drop XANES tasks with no target_element into `parse_errors`); decide
   the two "not recognized" warnings at batch level (suppressed when the batch
   produced any valid task); write the wrapped JSON via `dump_task_list` /stdout.

### Entry 2 — manifest CSV (`manifest_to_tasks.py`)

1. Validate every argument before touching the file (unknown spectrum type,
   malformed `--shard` / `--id-range`, `--shard` without `--run-id`) — all exit 1.
2. Read the manifest as `utf-8-sig`; refuse a missing header, a missing required
   column, or a header-only file.
3. Validate all rows and refuse the whole batch on any bad row, reporting every
   problem with its CSV line number.
4. Select rows: `--mol-ids`, `--id-range`, `--category`, `--shard` (membership from
   the full manifest). An empty selection is an error, not an empty batch.
5. Per row, decide which molecule x spectrum combinations exist: `1H`/`13C` from
   the presence of H/C in `formula`; XANES edges from C/N/O/F present in
   `elements`. Combinations that cannot exist go to the skipped report, never into
   the task_list (§2.6).
6. Build tasks with the SAME constructor entry 1 uses (`expand_task_list`), number
   them globally in manifest order (`assign_task_ids`), and wrap the batch
   (`finalize_batch`). `solvent_raw` / `solvent_std` stay empty.
7. Write the JSON, and the skipped report when `--skipped-report` is given.

## Integration

1. task-recognition **(this skill)**
2. name-to-smiles
3. smiles-to-xyz
4. structure-plot
5. param-recommendation
6. time-estimation
7. orca-{spec}
8. plot-{spec}
9. export-spectrum-data

**task-recognition (this skill)** is step 1: it turns the request into the
task_list that every downstream skill consumes, from either entry. Its
`molecule_name` feeds `name-to-smiles`; an empty `solvent_std` (with empty
`solvent_raw`) is filled by `param-recommendation`.

When the manifest entry was used, `molecule_name` is a `mol_id`, which no
dictionary or PubChem lookup can resolve — so the SAME manifest must be handed to
`name-to-smiles` via its `--manifest` flag, where it acts as the highest-priority
SMILES source (Tier 0). Forgetting that flag makes every task fail at step 2 with
an unresolvable name.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| ImportError: msa_shared | vendored scripts/msa_shared/ missing or bootstrap altered | run `tools/vendor_msa_shared.py sync`; keep the §4.1 self-locating block verbatim |
| exit 2 "msa_contract_version mismatch" | msa_shared/VERSION bumped, skill not synced | update the frontmatter and the `MSA_CONTRACT_VERSION` constant |
| a molecule word (甲苯/乙醇/丙酮/氯仿) swallowed as solvent | solvent context wrongly triggered | ensure the input has no false solvent context; only strong tokens or 溶剂/溶液/以…为溶剂/在…中/…条件下 enable solvent match |
| XANES task dropped into parse_errors | no target element given | add the edge element, e.g. "C K-edge" / "C K边" |
| a request dropped into parse_errors with "检测到开壳层相关描述" | the wording names an open-shell species (自由基/三重态/高自旋/卡宾/radical/triplet …) | expected — only closed-shell singlets are supported this version; remove the open-shell wording or wait for a later release |
| spurious "未识别出分子名称/未识别计算目标光谱" | old per-segment warning bug | fixed: these two are decided at batch level and suppressed once any task exists |
| manifest batch produced no `1H` task at all | the hydrogen test was pointed at `elements`, which lists heavy atoms only | judge H from `formula`; `elements` drives XANES edges only |
| exit 1 "--shard requires --run-id" | sharding without a shared run namespace | pass the same `--run-id` to every shard invocation |
| exit 1 "N bad row(s) ... the whole batch is refused" | a manifest row is empty / duplicated / open-shell / has unparseable SMILES | fix the named CSV lines; the refusal is deliberate, there is no skip-and-continue mode |
| `WARNING: rdkit unavailable` from the manifest entry | rdkit is a SOFT dependency | expected — only the "unparseable SMILES" pre-screen is skipped; install rdkit to enable it |
| fewer tasks than molecules x spectra | combinations that cannot exist were bypassed | read the `--skipped-report` CSV: one row per bypassed combination with its reason; tasks + skipped should equal the full product |
| exit 1 "no manifest row survived the selection filters" | `--mol-ids` / `--id-range` / `--category` / `--shard` intersect to nothing | widen the filters; an empty selection is refused rather than emitting an empty batch |
| every task fails in name-to-smiles with an unresolvable name | a manifest batch was passed on without its manifest | give `name-to-smiles` the same file via `--manifest` |
