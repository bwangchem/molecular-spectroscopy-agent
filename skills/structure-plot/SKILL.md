---
name: structure-plot
description: 2D molecular structure image renderer for the Molecular Spectroscopy Agent (MSA); it consumes the task_list produced by the upstream SMILES stage and appends a rendered 2D structure PNG path to every task. For each task it parses task["smiles"] with RDKit, draws the 2D depiction with the Cairo backend (rdMolDraw2D.MolDraw2DCairo at 500x400), and writes the PNG whose path comes from naming.structure_png. Trigger keywords structure image, 2D structure plot, draw molecule, molecular structure PNG, P1-4 preprocessing stage. When the pipeline needs a 2D structure picture of each molecule, the Agent MUST load this skill BEFORE running structure-plot. DO NOT hand-construct the task_list, DO NOT hand-build PNG filenames, DO NOT rewrite any upstream field, DO NOT use the legacy 3Dmol.js HTML route.
version: 0.1.0
msa_contract_version: 2.0
---

# structure-plot

## What this skill does

2D molecular structure image renderer for the Molecular Spectroscopy Agent
(MSA); it consumes the task_list produced by the upstream SMILES stage and
appends a rendered 2D structure PNG path to every task. For each task it parses
`task["smiles"]` with RDKit, draws the 2D depiction with the Cairo backend
(`rdMolDraw2D.MolDraw2DCairo(500, 400)` + `PrepareAndDrawMolecule`), and writes
the PNG byte stream to a file whose path is obtained from
`naming.structure_png(molecule_name)`. Per PIPELINE.md §7 the default structure
visualization is 2D (RDKit Draw + Cairo); this skill does NOT emit the legacy
interactive 3Dmol.js HTML, does NOT recommend calculation parameters, and does
NOT schedule jobs.

## Agent Behavior

**CRITICAL: Always interact in the same language the user is using.** When the
user writes in Chinese, reply in Chinese; when the user writes in English, reply
in English.

Rules for defaults / overrides:
- Defaults: DO NOT pass parameters; let the skill read them from `msa_shared/constants/`.
- Overrides: ONLY pass parameters the user explicitly specified.
- NEVER silently override a default value.

Task-source rule: this skill accepts only the task_list JSON produced by the
upstream skill (`smiles`); hand-assembling a task_list is forbidden.

## Task Field Contract

This skill sits at the **structure image (P1-4)** stage of the pipeline
(stage_id=`structure`, upstream=`smiles`).

**Consumes (from smiles):**
- `smiles`: string, the SMILES resolved by name-to-smiles.
- `molecule_name`: string, the original molecule name (may be Chinese / contain
  spaces / special characters); used only to derive the output file path through
  `naming.structure_png`.
- `smiles_status`: string; must equal `"success"` for this task to be processed
  (otherwise the task is skipped).

Note: a 2D depiction only needs `smiles`, so the upstream stage is `smiles`
rather than `xyz`. This lets the image be produced even when the 3D geometry
stage failed, keeping the pipeline success rate as high as possible.

**Appends (written by this skill, per PIPELINE.md §1.1 P1-4 row):**
- `structure_png`: string, path to the generated `.png` file (always returned by
  `naming.structure_png`, never hand-built).
- `structure_status`: string, `"success" | "failed" | "skipped"`.
- `structure_error`: string, present only when `structure_status == "failed"` (a
  one-line reason) or `"skipped"` (`"upstream smiles failed"`).

Rewriting any upstream field is forbidden; appending fields outside this
whitelist is forbidden; modifying `task_id` is forbidden.

## Scripts

`scripts/structure_plot.py` — CLI entry point.

```
python scripts/structure_plot.py --input local_workspace/<run_id>/task_list.json --output local_workspace/<run_id>/task_list.json
```

Arguments:

| Name | Required | Description |
|---|---|---|
| `--input` | yes | upstream task_list JSON path (bare list or `{task_list:[...]}`) |
| `--output` | yes | output task_list JSON path |

All logs and the `SUCCESS:` / `ERROR:` summary lines go to stderr; the task_list
is written to the `--output` file. RDKit is imported lazily inside the worker so
`--help` works even on a machine without RDKit installed.

## Workflow

1. Load the task_list from `--input` via `common_utils.load_task_list`
   (it accepts both a bare list and a `{task_list:[...]}` wrapper).
2. For each task, check `upstream_ok(task, "smiles")`; if it fails, `mark_skipped`
   (writes `structure_status="skipped"`, no `structure_png`).
3. Parse `task["smiles"]` with `Chem.MolFromSmiles`; a `None` result is a
   fail-soft error (invalid SMILES), never a raised exception.
4. Render the 2D depiction with `rdMolDraw2D.MolDraw2DCairo(500, 400)`,
   `rdMolDraw2D.PrepareAndDrawMolecule(d, mol)`, `d.FinishDrawing()`, then read
   the PNG byte stream with `d.GetDrawingText()`.
5. Write the PNG bytes in binary mode (`"wb"`) to
   `naming.structure_png(molecule_name)`; the filename is never hand-built.
6. On success write `structure_png` via `mark_success`; on any failure write
   `structure_status="failed"` + `structure_error` via `mark_failed` — never
   raise, never abort the batch.
7. Dump the task_list to `--output` via `common_utils.dump_task_list`.

## Integration

1. task-recognition
2. name-to-smiles
3. smiles-to-xyz
4. structure-plot **(this skill)**
5. param-recommendation
6. time-estimation
7. orca-{spec}
8. plot-{spec}
9. export-spectrum-data

**structure-plot (this skill)** is step 4: it turns the upstream `smiles` string
into a 2D structure PNG for previews and reports. It runs independently of the
3D geometry (`smiles-to-xyz`) stage and does not gate the downstream
`param-recommendation` / `orca-{spec}` calculation path.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| ImportError: msa_shared | vendored scripts/msa_shared/ missing or bootstrap altered | run `tools/vendor_msa_shared.py sync`; keep the §4.1 self-locating block verbatim |
| exit 2 "msa_contract_version mismatch" | msa_shared/VERSION bumped, skill not synced | update the frontmatter and the `MSA_CONTRACT_VERSION` constant |
| `structure_status="skipped"` on every task | upstream `smiles_status` is not `"success"` | fix the name-to-smiles stage first |
| `structure_status="failed"` with "invalid SMILES" | the upstream `smiles` string cannot be parsed by RDKit | correct the SMILES at the name-to-smiles stage |
| `structure_status="failed"` with "empty PNG" | the Cairo backend produced no bytes | check the RDKit build ships Cairo support (dev environment ships 2025.09.2) |
| `ImportError: rdkit` when running (not `--help`) | RDKit not installed in the environment | install rdkit (dev environment ships 2025.09.2); the skill does not auto-install |
