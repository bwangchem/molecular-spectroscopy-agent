---
name: time-estimation
description: Walltime estimator for the Molecular Spectroscopy Agent (MSA); it consumes the task_list produced by param-recommendation and appends a time_estimate object (per-step seconds + confidence, plus a summed total_seconds and an overall confidence) to every task. For each task it iterates the per-step params dict in sorted key order and calls timing.estimate_step_seconds(calc_type, num_atoms) for each step, using the returned seconds/confidence verbatim; total_seconds is the exact int sum of the per-step seconds. Trigger keywords time estimation, estimate walltime, job runtime prediction, time_estimate + total_seconds, P1-6 preprocessing stage. When the pipeline needs a walltime estimate before ORCA input assembly or SLURM submission, the Agent MUST load this skill AFTER param-recommendation and BEFORE any orca-{spec} skill. DO NOT hand-construct the task_list, DO NOT hard-code any timing numbers or power laws, DO NOT re-round the total, DO NOT rewrite any upstream field.
version: 0.1.0
msa_contract_version: 2.0
---

# time-estimation

## What this skill does

Walltime estimator for the Molecular Spectroscopy Agent (MSA); it consumes the
task_list produced by param-recommendation and appends a `time_estimate` object
to every task. For each task it reads the per-step `params` dict plus the
molecule size `num_atoms`, iterates the steps in sorted key order (`step1`,
`step2`, ...), and calls `timing.estimate_step_seconds(step["calc_type"],
num_atoms)` for each step, using the returned `(seconds, confidence)` verbatim.
The overall `total_seconds` is the exact integer sum of the per-step seconds and
the overall `confidence` is `timing.combine_confidence([...])`. It is a thin
lookup + arithmetic step only: it knows no timing numbers itself, defines no
power laws or prefactors, does not touch geometry, and does not import RDKit.

## Agent Behavior

**CRITICAL: Always interact in the same language the user is using.** When the
user writes in Chinese, reply in Chinese; when the user writes in English, reply
in English.

Rules for defaults / overrides:
- Defaults: DO NOT pass parameters; let the skill read all timing coefficients
  from `msa_shared/constants/timing.py`.
- Overrides: ONLY pass parameters the user explicitly specified.
- NEVER silently override a default value.

Task-source rule: this skill accepts only the task_list JSON produced by the
upstream skill (`params`); hand-assembling a task_list is forbidden.

## Task Field Contract

This skill sits at the **time-estimate (P1-6)** stage of the pipeline
(stage_id=`time_estimate`, upstream=`params`).

**Consumes (from param-recommendation):**
- `params`: per-step dict `{"step1": {...}, "step2": {...}, ...}`; each step
  carries a `calc_type` string — the v2.0 recipe vocabulary is `opt`, `opt_freq`,
  `numfreq_raman`, `uvvis_tddft`, `nmr`, `fluo_es_opt`, `fluo_emission`,
  `xanes_sp`, `xanes_tddft` — the key that selects the timing power law via
  `timing.estimate_step_seconds`. A `calc_type` the timing table does not know
  falls through to a default power law (low confidence), never raising, so new
  step vocabulary degrades gracefully rather than breaking the estimate.
- `num_atoms`: int, molecule size appended earlier by smiles-to-xyz (a
  missing / bad value is handled internally by the shared timing helper).
- `params_status`: string; must equal `"success"` for this task to be processed
  (otherwise the task is skipped).

**Appends (written by this skill, per PIPELINE.md §1.1 P1-6 row):**
- `time_estimate`: an object
  `{"steps": [{"name": <step_name>, "seconds": <int>, "confidence": <str>}, ...],
    "total_seconds": <int>, "confidence": <str>}`.
  - one estimate step per params step, iterated in sorted key order;
  - `total_seconds` == the exact int sum of the per-step `seconds`;
  - overall `confidence` == `timing.combine_confidence([...per-step confidences])`.
- `time_estimate_status`: string, `"success" | "failed" | "skipped"`.
- `time_estimate_error`: string, present only when `time_estimate_status ==
  "failed"` (a one-line reason, e.g. `params is missing or empty`) or
  `"skipped"` (`"upstream params failed"`, handled by the shared helper).

Rewriting any upstream field is forbidden; appending fields outside this
whitelist is forbidden; modifying `task_id` is forbidden.

## Scripts

`scripts/time_estimation.py` — CLI entry point.

```
python scripts/time_estimation.py --input local_workspace/<run_id>/task_list.json --output local_workspace/<run_id>/task_list.json
```

Arguments:

| Name | Required | Description |
|---|---|---|
| `--input` | yes | upstream task_list JSON path (bare list or `{task_list:[...]}`) |
| `--output` | yes | output task_list JSON path |

All logs and the `SUCCESS:` / `ERROR:` summary lines go to stderr; the task_list
is written to the `--output` file. This skill is pure standard library (nothing
beyond the bootstrap) and imports no heavy dependencies, so `--help` always
works.

## Workflow

1. Load the task_list from `--input` via `common_utils.load_task_list`
   (it accepts both a bare list and a `{task_list:[...]}` wrapper).
2. For each task, check `upstream_ok(task, "params")`; if it fails,
   `mark_skipped` (writes `time_estimate_status="skipped"`, no `time_estimate`).
3. Read `params = task["params"]`. A missing or empty `params` dict is a
   fail-soft error (`time_estimate_error="params is missing or empty"`), never a
   raised exception.
4. Read `num_atoms = task["num_atoms"]` (the shared helper handles a missing or
   non-numeric value internally, so it is never validated here).
5. Iterate the `params` steps in sorted key order (`step1`, `step2`, ...); for
   each step call `timing.estimate_step_seconds(step["calc_type"], num_atoms)`
   and record `{"name", "seconds", "confidence"}` using the returned values
   verbatim.
6. Set `total_seconds` to the exact int sum of the per-step `seconds` (never a
   re-rounded recompute) and `confidence` to
   `timing.combine_confidence([...per-step confidences])`.
7. On success write `time_estimate` via `mark_success`; on any failure write
   `time_estimate_status="failed"` + `time_estimate_error` via `mark_failed` —
   never raise.
8. Dump the task_list to `--output` via `common_utils.dump_task_list`.

## Integration

1. task-recognition
2. name-to-smiles
3. smiles-to-xyz
4. structure-plot
5. param-recommendation
6. time-estimation **(this skill)**
7. orca-{spec}
8. plot-{spec}
9. export-spectrum-data

**time-estimation (this skill)** is step 6: it turns the upstream per-step
`params` (specifically each step's `calc_type`) plus `num_atoms` into a
`time_estimate` walltime object. Downstream, the `orca-{spec}` calculation stage
and any SLURM submission step read `time_estimate.total_seconds` to size the job
(e.g. to set a wall-clock request), while the per-step breakdown supports finer
scheduling and reporting.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| ImportError: msa_shared | vendored scripts/msa_shared/ missing or bootstrap altered | run `tools/vendor_msa_shared.py sync`; keep the §4.1 self-locating block verbatim |
| exit 2 "msa_contract_version mismatch" | msa_shared/VERSION bumped, skill not synced | update the frontmatter and the `MSA_CONTRACT_VERSION` constant |
| `time_estimate_status="skipped"` on every task | upstream `params_status` is not `"success"` | fix the param-recommendation stage first |
| `time_estimate_status="failed"` with "params is missing or empty" | `params` is absent or an empty dict | ensure param-recommendation ran and populated `params` |
| every step reports the `MIN_STEP_SECONDS` floor | `num_atoms` is missing / non-positive so the timing helper falls back to size 1 | ensure smiles-to-xyz appended a valid `num_atoms` |
| overall `confidence` is `"low"` | timing coefficients are provisional (unfitted heuristics) | expected in v1.0; graduates when real timing logs replace the power laws |
