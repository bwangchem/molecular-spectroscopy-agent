"""TMS reference isotropic shieldings for NMR chemical-shift conversion.

A computed NMR chemical shift is ``delta = sigma_ref - sigma_sample`` where
``sigma_ref`` is the isotropic shielding of the reference nucleus (TMS for 1H /
13C) computed *at the same level of theory* as the sample.

TMS must be referenced at the same charge regime and shielding basis as the
sample. The chemistry expert supplied five supported pairs. New callers pass both
``system_type`` and ``basis_set``; a missing basis keeps the historical two-regime
selection for legacy task_lists.
"""

# system regime -> shielding basis -> {element -> isotropic shielding (ppm)}.
# Only expert-confirmed combinations are present.
TMS_ISOTROPIC_SHIELDING_BY_SYSTEM = {
    "neutral": {
        "pcSseg-2": {"H": 31.36, "C": 186.11},
        "def2-TZVP": {"H": 31.44, "C": 187.51},
    },
    "anion_zwitterion": {
        "aug-pcSseg-1": {"H": 31.32, "C": 186.58},
        "ma-def2-TZVP": {"H": 31.44, "C": 187.39},
        "ma-def2-SVP": {"H": 31.32, "C": 195.48},
    },
}

_LEGACY_DEFAULT_BASIS = {
    "neutral": "pcSseg-2",
    "anion_zwitterion": "aug-pcSseg-1",
}

# Backward-compatible neutral default for legacy imports.
TMS_ISOTROPIC_SHIELDING = TMS_ISOTROPIC_SHIELDING_BY_SYSTEM["neutral"]["pcSseg-2"]


def system_label(system_type=None) -> str:
    """The canonical regime key ("neutral" / "anion_zwitterion") for a system_type.

    Single source of truth for the anion/zwitterion mapping, so metadata writers
    can report WHICH reference set was applied without re-implementing the rule.
    """
    key = (system_type or "").strip().lower()
    if key in ("anion", "zwitterion", "anion_zwitterion"):
        return "anion_zwitterion"
    return "neutral"


def tms_reference_for(system_type=None, basis_set=None):
    """Return the expert TMS reference for ``(system_type, basis_set)``.

    Omitting ``basis_set`` selects the historical basis for that charge regime so
    old callers preserve their results. An explicit unsupported combination fails
    loudly instead of silently applying a reference computed at another level.
    """
    label = system_label(system_type)
    basis = basis_set or _LEGACY_DEFAULT_BASIS[label]
    try:
        return TMS_ISOTROPIC_SHIELDING_BY_SYSTEM[label][basis]
    except KeyError as exc:
        raise ValueError(
            f"no expert TMS reference for system_type={label!r}, basis_set={basis!r}"
        ) from exc
