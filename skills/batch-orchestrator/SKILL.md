---
name: batch-orchestrator
description: Scheduling / status roll-up stage (P1-7) for the Molecular Spectroscopy Agent (MSA). This is the SPECIAL stage — it does NOT process a molecule and appends no per-stage data field of its own. It consumes the whole task_list and appends exactly two fields to every task, a run-wide batch_id (batch_YYYYMMDD_HHMMSS, identical across the run) produced by make_batch_id(), and a single pipeline_status (ok / failed / blocked / pending) produced by rollup_pipeline_status(task) from the shared per-stage {stage}_status fields. Trigger keywords batch orchestration, batch_id, pipeline_status, schedule a run, roll up status, P1-7 orchestration stage. When a run must be stamped with a batch id and every task's stage statuses rolled up into one pipeline status, the Agent MUST load this skill LAST, after every processing stage. DO NOT hand-construct the task_list, DO NOT fabricate any {stage}_status field, DO NOT rewrite any upstream field or task_id, DO NOT re-derive the roll-up rules or the batch-id format (call the shared helpers only).
version: 0.1.0
msa_contract_version: 2.0
---

# batch-orchestrator

## What this skill does

Scheduling and status roll-up stage (P1-7) for the Molecular Spectroscopy Agent
(MSA). It is the **special** pipeline stage: unlike every other skill it does
NOT process a molecule and appends NO per-stage data field of its own. Per
meta-skill iron rule 24 and PIPELINE.md §1.1 it does exactly two things:

1. Stamp one run-wide `batch_id` (format `batch_YYYYMMDD_HHMMSS`, identical on
   every task in the run) produced by `make_batch_id()`.
2. Roll each task's per-stage `{stage}_status` fields up into a single
   `pipeline_status` produced by `rollup_pipeline_status(task)`.

It has NO `STAGE_ID` and NO `UPSTREAM_STAGE`, and it never calls
`mark_success` / `mark_failed` / `mark_skipped` / `upstream_ok`. All roll-up
rules and the batch-id format live in the shared helpers; this skill implements
no such logic itself.

## Agent Behavior

**CRITICAL: Always interact in the same language the user is using.** When the
user writes in Chinese, reply in Chinese; when the user writes in English, reply
in English.

Rules for defaults / overrides:
- Defaults: DO NOT pass parameters; the batch id and the roll-up are computed
  entirely by the shared helpers (`make_batch_id`, `rollup_pipeline_status`).
- Overrides: this stage takes no tunable knobs — never invent one.
- NEVER silently fabricate a `{stage}_status`, a `batch_id`, or a
  `pipeline_status` value by hand.

Task-source rule: this skill accepts only the task_list JSON produced by the
upstream processing stages; hand-assembling a task_list is forbidden.

## Pipeline File Management (CRITICAL — applies to the ENTIRE pipeline)

The Agent orchestrates the pipeline by writing and passing a task_list JSON file
between stages. **That file MUST be scoped by `run_id` to prevent concurrent
sessions from clobbering each other** (see PIPELINE.md §3.4).

Mandatory sequence:

1. After `task-recognition` produces the task_list (stdout or `--output`), read
   the `run_id` field from it.
2. Write the task_list to `local_workspace/<run_id>/task_list.json` — create the
   directory if it does not exist.
3. Pass that path as both `--input` and `--output` for every subsequent stage:
   ```
   python scripts/<skill>.py \
       --input  local_workspace/<run_id>/task_list.json \
       --output local_workspace/<run_id>/task_list.json
   ```
4. If you manually correct the task_list (e.g. fix a misidentified molecule),
   write the corrected version back to the **same** run_id-scoped path — never to
   a bare `task_list.json` or any generic shared path.

**FORBIDDEN**: using a bare generic filename such as `task_list.json`,
`temp/task_list.json`, `tasks.json`, or any path that does not contain the
`run_id`. Another Agent session sharing the same workspace WILL overwrite it.

## Skill Invocation Rule (CRITICAL — applies to the ENTIRE pipeline)

Every pipeline stage MUST be invoked via its **CLI entry script** — never by
importing `msa_shared` functions directly, never by writing a custom Python/
paramiko script that "replicates" what the skill does.

Correct invocation pattern (same for ALL stages):
```
python3 scripts/run_orca_ir.py \
    --input  local_workspace/<run_id>/task_list.json \
    --output local_workspace/<run_id>/task_list.json
```

The entry scripts (`run_orca_ir.py`, `run_orca_uvvis.py`, `run_orca_xanes.py`,
etc.) encapsulate critical logic that CANNOT be replicated by reading source:

- **run_id-scoped remote paths** (naming.set_run_id activated by load_task_list)
- **XANES two-submission dance** (opt+SP → parse orb_win → core TD-DFT)
- **NMR per-molecule nucleus merge** (group tasks, one submission, fan-out)
- **SSH reconnect + retry** in poll_slurm (survives multi-hour jobs)
- **Atomic mkdir → upload → sbatch** ordering in submit_slurm
- **mark_success / mark_failed / mark_skipped** + field-whitelist enforcement

**FORBIDDEN**: writing a temporary `.py` script that `import paramiko` and
manually calls `sftp.put` / `exec_command("sbatch ...")` / builds `.inp` files
by calling `orca_input.build_inp(...)` directly. This bypasses ALL of the above
safeguards and WILL fail (missing orb_win, remote dir race, no isolation, no
retry on SSH drop).

If a skill invocation fails, diagnose via its stderr / exit code, fix the
inputs, and re-invoke the same entry script — do NOT "work around" the skill.

**Also FORBIDDEN:**

- **Fabricating `{stage}_status` fields.** If downstream skills skip tasks
  because an upstream status is absent, (re-)run that upstream stage — do NOT
  manually inject `"success"`. The status certifies that the stage's side-effects
  (files, field appends, validation) actually happened; a fake status guarantees
  downstream crashes on missing artefacts.
- **Hand-assembling a task_list.** Tasks MUST originate from `task-recognition`
  (stamps `recognition_status`, validates fields, resolves names, assigns
  `run_id`). Constructing raw JSON task dicts from scratch will cause every
  downstream skill to skip (missing `recognition_status`) or fail (missing
  required fields like `molecule_name`, `target_spectra`, `task_id`).

## Task Field Contract

This is the **SPECIAL orchestration stage (P1-7)**. Unlike all other MSA skills
it has **no** `stage_id` and **no** upstream stage, appends **no** per-stage data
field, and touches **no** `{stage}_status` field (per iron rule 24 /
PIPELINE.md §1.1: it "appends no fields of its own, only schedules + rolls up
status"). It appends **only** `batch_id` + `pipeline_status`.

**Consumes (the whole task_list):**
- Reads whatever per-stage `{stage}_status` fields happen to be present on each
  task (via `rollup_pipeline_status`). No particular field is required, and a
  task with no status fields at all is valid.

**Appends (written by this skill — ONLY these two):**
- `batch_id`: string, `batch_YYYYMMDD_HHMMSS`, **identical on every task** in the
  run (`make_batch_id()` is called once and its value stamped on all tasks).
- `pipeline_status`: string, one of:
  - `"ok"` — all present stages succeeded;
  - `"failed"` — at least one stage failed;
  - `"blocked"` — a stage was skipped because its upstream did not succeed;
  - `"pending"` — no stage status is present yet / nothing has run.

Fabricating any `{stage}_status`, appending any field outside this two-field
whitelist, rewriting any upstream field, or modifying `task_id` is forbidden.

## Scripts

`scripts/batch_orchestrator.py` — CLI entry point.

```
python scripts/batch_orchestrator.py --input local_workspace/<run_id>/task_list.json --output local_workspace/<run_id>/task_list.json
```

Arguments:

| Name | Required | Description |
|---|---|---|
| `--input` | yes | upstream task_list JSON path (bare list or `{task_list:[...]}`) |
| `--output` | yes | output task_list JSON path |

All logs and the `SUCCESS:` / `ERROR:` / `WARNING:` summary lines go to stderr;
the task_list is written to the `--output` file. This skill is pure standard
library (nothing beyond the bootstrap) and imports no heavy dependencies, so
`--help` always works.

## Workflow

1. Load the task_list from `--input` via `common_utils.load_task_list`
   (it accepts both a bare list and a `{task_list:[...]}` wrapper).
2. Call `make_batch_id()` **once** to get the run-wide `batch_id`.
3. For each task, stamp `task["batch_id"] = batch_id` and
   `task["pipeline_status"] = rollup_pipeline_status(task)`. This is a trivial,
   never-raising loop: no per-task try/except is needed because the roll-up
   helper never raises.
4. Dump the task_list to `--output` via `common_utils.dump_task_list`.
5. Emit a `SUCCESS:` stderr line with the `batch_id` and the per-status counts
   (`ok` / `failed` / `blocked` / `pending`).

CLI exit codes: `0` normal, `1` corrupt input, `2` contract mismatch.

## Integration

1. task-recognition
2. name-to-smiles
3. smiles-to-xyz
4. structure-plot
5. param-recommendation
6. time-estimation
7. orca-{spec}
8. plot-{spec}
9. export-spectrum-data
10. batch-orchestrator **(this skill)**

**batch-orchestrator (this skill)** is the final orchestration stage. It runs
after the processing stages have stamped their per-stage `{stage}_status`
fields, giving each task a run-wide `batch_id` for traceability and a single
`pipeline_status` that summarizes the whole per-task pipeline for reporting,
scheduling, and re-run decisions.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| ImportError: msa_shared | vendored scripts/msa_shared/ missing or bootstrap altered | run `tools/vendor_msa_shared.py sync`; keep the §4.1 self-locating block verbatim |
| exit 2 "msa_contract_version mismatch" | msa_shared/VERSION bumped, skill not synced | update the frontmatter and the `MSA_CONTRACT_VERSION` constant |
| exit 1 "cannot load task_list" | input path missing or not valid task_list JSON | pass a real upstream task_list to `--input` |
| `pipeline_status` is `"pending"` on every task | tasks carry no `{stage}_status` fields yet | run the upstream processing stages before orchestration |
| `pipeline_status` is `"blocked"` | an upstream stage was skipped (its upstream did not succeed) | fix the earliest failed/skipped stage, then re-run the pipeline |
| `batch_id` differs between tasks | `make_batch_id()` was called per task instead of once | call it once in `main()` and stamp the same value on all tasks |
