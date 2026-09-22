---
name: smiles-to-xyz
description: SMILES-to-XYZ geometry generator for the Molecular Spectroscopy Agent (MSA); it consumes the task_list produced by name-to-smiles and appends an initial 3D geometry file plus atom count to every task. It parses each task's SMILES with RDKit, adds explicit hydrogens, embeds a 3D conformer (ETKDG with a random-coordinate fallback), runs an MMFF cleanup, and writes an .xyz file whose path comes from naming.xyz_initial. Trigger keywords SMILES -> XYZ, SMILES to 3D geometry, initial structure generation, P1-3 preprocessing stage. When the pipeline needs an initial 3D geometry before structure plotting or ORCA input assembly, the Agent MUST load this skill BEFORE running structure-plot or param-recommendation. DO NOT hand-construct the task_list, DO NOT hand-build .xyz filenames, DO NOT rewrite any upstream field.
version: 0.1.0
msa_contract_version: 2.0
---

# smiles-to-xyz

## What this skill does

SMILES-to-XYZ geometry generator for the Molecular Spectroscopy Agent (MSA); it
consumes the task_list produced by name-to-smiles and appends an initial 3D
geometry file plus atom count to every task. For each task it parses
`task["smiles"]` with RDKit, adds explicit hydrogens, embeds a 3D conformer
(ETKDG defaults with a random-coordinate fallback), runs a best-effort MMFF
optimization, and writes the geometry to an `.xyz` file whose path is obtained
from `naming.xyz_initial(molecule_name)`. It is a geometry preprocessing step
only: it does NOT plot structures, recommend calculation parameters, or schedule
jobs.

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

This skill sits at the **SMILES to XYZ (P1-3)** stage of the pipeline
(stage_id=`xyz`, upstream=`smiles`).

**Consumes (from smiles):**
- `smiles`: string, the SMILES resolved by name-to-smiles.
- `molecule_name`: string, the original molecule name (may be Chinese / contain
  spaces / special characters); used only to derive the output file path through
  `naming.xyz_initial`.
- `smiles_status`: string; must equal `"success"` for this task to be processed
  (otherwise the task is skipped).

**Appends (written by this skill, per PIPELINE.md §1.1 P1-3 row):**
- `xyz_file`: string, path to the generated `.xyz` file (always returned by
  `naming.xyz_initial`, never hand-built).
- `num_atoms`: int, total atom count after `AddHs` (hydrogens included).
- `xyz_status`: string, `"success" | "failed" | "skipped"`.
- `xyz_error`: string, present only when `xyz_status == "failed"` (a one-line
  reason) or `"skipped"` (`"upstream smiles failed"`).

Rewriting any upstream field is forbidden; appending fields outside this
whitelist is forbidden; modifying `task_id` is forbidden.

## Scripts

`scripts/smiles_to_xyz.py` — CLI entry point.

```
python scripts/smiles_to_xyz.py --input local_workspace/<run_id>/task_list.json --output local_workspace/<run_id>/task_list.json
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
   (writes `xyz_status="skipped"`, no `xyz_file`).
3. Parse `task["smiles"]` with `Chem.MolFromSmiles`; a `None` result is a
   fail-soft error (invalid SMILES), never a raised exception.
4. Add explicit hydrogens with `Chem.AddHs`, then embed a 3D conformer with
   `AllChem.EmbedMolecule` (ETKDG defaults first, `useRandomCoords=True` on
   fallback); a still-failed embedding fails soft.
5. Run `AllChem.MMFFOptimizeMolecule` as a best-effort cleanup (its return code
   is ignored, and an exception there does not break the batch).
6. Write the geometry with `Chem.MolToXYZFile` to `naming.xyz_initial(molecule_name)`
   and record `num_atoms = mol.GetNumAtoms()` (post-`AddHs`, an int).
7. On success write `xyz_file` / `num_atoms` via `mark_success`; on any failure
   write `xyz_status="failed"` + `xyz_error` via `mark_failed` — never raise.
8. Dump the task_list to `--output` via `common_utils.dump_task_list`.

## Integration

1. task-recognition
2. name-to-smiles
3. smiles-to-xyz **(this skill)**
4. structure-plot
5. param-recommendation
6. time-estimation
7. orca-{spec}
8. plot-{spec}
9. export-spectrum-data

**smiles-to-xyz (this skill)** is step 3: it turns the upstream `smiles` string
into an initial `.xyz` geometry that `structure-plot` renders and that
`param-recommendation` / the `orca-{spec}` calculation stage consume to build
ORCA inputs.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| ImportError: msa_shared | vendored scripts/msa_shared/ missing or bootstrap altered | run `tools/vendor_msa_shared.py sync`; keep the §4.1 self-locating block verbatim |
| exit 2 "msa_contract_version mismatch" | msa_shared/VERSION bumped, skill not synced | update the frontmatter and the `MSA_CONTRACT_VERSION` constant |
| `xyz_status="skipped"` on every task | upstream `smiles_status` is not `"success"` | fix the name-to-smiles stage first |
| `xyz_status="failed"` with "invalid SMILES" | the upstream `smiles` string cannot be parsed by RDKit | correct the SMILES at the name-to-smiles stage |
| `xyz_status="failed"` with "3D embedding failed" | RDKit could not embed a conformer even with random coordinates | check the molecule for exotic valence/charge; retry or supply an alternative SMILES |
| `ImportError: rdkit` when running (not `--help`) | RDKit not installed in the environment | install rdkit (dev environment ships 2025.09.2); the skill does not auto-install |
