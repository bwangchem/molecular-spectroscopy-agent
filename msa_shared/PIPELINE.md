# MSA Pipeline Contract

Version: v2.0 — Molecular Spectroscopy Agent (MSA)
Scope: Part 1 preprocessing + Part 2 ORCA calculation + Part 3 plotting/export
Golden rule: enable the agent to chain the entire pipeline end-to-end with the
highest possible success rate. Every skill obeys this contract; skills MUST NOT
extend or rename fields on their own.

---

## 0. Terminology and overview

- **task**: a single calculation unit — one molecule x one spectrum x one solvent
  condition. Skills pass data downstream through the task object; fields are only
  appended, never rewritten and never deleted.
- **task_list**: `List[task]`, the batch-flow carrier.
- **batch**: the set of tasks parsed from one user request, identified by
  `parse_batch_id` (parse_YYYYMMDD_HHMMSS) and `batch_id` (batch_YYYYMMDD_HHMMSS).
- **entry point**: a stage may expose **more than one CLI entry script**, one per
  shape of input, when the same work can be requested in genuinely different ways
  (P1-1 is the only such stage today: one user's natural language, or a curated
  manifest CSV expanded as molecules x spectra). Every entry of a stage emits the
  same field set with the same spellings, so no later stage can tell which one was
  used; the only per-entry differences are `run_id` (per batch) and `task_id` (per
  position), both of which are generated rather than derived from the input.
- **run**: one end-to-end pipeline execution, identified by `run_id`
  (run_YYYYMMDD_HHMMSS_<6hex>) stamped on every task. It is the isolation
  namespace that keeps concurrent runs from clobbering each other (see §1.4).
- **6 spectrum types**: `ir | raman | uvvis | nmr | fluo | xanes`
- **Shared library**: the `msa_shared/` Python package at the repository root. It
  holds spectrum_style.py, common_utils.py, naming.py, the constants package, and
  this contract.

### Path decoupling (important for open-source use)

At runtime there is **no repository and no shared install path**: a host Agent loads each
skill on its own. Every skill is therefore self-contained — a byte-identical copy
of exactly the `msa_shared` modules it imports is *vendored* into that skill's
`scripts/msa_shared/`, and the entry script self-locates its own `scripts/`
directory on `sys.path`:

```
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
```

The repo-level `msa_shared/` is the **dev-time single source of truth**; run
`python3 tools/vendor_msa_shared.py sync` after editing it and
`... check` as a checksum drift guard. No absolute paths are baked into any
skill. See §5.

---

## 1. task object field lifecycle

The task is the single data structure of the pipeline. Each skill appends only
the fields it owns; it must not modify upstream fields.

### 1.1 Field-append table by stage

| Stage | Skill | Consumes | Appends |
|---|---|---|---|
| P1-1 recognition | task-recognition | (user natural language, or a curated manifest CSV) | `run_id`, `task_id`, `molecule_name`, `target_spectra`, `solvent_raw`, `solvent_std`, `extra_params` |
| P1-2 name to SMILES | name-to-smiles | above | `smiles`, `smiles_source`, `molecule_name_en`, `smiles_status`, `smiles_error?`, `smiles_warnings?` |
| P1-3 SMILES to XYZ | smiles-to-xyz | above | `xyz_file`, `xyz_status`, `num_atoms`, `xyz_error?` |
| P1-4 structure image | structure-plot | above | `structure_png`, `structure_status`, `structure_error?` |
| P1-5 parameters | param-recommendation | above | `system_type`, `charge`, `multiplicity`, `need_zora`, `params` (per-step functional/basis_set/zora/calc_type/solvent_model), `params_fallbacks?`, `params_status`, `params_error?` |
| P1-6 time estimate | time-estimation | above | `time_estimate` (steps[], total_seconds, confidence), `time_estimate_status`, `time_estimate_error?` |
| P1-7 orchestration | batch-orchestrator | above | `batch_id`, `pipeline_status` (status roll-up; appends no per-stage data field) |
| P2 ORCA calc | orca-{ir,raman,uvvis,nmr,fluo,xanes} | above | `out_file`, `calc_status`, `calc_wallclock`, `slurm_job_id?`, `calc_error?`, `calc_attempts?`, `calc_fallback_applied?`, `calc_basis_original?`, `calc_basis_final?`, `calc_basis_fallback?`, `calc_fallback_steps?`, `calc_fallback_reason?`, `calc_scf_thresh?`, plus xanes-only `xanes_target_element?`, `xanes_orbwin?`, `xanes_optsp_out?`, `xanes_optsp_job_id?` |
| P3 plotting | plot-{ir,raman,uvvis,nmr,fluo,xanes} | above | `plot_png`, `plot_pdf`, `plot_status`, `plot_error?`, `plot_signal?`, `plot_message?`, `plot_warnings?` |
| P3+ data export | export-spectrum-data | above | `peaks_csv`, `spectrum_csv`, `export_status`, `export_error?`, `export_signal?`, `export_message?`, `export_warnings?` |

`params_fallbacks` is an ordered, bounded list owned by P1-5. Each item has
`fallback_kind` (`scf_threshold | basis`), `changed_steps` (step names), and a
complete replacement `params` object. Non-NMR calculations have at most one
fallback; iodine-containing ionic NMR may have two, first loosening the SCF
threshold and then changing the shielding basis. P2 MUST consume this list in
order and MUST NOT derive a new basis, threshold, or retry count.

`calc_attempts` is the append-only record of submissions that actually ran. Each
item has `attempt` (zero-based), `params_source` (`params` or
`params_fallbacks[N]`), `submission` (`single | optsp | core`), `changed_steps`,
`status` (`success | failed`), `failure_reason`, `out_file`, and optional
`slurm_job_id`. The permitted failure reasons are `none`, `scf_not_converged`,
`rpa_not_converged`, `numfreq_incomplete`, `optimization_not_converged`,
`cis_error`, and `orca_terminated_abnormally`. The remaining `calc_*` fields in
the P2 row are summaries derived from this history for downstream export. All
of these additions are optional so legacy task_lists remain valid.

### 1.2 Field naming conventions

- Status fields are always `{stage}_status`, values `success | failed | skipped`.
  This set is **closed** — a stage never invents a fourth value (see §2.5).
- Error fields are always `{stage}_error`, present only when status == failed.
- `{stage}_signal` is present only with the single value `"none"`, and only
  alongside `{stage}_status="success"`: the stage ran correctly but the physics
  offered nothing to draw (see §2.5). `{stage}_message` carries the one-sentence
  user-facing reason and is present exactly when `{stage}_signal` is.
- `{stage}_warnings` is a list of strings — non-fatal diagnostics that do NOT
  change the status (imaginary frequencies being the main case). It is appended
  through `common_utils.add_warning`, which deduplicates while preserving order,
  and it may accompany any status.
- `molecule_name_en` is the English display name resolved by name-to-smiles. It
  is the title source for every figure via `naming.title_name(task)`, which
  falls back to `molecule_name` when the English name is absent (see §3.3).
- `smiles_source` records **where** the structure came from, and its value domain
  is closed: `dict` (the skill's offline dictionary) | `pubchem` (live lookup) |
  `manifest` (a curated per-dataset CSV supplied to that run). The field exists so
  a published structure can be traced to its origin, which only works if the
  spelling is fixed here rather than invented per source.
- File-path fields are path strings, suffixed with `_file` / `_png` / `_pdf` /
  `_csv`. They are written **absolute**: `naming.workspace_dir` anchors every
  artifact via `os.path.abspath` at creation time, so a downstream stage resolves
  an upstream artifact regardless of its own working directory (a relative path
  would break the moment a later stage runs from a different cwd). Consumers of
  the initial geometry go through `naming.resolve_initial_xyz(task)`, which
  prefers the stored `xyz_file` and falls back to the canonical recomputed path
  for legacy/relocated task_lists.
- Time unit: seconds (int or float).
- Boolean fields use real booleans, never the strings "true"/"false".

### 1.3 task_id generation

Generated by task-recognition, format `task_{NN}_{molecule_name}_{target_spectra}`
where NN is a two-digit index within the batch. Downstream skills must not rewrite it.
Every P1-1 entry point (§0) builds it through the same constructor, numbering
globally within the batch, so the format cannot drift between entries.

### 1.4 run_id — run-isolation namespace

Generated by task-recognition, format `run_YYYYMMDD_HHMMSS_<6hex>` (the random
suffix defeats same-second collisions). It is stamped onto **every task** and onto
the wrapped batch payload, and downstream skills must not rewrite it.

The pipeline is content-addressed by `(molecule_name, spec)`: without a run
namespace, two runs that touch the same molecule (especially the same spectrum)
resolve to byte-identical local paths **and** the same directory under the shared
HPC `remote_root`, so they clobber each other's `.inp`/`.xyz`/`.out`. `run_id`
prevents this: `common_utils.load_task_list` reads it (top-level, else the first
task's copy) and calls `naming.set_run_id`, after which every `naming.*` path this
process builds nests under it — locally `./local_workspace/<run_id>/<slug>/…` and
remotely `<remote_root>/<run_id>/<slug>/…`. Concurrent runs are therefore mutually
isolated end-to-end with no per-skill code change. A task_list without a `run_id`
falls back to the legacy flat layout (backward compatible).

---

## 2. Failure contract (fail-soft)

**Core principle: a single failing task must never abort the batch. Errors are
written into task fields and the pipeline continues.**

### 2.1 In-skill rules

- Each skill's entry function catches exceptions, writes
  `{stage}_status="failed"` + `{stage}_error="<message>"`, then returns — it does
  not raise.
- The CLI exits 0 even when individual tasks failed (fail-soft): per-task
  failures live in task fields, not the exit code. It exits non-zero only for
  batch-wide conditions, per this fixed scheme:

  | Exit | Meaning | Agent action |
  |---|---|---|
  | 0 | batch processed (some tasks may be `failed`/`skipped`) | inspect task fields |
  | 1 | task_list could not be loaded (missing/corrupt input JSON) | fix the input path/JSON |
  | 2 | `msa_contract_version` mismatch (skill vs `msa_shared/VERSION`) | align skill + shared version |
  | 3 | `ClusterConfigError` — HPC profile incomplete (orca-* only) | supply cluster host/creds/paths |
  | 4 | environment not provisioned — a required dependency is missing | `pip install` the reported deps |

- stdout is reserved for structured JSON output; all logs and warnings go to
  stderr. Third-party stderr noise (RDKit etc.) must be silenced, e.g.
  `RDLogger.DisableLog('rdApp.*')`.

### 2.2 skipped propagation

- Tasks whose upstream failed are skipped downstream:
  `{stage}_status="skipped"`, `{stage}_error="upstream {stage_x} failed"`.
- A **no-signal** upstream (§2.5) does NOT trigger a skip: its status is
  `success`, so the downstream stage runs normally — export in particular must
  still write the raw peaks table even when no curve could be drawn.
- The orchestrator decides whether to dispatch the next step based on field
  status and does not re-report errors.

### 2.3 Error payload standard

```json
{
  "status": "failed",
  "error_type": "input_error | dependency_error | runtime_error | network_error",
  "message": "human readable one-line reason",
  "input": {"...echo of original input..."},
  "warnings": []
}
```

### 2.4 Dependency preflight (runtime-agnostic)

The pure-Python `msa_shared` modules are vendored into each skill, but the
heavy third-party libraries (`rdkit`, `numpy`, `matplotlib`, `paramiko`,
`pubchempy`) are compiled wheels / C-extensions that **cannot** be vendored as
source. At runtime a skill is loaded by an arbitrary host Agent — QoderWork or
otherwise — that does **not** read `requirements.txt`. A missing library must
therefore fail *loudly and once*, not silently degrade task after task.

- **Declaration.** Every entry script declares a module-scope
  `MSA_REQUIRES` list of `(import_name, pip_spec)` or
  `(import_name, pip_spec, "soft")` tuples. This list travels with the vendored
  skill and is the single machine-readable source of truth for that skill's
  deps. Skills that need only the stdlib declare `MSA_REQUIRES = []`.
- **Self-check.** `main()` calls `ensure_deps(MSA_REQUIRES, "<skill>")`
  (from vendored `common_utils`) immediately after `log_startup(...)`, before
  loading the task_list. `--help` short-circuits earlier in argparse, so it is
  unaffected.
- **Hard vs soft.** A missing **hard** dep prints one greppable
  `MSA_ENV_MISSING: <import> -- pip install "<spec>"` line per dep plus a
  ready-to-run install command, then exits **4** (see §2.1). A missing **soft**
  dep (e.g. `pubchempy`, which only powers name-to-smiles' PubChem fallback)
  prints `MSA_ENV_SOFT_MISSING: ...` and lets the skill degrade — it never
  gates the batch.
- **Opt-in auto-install.** With `MSA_AUTO_INSTALL=1` in the environment a
  missing dep is `pip install`-ed into the running interpreter and re-checked
  before being declared missing. Off by default so a skill never mutates its
  host environment without consent. The top-level `requirements.txt` remains the
  convenient union for provisioning an environment ahead of time.

### 2.5 "No valid signal" is a success, not a failure

A converged calculation can legitimately have nothing to draw: every vibrational
mode imaginary, a dark S1, every core transition below the oscillator-strength
threshold. The job ran, the numbers are trustworthy, and there is simply no
curve. This is **not** an execution error and must not be reported as one.

- Encoding: `{stage}_status="success"` + `{stage}_signal="none"` +
  `{stage}_message="<one-sentence reason>"`, written by
  `common_utils.mark_no_signal`. Artifact fields are set to the empty string
  (e.g. `plot_png=""`) so a consumer can tell "no artifact" apart from "field
  absent". No figure file is written.
- **Why not a fourth status value.** Every downstream gate is
  `upstream_ok(task, stage)`, i.e. `status == "success"`. A `no_signal` status
  would silently skip the export stage — but export is explicitly still required
  to write the raw peaks table in this case, because the discrete peaks may
  exist even when the broadened curve does not.
- `mark_success` clears any stale `{stage}_signal` / `{stage}_message`, so
  re-running a stage after the calculation was redone cannot leave a "nothing to
  draw" annotation next to a freshly written figure.
- Thresholds and wording are constants, never skill-local literals:
  `OSCILLATOR_STRENGTH_THRESHOLD = 1e-6` (dark-state cutoff shared by UV-Vis /
  fluorescence / XANES), `SIGNAL_EPS = 1e-12` (float-noise guard on the
  broadened curve) and the per-spec `NO_SIGNAL_MESSAGE` all live in
  `msa_shared/constants/plot_defaults.py` and are applied by the builders in
  `msa_shared/orca_output.py`. The message strings are expert-fixed wording —
  reuse them verbatim, do not paraphrase.
- XANES has one strict distinction: if the parsed raw transition table is
  non-empty but every transition lies outside the fixed element window, the
  builder returns an explicit error and retains the parsed energies for
  diagnosis. It MUST NOT silently convert this protocol/window mismatch into a
  no-signal success.

The parallel channel is `{stage}_warnings` (§1.2): the artifact IS produced but
carries a caveat the chemist must see. Builders return their diagnostics in
`spectrum["warnings"]`; the skill replays them through `add_warning`, and the
export stage writes the same list into the CSV metadata header — so the figure,
the task_list and the CSV can never disagree.

Together with §2.1/§2.2 this gives each stage exactly four outcomes:
`success` (with artifact) / `success` + `signal="none"` (no artifact) /
`failed` / `skipped`.

### 2.6 A combination that does not exist is not a status either

§2.5 is about a job that ran and found nothing to draw. This section is about the
case one step earlier: a molecule x spectrum combination that is **physically
impossible to begin with** — a ¹H spectrum of a hydrogen-free molecule, a nitrogen
K-edge of a molecule containing no nitrogen. There is no calculation to run and no
failure to fix.

- Such a combination **never enters the task_list**. It is filtered at the P1-1
  entry point that generated it, before any task object exists.
- The entry point records what it filtered in a **separate side report** (a CSV
  with columns `mol_id,target_spectra,variant,reason`), whose path the caller
  supplies. The report is what lets a chemist reconcile a published task count
  against the molecule count, so dropping the rows silently is not acceptable
  either.
- **Why not a status value.** `{stage}_status` is the closed three-value enum of
  §1.2 and every downstream gate is `upstream_ok` (== `"success"`). A
  `not_applicable` status would make these rows travel the whole pipeline as
  permanently-skipped tasks, inflating every per-stage count with rows that were
  never candidates; and unlike §2.5 there is no artifact anyone still needs. The
  right place for "this was never a task" is therefore outside the task_list.

### 2.7 ORCA failure classification and bounded fallback

P2 classifies each completed submission centrally through
`msa_shared/orca_diagnostics.py`, in this fixed order: **A → C → B → unknown
abnormal**. Process-progress messages (class D) never determine a terminal
state.

- **A1 — retryable SCF failure:**
  `SCF NOT CONVERGED AFTER\s+\d+\s+CYCLES`.
- **A2 — retryable TD-DFT/RPA failure:** both
  `RPA/TD-DFT did not converge! You may try raising the number of iterations.`
  and `ORCA finished by error termination in CIS` must occur. The matcher MUST
  NOT depend on a process number, source line, temporary filename, job name, or
  `mpirun` path.
- **A3 — retryable Raman NUMFREQ failure:** when the caller explicitly enables
  NUMFREQ diagnosis, any of `Calculation\s+\d+\s+of\s+\d+\s+is missing`,
  `Numerical calculation ISN'T COMPLETE`, or `failed to read Hessian file!`.
- **C — non-retryable CIS failure:** CIS error termination without the A2
  non-convergence sentence.
- **B — non-retryable optimization failure:** after excluding A and C, fewer
  `THE OPTIMIZATION HAS CONVERGED` banners than the optimization steps in that
  submission.
- Any other output without a normal-termination banner is
  `orca_terminated_abnormally` and is not retried.

Only A-class results may consume the next applicable `params_fallbacks` item.
When no item remains, the task fails. B, C, and unknown abnormal results fail
without fallback. The contract owns this data shape, ordering, and bounded
retry semantics; the concrete spectrum/system basis recipes remain solely in
`skills/param-recommendation/scripts/_functional_basis.py`.

### 2.8 Manifest-vs-derived parameter validation report

P1-5 may receive the source manifest together with an explicit report path and
write a batch-level CSV comparing manifest `charge`, `multiplicity`, and
`needs_ZORA` against values derived independently from the task SMILES/RDKit.
The comparison is performed once per unique `mol_id`; difference rows contain
`mol_id,field,manifest_value,pipeline_value,classification,reason`.

The report is diagnostic only: it is not embedded in the task_list, creates no
new status value, and cannot change `params_status`. An iodine-containing
molecule whose old manifest has `needs_ZORA=true` while the Br-only rule derives
false is classified `expected_rule_change`; all other differences are
`mismatch`. For the expert manifest used to define this migration, 88 such ZORA
differences are expected. The caller names the report through the CLI; as a
batch-level artifact it follows §3.1 and does not use `naming.*`.

---

## 3. Filename convention table

Defined centrally in `msa_shared/naming.py`; all skills import from it. Skills
must not concatenate filenames by hand.

### 3.1 Working-directory layout

```
./local_workspace/{run_id}/{molecule_slug}/
    {mol}_initial.xyz              # smiles-to-xyz output
    {mol}_structure.png            # structure-plot output
    {mol}_ir.inp / .out / .hess    # orca-ir input/output
    {mol}_raman.inp / .out
    {mol}_uvvis.inp / .out
    {mol}_nmr.inp / .out
    {mol}_fluo.inp / .out
    {mol}_xanes_C.inp / .out       # orca-xanes: one job PER absorbing element
    {mol}_ir_spectrum.png / .pdf   # plot-ir output
    {mol}_ir_peaks.csv             # export-spectrum-data
    {mol}_ir_spectrum.csv
    {mol}_nmr_1H_spectrum.png      # variant-tagged: one figure per nucleus
    {mol}_nmr_1H_peaks.csv
```

The separator inserted between the molecule slug and the spectrum tag is always
an underscore for ORCA job files, figures, and CSVs. A hyphen that is already
part of the molecule name remains part of its slug (for example,
`p-nitrophenol_ir.inp`). Never hand-build a filename — always call the
`naming.*` function (§3.2), which is the only place the separator is decided.

`local_workspace/` is created relative to the runner's current working directory,
so nothing is hard-coded to a specific machine; the path strings recorded in the
task_list, however, are absolutized at creation time (see §1.2) so a downstream
stage resolves them from any cwd. The `{run_id}` layer (see §1.4) isolates
concurrent runs — the remote side mirrors it as
`{remote_root}/{run_id}/{molecule_slug}/`. A task_list without a `run_id` omits
that layer (legacy flat layout).

The table above covers **per-molecule, per-spectrum** artifacts, which is exactly
what `naming.*` owns. **Batch-level** files — the task_list carrier itself (§3.4)
and the §2.6 / §2.8 side reports — are named by the caller through a CLI argument and
deliberately do **not** go through `naming.*`: they belong to a whole batch, so
there is no `(molecule, spec)` pair to derive a name from, and forcing them into
the naming module would invite a per-batch file to be written into one arbitrary
molecule's directory.

There is exactly one `{mol}_nmr.inp` / `{mol}_nmr.out` pair per molecule even when
several nuclei (e.g. ¹H and ¹³C) are requested: orca-nmr merges the same-molecule
NMR tasks into ONE shielding job whose `%eprnmr` block carries a `NUCLEI` line per
element (all requested nuclei share one electronic structure). The plot/export
stages then fan that single `.out` back out into one figure and one peaks CSV per
nucleus — the per-nucleus variant tag supplied by `naming.spec_variant(task)`
(e.g. `{mol}_nmr_1H_peaks.csv`, `{mol}_nmr_1H_spectrum.png`) disambiguates them.

### 3.2 Naming fields

Every row is produced by the named `naming.py` function. `{tag}` is `{spec}` or,
when the task has a variant, `{spec}_{variant}` (§3.2.1). `{mol}` is the slug of
§3.3.

| Purpose | Builder | Pattern | Example |
|---|---|---|---|
| initial XYZ | `xyz_initial(mol)` | `{mol}_initial.xyz` | `benzene_initial.xyz` |
| optimized XYZ | `xyz_opt(mol)` | `{mol}_opt.xyz` | `benzene_opt.xyz` |
| structure image | `structure_png(mol)` | `{mol}_structure.png` | `benzene_structure.png` |
| ORCA input | `orca_inp(mol, spec, variant)` | `{mol}_{tag}.inp` | `benzene_ir.inp`, `benzene_xanes_C.inp` |
| ORCA output | `orca_out(mol, spec, variant)` | `{mol}_{tag}.out` | `benzene_ir.out` |
| SLURM script | `orca_submit_sh(mol, spec, variant)` | `{mol}_{tag}.sh` | `benzene_ir.sh` |
| spectrum PNG | `spectrum_png(mol, spec, variant)` | `{mol}_{tag}_spectrum.png` | `benzene_nmr_1H_spectrum.png` |
| spectrum PDF | `spectrum_pdf(mol, spec, variant)` | `{mol}_{tag}_spectrum.pdf` | `benzene_xanes_C_spectrum.pdf` |
| peak table CSV | `peaks_csv(mol, spec, variant)` | `{mol}_{tag}_peaks.csv` | `benzene_xanes_C_peaks.csv` |
| curve CSV | `spectrum_csv(mol, spec, variant)` | `{mol}_{tag}_spectrum.csv` | `benzene_ir_spectrum.csv` |

`{spec}` in `ir raman uvvis nmr fluo xanes`.

#### 3.2.1 Variants — `spec_variant` vs `job_variant`

Two spectra fan a single molecule out into several artifacts, so the filename
needs one more token. The two accessors are deliberately different and passing
the wrong one is the classic way to clobber a file:

- `naming.spec_variant(task)` — **artifact-level**. NMR: the target nucleus
  (`1H`, `13C`). XANES: the absorbing element (`C`, `N`, ...). Feed it to
  `spectrum_png` / `spectrum_pdf` / `peaks_csv` / `spectrum_csv`.
- `naming.job_variant(task)` — **job-level**, XANES only. One NMR shielding job
  serves every nucleus (§3.1), so NMR job files must NOT carry a nucleus tag;
  `job_variant` returns `None` for NMR precisely to make that mistake
  impossible. Feed it to `orca_inp` / `orca_out` / `orca_submit_sh`.

The XANES edge element used for the plotted energy window comes from the same
`spec_variant` call (via `common_utils.spectrum_kwargs`), so the window and the
file it is written to can never refer to different edges.

### 3.3 Molecule-name normalization

`molecule_name` may be Chinese / contain spaces / special characters (it is
domain input data), so it has two normalizations that must not be confused:

- **Filesystem side** — `naming._safe_slug(molecule_name)` maps it to a safe
  token, used by every builder in §3.2. Skills always go through `naming.*`
  functions before touching files.
- **Display side** — `naming.display_name(name)` capitalizes for human
  consumption while honouring chemical convention: a leading locant / stereo
  descriptor keeps its lower case and the parent name takes the capital
  (`p-nitrophenol` → `p-Nitrophenol`, `N,N-dimethylformamide` →
  `N,N-Dimethylformamide`). Nothing is ever lower-cased, so `THF` / `CH3OH` and
  an already-correct PubChem Title survive untouched.

The display name lives in `naming` (not in `spectrum_style`) because
name-to-smiles must produce `molecule_name_en` without importing matplotlib, and
two implementations of "how do we capitalize a chemical name" would drift apart.
`spectrum_style.format_molecule_name` is retained only as an alias.

What a **figure title** shows is decided by exactly one function,
`naming.title_name(task)`: `molecule_name_en` when present, else the user's
original `molecule_name`. All six plot skills call it, so they cannot disagree
about which field wins.

A **CSV metadata header** deliberately does not collapse the two: it always
writes the original `molecule` and adds a separate `molecule_en` line only when
an English name exists and differs. A figure has room for one name; a data file
should lose neither.

### 3.4 Agent-side task_list file naming (CRITICAL)

The task_list JSON file passed via `--input` / `--output` between pipeline stages
is the Agent's responsibility to manage. **The Agent MUST scope its path with the
`run_id` so that concurrent sessions never collide**, even when they share the
same working directory. A generic bare name like `task_list.json` is **forbidden**.

Recommended convention — store the carrier file inside the run's directory:

```
local_workspace/<run_id>/task_list.json
```

Sequence:

1. `task-recognition` emits the task_list to stdout (or to `--output`); the Agent
   captures it and reads the `run_id` field (top-level or on any task).
2. The Agent writes the captured JSON to `local_workspace/<run_id>/task_list.json`
   (creating the directory if necessary).
3. Every subsequent stage invocation uses that same path for both `--input` and
   `--output`:
   ```
   python scripts/name_to_smiles.py \
       --input  local_workspace/<run_id>/task_list.json \
       --output local_workspace/<run_id>/task_list.json
   ```
4. The file is updated in-place stage by stage (fields only ever appended, never
   deleted, per §1). If the Agent needs the intermediate state for retry it may
   copy to `local_workspace/<run_id>/task_list_backup.json` — that path also
   lives inside the run_id directory.

**Why this matters**: multiple Agent sessions may share the same workspace
directory. Without `run_id` in the path, the last writer wins and earlier
sessions silently lose their data. The `run_id` is a natural per-session unique
key (it includes a timestamp + random suffix, see §1.4) and is always available
from the very first stage output.

### 3.5 Agent-side skill invocation (CRITICAL)

The Agent orchestrates the pipeline by **invoking each skill's CLI entry script**
(`python3 scripts/run_<skill>.py --input ... --output ...`). It MUST NOT bypass
a skill by importing `msa_shared` library functions directly or by writing a
custom script that replicates submission logic (e.g. calling `paramiko.SFTPClient`
or `orca_input.build_inp` in an ad-hoc temp file).

Entry scripts encapsulate non-obvious sequencing that cannot be reliably
reconstructed from reading source:

| Skill | Hidden complexity |
|-------|-------------------|
| orca-xanes | Two-submission dance: opt+SP → parse `orb_win` from `.out` → core-excitation TD-DFT |
| orca-nmr | Per-molecule nucleus merge: group same-molecule tasks → one shielding job → fan-out |
| ALL orca-* | `submit_slurm` atomic mkdir→upload→sbatch; `poll_slurm` reconnect + retry; run_id-scoped remote paths via `naming` |

A skill invocation failure (non-zero exit) MUST be diagnosed via its stderr /
exit code and retried with corrected inputs — never "worked around" by hand-
crafting a replacement script.

**Also FORBIDDEN (same rationale — bypasses the pipeline's integrity chain):**

- **Fabricating `{stage}_status` fields.** If downstream skills skip tasks
  because an upstream `{stage}_status` is missing, the correct action is to
  (re-)run that upstream stage — not to inject `"success"` by hand. A fabricated
  status hides the fact that the stage's side-effects (files, field appends,
  validation) never happened; downstream skills will crash on missing artefacts.
- **Hand-assembling a task_list.** The task_list MUST originate from
  `task-recognition` (which stamps `recognition_status` and validates required
  fields). Constructing tasks from scratch (e.g. raw JSON dicts) omits the
  three-layer name resolution, SMILES/spec validation, and run_id stamping that
  task-recognition performs — downstream will either skip (no status) or fail
  (missing fields).

---

## 4. Single source of truth for constants

**Any numeric/string constant is defined once in the constants package; other
skills import it. No scattered magic values.**

### 4.1 Constant ownership

| Category | Location | Example |
|---|---|---|
| functional/basis tiers | skills/param-recommendation/scripts/_functional_basis.py (skill-local) | `FUNCTIONAL_LIGHT="r2SCAN-3c"` |
| CPCM solvent map | msa_shared/constants/solvents.py | `{"水":"CPCM(Water)", ...}` |
| FWHM defaults | msa_shared/constants/plot_defaults.py | `FWHM_IR=12.0` (cm⁻¹, Lorentzian), `FWHM_UVVIS_NM=20.0` (nm, Gaussian) |
| axis windows | msa_shared/constants/plot_defaults.py | `IR_XLIM=(4000,400)`, `XANES_ENERGY_RANGE`, `NMR_XLIM_1H` |
| signal thresholds + fixed messages | msa_shared/constants/plot_defaults.py | `OSCILLATOR_STRENGTH_THRESHOLD=1e-6`, `SIGNAL_EPS=1e-12`, `NO_SIGNAL_MESSAGE` |
| CSV precision | msa_shared/constants/plot_defaults.py | `X_DECIMALS`, `Y_DECIMALS=6` |
| broadening kernels | msa_shared/broadening.py | area-normalized Lorentzian / peak-height Gaussian |
| TMS reference shielding | msa_shared/constants/nmr_ref.py | indexed by `(system_type, shielding_basis)` |
| ORCA failure classification | msa_shared/orca_diagnostics.py | A1/A2/A3/B/C/D terminal semantics |
| ORCA input control syntax | msa_shared/orca_input.py | `%geom`, `%scf Thresh` rendering |
| fallback chemistry recipes | skills/param-recommendation/scripts/_functional_basis.py (skill-local) | ordered per-spectrum/system replacements |
| SLURM walltime/cores | msa_shared/constants/slurm.py | `DEFAULT_NPROCS=16` |
| power-law fit coeffs | skills/time-estimation/scripts/_timing.py (skill-local) | `FITTED_POWER_LAWS`, `BASE_INTERCEPT=0.518` |
| plot style | msa_shared/spectrum_style.py | `FIGSIZE=(8,4.5)`, `DPI=300` |

**Single-consumer vs shared.** A constant read by exactly one skill lives *with
that skill* as a `_`-prefixed local module (`_functional_basis.py`,
`_timing.py`, and the batch-orchestrator's `_orchestration.py`), so the skill
carries its own value and stays self-contained. Constants shared by two or more
skills stay in `msa_shared/constants/` and are vendored into each consumer.

### 4.2 Versioning

- `msa_shared/VERSION` is the **compatibility gate**, not a content revision
  counter: every skill hard-checks `VERSION == MSA_CONTRACT_VERSION` and exits 2
  on mismatch, so bumping it breaks all 20 skills at once. It moves only on a
  breaking contract change (§6).
- Editing a constant therefore does **not** bump VERSION. Within v2.x the
  change must be additive or a pure re-baseline of a value (never a rename or
  type change), and its rationale must be documented at the owning definition
  or governing contract section.
- After any edit to canonical `msa_shared/`, run
  `python3 tools/vendor_msa_shared.py sync`; `... check` is the drift guard.
- On startup a skill prints the msa_shared version to stderr for debugging.

---

## 5. Shared-module location and reference

### 5.1 Layout

```
molecular-spectroscopy-agent/          # dev-time repo root (not needed at runtime)
├── msa_shared/                         # dev-time single source of truth
│   ├── __init__.py
│   ├── VERSION
│   ├── PIPELINE.md                     # this file
│   ├── spectrum_style.py               # figure style + render/save
│   ├── broadening.py                   # Lorentzian / Gaussian kernels
│   ├── orca_input.py                   # .inp construction
│   ├── orca_output.py                  # .out parsing -> build_spectrum
│   ├── common_utils.py
│   ├── naming.py
│   └── constants/
│       ├── __init__.py
│       ├── plot_defaults.py
│       ├── nmr_ref.py
│       ├── slurm.py
│       └── solvents.py
├── skills/                             # the 20 runtime skills
│   └── <skill>/scripts/
│       ├── <entry>.py                  # self-locates this scripts/ dir
│       └── msa_shared/                 # vendored, byte-identical copy of what it imports
├── tools/vendor_msa_shared.py          # sync (refresh copies) + check (drift guard)
├── meta/msa-skill-spec/                # the skill-generation meta-skill
└── tests/
```

### 5.2 How a skill references the shared library

Every skill's `scripts/*.py` begins with the self-locating bootstrap. The script
puts its own `scripts/` directory on `sys.path`, then imports from the vendored
`scripts/msa_shared/` copy sitting beside it — no env var, no absolute paths,
no dependency on the repository being present at runtime:

```python
import os, sys
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from msa_shared.common_utils import (
    load_task_list, dump_task_list,
    mark_success, mark_failed, mark_skipped, mark_no_signal, add_warning,
    upstream_ok, spectrum_kwargs,
    log_startup, read_shared_version, ensure_deps,
)
from msa_shared import naming
from msa_shared import orca_output                    # plot / export skills
import msa_shared.spectrum_style as spectrum_style    # plot skills only
```

A plot skill imports **no** FWHM / axis constant of its own: it calls
`orca_output.build_spectrum(SPEC, text, **spectrum_kwargs(task))` and the
builder reads `constants/plot_defaults.py`. That is what keeps a PNG and its CSV
describing the same curve.

Each skill therefore ships its own byte-identical copy of exactly the
`msa_shared` modules it imports, vendored under `scripts/msa_shared/`. The
repo-level `msa_shared/` is the dev-time single source of truth: after editing
it, run `python3 tools/vendor_msa_shared.py sync` to refresh every skill's copy,
and `python3 tools/vendor_msa_shared.py check` (a sha256 drift guard) to fail CI
if any vendored copy diverges. Single-consumer helpers are not vendored through
`msa_shared` at all — they live directly in the owning skill's `scripts/` as
underscore-prefixed modules (`_functional_basis.py`, `_timing.py`,
`_orchestration.py`).

---

## 6. Contract version management

- This is contract v2.0 (a breaking release from v1.x — new 3-class system_type
  with charge/multiplicity, orthogonal ZORA, the XANES three-step/two-submission
  flow, and the metadata-headed export CSV format).
- v2.x may only add new fields/constants; it must not delete or change types.
- The 2026-08-10 expert re-baseline (per-spec broadening kernels, the
  `{stage}_signal="none"` encoding of §2.5, `{stage}_warnings`, English figure
  titles via `molecule_name_en`, variant-tagged filenames) is **additive inside
  contract 2.0**: it adds optional fields and re-baselines constant values, so
  `VERSION` stays `2.0` and every skill keeps `msa_contract_version: 2.0`;
  the governing field semantics are specified in §1.1–§3 and the owning
  constants and modules are listed in §4.1.
- The 2026-09-08 manifest entry point (a second P1-1 entry script, plus the new
  `smiles_source` value `manifest`) is likewise **additive inside contract 2.0**:
  it adds no task field, renames none and retypes none — it adds an *input shape*
  and one value to an existing field's domain — so `VERSION` stays `2.0`.
- The 2026-09-08 expert computational-parameter closure adds optional ordered
  fallback and attempt-audit fields, centralized failure semantics, a diagnostic
  manifest comparison report, and re-baselines artifact separators and expert
  constants. These are additive fields or value/protocol re-baselines inside
  contract 2.0, so `VERSION` remains `2.0`.
- A further breaking change requires a v3.0 bump and a full migration document.
- Each skill's SKILL.md frontmatter carries `msa_contract_version: 2.0`; at
  runtime the script compares it against `msa_shared/VERSION` and exits on
  mismatch.

---

## 7. Open decisions

- Whether all 20 skills are implemented against this v1.0 in one pass; Stage 1
  only wires the IR path end-to-end.
- **RESOLVED (Stage 1):** `submit_slurm` uses the **scp-transfer + slurm-remote
  combination** over paramiko (SSH/SFTP), not a monolithic direct-SSH transport.
  The three transport primitives live in `common_utils`
  (`submit_slurm` = SFTP upload + `sbatch`, `poll_slurm` = `squeue` poll,
  `download_results` = SFTP get). The cluster profile (host / user / password or
  key_file / remote_root / orca_bin / mpi_prefix, plus optional partition / qos)
  ships **no** coordinates or credentials in the code; it is resolved at runtime
  by `common_utils.resolve_cluster` with precedence **explicit programmatic
  kwargs > `HPC_*` env vars**. Credentials are **never read from disk and never
  auto-discovered**: there is no `--cluster-config` flag, no `$MSA_CLUSTER_CONFIG`,
  and no fallback to a conventional path such as `~/.config/msa/cluster.json`, so
  leaving a config file lying around has no effect on any task. The host Agent
  must ask the user for each task's cluster coordinates and pass them as inline
  `HPC_*` env vars on the single run command. Missing required fields raise an
  actionable `ClusterConfigError`. `constants/slurm.py` retains only
  cluster-agnostic SLURM sizing defaults (`DEFAULT_NPROCS` / `DEFAULT_WALLTIME` /
  partition / qos).
- Whether structure visualization is 2D (RDKit Draw + Cairo) or 3D; default 2D,
  extend separately if 3D is needed.
- Whether the xanes/nmr multi-step energy windows / reference shielding tensors
  live inside `params.step1/step2`; decided at Stage 2.

---

## 8. Relationship to reference assets

- Legacy skills (if any) are kept only as reference material and are never
  imported. The runtime pipeline depends solely on `msa_shared`.
- New skills may reuse legacy code logic by copying it into their own `scripts/`,
  but must not import legacy skill paths.
- Domain data extracted from the method documents (SMILES dictionaries, solvent
  maps, SLURM templates) is centralized under `msa_shared/constants/` and the
  skill dictionaries; new skills always use the shared copy.
