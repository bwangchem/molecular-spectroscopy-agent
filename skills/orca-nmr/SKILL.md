---
name: orca-nmr
description: ORCA NMR-shielding calculation sub-agent (Part-2, stage_id=calc, spec=nmr). For each NMR task it builds a two-step ORCA input (ground-state opt then NMR shielding single point, chained with $new_job) from task["params"], submits it to the HPC via SLURM, polls to completion, and downloads the .out into local_workspace. Triggered when the user asks to "compute the NMR spectrum", "run NMR / shielding", or "orca-nmr". The Agent MUST load this skill BEFORE changing any ORCA or SLURM parameter; DO NOT hard-code functional / basis / solvent (they are read from task["params"]); DO NOT hand-construct the .inp or submit.sh (they come from msa_shared.orca_input). NOTE: several same-molecule NMR tasks (e.g. 1H and 13C) are merged into ONE shielding job whose %eprnmr block carries a NUCLEI line per nucleus, so each nucleus is not recomputed; the tasks share the resulting .out.
version: 0.1.0
msa_contract_version: 2.0
---

# orca-nmr

## What this skill does

ORCA NMR shielding calculation sub-agent. For every task whose `target_spectra`
is `"nmr"` it builds the ORCA `.inp` and a SLURM `submit.sh` from the
`task["params"]` produced by param-recommendation, uploads them together with the
initial XYZ to the cluster, submits the job via SLURM, polls to completion,
downloads the resulting `.out`, and verifies ORCA terminated normally.

The NMR recipe is two steps chained in a single input file with `$new_job`: a
ground-state geometry optimization followed by a shielding single point activated
by the ORCA `NMR` keyword, whose isotropic shieldings the downstream plot-nmr
skill turns into a spectrum. The shielding step emits a `%eprnmr` block naming the
nuclei to compute — one `NUCLEI = ALL <X> {SHIFT}` line per requested element —
and ionic systems additionally get a tightened `%scf` (both come from
`task["params"]`). The shielding job inherits the optimized geometry from the opt
job through the ORCA multi-job coordinate carry-over (a filename-less `* xyzfile`
line), so only the initial XYZ is uploaded.

**Per-molecule nucleus merge.** task-recognition splits a "¹H and ¹³C" request into
one NMR task per nucleus, but shielding for every nucleus of a molecule comes from
the *same* electronic structure — running a separate opt+SCF per nucleus would just
repeat identical work. This skill therefore groups the runnable NMR tasks by
molecule and submits ONE shielding job per molecule whose `%eprnmr` block carries a
`NUCLEI` line for every requested element (ORCA prints them successively in the
CHEMICAL SHIELDING SUMMARY). Every task in the group is stamped with the *same*
`out_file` / `slurm_job_id` / wallclock; plot-nmr and export-spectrum-data then
filter that shared summary by each task's `target_nucleus`, so a molecule still
yields one figure and one peaks CSV per nucleus.

Chemistry choices (functional / basis / solvent / ZORA) come entirely from
`task["params"]`, where param-recommendation has already resolved them per system
class and per element (the geometry step and the shielding step do NOT share a
basis, and iodine-containing molecules use a different basis family — read
`_functional_basis.py` for the current recipes rather than restating them here);
the ORCA and SLURM grammar comes from `msa_shared.orca_input`; the transport is the
`common_utils.submit_slurm / poll_slurm / download_results` primitives (PIPELINE.md
§7 scp-transfer + slurm-remote combination). This skill hard-codes no functional,
basis, solvent, or SLURM template of its own.

## Agent Behavior

**CRITICAL: Always interact in the same language the user is using.**

Rules for defaults / overrides:
- Chemistry: DO NOT pass parameters; let the skill read functional / basis /
  solvent from `task["params"]`.
- Cluster: this skill ships NO cluster coordinates or credentials, and NEVER
  auto-discovers them from disk. You MUST ask the user for this task's cluster
  profile every time and supply it explicitly at run time (see "Cluster profile"
  below) — never reuse credentials a previous task may have left on disk.
- NEVER silently change functional / basis / solvent / SLURM parameters.

Task-source rule: accept only the task_list produced by `param-recommendation`.
A task whose `target_spectra` is not `nmr` is passed through **untouched** so the
sibling orca-{spec} skills can run over the same batch without clobbering the
shared `calc_status` field. An NMR task whose upstream `params` stage did not
succeed is marked `calc_status="skipped"`.

## Task Field Contract

This skill sits at the **ORCA calculation** stage (stage_id=`calc`,
upstream=`params`, spec=`nmr`).

**Consumes:**
- `target_spectra`: str, must equal `"nmr"` (otherwise the task is passed through)
- `target_nucleus`: str, e.g. `"1H"` / `"13C"`; the requested nucleus. Same-molecule
  NMR tasks are merged into ONE shielding job, one `%eprnmr NUCLEI` line per nucleus
- `params`: dict; step1 holds `calc_type="opt"` and step2 holds `calc_type="nmr"`
  with functional / basis_set / zora / solvent_model, the `nmr_nucleus` element
  letter (H or C), and `scf_thresh` for ionic systems
- `params_fallbacks`: optional ordered upstream recipes; ionic systems consume
  threshold fallback before any configured basis fallback
- `xyz_file`: str, initial XYZ path (must exist on disk)
- `molecule_name`: str, used for file naming, the remote work dir, and the merge key

**Appends (only on an NMR task this skill actually runs):**
- `calc_status`: "success" | "failed" | "skipped"
- `calc_error`: str (only when failed / skipped)
- `out_file`: str, local `.out` path
- `calc_wallclock`: int, observed submit-to-completion wallclock (seconds)
- `slurm_job_id`: str
- `calc_attempts`: append-only submission history shared by same-molecule H/C tasks
- `calc_fallback_applied`, `calc_basis_original`, `calc_basis_final`,
  `calc_basis_fallback`, `calc_fallback_steps`, `calc_fallback_reason`,
  `calc_scf_thresh`: summaries derived from the shared attempts

Every output is classified by `msa_shared.orca_diagnostics`; only its retry
verdict may consume the next upstream fallback. Rewriting upstream fields is
forbidden, and a non-NMR task must be returned unchanged.

## Scripts

`scripts/run_orca_nmr.py` — CLI entry point

```
python scripts/run_orca_nmr.py --input local_workspace/<run_id>/task_list.json --output local_workspace/<run_id>/task_list.json
```

### Cluster profile

This skill submits to an HPC cluster over SSH/SLURM but ships **no** host,
credentials, or ORCA paths. You must provide a cluster profile at run time with
these fields:

- `host` (required) — cluster login node
- `user` (required) — SSH username
- `password` **or** `key_file` (one is required) — SSH auth
- `remote_root` (required) — base dir for remote work dirs
- `orca_bin` (required) — absolute path to the ORCA binary
- `mpi_prefix` (required) — OpenMPI install prefix (its `bin`/`lib` go on PATH)
- `partition`, `qos` (optional) — SLURM partition / QoS

Credentials are **never read from disk and never auto-discovered** — there is no
`--cluster-config` flag and no config-file path of any kind. The **user is the
only source** of the cluster coordinates. Pass them **inline** as `HPC_*`
environment variables on the run command shown above, so the secrets stay off
disk and cannot leak to a later task:

```
HPC_HOST=<login-node> HPC_USER=<username> HPC_PASSWORD=<password> \
HPC_REMOTE_ROOT=<remote-root> HPC_ORCA_BIN=<orca-binary> \
HPC_MPI_PREFIX=<openmpi-prefix> \
[HPC_KEY_FILE=<private-key>] [HPC_PARTITION=<partition>] [HPC_QOS=<qos>] \
<the run command shown above>
```

Use `HPC_KEY_FILE` instead of `HPC_PASSWORD` for key-based auth; `HPC_PARTITION`
and `HPC_QOS` are optional. Resolution precedence is **explicit programmatic
kwargs > `HPC_*` env vars**; nothing is ever read from a file, so dropping a
`cluster.json` anywhere has no effect.

If any required value is missing the skill exits with an actionable
`ClusterConfigError` naming the missing fields — that is your signal to ask the
user for the cluster coordinates. Within a single working session you MAY set the
`HPC_*` variables once (e.g. export them into your shell) and reuse them across
every orca-* run in that session — that is inline, in-memory reuse, not on-disk
persistence, so you need not re-prompt for each task. What you must NEVER do:
write the credentials to a file, read them back from disk, auto-discover them,
invent them, or offer to "recover / look them up from a previous run or a saved
config" — if the session no longer holds them, ask the user again.

## Workflow

1. Load task_list.
2. Partition tasks: a task with `target_spectra != "nmr"` is passed through
   **untouched**; a runnable NMR task (`upstream_ok(task, "params")` true) joins a
   per-molecule group keyed by `molecule_name`; an NMR task whose `params` stage did
   not succeed is marked `calc_status="skipped"`.
3. For each molecule group, build ONE job from the union of the group's nuclei:
   `orca_input.build_inp(params, xyz_basename)` with `nmr_nuclei` set on the
   shielding step -> a two-job `.inp` (opt then nmr, separated by `$new_job`, the
   nmr job carrying a filename-less `* xyzfile` line and a `%eprnmr` block with one
   `NUCLEI = ALL <X> {SHIFT}` line per requested element); `build_submit_sh(...)` ->
   `submit.sh`; both written via `naming.orca_inp` / `naming.orca_submit_sh`.
4. `common_utils.submit_slurm(remote_dir, script, upload_files=[inp, sh, xyz])` —
   once per molecule group.
5. `common_utils.poll_slurm(job_id)` until it leaves the queue.
6. `common_utils.download_results(remote_dir, [out_name], local_dir)`.
7. Verify the `.out` carries ORCA's normal-termination sentinel.
8. `mark_success(task, "calc", out_file=..., calc_wallclock=..., slurm_job_id=...)`
   on **every** task in the group, all sharing the same `.out` / `slurm_job_id` /
   wallclock (downstream stages filter that shared `.out` per `target_nucleus`).
9. Dump task_list.

## Integration

1. task-recognition
2. name-to-smiles
3. smiles-to-xyz
4. structure-plot
5. param-recommendation
6. time-estimation
7. **orca-nmr (this skill)**
8. plot-nmr
9. export-spectrum-data

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| calc_status=failed, error="ORCA_TERMINATED_ABNORMALLY" | the `.out` lacks the normal-termination sentinel | check the `.inp` parameters (opt or NMR step) and the XYZ geometry |
| calc_status=failed, error="ORCA .out not produced" | the job produced no output file | inspect the `.slurmout` on the cluster; check the SLURM script / ORCA module |
| calc_status=failed, error contains "SLURM poll returned TIMEOUT" | the job exceeded the poll timeout | raise the walltime / poll timeout or check the queue |
| no slurm_job_id | HPC submission failed (network / auth) | check the cluster profile (host / user / password or key_file) and network / VPN |
| task passed through with no calc_status | `target_spectra != "nmr"` | expected — another orca-{spec} skill owns that task |
