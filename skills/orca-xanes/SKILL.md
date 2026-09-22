---
name: orca-xanes
description: ORCA XANES (X-ray absorption) calculation sub-agent (Part-2, stage_id=calc, spec=xanes). For each XANES task it runs the three-step recipe as TWO cluster submissions: (A) ground-state opt (step1) + a KeepDens single point (step2) chained with $new_job, whose .out is parsed locally for the target element's 1s core-orbital numbers and the optimized geometry; then (B) a core-excitation TD-DFT single point (step3) on that geometry with the resolved %tddft orbwin[0] window. All chemistry comes from task["params"] (step1/step2/step3); the K-edge element from task["extra_params"]["target_element"] (C/N/O/F). It submits via SLURM, polls, and downloads the .out into local_workspace. Triggered when the user asks to "compute the XANES / X-ray absorption spectrum", "run core-excitation TD-DFT", or "orca-xanes". The Agent MUST load this skill BEFORE changing any ORCA or SLURM parameter; DO NOT hard-code functional / basis / solvent / ZORA / nroots / orb_win (they are read from task["params"], and orb_win is resolved by this skill from the SP output); DO NOT hand-construct the .inp or submit.sh (they come from msa_shared.orca_input).
version: 0.1.0
msa_contract_version: 2.0
---

# orca-xanes

## What this skill does

ORCA XANES (X-ray absorption near-edge) calculation sub-agent. For every task
whose `target_spectra` is `"xanes"` it runs the expert three-step XANES recipe on
the cluster and downloads the final `.out` into `local_workspace`.

Unlike the other five spectra, XANES cannot be a single chained input file: the
core-excitation TD-DFT step needs a `%tddft orbwin[0] = a, b, -1, -1` donor
window whose two integers are the orbital numbers of the target element's 1s core
orbitals, and those are only known after an intermediate single point has printed
its `ORBITAL ENERGIES` table. So this skill runs **two cluster submissions with a
local parse in between**:

- **Submission A — `opt` (step1) + `SP KeepDens` (step2)**, chained in one `.inp`
  via `$new_job` (the single point inherits the optimized geometry through the
  ORCA multi-job coordinate carry-over). Its `.out` yields both the step2
  `ORBITAL ENERGIES` table and the optimized geometry.
- **Local parse** — for the task's `target_element` the skill reads the
  core-orbital window `(a, b)` and recovers the optimized geometry (skill-local
  `_xanes_orbitals`), writing an `<mol>_opt.xyz`.
- **Submission B — core-excitation TD-DFT (step3)** as a fresh single point on the
  optimized geometry, with `orbwin[0] = a, b, -1, -1` filled in. Its `.out` is the
  canonical `<mol>-xanes_<element>.out` the downstream plot-xanes skill renders.

Chemistry choices (functional / basis / solvent / ZORA / nroots / MaxIter) come
entirely from `task["params"]` (step1/step2/step3); the K-edge element comes from
`task["extra_params"]["target_element"]` (C/N/O/F); the ORCA and SLURM grammar
comes from `msa_shared.orca_input`; the K-edge energy windows and orbital/geometry
parsing come from the skill-local `_xanes_orbitals`; the transport is the
`common_utils.submit_slurm / poll_slurm / download_results` primitives. This skill
hard-codes no functional, basis, solvent, ZORA, nroots, orb_win, or SLURM template
of its own — `orb_win` in particular is *resolved by this skill* from Submission
A's output, never hard-coded and never read from `params`.

## Agent Behavior

**CRITICAL: Always interact in the same language the user is using.**

Rules for defaults / overrides:
- Chemistry: DO NOT pass parameters; let the skill read functional / basis /
  solvent / ZORA / nroots / MaxIter from `task["params"]` (step1/step2/step3) and
  the K-edge element from `task["extra_params"]["target_element"]`. `orb_win` is
  NOT a parameter — the skill resolves it from Submission A's `ORBITAL ENERGIES`.
- Cluster: this skill ships NO cluster coordinates or credentials, and NEVER
  auto-discovers them from disk. You MUST ask the user for this task's cluster
  profile every time and supply it explicitly at run time (see "Cluster profile"
  below) — never reuse credentials a previous task may have left on disk.
- NEVER silently change functional / basis / solvent / ZORA / nroots / MaxIter /
  target_element / SLURM parameters.

Task-source rule: accept only the task_list produced by `param-recommendation`.
A task whose `target_spectra` is not `xanes` is passed through **untouched** so
the sibling orca-{spec} skills can run over the same batch without clobbering the
shared `calc_status` field. A XANES task whose upstream `params` stage did not
succeed is marked `calc_status="skipped"`.

## Task Field Contract

This skill sits at the **ORCA calculation** stage (stage_id=`calc`,
upstream=`params`, spec=`xanes`).

**Consumes:**
- `target_spectra`: str, must equal `"xanes"` (otherwise the task is passed through)
- `params`: dict with three steps — `step1` holds `calc_type="opt"`; `step2` holds
  `calc_type="xanes_sp"` (a `KeepDens` single point whose `.out` prints the
  `ORBITAL ENERGIES` table); `step3` holds `calc_type="xanes_tddft"` (the core-excitation
  TD-DFT single point) — each carrying functional / basis_set / zora / solvent_model
  plus, on step3, `nroots` and `tddft_maxiter`. `orb_win` is NOT provided here; it is
  resolved by this skill and injected into step3 at run time.
- `params_fallbacks`: optional ordered upstream recipes; `optsp` consumes only
  step2 changes and `core` independently consumes only step3 changes
- `extra_params.target_element`: str, the XANES K-edge element (C/N/O/F); a
  missing/unsupported value is a hard error, never silently defaulted.
- `charge`, `multiplicity`: int (default 0 / 1), stamped onto every `* xyzfile` line
- `xyz_file`: str, initial XYZ path (must exist on disk)
- `molecule_name`: str, used for file naming and the remote work dir

**Appends (only on a XANES task this skill actually runs):**
- `calc_status`: "success" | "failed" | "skipped"
- `calc_error`: str (only when failed / skipped)
- `out_file`: str, local canonical `<mol>_xanes_<element>.out` (Submission B) path
- `calc_wallclock`: int, observed submit-to-completion wallclock across BOTH
  submissions (seconds)
- `slurm_job_id`: str, the SLURM id of Submission B (the TD-DFT job)
- `xanes_orbwin`: list[int], the resolved `[a, b, -1, -1]` donor window
- `xanes_target_element`: str, the resolved K-edge element
- `xanes_optsp_out`: str, local `<mol>_xanes_<element>_optsp.out` (Submission A) path
- `xanes_optsp_job_id`: str, the SLURM id of the final successful Submission A
- `calc_attempts`: append-only record across independent `optsp` and `core` attempts
- `calc_fallback_applied`, `calc_basis_original`, `calc_basis_final`,
  `calc_basis_fallback`, `calc_fallback_steps`, `calc_fallback_reason`,
  `calc_scf_thresh`: summaries derived from `calc_attempts`; production basis is step3

Every output is classified by `msa_shared.orca_diagnostics`; a successful
submission is never rerun. Rewriting upstream fields is forbidden, and a
non-XANES task must be returned unchanged.

**Filenames** come from `naming.orca_inp` / `naming.orca_submit_sh` /
`naming.orca_out` with `variant=naming.job_variant(task)` — for XANES, and only
XANES, that variant is the edge element. A XANES job is genuinely per-edge (a
different core-orbital window per element), so without the tag a C-edge and an
N-edge run of the same molecule would overwrite each other's `.inp` and `.out`.
The other five spectra, NMR included, run one job per molecule and take no
variant (PIPELINE.md §3.2.1).

## Scripts

`scripts/run_orca_xanes.py` — CLI entry point

```
python scripts/run_orca_xanes.py --input local_workspace/<run_id>/task_list.json --output local_workspace/<run_id>/task_list.json
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
2. For each task: pass through if `target_spectra != "xanes"`; skip if
   `upstream_ok(task, "params")` is false; hard-fail if `params` lacks step1/step2/
   step3, if `xyz_file` is missing, or if `extra_params.target_element` is absent /
   unsupported.
3. **Submission A** — `build_inp({step1, step2}, xyz, charge, mult)` -> a two-job
   `.inp` (opt then `SP KeepDens`, separated by `$new_job`, the SP job carrying a
   filename-less `* xyzfile <charge> <mult>` line); written to `<mol>-xanes_<element>-optsp.inp`
   / `.sh` / `.out`. Submit, poll, download, verify ORCA normal termination.
4. **Local parse** — `_xanes_orbitals.parse_core_orbital_range(text, target_element)`
   -> `(a, b)`; `extract_last_geometry(text)` + `geometry_to_xyz(...)` -> write the
   optimized geometry to `<mol>_opt.xyz`.
5. **Submission B** — set `step3["orb_win"] = [a, b, -1, -1]`; `build_inp({step1:
   step3}, opt_xyz, charge, mult)` -> a single-job core-excitation TD-DFT `.inp`
   (`%tddft nroots N tda false MaxIter K orbwin[0] = a, b, -1, -1 end`); written to
   the canonical `<mol>-xanes_<element>.inp` / `.sh` / `.out`. Submit, poll, download, verify.
6. `mark_success(task, "calc", out_file=<mol>-xanes_<element>.out, calc_wallclock=...,
   slurm_job_id=<job B>, xanes_orbwin=[a,b,-1,-1], xanes_target_element=...,
   xanes_optsp_out=<mol>-xanes_<element>-optsp.out, xanes_optsp_job_id=<job A>)`.
7. Dump task_list.

## Integration

1. task-recognition
2. name-to-smiles
3. smiles-to-xyz
4. structure-plot
5. param-recommendation
6. time-estimation
7. **orca-xanes (this skill)**
8. plot-xanes
9. export-spectrum-data

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| calc_status=failed, error starts "XANES opt/SP step: ORCA_TERMINATED_ABNORMALLY" | Submission A (opt+SP KeepDens) `.out` lacks the normal-termination sentinel | check step1/step2 `params` and the initial XYZ geometry |
| calc_status=failed, error starts "XANES TD-DFT step: ORCA_TERMINATED_ABNORMALLY" | Submission B (core-excitation TD-DFT) `.out` lacks the sentinel | check step3 `params` (nroots / MaxIter) and the resolved `orbwin` |
| calc_status=failed, error contains "needs a supported target_element" | `extra_params.target_element` is absent or not one of C/N/O/F | re-run task-recognition; XANES tasks must carry a K-edge element |
| calc_status=failed, error mentions core-orbital range / geometry parse | Submission A's `.out` had no parseable `ORBITAL ENERGIES` / geometry for that element | check the SP step actually ran with `KeepDens` on step2's `basis_set` from `params`; verify the element is present in the molecule |
| calc_status=failed, error="ORCA .out not produced" | the job produced no output file | inspect the `.slurmout` on the cluster; check the SLURM script / ORCA module |
| calc_status=failed, error contains "SLURM poll returned TIMEOUT" | the job exceeded the poll timeout | raise the walltime / poll timeout or check the queue |
| no slurm_job_id | HPC submission failed (network / auth) | check the cluster profile (host / user / password or key_file) and network / VPN |
| task passed through with no calc_status | `target_spectra != "xanes"` | expected — another orca-{spec} skill owns that task |
