"""Functional / basis-set recipes and system classification — single source of truth.

PIPELINE.md §4.1. param-recommendation (P1-5) turns a task into a multi-step ORCA
``params`` recipe by calling :func:`recipe_for`; the orca-{spec} skills (P2) read
every chemistry choice (functional / dispersion / basis_set / zora / ri /
auxiliary_basis / decontract_aux / keepdens / tightscf / solvent_model + the
excited-state block controls) straight out of ``task["params"][stepN]``. No other
skill may define these values locally, and this module hard-codes no ORCA *grammar*
(that lives in ``msa_shared/orca_input.py``): it only chooses chemistry.

Provenance (v2.0)
-----------------
The recipes reproduce, verbatim in chemistry, the team's calibrated master input
spec ``INP_setup_for_all`` (2026-07-27) across all six spectra × three system
classes. The keyword-assembly order follows the ``重要修改`` directive:

    task -> functional -> dispersion -> basis -> ZORA -> RI -> aux
         -> DecontractAux -> KeepDens -> TightSCF -> solvent

System classification (v2.0)
----------------------------
Three classes, keyed purely on formal charge (RDKit ``Chem.GetFormalCharge``):

  * ``neutral``      net charge 0, no separated formal +/- centres.  charge 0, mult 1
  * ``anion``        net charge -1 (closed-shell).                    charge -1, mult 1
  * ``zwitterion``   net charge 0 but bears separated +/- centres.    charge 0, mult 1

``anion`` and ``zwitterion`` share the same functional/basis choices (the
"augmented" variant of every recipe); they differ only in molecular charge.

ZORA is an orthogonal flag, *not* a class: it is enabled only for molecules that
contain Br (Z=35) and do not contain I (Z=53). Iodine vetoes ZORA, including for
molecules that contain both Br and I.
"""

# --------------------------------------------------------------------------- #
# System classification
# --------------------------------------------------------------------------- #
SYSTEM_TYPE_DEFAULT = "neutral"
SUPPORTED_SYSTEM_TYPES = ("neutral", "anion", "zwitterion")

# (charge, multiplicity) per class. All three baseline classes are closed-shell.
SYSTEM_CHARGE_MULT = {
    "neutral": (0, 1),
    "anion": (-1, 1),
    "zwitterion": (0, 1),
}

# Expert-confirmed relativistic rule: Br-only enables ZORA; any I vetoes it.
BR_ATOMIC_NUMBER = 35
I_ATOMIC_NUMBER = 53

# The six spectrum types the pipeline supports.
SUPPORTED_SPECTRA = ("ir", "raman", "uvvis", "nmr", "fluo", "xanes")

# Per-spectrum default implicit-solvent model (ORCA CPCM keyword). Empty string
# means gas phase. param-recommendation overrides this with the task's explicit
# solvent when one was requested.
DEFAULT_SOLVENT = {
    "ir": "",
    "raman": "",
    "uvvis": "CPCM(Cyclohexane)",
    "fluo": "CPCM(Cyclohexane)",
    "nmr": "CPCM(Chloroform)",
    "xanes": "CPCM(Cyclohexane)",
}

# Spectra whose recipes are run in implicit solvent by default; for IR / Raman a
# solvent is applied only when the task explicitly asks for one.
_SOLVATED_SPECTRA = frozenset({"uvvis", "fluo", "nmr", "xanes"})


def _has_separated_charges(mol):
    """True iff the molecule bears spatially *separated* opposite formal charges.

    A charged atom is "balanced" when at least one of its direct neighbours carries
    the opposite formal charge — i.e. the +/- pair forms a single compact,
    semipolar functional group (nitro ``N(+)-O(-)``, N-oxide, azide, amine-oxide
    such as TMAO). Such groups are chemically neutral dipoles and do NOT need the
    augmented/diffuse basis a real zwitterion requires. A genuine zwitterion
    (glycine ``NH3(+)…COO(-)``, betaine) has at least one positive centre AND one
    negative centre whose balancing counter-charge is *not* a bonded neighbour, so
    the charges are spatially separated. We key the augmented recipe on that.
    """
    def _unbalanced(atom):
        want_positive_neighbour = atom.GetFormalCharge() < 0
        for nb in atom.GetNeighbors():
            q = nb.GetFormalCharge()
            if q != 0 and (q > 0) == want_positive_neighbour:
                return False  # a bonded opposite charge balances this centre
        return True

    pos_unbalanced = any(_unbalanced(a) for a in mol.GetAtoms() if a.GetFormalCharge() > 0)
    neg_unbalanced = any(_unbalanced(a) for a in mol.GetAtoms() if a.GetFormalCharge() < 0)
    return pos_unbalanced and neg_unbalanced


def _classify_charge(mol):
    """Map an RDKit mol to one of the three system classes.

    Uses the *formal* charge (``Chem.GetFormalCharge``) for the net-charge test —
    robust where a regex over the SMILES string would misread a lone ``-`` or
    ``+``. A net-zero molecule is a zwitterion only when it carries spatially
    *separated* opposite charges (see :func:`_has_separated_charges`); compact
    semipolar groups such as nitro, N-oxide or azide — which always render with
    formal +/- in SMILES yet are chemically neutral — stay ``neutral``.
    """
    from rdkit import Chem

    net_charge = Chem.GetFormalCharge(mol)
    if net_charge == -1:
        return "anion"
    if net_charge == 0:
        return "zwitterion" if _has_separated_charges(mol) else "neutral"
    # Cations / multiply-charged anions are out of the v2.0 baseline scope; fall
    # back to the neutral recipe so the pipeline still produces a runnable input,
    # and let param-recommendation surface a warning.
    return "neutral"


def detect_system_type(smiles):
    """Classify a molecule from its SMILES.

    Returns ``(system_type, need_zora)`` where ``system_type`` is one of
    ``neutral`` / ``anion`` / ``zwitterion`` and ``need_zora`` is True only when
    the molecule contains Br and no I. Raises ``ValueError`` if RDKit cannot parse
    the SMILES so the caller can fail soft with a clear message.
    """
    from rdkit import Chem

    if not smiles or not str(smiles).strip():
        raise ValueError("detect_system_type requires a non-empty SMILES")
    mol = Chem.MolFromSmiles(str(smiles).strip())
    if mol is None:
        raise ValueError(f"RDKit could not parse SMILES {smiles!r}")

    system_type = _classify_charge(mol)
    atomic_numbers = {a.GetAtomicNum() for a in mol.GetAtoms()}
    need_zora = BR_ATOMIC_NUMBER in atomic_numbers and I_ATOMIC_NUMBER not in atomic_numbers
    return system_type, need_zora


def _contains_atomic_number(smiles, atomic_number):
    """Return whether a parseable SMILES contains the requested element."""
    from rdkit import Chem

    mol = Chem.MolFromSmiles(str(smiles or "").strip())
    if mol is None:
        raise ValueError(f"RDKit could not parse SMILES {smiles!r}")
    return any(atom.GetAtomicNum() == atomic_number for atom in mol.GetAtoms())


def contains_iodine(smiles):
    """Return whether a parseable SMILES contains iodine."""
    return _contains_atomic_number(smiles, I_ATOMIC_NUMBER)


def contains_bromine(smiles):
    """Return whether a parseable SMILES contains bromine."""
    return _contains_atomic_number(smiles, BR_ATOMIC_NUMBER)


# --------------------------------------------------------------------------- #
# Excited-state / block control constants (chemistry choices)
# --------------------------------------------------------------------------- #
UVVIS_NROOTS = 30
UVVIS_MAXDIM = 60          # Davidson subspace ~1.5-2x nroots (INP_setup note)
FLUO_NROOTS = 5
FLUO_IROOT = 1
XANES_NROOTS = 50
XANES_TDDFT_MAXITER = 500
TDDFT_TDA = False          # full linear-response TD-DFT (not Tamm-Dancoff)

# Common DFT acceleration used by every hybrid-functional recipe (UV-Vis / NMR /
# fluorescence / XANES). The composite vibrational recipes (r2SCAN-3c / r2SCAN)
# carry neither.
_RIJCOSX = "RIJCOSX"
_AUTOAUX = "AutoAux"


# --------------------------------------------------------------------------- #
# Recipe assembly
# --------------------------------------------------------------------------- #
def _step(calc_type, functional, *, dispersion=None, basis_set=None,
          ri_acceleration=None, auxiliary_basis=None, decontract_aux=False,
          keepdens=False, tightscf=True, solvent_model=None, zora=False,
          **blocks):
    """Build one params step with the full v2.0 contract-field set.

    ``blocks`` carries the excited-state / method-block controls a given
    ``calc_type`` needs (nroots, iroot, tda, maxdim, nmr_nucleus, scf_tight,
    cpcmeq, tddft_maxiter, orb_win); orca_input reads them in ``_extra_blocks``.
    """
    step = {
        "calc_type": calc_type,
        "functional": functional,
        "dispersion": dispersion,
        "basis_set": basis_set,
        "zora": zora,
        "ri_acceleration": ri_acceleration,
        "auxiliary_basis": auxiliary_basis,
        "decontract_aux": decontract_aux,
        "keepdens": keepdens,
        "tightscf": tightscf,
        "solvent_model": solvent_model,
    }
    step.update(blocks)
    return step


def _is_ionic(system_type):
    """anion and zwitterion share the augmented functional/basis choices."""
    return system_type in ("anion", "zwitterion")


def _nucleus_letter(target_nucleus):
    """Map a task nucleus label (``1H`` / ``13C`` / ``H`` / ``C``) to ORCA's
    element letter for the ``%eprnmr NUCLEI = ALL <X> {SHIFT}`` block."""
    s = (target_nucleus or "").strip().upper()
    if s in ("13C", "C"):
        return "C"
    # Default to proton NMR when unspecified or explicitly 1H.
    return "H"


def recipe_for(target_spectra, system_type="neutral", need_zora=False,
               solvent_model=None, target_nucleus=None, contains_iodine=False):
    """Return the per-step ORCA recipe for one spectrum + system class.

    ``system_type`` selects neutral vs augmented (anion/zwitterion) chemistry;
    ``need_zora`` stamps ``ZORA`` on every step; ``solvent_model`` is the resolved
    CPCM keyword (task override or :data:`DEFAULT_SOLVENT`) already chosen by
    param-recommendation; ``target_nucleus`` picks the NMR nucleus;
    ``contains_iodine`` selects the expert-confirmed def2 NMR recipes. Returns a fresh
    ``dict`` mapping ``step1``/``step2``/... to step dicts, or ``None`` for an
    unknown spectrum so the caller can fail soft.
    """
    spec = (target_spectra or "").strip().lower()
    if spec not in SUPPORTED_SPECTRA:
        return None
    if system_type not in SUPPORTED_SYSTEM_TYPES:
        system_type = SYSTEM_TYPE_DEFAULT
    ionic = _is_ionic(system_type)

    # Resolve the solvent that will be stamped onto solvated steps. IR / Raman are
    # gas-phase unless the task explicitly asked for a solvent.
    solv = solvent_model if solvent_model else None
    if spec not in _SOLVATED_SPECTRA and not solvent_model:
        solv = None

    z = bool(need_zora)

    if spec == "ir":
        if ionic:
            return {"step1": _step("opt_freq", "r2SCAN", dispersion="D4",
                                   basis_set="ma-def2-SVP", zora=z,
                                   solvent_model=solv)}
        return {"step1": _step("opt_freq", "r2SCAN-3c", zora=z,
                               solvent_model=solv)}

    if spec == "raman":
        if ionic:
            return {
                "step1": _step("opt_freq", "r2SCAN", dispersion="D4",
                               basis_set="ma-def2-SVP", zora=z, solvent_model=solv),
                "step2": _step("numfreq_raman", "r2SCAN", dispersion="D4",
                               basis_set="ma-def2-TZVP", zora=z, solvent_model=solv),
            }
        return {
            "step1": _step("opt_freq", "r2SCAN-3c", zora=z, solvent_model=solv),
            "step2": _step("numfreq_raman", "r2SCAN-3c", zora=z, solvent_model=solv),
        }

    if spec == "uvvis":
        basis = "ma-def2-TZVP" if ionic else "def2-TZVP"
        return {
            "step1": _step("opt_freq", "CAM-B3LYP", basis_set=basis,
                           ri_acceleration=_RIJCOSX, auxiliary_basis=_AUTOAUX,
                           zora=z, solvent_model=solv),
            "step2": _step("uvvis_tddft", "CAM-B3LYP", basis_set=basis,
                           ri_acceleration=_RIJCOSX, auxiliary_basis=_AUTOAUX,
                           zora=z, solvent_model=solv,
                           nroots=UVVIS_NROOTS, tda=TDDFT_TDA, maxdim=UVVIS_MAXDIM),
        }

    if spec == "nmr":
        nucleus = _nucleus_letter(target_nucleus)
        if contains_iodine:
            # Two-tier def2 recipe: the geometry step stays at SVP and only the
            # shielding step goes to TZVP, matching the archived .inp files. The
            # TMS reference is indexed by the *shielding* basis, so it is the
            # step2 value that must stay def2-TZVP / ma-def2-TZVP.
            opt_basis = "ma-def2-SVP" if ionic else "def2-SVP"
            nmr_basis = "ma-def2-TZVP" if ionic else "def2-TZVP"
        elif ionic:
            opt_basis = "aug-pcSseg-1"
            nmr_basis = "aug-pcSseg-1"
        else:
            opt_basis = "pcSseg-1"
            nmr_basis = "pcSseg-2"
        return {
            "step1": _step("opt", "PBE0", dispersion="D4", basis_set=opt_basis,
                           ri_acceleration=_RIJCOSX, auxiliary_basis=_AUTOAUX,
                           zora=z, solvent_model=solv),
            "step2": _step("nmr", "PBE0", dispersion="D4", basis_set=nmr_basis,
                           ri_acceleration=_RIJCOSX, auxiliary_basis=_AUTOAUX,
                           zora=z, solvent_model=solv,
                           nmr_nucleus=nucleus, scf_tight=ionic,
                           scf_thresh="1e-8" if ionic else None),
        }

    if spec == "fluo":
        svp = "ma-def2-SVP" if ionic else "def2-SVP"
        tzvp = "ma-def2-TZVP" if ionic else "def2-TZVP"
        return {
            "step1": _step("opt", "wB97X-D3", basis_set=svp,
                           ri_acceleration=_RIJCOSX, auxiliary_basis=_AUTOAUX,
                           zora=z, solvent_model=solv),
            "step2": _step("fluo_es_opt", "wB97X-D3", basis_set=svp,
                           ri_acceleration=_RIJCOSX, auxiliary_basis=_AUTOAUX,
                           zora=z, solvent_model=solv,
                           nroots=FLUO_NROOTS, iroot=FLUO_IROOT, tda=TDDFT_TDA),
            "step3": _step("fluo_emission", "wB97X-D3", basis_set=tzvp,
                           ri_acceleration=_RIJCOSX, auxiliary_basis=_AUTOAUX,
                           zora=z, solvent_model=solv,
                           nroots=FLUO_NROOTS, iroot=FLUO_IROOT, tda=TDDFT_TDA,
                           cpcmeq=False),
        }

    if spec == "xanes":
        svp = "ma-def2-SVP" if ionic else "def2-SVP"
        tzvp = "ma-def2-TZVP" if ionic else "def2-TZVP"
        return {
            "step1": _step("opt", "PBE0", dispersion="D4", basis_set=svp,
                           ri_acceleration=_RIJCOSX, auxiliary_basis=_AUTOAUX,
                           zora=z, solvent_model=solv),
            "step2": _step("xanes_sp", "PBE0", dispersion="D4", basis_set=tzvp,
                           ri_acceleration=_RIJCOSX, auxiliary_basis=_AUTOAUX,
                           decontract_aux=True, keepdens=True,
                           zora=z, solvent_model=solv),
            "step3": _step("xanes_tddft", "PBE0", dispersion="D4", basis_set=tzvp,
                           ri_acceleration=_RIJCOSX, auxiliary_basis=_AUTOAUX,
                           decontract_aux=True, keepdens=True,
                           zora=z, solvent_model=solv,
                           nroots=XANES_NROOTS, tda=TDDFT_TDA,
                           tddft_maxiter=XANES_TDDFT_MAXITER, orb_win=None),
        }

    return None


def fallback_recipes_for(target_spectra, system_type="neutral", need_zora=False,
                         solvent_model=None, target_nucleus=None,
                         contains_iodine=False):
    """Return the ordered, bounded fallback recipes owned by P1-5.

    Every item contains a complete replacement ``params`` object. P2 consumes
    these entries in order and never derives chemistry choices locally.
    """
    spec = (target_spectra or "").strip().lower()
    baseline = recipe_for(
        spec,
        system_type=system_type,
        need_zora=need_zora,
        solvent_model=solvent_model,
        target_nucleus=target_nucleus,
        contains_iodine=contains_iodine,
    )
    if baseline is None or spec == "ir":
        return []

    ionic = _is_ionic(system_type)

    def basis_fallback(changed_steps, basis_set, params=None):
        replacement = params or recipe_for(
            spec,
            system_type=system_type,
            need_zora=need_zora,
            solvent_model=solvent_model,
            target_nucleus=target_nucleus,
            contains_iodine=contains_iodine,
        )
        for step_name in changed_steps:
            replacement[step_name]["basis_set"] = basis_set
        return {
            "fallback_kind": "basis",
            "changed_steps": list(changed_steps),
            "params": replacement,
        }

    if spec == "raman" and ionic:
        return [basis_fallback(["step2"], "ma-def2-SVP")]

    if spec == "uvvis" and ionic:
        return [basis_fallback(["step1", "step2"], "ma-def2-SVP")]

    if spec == "fluo" and ionic:
        return [basis_fallback(["step3"], "ma-def2-SVP")]

    if spec == "xanes":
        basis = "ma-def2-SVP" if ionic else "def2-SVP"
        return [basis_fallback(["step2", "step3"], basis)]

    if spec == "nmr" and ionic:
        threshold_params = recipe_for(
            spec,
            system_type=system_type,
            need_zora=need_zora,
            solvent_model=solvent_model,
            target_nucleus=target_nucleus,
            contains_iodine=contains_iodine,
        )
        threshold_params["step2"]["scf_thresh"] = "1e-7"
        fallbacks = [{
            "fallback_kind": "scf_threshold",
            "changed_steps": ["step2"],
            "params": threshold_params,
        }]
        if contains_iodine:
            basis_params = recipe_for(
                spec,
                system_type=system_type,
                need_zora=need_zora,
                solvent_model=solvent_model,
                target_nucleus=target_nucleus,
                contains_iodine=True,
            )
            basis_params["step2"]["scf_thresh"] = "1e-7"
            fallbacks.append(basis_fallback(["step2"], "ma-def2-SVP", basis_params))
        return fallbacks

    return []
