# Molecular Spectroscopy Agent (MSA)

**Version:** 2.0

Molecular Spectroscopy Agent (MSA) is a modular agentic workflow for
multimodal computational spectroscopy. It supports both natural-language
interaction and manifest-based batch execution. Both interfaces use the same
validated modules for protocol construction, ORCA calculations, spectral
visualization, and data export.

MSA is implemented as a collection of independent **skills**. Each skill
contains a `SKILL.md` contract and the scripts required to perform a defined
stage of the workflow. Skills read from and append validated fields to a shared
`task` object. The authoritative task schema and inter-skill data contract are
defined in `msa_shared/PIPELINE.md`.

## Design principles

- **Consistent execution:** interactive and batch interfaces invoke the same
  computational workflow.
- **Modular composition:** each skill performs one well-defined task and can be
  developed and tested independently.
- **Explicit data contracts:** all inter-skill inputs, outputs, and status fields
  follow the contract in `msa_shared/PIPELINE.md`.
- **Runtime independence:** every skill includes the shared modules it requires
  and does not depend on a repository-level installation path.
- **Reproducible maintenance:** vendored modules are checked for byte-level
  consistency with their canonical development copies.

## Repository structure

```text
molecular-spectroscopy-agent/          # development repository; not required at runtime
├── msa_shared/                         # canonical development-time shared modules
│   ├── PIPELINE.md                     # task schema and inter-skill data contract
│   ├── VERSION                         # contract version
│   ├── common_utils.py                 # task I/O, status handling, and version checks
│   ├── naming.py                       # filename and path construction
│   ├── spectrum_style.py               # shared Matplotlib style
│   └── constants/                      # canonical numerical and string constants
├── tools/vendor_msa_shared.py          # synchronization and SHA-256 drift checks
├── meta/msa-skill-spec/                # specification for creating compliant skills
│   ├── SKILL.md                        # skill-authoring requirements
│   ├── scripts/validate_skill.py       # static skill validator
│   ├── scripts/dump_task_schema.py     # canonical task-schema generator
│   └── templates/                      # templates for supported skill classes
├── skills/                             # runtime skills, e.g. task-recognition and orca-*
│   └── <skill>/scripts/
│       ├── <entry>.py                  # skill entry point
│       └── msa_shared/                 # vendored shared modules used by the skill
└── tests/                              # cross-skill and regression tests
```

## Runtime isolation and shared-module vendoring

At runtime, a host agent may load each skill independently, without access to
the development repository or a common installation directory. Each skill is
therefore self-contained and vendors the required `msa_shared` modules under
`scripts/msa_shared/`.

The entry script resolves its own `scripts/` directory before importing the
vendored modules:

```python
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from msa_shared.common_utils import ...
```

The repository-level `msa_shared/` directory is the canonical development
source. After modifying a shared module, synchronize the vendored copies and
verify that they remain identical:

```bash
python3 tools/vendor_msa_shared.py sync
python3 tools/vendor_msa_shared.py check
```

Helpers used by only one skill remain in that skill's `scripts/` directory and
use underscore-prefixed module names, for example `_functional_basis.py`,
`_timing.py`, and `_orchestration.py`.

## Adding a skill

1. Read `msa_shared/PIPELINE.md` and `meta/msa-skill-spec/SKILL.md`.
2. Select the appropriate template from
   `meta/msa-skill-spec/templates/{class}/`.
3. Complete the template fields and implement the domain-specific logic.
4. Synchronize the required shared modules:

   ```bash
   python3 tools/vendor_msa_shared.py sync
   ```

5. Validate the skill structure and contract compliance:

   ```bash
   python3 meta/msa-skill-spec/scripts/validate_skill.py skills/<name>/
   ```

6. Run the skill-level smoke tests and the relevant golden and cross-skill
   regression tests.

## Testing

Run the task-recognition verification suite with:

```bash
python3 skills/task-recognition/tests/verify_task_recognition.py
```

Additional skill-level and cross-skill tests are located under `tests/` and the
corresponding skill directories.

## License

This project is distributed under the MIT License. See `LICENSE` for details.
