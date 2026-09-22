"""msa_shared — shared runtime library for the Molecular Spectroscopy Agent (MSA).

This package is the single source of truth for:
- the task-list data contract (see PIPELINE.md),
- file-naming conventions (naming.py),
- plotting style (spectrum_style.py),
- runtime helpers and status contract (common_utils.py),
- project constants (constants/).

Path decoupling (self-location + vendoring)
--------------------------------------------
At runtime there is no repository and no shared install path: a host Agent loads
each skill on its own, so every skill must be self-contained. This ``msa_shared/``
package is the **dev-time single source of truth**. A byte-identical physical
copy of exactly the modules a skill imports is *vendored* into that skill's
``scripts/msa_shared/`` directory (see ``tools/vendor_msa_shared.py``).

Every skill entry script self-locates its own ``scripts/`` directory and imports
the vendored copy — no env var, no repo layout, no shared install:

    import os, sys
    _SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from msa_shared.common_utils import load_task_list, dump_task_list, ...

Editing rule: change canonical ``msa_shared/`` only, then run
``python3 tools/vendor_msa_shared.py sync`` to refresh the copies.
``... check`` is a checksum drift guard that fails if any copy diverges.
"""

__all__ = ["common_utils", "naming", "spectrum_style", "constants"]
