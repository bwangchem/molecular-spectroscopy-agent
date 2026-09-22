#!/usr/bin/env python3
"""vendor_msa_shared.py — dev-time vendoring of the shared library into skills.

WHY
---
At runtime there is no repository and no shared install path: a host Agent loads
each skill individually, so every skill must be *self-contained*. We therefore keep
one canonical copy of ``msa_shared/`` in the repo (the dev-time single source of
truth) and vendor a **byte-identical physical copy** of exactly the modules a
skill imports into that skill's ``scripts/msa_shared/`` directory. The skill's
entry script self-locates its own ``scripts/`` dir on ``sys.path`` and imports
the vendored copy — no repo, no env var, no shared install required.

Single-consumer helpers are NOT vendored; they were moved to live directly in
their owner skill (``_functional_basis.py`` in param-recommendation, ``_timing.py``
in time-estimation, ``_orchestration.py`` in batch-orchestrator).

USAGE
-----
    python3 tools/vendor_msa_shared.py sync    # refresh every vendored copy
    python3 tools/vendor_msa_shared.py check   # drift guard: exit 1 on mismatch

``check`` is the checksum drift guard. Run it in CI / before committing: it fails
if any vendored file differs from (or is missing relative to) the canonical
source, i.e. someone edited a copy directly or forgot to re-``sync`` after
editing canonical.
"""

import argparse
import hashlib
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANON = os.path.join(REPO_ROOT, "msa_shared")
SKILLS_DIR = os.path.join(REPO_ROOT, "skills")

# Files every consuming skill needs (relative to msa_shared/).
BASE = ["__init__.py", "VERSION", "common_utils.py", "naming.py"]

# calc family: ORCA/SLURM input grammar, diagnostics + SLURM constants.
_CALC = BASE + ["orca_input.py", "orca_diagnostics.py",
                "constants/__init__.py", "constants/slurm.py"]

# plot/export family: parsing + broadening + plot/nmr constants.
_SPEC = BASE + ["orca_output.py", "broadening.py", "constants/__init__.py",
                "constants/plot_defaults.py", "constants/nmr_ref.py"]

# Per-skill vendor manifest. A skill absent here vendors nothing (has no
# msa_shared imports) — currently every skill imports at least common_utils.
MANIFEST = {
    # preprocessing (contract core only)
    "name-to-smiles":       BASE,
    "smiles-to-xyz":        BASE,
    "structure-plot":       BASE,
    "task-recognition":     BASE,
    # param-recommendation: shared solvents (functional_basis is skill-local)
    "param-recommendation": BASE + ["constants/__init__.py", "constants/solvents.py"],
    # time-estimation: contract core only (_timing is skill-local, slurm-decoupled)
    "time-estimation":      BASE,
    # batch-orchestrator: contract core only (_orchestration is skill-local)
    "batch-orchestrator":   BASE,
    # ORCA calc skills
    "orca-ir":    _CALC,
    "orca-raman": _CALC,
    "orca-uvvis": _CALC,
    "orca-fluo":  _CALC,
    "orca-nmr":   _CALC,
    "orca-xanes": _CALC,
    # plot skills (also need the shared matplotlib style)
    "plot-ir":    _SPEC + ["spectrum_style.py"],
    "plot-raman": _SPEC + ["spectrum_style.py"],
    "plot-uvvis": _SPEC + ["spectrum_style.py"],
    "plot-fluo":  _SPEC + ["spectrum_style.py"],
    "plot-nmr":   _SPEC + ["spectrum_style.py"],
    "plot-xanes": _SPEC + ["spectrum_style.py"],
    # export skill (no plotting, so no spectrum_style)
    "export-spectrum-data": _SPEC,
}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _dest_dir(skill: str) -> str:
    return os.path.join(SKILLS_DIR, skill, "scripts", "msa_shared")


def _dedup(files):
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def sync() -> int:
    n = 0
    for skill, files in MANIFEST.items():
        dest_root = _dest_dir(skill)
        for rel in _dedup(files):
            src = os.path.join(CANON, rel)
            if not os.path.isfile(src):
                sys.stderr.write(f"ERROR: canonical file missing: {src}\n")
                return 2
            dst = os.path.join(dest_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
        print(f"  vendored {len(_dedup(files)):2d} files -> skills/{skill}/scripts/msa_shared/")
    print(f"\nsync OK: {n} files across {len(MANIFEST)} skills")
    return 0


def check() -> int:
    problems = []
    for skill, files in MANIFEST.items():
        dest_root = _dest_dir(skill)
        for rel in _dedup(files):
            src = os.path.join(CANON, rel)
            dst = os.path.join(dest_root, rel)
            if not os.path.isfile(src):
                problems.append(f"{skill}: canonical missing {rel}")
                continue
            if not os.path.isfile(dst):
                problems.append(f"{skill}: vendored copy missing {rel} (run: sync)")
                continue
            if _sha256(src) != _sha256(dst):
                problems.append(f"{skill}: DRIFT in {rel} (vendored != canonical; run: sync)")
    if problems:
        sys.stderr.write("msa_shared vendoring drift detected:\n")
        for p in problems:
            sys.stderr.write(f"  - {p}\n")
        sys.stderr.write(f"\nFAIL: {len(problems)} problem(s). Run `python3 tools/vendor_msa_shared.py sync`.\n")
        return 1
    print(f"check OK: all vendored copies match canonical across {len(MANIFEST)} skills")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["sync", "check"])
    args = p.parse_args()
    sys.exit(sync() if args.action == "sync" else check())


if __name__ == "__main__":
    main()
