---
name: msa-skill-spec
description: Skill-generation / revision meta-skill for the Molecular Spectroscopy Agent (MSA). When the user asks to create, modify, generate, or audit any MSA skill (task-recognition / name-to-smiles / smiles-to-xyz / structure-plot / param-recommendation / time-estimation / batch-orchestrator / orca-* / plot-* / export-spectrum-data), or asks to "generate a skill from the contract", "build an MSA skill", "create a new MSA skill", or "build a skill per PIPELINE", the Agent MUST load this meta-skill BEFORE writing anything. This meta-skill encodes the naming rules, directory layout, task field-lifecycle contract, shared-module reference convention, failure contract, Python script skeleton, and pre-delivery verification checklist for every MSA skill. DO NOT hand-write SKILL.md, DO NOT invent task field names, DO NOT hard-code default values.
version: 2.3.0
msa_contract_version: 2.0
---

# MSA Skill Generator — Molecular Spectroscopy Agent skill meta-skill

## What this skill does

This meta-skill is the **development constrainer** for the Molecular Spectroscopy
Agent (MSA) project; it is not a runtime component. It reads the contract at
`msa_shared/PIPELINE.md` (repo-relative; this meta-skill runs at dev time inside
the repo) and turns that contract into an executable workflow for "generate a new
skill" or "audit an existing skill".

Golden rule: enable the Agent to chain the entire pipeline end-to-end with the
highest possible success rate. Every generation decision serves that goal;
code elegance and conservative naming may be sacrificed for it.

## Path decoupling

At runtime there is **no repository and no shared install path** — a host Agent loads each
skill on its own. Every generated skill is therefore self-contained: it vendors a
byte-identical copy of exactly the `msa_shared` modules it imports under its own
`scripts/msa_shared/`, and the entry script self-locates that directory on
`sys.path`:

```
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
```

At dev time the repo-level `msa_shared/` is the single source of truth. After
generating or editing a skill, run `python3 tools/vendor_msa_shared.py sync` to
vendor the shared modules it imports, and `... check` as a sha256 drift guard.
Nowhere in this meta-skill, its scripts, or any generated skill is an absolute
machine path or environment-variable dependency baked in.

## Agent Behavior

**CRITICAL: Always interact in the same language the user is using.** When the
user writes in Chinese, reply in Chinese; when the user writes in English, reply
in English. Every user-facing confirmation or report mirrors the input language.

Rules block (must appear verbatim in every generated SKILL.md):

- Defaults: DO NOT pass parameters; let the skill read them automatically from its vendored `msa_shared/constants/`.
- Overrides: ONLY pass parameters the user explicitly specified.
- NEVER silently override: any change to a default must be confirmed with the user first.

## Workflow overview

```
Phase 1: Read the contract + method docs
  └─ msa_shared/PIPELINE.md (in full)
  └─ (optional) the matching method .docx supplied by the user

Phase 2: Classify the skill + locate its task field append-set
  └─ one of the five classes A/B/C/D/S
  └─ pull that stage's append fields from PIPELINE.md §1.1

Phase 3: Generate SKILL.md
  └─ copy from the matching template
  └─ fill frontmatter (version + msa_contract_version)
  └─ embed the Task Field Contract block

Phase 4: Generate scripts/*.py
  └─ mandatory self-locating bootstrap block (version 2.0, ensure_deps import)
  └─ declare MSA_REQUIRES (C-extension deps) + call ensure_deps in main()
  └─ B-class (orca-*) resolve the cluster profile (inline HPC_* env vars only)
  └─ load/dump task_list only
  └─ mark_success/failed/skipped only
  └─ stdout = JSON, stderr = log

Phase 5: Four-tier verification
  └─ 5.1 static lint (validate_skill.py, automated)
  └─ 5.2 smoke test (--help never errors, empty batch runs clean)
  └─ 5.3 end-to-end dry-run (the "benzene IR" golden case)
  └─ 5.4 cross-skill interface consistency (upstream/downstream fields align)
```

The user only needs to confirm one thing: **the skill name + its stage**.
Everything else is taken from PIPELINE.md and `msa_shared/constants/`; never
invent values.

---

## Phase 1: contract and document extraction checklist

Required files (the Agent MUST Read them in full, not from memory):

| File | Purpose |
|---|---|
| `msa_shared/PIPELINE.md` | Main contract: field-lifecycle table + naming table + failure contract |
| `msa_shared/VERSION` | Current msa_contract_version |
| `msa_shared/naming.py` | The file-path function inventory |
| `msa_shared/common_utils.py` | task IO / status helper inventory |
| `msa_shared/constants/*.py` | The whitelist of constants a skill may import |

Optional reference documents (read only if the user attaches the method docs):
the MSA method documents (Part 1 preprocessing, Part 2 ORCA calculation, Part 3
plotting/export). If the user supplies no reference document, skip the optional
part of Phase 1 and fill gaps from PIPELINE.md plus domain common sense.

---

## Phase 2: skill classification

Every MSA skill falls into one of five classes; templates and rules differ:

| Class | Examples | Part | Key difference |
|---|---|---|---|
| A — preprocessing | task-recognition, name-to-smiles, smiles-to-xyz, structure-plot, param-recommendation, time-estimation | Part 1 | Pure local Python, no SLURM; task_list in, task_list out |
| B — ORCA calc | orca-ir, orca-raman, orca-uvvis, orca-nmr, orca-fluo, orca-xanes | Part 2 | Needs SLURM remote submit; calls common_utils.submit_slurm |
| C — plotting | plot-ir, plot-raman, plot-uvvis, plot-nmr, plot-fluo, plot-xanes | Part 3 | Pure local matplotlib; import spectrum_style; output PNG+PDF |
| D — data export | export-spectrum-data | Part 3 | Extract peak table + curve CSV from the ORCA .out |
| S — special | batch-orchestrator | Part 1 | Orchestration layer; appends no fields of its own, only schedules |

### Naming convention

- Skill directory name = the frontmatter `name` field, lowercase-hyphenated.
- The stage ID (used as the status-field prefix) matches the last segment of the
  skill name:
  - `name-to-smiles` → stage=`smiles` (fields `smiles_status`, `smiles_error`)
  - `smiles-to-xyz` → stage=`xyz`
  - `structure-plot` → stage=`structure`
  - `param-recommendation` → stage=`params`
  - `time-estimation` → stage=`time_estimate`
  - `orca-ir` → stage=`calc` (all orca-* share the `calc_*` prefix, disambiguated by `target_spectra`)
  - `plot-ir` → stage=`plot`
  - `export-spectrum-data` → stage=`export`

A stage ID, once fixed, must never change.

### Multi-entry skills

A skill normally has one CLI entry script. It may have **more than one** when the
same stage genuinely accepts more than one *shape* of input — today only
`task-recognition`, whose stage can be driven either by a user's natural language
(`scripts/task_recognition.py`) or by a curated manifest CSV
(`scripts/manifest_to_tasks.py`). See PIPELINE.md §0 ("entry point").

All entry scripts of one skill share the same `STAGE_ID`, the same append-field
whitelist, and the same directory; adding an entry never adds a stage. What must
not happen is two entries drifting apart — see §4.6 and ironclad rule 36.

---

## Phase 3: SKILL.md generation

Templates live in `templates/{preprocessing_skill,orca_skill,plot_skill,export_skill}/SKILL.md.tmpl`.
A **second** entry script of an existing skill needs no template change: the
templates describe a skill's shape (frontmatter + required sections), and an extra
entry only adds a subsection under `## Scripts`.
Placeholder conventions:

- `{{SKILL_NAME}}` — lowercase-hyphenated skill name
- `{{STAGE_ID}}` — status-field prefix (see Phase 2)
- `{{SPEC}}` — spectrum type (B/C classes only; one of `ir raman uvvis nmr fluo xanes`)
- `{{APPEND_FIELDS}}` — the fields this skill may append (JSON snippet copied from PIPELINE.md §1.1)
- `{{UPSTREAM_STAGE}}` — upstream stage ID
- `{{DESCRIPTION_SENTENCE}}` — the first sentence of the description

### Frontmatter hard requirements

Every generated SKILL.md must contain:

```yaml
---
name: {{SKILL_NAME}}
description: <sentence 1: object + action + input + output>. <sentence 2: trigger keywords>. When the user ..., the Agent MUST load this skill BEFORE .... DO NOT ....
version: 0.1.0
msa_contract_version: 2.0
---
```

- `version` follows semver; first generation is `0.1.0`.
- `msa_contract_version` must equal `msa_shared/VERSION`; a runtime mismatch is a
  hard error and the script exits.

### Task Field Contract block

Every SKILL.md body must contain the following block, copied from the matching
row of PIPELINE.md §1.1:

```markdown
## Task Field Contract

This skill sits at the **{{STAGE_NAME}}** stage of the pipeline. The authoritative
field schema is **PIPELINE.md §1.1**; the rows below are this skill's slice of it.

**Consumes (from {{UPSTREAM_STAGE}}, per PIPELINE.md §1.1):**
- `field_a`: type, meaning
- `field_b`: ...

**Appends (written by this skill, per PIPELINE.md §1.1):**
- `{{STAGE_ID}}_status`: "success" | "failed" | "skipped"
- `{{STAGE_ID}}_error`: string (present only when failed)
- other fields ...

**Forbidden**: rewriting any upstream field; appending fields outside the
whitelist; modifying task_id.
```

This is the single most important mandatory block of msa-skill-spec. The audit
tool (validate_skill.py) regex-checks that every SKILL.md contains this section.
For the current contract, P1-5 must document the optional ordered
`params_fallbacks`; every P2 orca-* skill must document optional
`calc_attempts`, `calc_fallback_applied`, `calc_basis_original`,
`calc_basis_final`, `calc_basis_fallback`, `calc_fallback_steps`,
`calc_fallback_reason`, and `calc_scf_thresh`. Their exact authority and
optionality still come from PIPELINE.md §1.1 rather than this explanatory list.

### Required sections checklist

A generated SKILL.md body must contain at minimum:

1. `## What this skill does` (first sentence = frontmatter description sentence 1)
2. `## Agent Behavior` (language mirror + three-line Rules block + task_list source rule)
3. `## Task Field Contract` (see above)
4. `## Scripts` (CLI signature + argument table + example)
5. `## Workflow` (steps 1..N)
6. `## Integration` (6–8 step upstream/downstream list; mark the current skill `(this skill)`)
7. `## Troubleshooting` (common error → diagnosis)

---

## Phase 4: Python script generation

### 4.1 Self-locating bootstrap block (mandatory at the top of every scripts/*.py)

```python
#!/usr/bin/env python3
"""{{SKILL_NAME}} — {{DESCRIPTION_SENTENCE}}"""
import os, sys, json, argparse

# ---- msa_shared bootstrap (DO NOT MODIFY) ----
# The skill vendors a byte-identical copy of msa_shared under its own scripts/
# dir; self-locate that dir on sys.path. No env var, no absolute paths.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from msa_shared.common_utils import (
    load_task_list, dump_task_list,
    mark_success, mark_failed, mark_skipped, upstream_ok,
    log_startup, read_shared_version, ensure_deps,
)
from msa_shared import naming

MSA_CONTRACT_VERSION = "2.0"
# C-extension deps cannot be vendored — declare them so main() can preflight
# (hard-miss -> exit 4). Empty for a pure-stdlib skill. Each entry is
# (import_name, pip_spec[, "soft"]); "soft" degrades instead of gating.
MSA_REQUIRES = []
STAGE_ID = "{{STAGE_ID}}"
UPSTREAM_STAGE = "{{UPSTREAM_STAGE}}"
# ---- end bootstrap ----
```

### 4.2 Entry-function template

```python
def process_task(task: dict) -> dict:
    """Process a single task; never raises."""
    if not upstream_ok(task, UPSTREAM_STAGE):
        return mark_skipped(task, STAGE_ID, UPSTREAM_STAGE)
    try:
        # ... business logic ...
        result_fields = do_work(task)
        return mark_success(task, STAGE_ID, **result_fields)
    except Exception as e:
        return mark_failed(task, STAGE_ID, str(e))
```

A **C-class artifact skill** (plot-* / export-spectrum-data) uses the
four-outcome shape instead — success / success-with-no-signal / failed / skipped
(rules 31–32). The worker returns the builder's verdict in three `_`-prefixed
keys that `process_task` pops before they can reach the task:

```python
def process_task(task: dict) -> dict:
    """Single-task orchestrator. Never raises; enforces the fail-soft contract."""
    if task.get("target_spectra") != SPEC:   # plot-* only (rule 28: pass-through)
        return task
    if not upstream_ok(task, UPSTREAM_STAGE):
        return mark_skipped(task, STAGE_ID, UPSTREAM_STAGE)
    try:
        fields = do_work(task)   # {"_has_signal","_message","_warnings", ...artifacts}
    except Exception as e:  # noqa: BLE001 — fail soft, never abort the batch
        return mark_failed(task, STAGE_ID, f"{type(e).__name__}: {e}")

    has_signal = fields.pop("_has_signal", True)
    message = fields.pop("_message", "")
    warnings = fields.pop("_warnings", [])
    if has_signal:
        mark_success(task, STAGE_ID, **fields)
    else:
        mark_no_signal(task, STAGE_ID, message, **fields)  # artifact fields are ""
    for w in warnings:
        add_warning(task, STAGE_ID, w)
    return task
```

```python
def main():
    parser = argparse.ArgumentParser(description="{{SKILL_NAME}}")
    parser.add_argument("--input", required=True, help="input task_list.json path")
    parser.add_argument("--output", required=True, help="output task_list.json path")
    args = parser.parse_args()

    log_startup("{{SKILL_NAME}}")
    ensure_deps(MSA_REQUIRES, "{{SKILL_NAME}}")  # hard-miss -> exit 4
    shared_v = read_shared_version()
    if shared_v != MSA_CONTRACT_VERSION:
        print(f"ERROR: msa_contract_version mismatch (skill={MSA_CONTRACT_VERSION}, shared={shared_v})",
              file=sys.stderr)
        sys.exit(2)

    tasks = load_task_list(args.input)
    tasks = [process_task(t) for t in tasks]
    dump_task_list(tasks, args.output)
    print(f"SUCCESS: wrote {len(tasks)} tasks to {args.output}", file=sys.stderr)

if __name__ == "__main__":
    main()
```

### 4.3 IO hard rules

- **stdout**: emit JSON only when the skill is explicitly designed as a pipe
  stream (rare). By default no normal path prints to stdout.
- **stderr**: all logs, progress, and SUCCESS/ERROR/WARNING prefix lines go here.
- **task read/write**: only call `load_task_list` / `dump_task_list`; never bare
  `json.load(open(...))`.
- **file paths**: only use `naming.xxx(molecule_name, ...)`; never concatenate
  strings.
- **defaults**: import from `msa_shared.constants.*` only; never define
  `DEFAULT_*` constants inside a skill.
- **lazy import of heavy libs**: heavy libraries (rdkit / xgboost / paramiko /
  matplotlib and beyond) must be imported inside function bodies so `--help`
  works on a machine without those dependencies.
- **matplotlib**: plotting skills must `import msa_shared.spectrum_style`
  (whose side effect sets rcParams + the Agg backend); never define a custom
  style.

### 4.4 Dependency preflight and exit codes

C-extension packages (rdkit / numpy / matplotlib / paramiko / pubchempy …)
**cannot be vendored** the way pure-Python `msa_shared` modules are. Each skill
therefore declares what it needs and preflights it:

- Declare a module-scope `MSA_REQUIRES` list of `(import_name, pip_spec[, "soft"])`
  tuples (empty for a pure-stdlib skill).
- `main()` calls `ensure_deps(MSA_REQUIRES, "<skill>")` right after `log_startup`
  and before the version check.
- A **hard** miss prints `MSA_ENV_MISSING: <mod> -- pip install "<spec>"`, then a
  single consolidated `pip install` line, and exits **4**.
- A **soft** miss prints `MSA_ENV_SOFT_MISSING: <mod>` and degrades (the skill
  keeps running with reduced capability); `ensure_deps` returns the set of
  soft-missing modules so the skill can branch on it.
- `MSA_AUTO_INSTALL=1` makes `ensure_deps` `pip install` the missing package
  itself before re-checking.
- `--help` short-circuits inside argparse before `ensure_deps` runs, so help
  works on a machine without the deps.

Class-typical `MSA_REQUIRES`: preprocessing = `[]` (add rdkit / pubchempy-soft
per skill); orca-* = `[('paramiko', 'paramiko>=3.0')]`; plot-* =
`[('numpy', 'numpy>=1.24'), ('matplotlib', 'matplotlib>=3.7')]`;
export = `[('numpy', 'numpy>=1.24')]`.

Canonical exit codes (every skill CLI):

| Code | Meaning |
|---|---|
| 0 | batch processed (per-task failures are reported inside the task_list, not via exit code) |
| 1 | could not load the input task_list (bad path / malformed JSON) |
| 2 | `msa_contract_version` mismatch between the skill and `msa_shared/VERSION` |
| 3 | `ClusterConfigError` — required cluster fields missing (orca-* only) |
| 4 | a hard `MSA_REQUIRES` dependency is not importable |

### 4.5 B-class (orca-*) cluster resolution

An orca-* skill ships **no** cluster coordinates or credentials. Its `main()`
resolves the profile after loading the task_list — with no extra argument, since
there is no config-file channel:

```python
from msa_shared.common_utils import resolve_cluster, ClusterConfigError
try:
    cluster = resolve_cluster()
except ClusterConfigError as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(3)

tasks = [process_task(t, cluster) for t in tasks]
```

`resolve_cluster` precedence is **explicit programmatic kwargs > `HPC_*` env
vars**. Credentials are **never read from disk and never auto-discovered** —
there is no `--cluster-config` flag, no `$MSA_CLUSTER_CONFIG`, and no fallback to
a conventional path such as `~/.config/msa/cluster.json`, so a stale config file
can never be picked up by a later task. The host Agent asks the user for each
task's cluster coordinates and passes them as inline `HPC_*` env vars on the one
run command.
`process_task(task, cluster)` takes the resolved profile as a second argument.

Spec routing for B- and C-class skills is a **pass-through, not a skip**: a task
whose `target_spectra` does not match the skill's `SPEC` is returned untouched
(no `{stage}_status` written) so the six sibling orca-{spec} / plot-{spec} skills
can run over the same batch without clobbering the shared status field.
`mark_skipped` is reserved for the real case — a matching-spec task whose upstream
stage did not succeed.

### 4.6 Multi-entry skills: share the constructor, never restate it

When a skill has a second entry script (Phase 2, "Multi-entry skills"), that
script **imports the task-construction primitives from the primary entry** and
implements only its own input parsing plus its own input-specific filtering:

```python
from task_recognition import (
    expand_task_list, assign_task_ids, validate_tasks, finalize_batch,
    new_parse_batch_id, new_run_id,
)
```

This is an import **within one skill's own `scripts/` directory**, which ironclad
rule 13 permits — that rule forbids reaching into *another* skill.

A second entry must NOT restate the task dict literal, the `task_id` format, the
global numbering, the `run_id` stamping, the status stamping, or the batch wrapper.
Restating them makes the two entries agree only on the day they are written: the
next edit to either one silently produces two dialects of the same stage, and no
downstream skill can tell it is looking at a second dialect. Sharing the
constructor makes the equivalence of rule 36 structural instead of aspirational.

Extracting those primitives from the primary entry is a **behaviour-preserving**
refactor: the primary entry's output must stay byte-identical, and the order of
operations must be preserved even where it looks accidental (in `task-recognition`,
global numbering runs *before* `validate_tasks` drops a task, so dropped tasks
leave gaps in the numbering — that is existing behaviour and the extraction must
not "fix" it).

### 4.7 Bounded fallback and centralized diagnostics

An orca-* entry receives one baseline `params` object plus an optional ordered
`params_fallbacks` list from P1-5. It builds each submission from exactly one of
those recipes, classifies the returned output through
`msa_shared.orca_diagnostics.classify_orca_output`, and appends one real
`calc_attempts` record. It may consume the next recipe only when that classifier
returns a retryable A-class result. Business scripts do not contain copies of
ORCA terminal strings and do not derive replacement basis sets, SCF thresholds,
or retry counts.

The summary fields (`calc_fallback_applied`, basis original/final/fallback,
fallback steps/reason, and SCF threshold) are derived from the append-only attempt
history. They are never used to fabricate a history after the fact. All job paths,
including XANES optsp variants, come from `naming.*`.

---

## Ironclad rules

Numbers are fixed once; new lessons may only be appended, never renumbered.

### Description and language

1. **description rule**: every generated SKILL.md description must contain "Agent
   MUST load this skill BEFORE", at least one explicit trigger keyword/scenario,
   and at least one "DO NOT".
2. **language-mirror rule**: always mirror the user's input language. Chinese in,
   Chinese out; English in, English out.
3. **three-line Rules block** (must appear verbatim in every SKILL.md Agent
   Behavior section):
   - Defaults: DO NOT pass parameters; let the skill read from `msa_shared/constants/`.
   - Overrides: ONLY pass parameters the user explicitly specified.
   - NEVER silently override: any default change must be confirmed with the user first.

### Directory and naming

4. **skill directory name = frontmatter.name**, and must be lowercase-hyphenated.
5. **filenames must go through naming.py**: scripts must never contain
   `f"{mol}_..."` or `os.path.join(..., mol + "_...")` filename concatenation.
6. **slugify rule**: any file IO involving CJK/space molecule names must first go
   through `naming._safe_slug()` (already wrapped by the naming module — just call
   `naming.xyz_initial(name)`).

### task contract

7. **task append-field whitelist**: a skill may append only the fields listed in
   its row of PIPELINE.md §1.1 — not one more.
8. **upstream fields are immutable**: rewriting one is an immediate failure.
9. **fail-soft rule**: when a single task errors, never raise and abort the batch.
   Write `{stage}_status="failed"` + `{stage}_error=<one-line>` then return, so
   downstream can skip.
10. **task_id is generated by task-recognition**; downstream skills must not
    rewrite it.

### shared module

11. **the self-locating bootstrap block must appear verbatim at the top of every
    scripts/*.py** (see §4.1); you may add lines but never remove them or change
    the resolution mechanism.
12. **no `DEFAULT_*` constants inside a skill**. All defaults import from
    `msa_shared.constants.*`.
13. **no cross-skill import**: code in one skill's scripts/ must not import
    another skill's module. Shared logic lives in the vendored msa_shared; a
    module consumed by only one skill lives in that skill's scripts/ as an
    underscore-prefixed local (e.g. `_functional_basis.py`), never as a
    cross-skill import.

### IO

14. **stdout = structured JSON only** (off by default unless an explicit pipe
    stream is required).
15. **stderr message prefixes**: `SUCCESS: | ERROR: | WARNING:` — all caps, colon,
    space.
16. **shebang** is always `#!/usr/bin/env python3`.
17. **argparse help** is always `f"... (default: {DEFAULT_X})"` (`DEFAULT_X`
    imported from constants may be used in the help string).

### calculation and plotting

18. **matplotlib**: a plotting skill must `import msa_shared.spectrum_style` as its
    first style action; never call `matplotlib.use(...)` or
    `plt.rcParams.update(...)` directly.
19. **lazy import of heavy libs**: paramiko / rdkit / xgboost / mendeleev /
    mammoth and similar heavy libs must be imported inside function bodies.
20. **ORCA input generation**: an orca-* skill's `.inp` must be assembled from a
    template; the functional/basis/ZORA/RIJCOSX/AutoAux keys must be read from
    `task["params"][step]`, never hard-coded.
21. **CPCM solvent map**: take it only from `msa_shared.constants.solvents`; never
    define a local dictionary inside a skill.

### version

22. **frontmatter must contain `version` and `msa_contract_version`**. At runtime
    the script compares `msa_shared/VERSION` against its own `MSA_CONTRACT_VERSION`
    constant and exits 2 on mismatch.
23. **a breaking msa_contract_version bump requires a full migration document**
    and updates `msa_shared/VERSION`, the frontmatter of msa-skill-spec plus
    every downstream skill, and each script's `MSA_CONTRACT_VERSION` constant.

### orchestration

24. **batch-orchestrator appends no fields of its own** (it only schedules +
    rolls up status); it is the sole exception.
25. **every skill CLI must support `--input` / `--output` task_list JSON path
    arguments**, and defaults to exit code 0 (except on contract-version mismatch
    or a fatal error such as failed JSON load).

### dependency and cluster (v2.0)

26. **dependency preflight is mandatory**: every skill declares a module-scope
    `MSA_REQUIRES` list and `main()` calls `ensure_deps(MSA_REQUIRES, "<skill>")`
    after `log_startup` and before the version check. A hard-missing C-extension
    dependency exits 4; a soft one degrades. Pure-stdlib skills still declare
    `MSA_REQUIRES = []`.
27. **orca-* never ships credentials, and never reads them from disk**: a B-class
    skill resolves its cluster profile via `resolve_cluster()` (no argument) and
    exits 3 on `ClusterConfigError`; host / user / password / paths are never
    hard-coded and never committed. There is **no config-file channel of any
    kind** — no `--cluster-config` flag, no `$MSA_CLUSTER_CONFIG`, and no fallback
    to a conventional location like `~/.config/msa/cluster.json`. Every task
    obtains its cluster coordinates freshly from the user and receives them as
    inline `HPC_*` env vars on the single run command; a file a previous task left
    on disk is never reused. `ClusterConfigError` is the signal to ask the user,
    not to hunt for a cached credential file.
28. **spec routing is pass-through, not skip**: in a B- or C-class skill a task
    whose `target_spectra != SPEC` is returned untouched (no `{stage}_status`
    written); `mark_skipped` is reserved for a matching-spec task whose upstream
    stage failed. This keeps the six sibling orca-{spec} / plot-{spec} skills from
    clobbering each other's shared status field over one batch.
29. **run isolation is automatic — never bypass it**: task-recognition stamps a
    unique `run_id` (PIPELINE.md §1.4) on every task, and `common_utils.load_task_list`
    activates it via `naming.set_run_id`, so every `naming.*` path nests under
    `.../<run_id>/<slug>/…` locally and on the shared HPC `remote_root`. This is
    what lets concurrent runs of the same molecule/spectrum coexist without
    clobbering each other. A generated skill therefore MUST route all file IO
    through `naming.*` (never hand-built paths — see rule 5) and MUST NOT rewrite
    `run_id` (it is an upstream field — see rule 8).
30. **invoke skills only via their CLI entry scripts — never bypass** (PIPELINE.md
    §3.5): the Agent MUST call `python3 scripts/run_<skill>.py --input ... --output
    ...` for every stage. It MUST NOT import `msa_shared` functions in an ad-hoc
    script, write a temp file that calls `paramiko` / `orca_input.build_inp`
    directly, fabricate any `{stage}_status` field by hand, or construct a
    task_list from scratch without running `task-recognition`. A fabricated status
    hides the absence of that stage's artefacts and guarantees downstream crashes;
    a hand-assembled task_list lacks `recognition_status`, `run_id`, and the
    three-layer name resolution — downstream will skip or error on every task.
    If a stage's status is missing, (re-)run that stage; if a stage fails, debug
    its stderr and retry with corrected inputs.

### signal, warnings and display (v2.1 re-baseline)

31. **"no valid signal" is a success, never a failure and never a fourth status**
    (PIPELINE.md §2.5): a converged run with nothing to draw — all frequencies
    imaginary, a dark S1, every oscillator strength below
    `OSCILLATOR_STRENGTH_THRESHOLD` — is recorded with
    `common_utils.mark_no_signal(task, STAGE, message, plot_png="", plot_pdf="")`,
    i.e. `{stage}_status="success"` + `{stage}_signal="none"` +
    `{stage}_message`. No artifact file is written and the artifact fields are
    set to `""` (not omitted). Inventing a `no_signal` *status* is forbidden:
    every downstream gate is `upstream_ok` (== "success"), so it would silently
    skip export, which must still write the raw peaks table. The message text is
    the expert-fixed `constants/plot_defaults.NO_SIGNAL_MESSAGE[spec]` — reuse it
    verbatim, never paraphrase, never build the sentence in a skill.
32. **warnings never change status**: a non-fatal diagnostic (imaginary
    frequencies being the main case) goes to `{stage}_warnings` via
    `common_utils.add_warning`, which deduplicates and preserves order. A skill
    replays `spectrum["warnings"]` from the builder rather than composing its own
    text, so the figure, the task_list and the CSV metadata header can never
    disagree. Never downgrade a warning to a failure, or hide it in the message.
33. **the on-figure molecule name comes from `naming.title_name(task)`** — never
    `task["molecule_name"]` directly, and never a locally written
    `.capitalize()` / `name[0].upper()`. Capitalization of a chemical name is
    `naming.display_name` (`p-nitrophenol` → `p-Nitrophenol`), which lives in
    `naming` so name-to-smiles can produce `molecule_name_en` without importing
    matplotlib. A plot skill calls `spectrum_style.set_title(ax, spectrum,
    naming.title_name(task))`; a figure with no title is a defect.
34. **variant-tagged filenames: `spec_variant` for artifacts, `job_variant` for
    job files** (PIPELINE.md §3.2.1). Figures and CSVs take
    `variant=naming.spec_variant(task)` (NMR nucleus, XANES element); `.inp` /
    `.out` / `.sh` take `variant=naming.job_variant(task)`, which is `None` for
    NMR precisely because one merged shielding job serves every nucleus. Passing
    `spec_variant` to a job-file builder, or omitting the variant entirely, makes
    two tasks of the same molecule overwrite each other's output.
35. **builder arguments go through `common_utils.spectrum_kwargs(task)`** — a
    skill must not hand-map task fields into `orca_output.build_spectrum`
    (`target_nucleus=task["extra_params"].get(...)` and friends). One mapping
    means the plot skills and the export skill can never disagree about where a
    value lives, and the XANES edge used for the energy window is by construction
    the same one that goes into the filename.

### multi-entry equivalence (v2.2)

36. **all entry points of one stage must be equivalent, by construction**
    (PIPELINE.md §0): for the same molecule and spectrum, every entry script of a
    skill emits a **field-for-field identical task** — same keys, same order, same
    spellings — apart from `run_id` (per batch) and `task_id` (per position). The
    equivalence must come from the second entry *calling* the primary entry's task
    constructor (§4.6), not from copying it; a second entry that restates the task
    dict is a defect even while its output happens to match. Each entry script gets
    its own subsection under `## Scripts` in SKILL.md, and the skill's test suite
    must contain a case that **runs both entries on the same molecule and diffs the
    tasks field by field**. Equivalence is the entire reason a curated batch can be
    published as output of the same pipeline a typed request uses, so it is asserted
    directly, never argued.

### bounded fallback provenance (v2.3)

37. **ORCA fallback is bounded, centralized, and auditable** (PIPELINE.md §2.7):
    an orca-* skill consumes only `params` and the ordered `params_fallbacks`
    supplied by P1-5; it MUST NOT construct a replacement basis, SCF threshold, or
    retry count. Every terminal output is classified by
    `msa_shared.orca_diagnostics`, so business scripts MUST NOT embed A/B/C/D
    signature strings. Every actual submission appends a `calc_attempts` record,
    and the `calc_*` summaries are derived from that history rather than fabricated
    independently. Job filenames, including XANES optsp variants, MUST use
    `naming.*` and never a local `_suffixed` helper.

---

## Task Field Contract table — single source of truth

The per-skill Consumes/Appends schema lives in **PIPELINE.md §1.1** and nowhere
else. When Phase 3 generates a SKILL.md, read the matching row directly from
PIPELINE.md §1.1 and copy that skill's slice into the SKILL.md body's
`## Task Field Contract` section. This meta-skill deliberately keeps **no** copy
of the table — a second copy would drift. If you need the field list, open
PIPELINE.md §1.1.

---

## Phase 5: four-tier verification checklist

### 5.1 Static lint (automated, run `scripts/validate_skill.py`)

- [ ] SKILL.md exists with valid YAML frontmatter
- [ ] frontmatter has `name` / `description` / `version` / `msa_contract_version`
- [ ] `name` equals the directory name
- [ ] `msa_contract_version` equals `msa_shared/VERSION`
- [ ] description contains "MUST load" and "DO NOT"
- [ ] SKILL.md body contains a `## Task Field Contract` section
- [ ] body contains `## Agent Behavior`, `## Scripts`, `## Workflow`, `## Integration`, `## Troubleshooting`
- [ ] every `.py` under scripts/ begins with `#!/usr/bin/env python3`
- [ ] scripts/*.py contains the self-locating bootstrap block (regex match)
- [ ] every entry script's filename appears in SKILL.md (rule 36: each entry is documented)
- [ ] a multi-entry skill's entry scripts all declare the same `STAGE_ID` (rule 36)
- [ ] orca-* business scripts embed no terminal diagnostic strings and use `msa_shared.orca_diagnostics` (rule 37)
- [ ] orca-* business scripts hand-build no `.inp/.out/.sh` paths and define no `_suffixed` helper (rule 37)
- [ ] scripts/*.py defines no bare `DEFAULT_` constant (regex blacklist)
- [ ] scripts/*.py has no `matplotlib.use(` or `plt.rcParams.update(` (outside spectrum_style)
- [ ] entry script's `MSA_CONTRACT_VERSION` constant equals `msa_shared/VERSION`
- [ ] entry script declares a module-scope `MSA_REQUIRES` and `main()` calls `ensure_deps(`
- [ ] orca-* entry script calls `resolve_cluster()` (no argument) and declares NO `--cluster-config`
- [ ] plot-* / export-spectrum-data entry script calls `mark_no_signal(` (rule 31)
- [ ] plot-* / export-spectrum-data entry script calls `spectrum_kwargs(` and hand-maps no `target_nucleus=` / `target_element=` (rule 35)
- [ ] plot-* / export-spectrum-data entry script tags filenames with `naming.spec_variant(` (rule 34)
- [ ] plot-* entry script titles the figure via `naming.title_name(` (rule 33)
- [ ] the skill has a producer row in PIPELINE.md §1.1 (parsed from the contract table)
- [ ] SKILL.md's declared append-field set ⊆ that row's "Appends" column
- [ ] SKILL.md declares every non-optional field of that row

### 5.2 Smoke test

- [ ] `python scripts/<entry>.py --help` exits 0 with no traceback
- [ ] running the skill on an empty `task_list.json` (`[]`) exits 0 and outputs `[]`

### 5.3 End-to-end dry-run (the "benzene IR" golden case)

Use the fixed golden case to check pipeline chaining:

```json
[{
  "run_id": "run_20260101_120000_a1b2c3",
  "task_id": "task_01_benzene_ir",
  "molecule_name": "benzene",
  "target_spectra": "ir",
  "solvent_raw": "",
  "solvent_std": "gas",
  "extra_params": {}
}]
```

Before delivery a new skill must:
- [ ] use a real task_list produced by the upstream skill as input (not hand-written)
- [ ] emit a task_list the downstream skill can consume
- [ ] have at least one task reach `success` at this skill along the chain

### 5.4 Cross-skill interface consistency

The field-set direction of this tier is now enforced automatically in 5.1:
`validate_skill.py` parses PIPELINE.md §1.1 and rejects a skill that appends a
field its row does not grant, or omits a non-optional field the row requires.
The schema mirror in `dump_task_schema.py` is guarded the same way by
`dump_task_schema.py --check`.

What still needs a human pass:

- [ ] this skill's `UPSTREAM_STAGE` is its true **data-dependency** upstream, which
      is not always the previous §1.1 row — the pipeline branches (both
      `structure-plot` and `param-recommendation` depend on `xyz`), so table order
      alone does not settle it
- [ ] the downstream skill's consumed fields ⊆ this skill's appended fields (if a
      direct downstream exists)
- [ ] a multi-entry skill has a test that runs both entries on the same molecule and
      diffs the emitted tasks field by field, ignoring only `run_id` / `task_id`
      (rule 36) — a field-set check cannot see two entries spelling the same field
      differently

---

## Known drift and grandfathered exceptions

Empty at first. Each time a downstream skill's deviation/exception is found,
record it here for future generators to avoid. Format:

```
### <skill-name> — <deviation>
Found: YYYY-MM-DD
Deviation: ...
Temporary mitigation: ...
Root fix: ...
```

---

## How to use this (for the Agent)

When asked to generate/modify an MSA skill:

1. Read PIPELINE.md in full, read this file in full, read the user's attached method doc (if any).
2. Classify the skill (A/B/C/D/S); fix the stage_id and upstream_stage.
3. Copy from `templates/{class}/` into `skills/{skill-name}/`.
4. Replace every `{{XXX}}` placeholder with sed or Edit.
5. Hand-write the business logic (templates never generate business logic).
6. Run `python3 tools/vendor_msa_shared.py sync` to vendor the shared modules the skill imports.
7. Run `python3 meta/msa-skill-spec/scripts/validate_skill.py skills/{skill-name}/`.
8. Run the 5.2 smoke test.
9. Run the 5.3 end-to-end dry-run.
10. On pass, report to the user with a file:// link.

## Integration

- Upstream: none (this meta-skill is not in the runtime pipeline).
- Downstream: all 20 MSA skills.
- Reference contract: `msa_shared/PIPELINE.md`.
- Legacy reference skills, if any, are for reading only and must never be imported.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| validate_skill.py reports `missing msa_contract_version` | frontmatter lacks the key | add it, value = msa_shared/VERSION |
| smoke test traceback `ImportError: msa_shared` | vendored `scripts/msa_shared/` missing or bootstrap block altered | run `tools/vendor_msa_shared.py sync`; copy the §4.1 self-locating block to the script top |
| upstream skill output unreadable by downstream in dry-run | append field names diverge from PIPELINE.md §1.1 | PIPELINE.md is authoritative; fix this skill |
| description fails lint | missing MUST/DO NOT | add "Agent MUST load ... BEFORE ..." and "DO NOT ..." |
