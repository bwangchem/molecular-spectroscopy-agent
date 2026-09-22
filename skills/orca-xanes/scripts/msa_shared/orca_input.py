"""ORCA input (.inp) and SLURM submit-script builders — shared grammar.

Single source of truth for translating a param-recommendation ``params`` step
into ORCA "simple input" syntax, and for the SLURM batch script that runs ORCA on
the cluster. Every orca-{spec} skill (Part 2) builds its ``.inp`` / ``submit.sh``
through this module so the calc_type->keyword grammar and the SLURM template never
drift across the six spectra.

Division of responsibility (PIPELINE.md §4.1): chemistry *choices* — functional,
basis set, solvent model, ZORA — always come from the task's ``params`` (which
param-recommendation filled from its skill-local ``_functional_basis`` +
``constants/solvents``). This module owns only the ORCA/SLURM *grammar*: how a
calc_type maps
to a keyword, how the ``%pal`` / ``%maxcore`` blocks and the ``* xyzfile`` line
are laid out, and the shape of the SLURM script. It hard-codes no functional,
basis, or solvent.

Supported so far: the single-step vibrational path (IR / Raman, and a plain
``opt``); the two-step ground-state ``opt`` -> excited-state ``tddft`` path
(UV-Vis); the three-step ground-state ``gs_opt`` -> excited-state ``es_opt`` ->
``emission_tddft`` path (fluorescence); the two-step ``opt`` -> ``nmr`` shielding
path (NMR); and the two-step ``opt`` -> ``xas_tddft`` core-excitation TD-DFT path
(XANES) — all chained in one input file via ``$new_job``. In a multi-job file a
downstream job inherits the previous job's (optimized) geometry by giving a
``* xyzfile <charge> <mult>`` line with no filename (ORCA manual, "Input of
Coordinates": a new job reads the coordinates from the previous job when no file
is named). Any ``calc_type`` without a keyword mapping still raises
``NotImplementedError`` so nothing silently emits a half-built input.
"""

from msa_shared.constants.slurm import (
    DEFAULT_NPROCS,
    DEFAULT_WALLTIME,
    DEFAULT_MEMORY_MB_PER_CORE,
    DEFAULT_PARTITION,
    DEFAULT_QOS,
)
from msa_shared.orca_diagnostics import ORCA_NORMAL_TERMINATION

# calc_type role -> ORCA simple-input run-type keyword fragment.
# A "" fragment means the job carries no run-type keyword (a single-point SCF);
# for such a step the run is driven entirely by a %-block (e.g. %tddft), added by
# _extra_blocks below. The role also selects which method blocks are emitted.
CALC_TYPE_KEYWORDS = {
    "opt_freq": "OPT FREQ",       # opt + analytical frequencies (IR; Raman/UV-Vis step 1)
    "numfreq_raman": "NUMFREQ",   # numerical frequencies + polarizability (Raman step 2)
    "opt": "OPT",                 # plain geometry optimisation (NMR/Fluo/XANES step 1)
    "nmr": "NMR",                 # NMR shielding single point (ORCA simple keyword "NMR")
    "xanes_sp": "SP",             # KeepDens single point feeding the XANES TD-DFT (XANES step 2)
    "uvvis_tddft": "",            # single point; valence excited states via %tddft (UV-Vis step 2)
    "fluo_es_opt": "OPT",         # excited-state geometry opt via %tddft iroot (Fluo step 2)
    "fluo_emission": "",          # single point; vertical emission via %tddft at ES geometry (Fluo step 3)
    "xanes_tddft": "",            # single point; core-excitation TD-DFT via %tddft orbwin (XANES step 3)
}

def _keyword_line(step):
    """Build the ``! ...`` keyword line for one params step.

    Canonical assembly order (``重要修改`` directive, PIPELINE §4.1)::

        ! <task> <functional> [<dispersion>] [<basis_set>] [ZORA]
          [<ri_acceleration>] [<auxiliary_basis>] [DecontractAux] [KeepDens]
          [TightSCF] [<solvent_model>]

    ``functional`` is required (must come from params). ``basis_set`` is optional
    (composite methods like r2SCAN-3c carry their own basis, so it is None).
    ``dispersion`` is a keyword such as ``D4`` (None for composite/range-separated
    functionals that already include their own dispersion). ``solvent_model`` is a
    ready-made CPCM keyword (e.g. ``CPCM(Water)``) or None.
    """
    calc_type = step.get("calc_type")
    if calc_type not in CALC_TYPE_KEYWORDS:
        raise NotImplementedError(
            f"orca_input has no keyword grammar for calc_type={calc_type!r} yet "
            f"(supported: {', '.join(sorted(CALC_TYPE_KEYWORDS))})"
        )
    functional = step.get("functional")
    if not functional:
        raise ValueError("params step is missing 'functional' (must come from params)")

    parts = ["!"]
    keyword = CALC_TYPE_KEYWORDS[calc_type]
    if keyword:
        parts.append(keyword)
    parts.append(functional)
    if step.get("dispersion"):
        parts.append(step["dispersion"])
    if step.get("basis_set"):
        parts.append(step["basis_set"])
    if step.get("zora"):
        parts.append("ZORA")
    if step.get("ri_acceleration"):
        parts.append(step["ri_acceleration"])
    if step.get("auxiliary_basis"):
        parts.append(step["auxiliary_basis"])
    if step.get("decontract_aux"):
        parts.append("DecontractAux")
    if step.get("keepdens"):
        parts.append("KeepDens")
    if step.get("tightscf"):
        parts.append("TightSCF")
    if step.get("solvent_model"):
        parts.append(step["solvent_model"])
    return " ".join(parts)


def _require_pos_int(step, field, calc_type):
    """Read a required positive-int chemistry choice from a params step.

    Excited-state control values (``nroots`` / ``iroot``) are chemistry choices
    and must arrive in the step from params (param-recommendation fills them from
    its skill-local ``_functional_basis``); this module never invents them. Raises
    ``ValueError`` if the field is absent, a bool, or not a positive int.
    """
    val = step.get(field)
    if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
        raise ValueError(
            f"{calc_type} step requires a positive int {field!r} from params, "
            f"got {val!r}"
        )
    return val


def _nmr_nuclei(step):
    """Canonical list of NMR element letters requested by a shielding step.

    Prefers a ``nmr_nuclei`` list (e.g. ``["H", "C"]`` when several nuclei are
    merged into one shielding job) and falls back to the single ``nmr_nucleus``.
    Entries are normalized to stripped upper-case element letters, de-duplicated,
    and returned in a deterministic order (H, then C, then the rest alphabetical)
    so the emitted ``%eprnmr`` block is stable regardless of task ordering. An
    empty/absent selection defaults to proton NMR (``["H"]``).
    """
    raw = step.get("nmr_nuclei")
    if raw is None:
        raw = [step.get("nmr_nucleus") or "H"]
    elif isinstance(raw, str):
        raw = [raw]
    seen = []
    for x in raw:
        el = (x or "").strip().upper()
        if el and el not in seen:
            seen.append(el)
    if not seen:
        seen = ["H"]
    _order = {"H": 0, "C": 1}
    return sorted(seen, key=lambda e: (_order.get(e, 2), e))


def _extra_blocks(step):
    """Return the ``%``-block lines a step needs beyond ``%pal`` / ``%maxcore``.

    Excited-state / method-block control values are *chemistry choices* and must
    arrive in the step from params; this module never invents them (see
    ``_require_pos_int``). Blocks are keyed on the ``calc_type`` role:

    - ``numfreq_raman`` (Raman step 2): ``%elprop Polar 1 end`` so ORCA computes the
      polarizability derivatives and prints a RAMAN SPECTRUM block.
    - ``uvvis_tddft`` (UV-Vis single point): ``%tddft nroots N[ tda ..] maxdim M end``;
      ``maxdim`` sizes the Davidson subspace (~1.5-2x nroots).
    - ``nmr`` (shielding): only the optional ``%scf Thresh 1e-8 MaxIter 500 end``
      (for ionic systems with ``scf_tight`` truthy). The ``%eprnmr`` property block
      is emitted by ``_post_coord_blocks`` — ORCA requires it AFTER the coordinate
      specification (``* xyzfile``), unlike computational-control blocks.
    - ``fluo_es_opt`` (fluorescence ES geometry opt): a robust-convergence pair
      ``%geom MaxIter 300 cartfallback true end`` + ``%scf MaxIter 300 AutoTraH
      false end`` followed by ``%tddft nroots N iroot M[ tda ..] end``.
    - ``fluo_emission`` (vertical emission at the ES geometry): ``%tddft nroots N
      iroot M[ tda ..] cpcmeq false end`` — ``cpcmeq false`` uses the
      non-equilibrium solvent response appropriate for vertical emission (emitted
      whenever the step carries ``cpcmeq`` present-and-false).
    - ``xanes_tddft`` (XANES core-excitation TD-DFT single point): ``%tddft nroots
      N[ tda ..] MaxIter K orbwin[0] = a, b, c, d end`` where ``step["orb_win"]``
      is the four-int core-orbital donor window (first-occ, last-occ, first-virt,
      last-virt; ``-1`` = auto) resolved by the orca-xanes skill.

    Any excited-state step may carry a ``tda`` flag; because ``False`` is falsy it
    is keyed on *presence*, so an explicit ``tda: False`` still emits ``tda false``
    (full linear-response TD-DFT rather than ORCA's default Tamm-Dancoff).

    Steps that need no extra block return an empty list.
    """
    calc_type = step.get("calc_type")

    # ``tda false`` (full linear-response TD-DFT) vs ORCA's default ``tda true``.
    # A ``tda`` key in the step is authoritative; because ``False`` is falsy we key
    # on presence, not truthiness, so an explicit ``tda: False`` still emits.
    def _tda_frag():
        if "tda" in step:
            return f" tda {'true' if step['tda'] else 'false'}"
        return ""

    if calc_type in ("opt", "opt_freq"):
        return ["%geom MaxIter 300 cartfallback true end"]

    if calc_type == "numfreq_raman":
        return ["%elprop Polar 1 end"]

    if calc_type == "uvvis_tddft":
        nroots = _require_pos_int(step, "nroots", calc_type)
        maxdim = _require_pos_int(step, "maxdim", calc_type)
        return [f"%tddft nroots {nroots}{_tda_frag()} maxdim {maxdim} end"]

    if calc_type == "nmr":
        # Only computational-control blocks here; the property-definition
        # %eprnmr block goes AFTER the coordinate line (ORCA requirement) and
        # is emitted by _post_coord_blocks below. ``scf_tight`` is retained as
        # the legacy spelling for an explicit 1e-8 threshold.
        threshold = step.get("scf_thresh")
        if threshold is None and step.get("scf_tight"):
            threshold = "1e-8"
        if threshold is None:
            return []
        threshold = str(threshold)
        if threshold not in ("1e-8", "1e-7"):
            raise ValueError(
                "nmr scf_thresh must be one of '1e-8' or '1e-7', "
                f"got {threshold!r}"
            )
        return [f"%scf Thresh {threshold} MaxIter 500 end"]

    if calc_type == "fluo_es_opt":
        nroots = _require_pos_int(step, "nroots", calc_type)
        iroot = _require_pos_int(step, "iroot", calc_type)
        return [
            "%geom MaxIter 300 cartfallback true end",
            "%scf MaxIter 300 AutoTraH false end",
            f"%tddft nroots {nroots} iroot {iroot}{_tda_frag()} end",
        ]

    if calc_type == "fluo_emission":
        nroots = _require_pos_int(step, "nroots", calc_type)
        iroot = _require_pos_int(step, "iroot", calc_type)
        cpcmeq = " cpcmeq false" if ("cpcmeq" in step and not step["cpcmeq"]) else ""
        return [f"%tddft nroots {nroots} iroot {iroot}{_tda_frag()}{cpcmeq} end"]

    if calc_type == "xanes_tddft":
        nroots = _require_pos_int(step, "nroots", calc_type)
        maxiter = _require_pos_int(step, "tddft_maxiter", calc_type)
        orb_win = step.get("orb_win")
        if (not isinstance(orb_win, (list, tuple)) or len(orb_win) != 4
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in orb_win)):
            raise ValueError(
                "xanes_tddft step requires 'orb_win' as a list/tuple of 4 ints "
                f"(first-occ, last-occ, first-virt, last-virt), got {orb_win!r}"
            )
        window = ", ".join(str(x) for x in orb_win)
        return [
            f"%tddft nroots {nroots}{_tda_frag()} MaxIter {maxiter} "
            f"orbwin[0] = {window} end"
        ]
    return []


def _post_coord_blocks(step):
    """Return ``%``-block lines that ORCA requires AFTER the coordinate line.

    ORCA's ``%eprnmr`` property-definition block must appear after the ``* xyzfile``
    coordinate specification (unlike computational-control blocks such as ``%scf``,
    ``%tddft``, ``%geom`` which go before). This function emits blocks in that
    post-coordinate position; all other calc_types return an empty list.
    """
    calc_type = step.get("calc_type")
    if calc_type == "nmr":
        nuclei = _nmr_nuclei(step)
        if len(nuclei) == 1:
            # Single element: compact one-liner (byte-identical to the pre-merge
            # form the single-spectrum golden tests assert).
            return [f"%eprnmr NUCLEI = ALL {nuclei[0]} {{SHIFT}} end"]
        # Several elements (e.g. H + C requested for one molecule): ORCA
        # computes every element's shielding in ONE job, printing them
        # successively in the CHEMICAL SHIELDING SUMMARY. One %eprnmr block,
        # one NUCLEI line per element.
        lines = ["%eprnmr"]
        lines.extend(f"  NUCLEI = ALL {el} {{SHIFT}}" for el in nuclei)
        lines.append("end")
        return ["\n".join(lines)]
    return []


def build_inp(params, xyz_filename, charge=0, multiplicity=1,
              nprocs=DEFAULT_NPROCS, maxcore_mb=DEFAULT_MEMORY_MB_PER_CORE):
    """Assemble ORCA ``.inp`` text from a params dict.

    Steps run in sorted key order (``step1``, ``step2``, ...) and are chained in a
    single input file with ``$new_job`` separators. The first step names the
    initial geometry via ``* xyzfile <charge> <mult> <xyz_filename>`` (the caller
    uploads that XYZ into the same remote work dir as the ``.inp``); every later
    step omits the filename (``* xyzfile <charge> <mult>``) so ORCA feeds it the
    previous job's optimized geometry. A single-step recipe (IR / Raman) produces
    exactly one job block and no ``$new_job`` line, byte-identical to before.

    Chemistry choices come only from ``params``; an unsupported ``calc_type``
    raises ``NotImplementedError`` (via ``_keyword_line``) rather than emitting a
    partial input.
    """
    if not isinstance(params, dict) or not params:
        raise ValueError("params is missing or empty")
    steps = [params[k] for k in sorted(params.keys())]

    job_blocks = []
    for i, step in enumerate(steps):
        lines = [
            _keyword_line(step),
            f"%pal nprocs {nprocs} end",
            f"%maxcore {maxcore_mb}",
        ]
        lines.extend(_extra_blocks(step))
        if i == 0:
            lines.append(f"* xyzfile {charge} {multiplicity} {xyz_filename}")
        else:
            # No filename: inherit the previous job's (optimized) geometry.
            lines.append(f"* xyzfile {charge} {multiplicity}")
        # Property-definition blocks that ORCA requires AFTER the coord line
        # (currently only %eprnmr for NMR shielding).
        lines.extend(_post_coord_blocks(step))
        job_blocks.append("\n".join(lines))

    return "\n\n$new_job\n\n".join(job_blocks) + "\n"


def build_submit_sh(job_name, inp_filename, out_filename,
                    orca_bin, mpi_prefix,
                    nprocs=DEFAULT_NPROCS, walltime=DEFAULT_WALLTIME,
                    maxcore_mb=DEFAULT_MEMORY_MB_PER_CORE,
                    partition=DEFAULT_PARTITION, qos=DEFAULT_QOS):
    """Return a SLURM batch script that runs ORCA on the cluster.

    ``orca_bin`` (full path to the ORCA binary) and ``mpi_prefix`` (the matching
    OpenMPI install root) are **required** and come from the runtime cluster
    profile (``common_utils.resolve_cluster``) — they are never baked in, so the
    published skill carries no cluster-specific path. Parallel ORCA needs the
    full-path binary AND a matching OpenMPI on PATH/LD_LIBRARY_PATH (never
    ``which orca``, which may resolve to an unrelated build with no matching MPI).

    The template is a verbatim-in-shape reproduction of the teacher's Part_2 spec
    (and the cluster's proven ``submit.sh``):

    * ``--partition`` / ``--qos`` / ``--nodes=1`` / ``--ntasks`` / ``--time`` only;
      it emits **no** ``--mem-per-cpu`` line — the nodes advertise no RealMemory,
      so a memory request makes the scheduler reject the job.
    * SLURM stdout/stderr go to ``%j.out`` / ``%j.err`` (job-id-scoped, distinct
      from ORCA's own ``out_filename`` which is produced by shell redirection).
    * Parallel ORCA needs a matching MPI: it exports ``OMPI_MCA_plm="^tm"`` and
      prepends ``mpi_prefix`` ``bin``/``lib`` to ``PATH`` / ``LD_LIBRARY_PATH``,
      then runs the full-path ``orca_bin`` (never ``which orca``).
    * It stages into a node-local scratch dir ``/tmp/$USER/$SLURM_JOBID``: everything
      in ``$SLURM_SUBMIT_DIR`` (``.inp`` + ``.xyz`` + any aux files) is copied in, ORCA
      runs there, all products are copied back to the submit dir, then scratch is
      removed. This keeps heavy ORCA scratch I/O off the shared NFS.

    ``maxcore_mb`` is retained in the signature for backward-compatible callers but
    is no longer emitted here (it drives ORCA's in-input ``%maxcore`` in
    ``build_inp``, not an SBATCH request).
    """
    if not orca_bin or not mpi_prefix:
        raise ValueError(
            "build_submit_sh requires orca_bin and mpi_prefix from the cluster "
            "profile (resolve_cluster); refusing to emit a submit.sh with a "
            "missing ORCA/OpenMPI path."
        )
    return (
        "#!/bin/bash\n"
        f"#SBATCH --job-name={job_name}\n"
        f"#SBATCH --partition={partition}\n"
        f"#SBATCH --qos={qos}\n"
        "#SBATCH --nodes=1\n"
        f"#SBATCH --ntasks={nprocs}\n"
        f"#SBATCH --time={walltime}\n"
        "#SBATCH --output=%j.out\n"
        "#SBATCH --error=%j.err\n"
        "\n"
        'export OMPI_MCA_plm="^tm"\n'
        f"export PATH={mpi_prefix}/bin:$PATH\n"
        f"export LD_LIBRARY_PATH={mpi_prefix}/lib:$LD_LIBRARY_PATH\n"
        f'ORCA_BIN="{orca_bin}"\n'
        f'INP_FILE="{inp_filename}"\n'
        f'OUT_FILE="{out_filename}"\n'
        "\n"
        "SUBMIT_DIR=$SLURM_SUBMIT_DIR\n"
        'LOCAL_WORK_DIR="/tmp/$USER/$SLURM_JOBID"\n'
        "mkdir -p $LOCAL_WORK_DIR\n"
        "cp -r $SUBMIT_DIR/* $LOCAL_WORK_DIR/ 2>/dev/null\n"
        "cd $LOCAL_WORK_DIR\n"
        '"$ORCA_BIN" "$INP_FILE" > "$OUT_FILE"\n'
        "cp -f $LOCAL_WORK_DIR/* $SUBMIT_DIR/ 2>/dev/null\n"
        "cd $SUBMIT_DIR\n"
        "rm -rf $LOCAL_WORK_DIR\n"
    )
