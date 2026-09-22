"""Provisional walltime-estimation power laws (PIPELINE.md §4.1).

time-estimation (P1-6) reads this module to turn a task's per-step ``calc_type``
plus molecule size (``num_atoms``) into a rough seconds estimate. No skill defines
timing numbers locally; all of them live here (single source of truth, §4.1).

Provenance / honesty
--------------------
These coefficients are **NOT** fitted to real cluster benchmarks — no such
dataset exists yet. They are order-of-magnitude planning heuristics chosen so the
*relative* cost ordering is sensible:

    single-point/opt  <  opt+freq  <  numeric-freq / TD-DFT / NMR / core-XAS

Because they are unfitted, every estimate is tagged ``confidence="low"``. When
real timing logs are collected they replace ``FITTED_POWER_LAWS`` wholesale and
the confidence tags graduate; the model *shape* (a per-calc_type power law of
``num_atoms``) and the ``time_estimate`` contract fields are expected to stay
stable. Accordingly, the golden test asserts structure + monotonicity rather than
exact seconds.

Model
-----
    seconds ≈ 10**BASE_INTERCEPT * prefactor * num_atoms**exponent / cores

clamped to a floor of ``MIN_STEP_SECONDS`` so a tiny molecule still reports a
non-zero, plottable estimate.
"""

# Cores-per-job divisor for the rough walltime heuristic. Mirrors the cluster's
# default cores-per-job (msa_shared.constants.slurm.DEFAULT_NPROCS), inlined here
# so time-estimation stays self-contained and never vendors the SLURM module
# (which carries cluster credentials). Only a divisor in a provisional estimate,
# not a submit-time core count.
_DEFAULT_CORES = 16

# log10-seconds intercept for a one-atom reference calculation.
BASE_INTERCEPT = 0.518

# Floor so a trivially small system never estimates to 0 s.
MIN_STEP_SECONDS = 30

# Confidence rank, low (worst) -> high (best); used to roll a step list up to a
# single overall confidence (the batch is only as trustworthy as its worst step).
CONFIDENCE_RANK = ("low", "medium", "high")

# Per-calc_type power law. These keys are PROVISIONAL cost buckets, NOT a 1:1
# mirror of functional_basis.recipe_for: the v2.0 recipe emits
#   opt | opt_freq | numfreq_raman | uvvis_tddft | nmr | fluo_es_opt |
#   fluo_emission | xanes_sp | xanes_tddft
# and any calc_type not present as a key below resolves to DEFAULT_POWER_LAW via
# power_law_for() (never raises). Re-fitting/re-keying these buckets to the v2.0
# vocabulary is a pending chemistry-side calibration task.
# All PROVISIONAL (see module docstring).
FITTED_POWER_LAWS = {
    "opt":            {"exponent": 2.6, "prefactor": 1.0, "confidence": "low"},
    "opt+freq":       {"exponent": 3.0, "prefactor": 2.0, "confidence": "low"},
    "opt+numfreq":    {"exponent": 3.2, "prefactor": 6.0, "confidence": "low"},
    "tddft":          {"exponent": 3.0, "prefactor": 3.0, "confidence": "low"},
    "gs_opt":         {"exponent": 2.6, "prefactor": 1.0, "confidence": "low"},
    "es_opt":         {"exponent": 3.0, "prefactor": 4.0, "confidence": "low"},
    "emission_tddft": {"exponent": 3.0, "prefactor": 3.0, "confidence": "low"},
    "nmr":            {"exponent": 3.4, "prefactor": 4.0, "confidence": "low"},
    "xas_tddft":      {"exponent": 3.0, "prefactor": 5.0, "confidence": "low"},
}

# Fallback for an unrecognized calc_type (never raise on new step vocabulary).
DEFAULT_POWER_LAW = {"exponent": 3.0, "prefactor": 2.0, "confidence": "low"}


def power_law_for(calc_type):
    """Return a fresh coeff dict (exponent/prefactor/confidence) for a calc_type.

    Unknown calc_type -> a copy of ``DEFAULT_POWER_LAW``; never raises. A fresh
    dict is returned so callers cannot corrupt module state.
    """
    key = (calc_type or "").strip().lower()
    return dict(FITTED_POWER_LAWS.get(key, DEFAULT_POWER_LAW))


def estimate_step_seconds(calc_type, num_atoms, cores=_DEFAULT_CORES):
    """Rough per-step walltime in seconds. Provisional heuristic; never raises.

    Returns ``(seconds: int, confidence: str)``. A missing / non-numeric /
    non-positive ``num_atoms`` falls back to a size of 1 so the estimate is
    always defined; a missing / non-positive ``cores`` falls back to
    ``_DEFAULT_CORES``. The result is clamped to at least ``MIN_STEP_SECONDS``.
    """
    coeff = power_law_for(calc_type)
    try:
        n = float(num_atoms)
    except (TypeError, ValueError):
        n = 1.0
    if n < 1.0:
        n = 1.0
    try:
        c = float(cores)
    except (TypeError, ValueError):
        c = float(_DEFAULT_CORES)
    if c <= 0:
        c = float(_DEFAULT_CORES)

    raw = (10.0 ** BASE_INTERCEPT) * coeff["prefactor"] * (n ** coeff["exponent"]) / c
    seconds = int(max(MIN_STEP_SECONDS, round(raw)))
    return seconds, coeff["confidence"]


def combine_confidence(confidences):
    """Roll a list of per-step confidences up to a single overall value.

    Returns the *worst* (lowest-ranked) confidence in the list — a chain is only
    as trustworthy as its least-trustworthy step. An empty / all-unknown list
    yields ``"low"``.
    """
    ranks = [CONFIDENCE_RANK.index(c) for c in confidences if c in CONFIDENCE_RANK]
    if not ranks:
        return "low"
    return CONFIDENCE_RANK[min(ranks)]
