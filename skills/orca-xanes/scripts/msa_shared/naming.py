"""Filename conventions for the MSA pipeline. Single source of truth.

Every skill that reads/writes a task-scoped file MUST route through this module
instead of hand-rolling f-strings. This keeps filenames consistent across the
whole pipeline so downstream skills can find upstream artifacts.

Molecule names may be Chinese / contain spaces / special characters; they are
domain input data. `_safe_slug` maps any name to a filesystem-safe token while
the human-readable name is preserved inside the task object.
"""

import os
import re
from typing import Literal

Spec = Literal["ir", "raman", "uvvis", "nmr", "fluo", "xanes"]

# Working directory is created in the current working directory of whoever runs
# the pipeline (typically the repo root or a job directory). The base is a
# cwd-relative default so nothing is hard-coded to a specific machine, but the
# path builders below anchor each artifact to an ABSOLUTE path at creation time
# (see ``workspace_dir``). This freezes the location to the cwd that first built
# it, so a downstream stage resolves an upstream artifact regardless of its own
# working directory — a relative path stored in the task_list would otherwise
# break the moment a later stage runs from a different cwd.
_WORKSPACE_ROOT = "./local_workspace"


# ---------- run-isolation namespace ----------
#
# The pipeline is content-addressed by (molecule_name, spec): two pipeline runs
# that touch the SAME molecule (and especially the same spectrum) would otherwise
# resolve to byte-identical local paths AND the same directory under the shared
# HPC ``remote_root`` — so concurrent runs clobber each other's .inp/.xyz/.out
# (the classic "input file was overwritten by another task" symptom).
#
# To make runs mutually isolated, every task carries a unique ``run_id`` (stamped
# by task-recognition). ``common_utils.load_task_list`` activates it here exactly
# once per process via ``set_run_id``; all path builders below then nest their
# artifacts under that run namespace, both locally (``local_workspace/<run>/…``)
# and remotely (``<remote_root>/<run>/…``). An empty run id falls back to the
# legacy flat layout, so task_lists produced before run_id existed still work.
_RUN_ID = ""


def set_run_id(run_id: str) -> None:
    """Set (or clear) the process-wide run-isolation namespace.

    Called once per process by ``common_utils.load_task_list`` from the task_list's
    ``run_id``. Idempotent within a process; a falsy value clears it (restoring the
    legacy flat layout). The id is slugified so it is always filesystem-safe.
    """
    global _RUN_ID
    _RUN_ID = _safe_slug(run_id) if run_id else ""


def current_run_id() -> str:
    """Return the active run-isolation namespace ('' when none is set)."""
    return _RUN_ID


def _safe_slug(molecule_name: str) -> str:
    """Filesystem-safe slug of a molecule name.

    Preserves Unicode word characters (ASCII letters/digits/underscore AND CJK,
    since `\\w` is Unicode-aware for `str` in Python 3), hyphens included. Spaces
    and filesystem-hostile punctuation collapse to '_'. This keeps distinct
    molecule names (e.g. 苯 vs 甲苯 vs 对硝基苯酚) mapped to distinct, readable
    tokens instead of colliding. Empty result falls back to 'mol'.
    """
    s = molecule_name.strip()
    s = re.sub(r"[^\w\-]+", "_", s)  # \w keeps CJK; everything else -> '_'
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "mol"


def workspace_dir(molecule_name: str) -> str:
    root = os.path.join(_WORKSPACE_ROOT, _RUN_ID) if _RUN_ID else _WORKSPACE_ROOT
    d = os.path.join(root, _safe_slug(molecule_name))
    os.makedirs(d, exist_ok=True)
    # Absolute so a path stored in the task_list resolves from any downstream cwd.
    return os.path.abspath(d)


def remote_work_dir(molecule_name: str, remote_root: str) -> str:
    """Remote SLURM working directory for a molecule.

    Mirrors the local safe-slug convention so a molecule maps to the same token
    locally and on the cluster (e.g. remote_root/对硝基苯酚 -> .../<slug>). When a
    run-isolation namespace is active it is inserted between the shared
    ``remote_root`` and the molecule slug (``<remote_root>/<run>/<slug>``) so
    concurrent runs never share a remote submit directory. Pure string join:
    unlike ``workspace_dir`` it creates nothing (the remote side is created by
    ``common_utils.submit_slurm`` via ``mkdir -p``).
    """
    root = remote_root.rstrip("/")
    if _RUN_ID:
        root = f"{root}/{_RUN_ID}"
    return f"{root}/{_safe_slug(molecule_name)}"


def xyz_initial(molecule_name: str) -> str:
    return os.path.join(workspace_dir(molecule_name), f"{_safe_slug(molecule_name)}_initial.xyz")


def resolve_initial_xyz(task: dict) -> str:
    """Resolve the on-disk initial-geometry path for a task, robust to cwd drift.

    Single source of truth for how an orca-* consumer locates the XYZ produced by
    smiles-to-xyz. Resolution order:
      1. ``task["xyz_file"]`` if that literal path is on disk (the normal case now
         that paths are absolutized at creation time);
      2. otherwise recompute the canonical location via ``xyz_initial`` from
         ``molecule_name`` under the active run_id — this rescues a legacy or
         relocated task_list whose stored path went stale (e.g. produced under a
         different cwd before absolutization).
    Raises ``FileNotFoundError`` (naming both candidates) if neither is on disk;
    ``process_task`` converts that into ``calc_status="failed"``.
    """
    stored = task.get("xyz_file")
    if stored and os.path.isfile(stored):
        return stored
    recomputed = xyz_initial(task.get("molecule_name", ""))
    if os.path.isfile(recomputed):
        return recomputed
    raise FileNotFoundError(
        f"xyz_file missing or not on disk: stored={stored!r}, recomputed={recomputed!r}"
    )


def xyz_opt(molecule_name: str) -> str:
    return os.path.join(workspace_dir(molecule_name), f"{_safe_slug(molecule_name)}_opt.xyz")


def structure_png(molecule_name: str) -> str:
    return os.path.join(workspace_dir(molecule_name), f"{_safe_slug(molecule_name)}_structure.png")


def _tag(spec: str, variant=None) -> str:
    """Join a spectrum with its optional variant into one filename tag.

    ``("nmr", "1H") -> "nmr_1H"``; ``("xanes", "C") -> "xanes_C"``;
    ``("ir", None) -> "ir"``. Slugified so an odd variant cannot escape the
    filename.
    """
    v = str(variant or "").strip()
    return f"{spec}_{_safe_slug(v)}" if v else str(spec)


def spec_variant(task: dict):
    """The ARTIFACT-level variant for a task, or None.

    Two spectra produce more than one artifact per molecule, so their figures and
    CSVs must carry a discriminator or a second edge/nucleus silently overwrites
    the first:

      * NMR   -> the target nucleus (``1H`` / ``13C``)
      * XANES -> the absorbing element, upper-cased (``C`` / ``N`` / ``O`` / ``F``);
        ``xanes_target_element`` (resolved by orca-xanes) wins over the request in
        ``extra_params.target_element``.

    Every other spectrum is one-per-molecule and returns None.
    """
    spec = (task.get("target_spectra") or "").strip().lower()
    extra = task.get("extra_params") or {}
    if spec == "nmr":
        return (extra.get("target_nucleus") or "").strip() or None
    if spec == "xanes":
        el = task.get("xanes_target_element") or extra.get("target_element") or ""
        return el.strip().upper() or None
    return None


def job_variant(task: dict):
    """The ORCA-JOB-level variant for a task, or None.

    Deliberately narrower than ``spec_variant``: an NMR job prints the shielding
    of EVERY nucleus into one shared ``.out``, so orca-nmr runs a single job per
    molecule and the downstream stages filter it per nucleus. Tagging the
    ``.inp`` / ``.sh`` / ``.out`` with the nucleus would fabricate two jobs where
    there is one. XANES is genuinely per-edge (a different core-orbital window per
    element), so its job files DO carry the element.

    Keeping this rule here rather than in the skills means no caller can get it
    wrong by passing ``spec_variant`` to a job-file builder.
    """
    if (task.get("target_spectra") or "").strip().lower() == "xanes":
        return spec_variant(task)
    return None


def orca_inp(molecule_name: str, spec: Spec, variant=None) -> str:
    return os.path.join(workspace_dir(molecule_name),
                        f"{_safe_slug(molecule_name)}_{_tag(spec, variant)}.inp")


def orca_out(molecule_name: str, spec: Spec, variant=None) -> str:
    return os.path.join(workspace_dir(molecule_name),
                        f"{_safe_slug(molecule_name)}_{_tag(spec, variant)}.out")


def orca_submit_sh(molecule_name: str, spec: Spec, variant=None) -> str:
    return os.path.join(workspace_dir(molecule_name),
                        f"{_safe_slug(molecule_name)}_{_tag(spec, variant)}.sh")


def spectrum_png(molecule_name: str, spec: Spec, variant=None) -> str:
    return os.path.join(workspace_dir(molecule_name),
                        f"{_safe_slug(molecule_name)}_{_tag(spec, variant)}_spectrum.png")


def spectrum_pdf(molecule_name: str, spec: Spec, variant=None) -> str:
    return os.path.join(workspace_dir(molecule_name),
                        f"{_safe_slug(molecule_name)}_{_tag(spec, variant)}_spectrum.pdf")


def peaks_csv(molecule_name: str, spec: Spec, variant=None) -> str:
    """Peaks CSV path — ``{slug}_{spec}[_{variant}]_peaks.csv``."""
    return os.path.join(workspace_dir(molecule_name),
                        f"{_safe_slug(molecule_name)}_{_tag(spec, variant)}_peaks.csv")


def spectrum_csv(molecule_name: str, spec: Spec, variant=None) -> str:
    """Broadened-curve CSV path — ``{slug}_{spec}[_{variant}]_spectrum.csv``."""
    return os.path.join(workspace_dir(molecule_name),
                        f"{_safe_slug(molecule_name)}_{_tag(spec, variant)}_spectrum.csv")


# ---------- human-readable molecule name (figure titles / CSV metadata) ----------
#
# This is the DISPLAY counterpart of ``_safe_slug``: the slug goes into filenames,
# ``display_name`` goes into figure titles and CSV metadata headers. It lives here
# rather than in ``spectrum_style`` because ``name-to-smiles`` must produce
# ``molecule_name_en`` without importing matplotlib, and two implementations of
# "how do we capitalize a chemical name" would inevitably drift apart.
#
# Locant / stereo descriptors that may precede a parent name. A leading
# hyphen-terminated group made only of these (or only of digits/commas/primes)
# is a descriptor, so the capital belongs to the parent name after it, not to it.
_NAME_DESCRIPTORS = frozenset({
    "n", "o", "s", "c", "p", "h",                      # heteroatom locants
    "cis", "trans", "syn", "anti", "endo", "exo",      # geometry
    "e", "z", "r", "d", "l", "dl",                     # configuration
    "sec", "tert", "iso", "neo",                       # chain descriptors
    "ortho", "meta", "para", "m",                      # ring positions (o/p above)
    "alpha", "beta", "gamma", "delta", "epsilon",      # Greek positions
    "erythro", "threo",
})


def _is_descriptor_group(head: str) -> bool:
    """True when a hyphen-terminated name prefix is a locant / stereo descriptor.

    Accepts pure locants (``2``, ``2,4``, ``1,3,5``, ``2'``) and comma-separated
    descriptor tokens (``N,N``, ``cis``, ``tert``, ``alpha``).
    """
    if not head:
        return False
    tokens = [t.strip() for t in head.split(",")]
    if not all(tokens):
        return False
    for t in tokens:
        bare = t.rstrip("'\u2032")
        if bare.isdigit():
            continue
        if bare.lower() in _NAME_DESCRIPTORS:
            continue
        return False
    return True


def display_name(molecule_name: str) -> str:
    """Capitalize a molecule name for display, honouring chemical prefixes.

    Plain ``str.capitalize`` / ``name[0].upper()`` is wrong for chemistry: the
    leading locant or stereo descriptor of ``p-nitrophenol`` is a lower-case
    ``p`` by convention, and the letter that must be capitalized is the first one
    of the parent name. So any leading run of descriptor groups (see
    ``_NAME_DESCRIPTORS``) is skipped and the first character after it is
    upper-cased. Nothing is ever lower-cased, so an acronym or formula such as
    ``THF`` / ``CH3OH`` and an already-correct PubChem Title survive untouched.

        benzene               -> Benzene
        p-nitrophenol         -> p-Nitrophenol
        2,4-dinitrotoluene    -> 2,4-Dinitrotoluene
        N,N-dimethylformamide -> N,N-Dimethylformamide
        trans-stilbene        -> trans-Stilbene
        ethylene glycol       -> Ethylene glycol
        CH3OH                 -> CH3OH
    """
    if not molecule_name:
        return molecule_name
    i = 0
    while True:
        j = molecule_name.find("-", i)
        if j < 0 or not _is_descriptor_group(molecule_name[i:j]):
            break
        i = j + 1
    if i >= len(molecule_name):
        return molecule_name
    return molecule_name[:i] + molecule_name[i].upper() + molecule_name[i + 1:]


def title_name(task: dict) -> str:
    """The molecule name a figure title / CSV metadata header should show.

    English first (``molecule_name_en``, resolved and display-cased by
    name-to-smiles), falling back to the user's original ``molecule_name`` when no
    English name could be resolved. Centralized so all six plot skills and the
    export skill can never disagree about which field wins.
    """
    return ((task.get("molecule_name_en") or "").strip()
            or (task.get("molecule_name") or "").strip())

