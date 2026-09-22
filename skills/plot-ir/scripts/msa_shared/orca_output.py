"""Shared ORCA .out parsing + spectrum reconstruction for Part-3 (plot / export).

This module is the *output* twin of ``orca_input.py``: where ``orca_input``
owns the grammar written *into* an ORCA job, this module owns the grammar read
*out of* the resulting ``.out``. Every Part-3 skill (plot-{spec} x6 and
export-spectrum-data) delegates all ``.out`` parsing, broadening, and axis
conventions here, so there is a single verified source of truth for what a
spectrum looks like — no skill parses ORCA text or picks an FWHM on its own.

The block layouts parsed below were verified against the ORCA 6.0 manual /
tutorials:
  * IR / Raman  vibrational tables  (5.18 / typical/properties)
  * ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS  (TD-DFT; UV-Vis,
    fluorescence, XANES) — ORCA 6.0 "Transition  Energy(eV)  Energy(cm-1)
    Wavelength(nm)  fosc(D2) ..." layout, with a fallback for the classic
    ORCA-5 "State  Energy(cm-1)  Wavelength(nm)  fosc ..." layout.
  * CHEMICAL SHIELDING SUMMARY (ppm)  (NMR tutorial)

Public high-level API — one ``build_{spec}`` per spectrum returning a uniform
dict that BOTH the plot skill and the export skill consume verbatim:

    build_ir(text)     build_raman(text)   build_uvvis(text)
    build_fluo(text)   build_nmr(text)     build_xanes(text)
    build_spectrum(spec, text)     # dispatch on target_spectra

    -> {
        "spec":         str,                            # "ir" / "raman" / ...
        "peaks":        [ {<col>: <val>, ...}, ... ],   # rows for the peaks CSV
        "peak_columns": [ "<col>", ... ],               # ordered peaks CSV header
        "x": numpy.ndarray,                             # shared x grid
        "y": numpy.ndarray,                             # normalized curve (plotted)
        "y_raw": numpy.ndarray,                         # un-normalized curve (CSV)
        "spectrum_columns": [ "<col>", ... ],           # 3-col spectrum CSV header
                                                        # ([] for NMR: no curve)
        "csv_reverse":  bool,                           # write spectrum CSV rows
                                                        # high-x-first (IR only)
        "x_label": str, "y_label": str,
        "title_template": str,                          # contains "{molecule}"
        "x_lim": (lo, hi) | None,                       # matplotlib x-axis order
        "y_lim": (lo, hi) | None,                       # matplotlib y-axis range
        "render": "line" | "stick",                     # how a plot skill draws it
        "normalized": bool,                             # was y scaled to max == 1
        "has_signal": bool,                             # False -> draw NO figure
        "message": str,                                 # why, when has_signal False
        "warnings": [ str, ... ],                       # CSV header + final report
        "meta": { str: value },                         # CSV "# key: value" block
       }

v2.1 conventions (expert feedback 2026-08-10):
  * IR / Raman broadened with an AREA-normalized LORENTZIAN (FWHM 12 / 10 cm^-1)
    on a 1 cm^-1 grid, so the raw curve carries km*mol^-1*cm / A^4*amu^-1*cm.
  * Imaginary modes (nu < 0 only, no near-zero threshold) stay in the peaks table
    but are excluded from the curve, and raise a ``warnings`` entry.
  * UV-Vis / fluorescence broadened in the WAVELENGTH (nm) domain after a Jacobi
    transform f_lambda = f * hc / lambda^2; fluorescence keeps S1 only (Kasha).
  * XANES broadened in eV on a XANES_ENERGY_STEP grid over the FIXED conventional
    window for the absorbing element (C/N/O/F), auto-fit only as a fallback.
  * The three electronic spectra ignore transitions below
    OSCILLATOR_STRENGTH_THRESHOLD; if nothing survives, ``has_signal`` is False —
    a SUCCESSFUL calculation with no figure, not an error.
  * NMR is a STICK plot (render == "stick"): equal-height sticks, y-axis hidden,
    NOT normalized; TMS reference chosen by the task's system_type.

Low-level helpers (``parse_*``, ``gaussian_broaden``, ``lorentzian_broaden``,
``make_grid``, ``normalize_curve``, ``ev_to_nm``) are exposed for tests; the two
kernels now live in ``msa_shared.broadening`` and are re-exported here so the
export layer and the plot layer share ONE implementation. This module imports
only numpy — never matplotlib — so plot skills stay the sole owners of figure
style.
"""

import re
from typing import Any, Dict, List, Tuple

import numpy as np

from msa_shared.broadening import (
    make_grid, gaussian_broaden, lorentzian_broaden,
    normalize_curve, has_valid_signal,
)
from msa_shared.constants.plot_defaults import (
    FWHM_IR, FWHM_RAMAN, FWHM_XANES,
    FWHM_UVVIS_NM, FWHM_FLUO_NM,
    IR_XLIM, RAMAN_XLIM, UVVIS_XLIM_NM, FLUO_XLIM_NM,
    NMR_XLIM_1H, NMR_XLIM_13C,
    XANES_ENERGY_RANGE, XANES_ENERGY_STEP, XANES_FALLBACK_MARGIN,
    OSCILLATOR_STRENGTH_THRESHOLD, SIGNAL_EPS, Y_LIM_TOP, NO_SIGNAL_MESSAGE,
)
from msa_shared.constants.nmr_ref import tms_reference_for, system_label as nmr_system_label

# Re-exported for tests and for callers that reach the kernels through this
# module; ``msa_shared.broadening`` remains the single implementation.
__all__ = [
    "make_grid", "gaussian_broaden", "lorentzian_broaden", "normalize_curve",
    "has_valid_signal", "ev_to_nm",
    "parse_ir", "parse_raman", "parse_absorption", "parse_nmr",
    "build_ir", "build_raman", "build_uvvis", "build_fluo", "build_nmr",
    "build_xanes", "build_spectrum", "xanes_window",
]

# Planck relation constant for eV <-> nm (h*c in eV*nm).
_EV_NM = 1239.841984

# hc used for the Jacobi transform f_lambda = f * hc / lambda^2 when converting
# an oscillator strength (per unit energy) to an intensity per unit wavelength.
# The expert-provided plot code uses the rounded value hc = 1240 eV*nm.
_HC_JACOBI = 1240.0

# A vibrational-table data row: right-justified mode index, a colon, then floats.
# The leading ``-?`` is required: imaginary modes print a negative frequency and
# must still be parsed (they are filtered later, not here).
#   "  9:   1806.49   0.020286  102.51  0.003504  ( ... )"
_MODE_ROW = re.compile(r"^\s*\d+:\s+(-?\d.*)$")

# An NMR summary row: "     1       C          184.526          7.008"
_NMR_ROW = re.compile(r"^\s*(\d+)\s+([A-Z][a-z]?)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)")


# --------------------------------------------------------------------------- #
# numeric helpers
# --------------------------------------------------------------------------- #
def ev_to_nm(ev: float) -> float:
    """Convert a photon energy (eV) to a wavelength (nm)."""
    return _EV_NM / ev if ev else float("inf")


def _jacobi(osc: float, wavelength_nm: float) -> float:
    """Oscillator strength -> intensity per unit WAVELENGTH.

    ``f`` is defined per unit energy, so a lineshape drawn against nm needs the
    Jacobian ``|dE/dlambda| = hc / lambda^2``. A zero/absent wavelength yields 0
    rather than dividing by zero.
    """
    if not wavelength_nm:
        return 0.0
    return float(osc) * _HC_JACOBI / (float(wavelength_nm) ** 2)


# --------------------------------------------------------------------------- #
# low-level block parsers
# --------------------------------------------------------------------------- #
def _iter_block_rows(text: str, title: str):
    """Yield the raw data-row strings of the LAST block whose header == ``title``.

    ORCA prints a spectrum block as a title line, a couple of header/units lines,
    a dashed separator, then data rows, ending at a blank line. Multi-step jobs
    ($new_job) print the block once per relevant step; we take the LAST occurrence
    so a downstream TD-DFT / freq step wins over an earlier one.
    """
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == title]
    if not starts:
        return
    i = starts[-1] + 1
    n = len(lines)
    seen_data = False
    while i < n:
        ln = lines[i]
        i += 1
        if _MODE_ROW.match(ln) or _NMR_ROW.match(ln):
            seen_data = True
            yield ln
            continue
        if seen_data and ln.strip() == "":
            break  # trailing blank ends the data section
        # header / units / dashed separators before data: skip


def parse_ir(text: str) -> List[Tuple[float, float]]:
    """(frequency cm^-1, IR intensity km/mol) from the ``IR SPECTRUM`` block."""
    peaks: List[Tuple[float, float]] = []
    for ln in _iter_block_rows(text, "IR SPECTRUM"):
        toks = ln.replace(":", " ").split()
        # toks: [mode, freq, eps, Int, T2, (TX, TY, TZ)]
        if len(toks) >= 4:
            try:
                peaks.append((float(toks[1]), float(toks[3])))
            except ValueError:
                continue
    return peaks


def parse_raman(text: str) -> List[Tuple[float, float]]:
    """(frequency cm^-1, Raman activity A^4/amu) from the ``RAMAN SPECTRUM`` block."""
    peaks: List[Tuple[float, float]] = []
    for ln in _iter_block_rows(text, "RAMAN SPECTRUM"):
        toks = ln.replace(":", " ").split()
        # toks: [mode, freq, activity, depolarization]
        if len(toks) >= 3:
            try:
                peaks.append((float(toks[1]), float(toks[2])))
            except ValueError:
                continue
    return peaks


def parse_absorption(text: str) -> List[Dict[str, float]]:
    """Excited-state absorption lines from the electric-dipole spectrum block.

    Returns dicts with ``energy_eV`` / ``osc_strength`` / ``wavelength_nm``.
    Handles the ORCA 6.0 layout
        Transition  Energy(eV)  Energy(cm-1)  Wavelength(nm)  fosc(D2)  D2 ...
        0-1A  ->  1-1A   9.123912  73589.3  135.9  0.430770278  ...
    and falls back to the classic ORCA-5 layout
        State  Energy(cm-1)  Wavelength(nm)  fosc  T2 ...
           1   38601.9   259.1   0.086012678  ...
    """
    title = "ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS"
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == title]
    if not starts:
        return []
    peaks: List[Dict[str, float]] = []
    i = starts[-1] + 1
    n = len(lines)
    seen_data = False
    while i < n:
        ln = lines[i]
        i += 1
        s = ln.strip()
        if "->" in ln:  # ORCA 6.0: <label> -> <label>  eV  cm-1  nm  fosc ...
            rhs = ln.split("->", 1)[1].split()
            # rhs: [final_label, eV, cm-1, nm, fosc, ...]
            if len(rhs) >= 5:
                try:
                    ev = float(rhs[1]); nm = float(rhs[3]); fosc = float(rhs[4])
                    peaks.append({"energy_eV": ev, "osc_strength": fosc, "wavelength_nm": nm})
                    seen_data = True
                    continue
                except ValueError:
                    pass
        elif re.match(r"^\s*\d+\s+\d", ln):  # ORCA 5: State cm-1 nm fosc ...
            toks = s.split()
            if len(toks) >= 4:
                try:
                    cm1 = float(toks[1]); nm = float(toks[2]); fosc = float(toks[3])
                    ev = cm1 / 8065.544
                    peaks.append({"energy_eV": ev, "osc_strength": fosc, "wavelength_nm": nm})
                    seen_data = True
                    continue
                except ValueError:
                    pass
        if seen_data and s == "":
            break
    return peaks


def parse_nmr(text: str) -> List[Dict[str, Any]]:
    """(nucleus index, element, isotropic shielding ppm) from the shielding summary."""
    out: List[Dict[str, Any]] = []
    for ln in _iter_block_rows(text, "CHEMICAL SHIELDING SUMMARY (ppm)"):
        m = _NMR_ROW.match(ln)
        if m:
            out.append({"nucleus": int(m.group(1)),
                        "element": m.group(2),
                        "shielding": float(m.group(3))})
    return out


# --------------------------------------------------------------------------- #
# high-level per-spectrum builders (peaks + curve + axis conventions)
# --------------------------------------------------------------------------- #
#
# Every builder returns the SAME dict shape, consumed verbatim by both a
# plot-{spec} skill and export-spectrum-data. See the module docstring for the
# key list. Two conventions matter to callers:
#
#   * ``title_template`` carries the literal placeholder ``{molecule}``. Callers
#     substitute with ``str.replace`` — NOT ``str.format`` — because some
#     templates contain mathtext braces (``$^{1}$H``) that ``format`` would
#     misparse as positional fields.
#   * ``has_signal`` False means the calculation completed but there is nothing
#     to draw (all modes imaginary, dark S1, all oscillator strengths below the
#     threshold). That is a SUCCESSFUL stage with an empty figure suppressed, not
#     a failure; ``message`` then carries the fixed user-facing sentence.


def _imaginary_warning(imaginary: List[float]) -> str:
    """Fixed wording for the imaginary-frequency warning (CSV header + report)."""
    listed = ", ".join(f"{f:.2f}" for f in imaginary)
    plural = "y" if len(imaginary) == 1 else "ies"
    return (f"{len(imaginary)} imaginary frequenc{plural} detected ({listed} cm-1): "
            f"the optimized geometry is not a true minimum. Imaginary modes are "
            f"excluded from the broadened spectrum but kept in the peaks table.")


def _out_of_window_warning(spec: str, lo: float, hi: float, unit: str) -> str:
    """Diagnostic for 'bright transitions exist but the plotted curve is flat'.

    The fixed no-signal message blames a zero oscillator strength, which would be
    the wrong diagnosis here: the transitions ARE bright, they simply all sit
    outside the plotted window (most often a XANES edge element that does not
    match the one the job actually excited). Emitted as an extra warning so the
    expert-specified message text stays untouched.
    """
    return (f"All {spec} transitions above the oscillator-strength threshold fall "
            f"outside the plotted window {lo:g}-{hi:g} {unit}; the broadened curve "
            f"is empty. Check that the requested edge/window matches the job.")


def _result(**kw) -> Dict[str, Any]:
    """Fill the uniform builder dict, defaulting every optional key."""
    base: Dict[str, Any] = {
        "spec": "", "peaks": [], "peak_columns": [],
        "x": np.asarray([], dtype=float), "y": np.asarray([], dtype=float),
        "y_raw": np.asarray([], dtype=float),
        "spectrum_columns": [], "csv_reverse": False,
        "x_label": "", "y_label": "", "title_template": "",
        "x_lim": None, "y_lim": None,
        "render": "line", "normalized": True,
        "has_signal": True, "message": "", "warnings": [], "meta": {},
    }
    base.update(kw)
    return base


def _vibrational(spec: str, text: str, parser, fwhm: float, x_lim,
                 x_label: str, y_label: str, title_template: str,
                 peak_columns: List[str], spectrum_columns: List[str],
                 raw_unit: str, csv_reverse: bool) -> Dict[str, Any]:
    """IR / Raman: area-normalized Lorentzian broadening on a 1 cm^-1 grid.

    Imaginary modes (nu < 0) are kept verbatim in the peaks table — it is the
    unfiltered record of what ORCA produced — but excluded from the broadened
    curve, and their count/values are reported both as a warning and in the CSV
    metadata. Only nu < 0 is dropped; near-zero real modes are NOT filtered.
    """
    peaks = parser(text)
    imaginary = [f for f, _ in peaks if f < 0]
    warnings = [_imaginary_warning(imaginary)] if imaginary else []
    real = [(f, i) for f, i in peaks if f > 0]

    grid_lo, grid_hi = min(x_lim), max(x_lim)
    x = make_grid(grid_lo, grid_hi, n=int(round(grid_hi - grid_lo)) + 1)
    y_raw = lorentzian_broaden(x, [f for f, _ in real], [i for _, i in real], fwhm)
    ok = has_valid_signal(y_raw, SIGNAL_EPS)

    return _result(
        spec=spec,
        peaks=[dict(zip(peak_columns, (i + 1, f, inten)))
               for i, (f, inten) in enumerate(peaks)],
        peak_columns=peak_columns,
        x=x, y=normalize_curve(y_raw), y_raw=y_raw,
        spectrum_columns=spectrum_columns, csv_reverse=csv_reverse,
        x_label=x_label, y_label=y_label, title_template=title_template,
        x_lim=tuple(x_lim), y_lim=(0.0, Y_LIM_TOP),
        has_signal=ok, message="" if ok else NO_SIGNAL_MESSAGE[spec],
        warnings=warnings,
        meta={
            "broadening": f"Lorentzian FWHM={fwhm:g} cm-1 (area-normalized)",
            "y_raw_unit": raw_unit,
            "y_normalized_note": "0-1 normalized, identical to plot",
            "num_peaks": len(peaks),
            "n_imaginary_freqs": len(imaginary),
            "imaginary_freqs_cm-1": ", ".join(f"{f:.4f}" for f in imaginary) or "none",
        },
    )


def build_ir(text: str, **_) -> Dict[str, Any]:
    return _vibrational(
        "ir", text, parse_ir, FWHM_IR, IR_XLIM,
        r"Wavenumber / cm$^{-1}$", "Normalized IR Intensity / a.u.",
        "IR Spectrum ({molecule})",
        ["peak_index", "frequency_cm-1", "ir_intensity_km/mol"],
        ["wavenumber_cm-1", "raw_intensity_km·mol⁻¹·cm", "normalized_intensity"],
        "km·mol⁻¹·cm (area-normalized Lorentzian)",
        csv_reverse=True,   # high wavenumber first, matching the reversed IR axis
    )


def build_raman(text: str, **_) -> Dict[str, Any]:
    return _vibrational(
        "raman", text, parse_raman, FWHM_RAMAN, RAMAN_XLIM,
        r"Raman Shift / cm$^{-1}$", "Normalized Raman Intensity / a.u.",
        "Raman Spectrum ({molecule})",
        ["peak_index", "raman_shift_cm-1", "raman_activity_A4/amu"],
        ["raman_shift_cm-1", "raw_activity_A4/amu·cm", "normalized_intensity"],
        "A^4/amu·cm (area-normalized Lorentzian)",
        csv_reverse=False,
    )


def _absorption_nm(spec: str, text: str, fwhm_nm: float, x_lim_nm,
                   y_label: str, title_template: str, peak_columns: List[str],
                   first_only: bool, count_key: str) -> Dict[str, Any]:
    """UV-Vis / fluorescence: Jacobi-transform, then Gaussian-broaden in nm.

    An ORCA oscillator strength is a per-unit-energy quantity, so plotting a
    lineshape versus wavelength needs the Jacobi factor ``f * hc / lambda^2``
    (hc = 1240 eV*nm) applied BEFORE broadening. Transitions whose oscillator
    strength is below OSCILLATOR_STRENGTH_THRESHOLD are excluded from the curve
    but kept in the peaks table.

    ``first_only`` restricts the BROADENED CURVE to the lowest-energy transition
    (S1, Kasha's rule) — the peaks table still lists every parsed state, which is
    the unfiltered record of what ORCA produced. A dark S1 therefore yields
    has_signal=False plus a full peaks table, rather than a flat line.
    """
    lines = parse_absorption(text)
    peaks = [
        dict(zip(peak_columns,
                 (i + 1, p["wavelength_nm"], p["osc_strength"],
                  _jacobi(p["osc_strength"], p["wavelength_nm"]))))
        for i, p in enumerate(lines)
    ]

    selected = lines[:1] if first_only else lines
    bright = [p for p in selected if p["osc_strength"] >= OSCILLATOR_STRENGTH_THRESHOLD]
    lo, hi = min(x_lim_nm), max(x_lim_nm)
    x = make_grid(lo, hi, n=int(round(hi - lo)) + 1)
    y_raw = gaussian_broaden(
        x,
        [p["wavelength_nm"] for p in bright],
        [_jacobi(p["osc_strength"], p["wavelength_nm"]) for p in bright],
        fwhm_nm,
    )
    in_window = has_valid_signal(y_raw, SIGNAL_EPS)
    ok = bool(bright) and in_window
    warnings = [] if (ok or not bright) else [_out_of_window_warning(spec, lo, hi, "nm")]

    meta = {
        "broadening": f"Gaussian FWHM={fwhm_nm:g} nm",
        "y_raw_unit": "arbitrary unit (Jacobi corrected, un-normalized)",
        "y_normalized_note": "0-1 normalized, identical to plot",
        "jacobi_correction": f"applied, hc/lambda^2 with hc={_HC_JACOBI:g} eV*nm",
        "oscillator_threshold": OSCILLATOR_STRENGTH_THRESHOLD,
        count_key: len(lines),
    }
    if first_only:
        meta["kasha_rule"] = ("S1 state only (Kasha's rule); the peaks table still "
                              "lists every parsed state")
        meta["s1_wavelength_nm"] = lines[0]["wavelength_nm"] if lines else ""
        meta["s1_oscillator_strength"] = lines[0]["osc_strength"] if lines else ""
        meta["s1_dark"] = not bool(bright)

    return _result(
        spec=spec, peaks=peaks, peak_columns=peak_columns,
        x=x, y=normalize_curve(y_raw), y_raw=y_raw,
        spectrum_columns=["wavelength_nm", "raw_intensity_jacobi", "normalized_intensity"],
        x_label="Wavelength / nm", y_label=y_label, title_template=title_template,
        x_lim=tuple(x_lim_nm), y_lim=(0.0, Y_LIM_TOP),
        has_signal=ok, message="" if ok else NO_SIGNAL_MESSAGE[spec],
        warnings=warnings, meta=meta,
    )


def build_uvvis(text: str, **_) -> Dict[str, Any]:
    return _absorption_nm(
        "uvvis", text, FWHM_UVVIS_NM, UVVIS_XLIM_NM,
        "Normalized Absorbance / a.u.", "UV-Vis Absorption Spectrum ({molecule})",
        ["transition_index", "wavelength_nm", "oscillator_strength", "epsilon_jacobi"],
        first_only=False, count_key="num_transitions",
    )


def build_fluo(text: str, **_) -> Dict[str, Any]:
    # Kasha's rule: emission comes from the S1 minimum, so only the lowest
    # transition of the emission TD-DFT job is broadened.
    return _absorption_nm(
        "fluo", text, FWHM_FLUO_NM, FLUO_XLIM_NM,
        "Normalized Fluorescence Intensity / a.u.",
        "Fluorescence Emission Spectrum ({molecule})",
        ["state_index", "wavelength_nm", "oscillator_strength", "intensity_jacobi"],
        first_only=True, count_key="num_states",
    )


def xanes_window(target_element, energies) -> Tuple[float, float]:
    """The (e_min, e_max) eV window for a XANES edge.

    A supported absorbing element (C/N/O/F) gets its FIXED conventional K-edge
    window so the same element is always plotted on the same axis and two
    molecules are directly comparable. Anything else falls back to an auto-fit
    window around the parsed transitions.
    """
    key = (target_element or "").strip().upper()
    if key in XANES_ENERGY_RANGE:
        return XANES_ENERGY_RANGE[key]
    if energies:
        below, above = XANES_FALLBACK_MARGIN
        # A photon energy cannot be negative, so the margin is clamped at 0.
        return (max(0.0, min(energies) - below), max(energies) + above)
    return (0.0, 1.0)


def build_xanes(text: str, target_element=None, **_) -> Dict[str, Any]:
    """XANES: core-excitation TD-DFT, Gaussian-broadened on a 0.1 eV grid.

    The energy axis is linear and carries NO Jacobi correction. Transitions below
    OSCILLATOR_STRENGTH_THRESHOLD are kept in the peaks table but excluded from
    the curve; if nothing survives, has_signal is False and no figure is drawn.
    """
    lines = parse_absorption(text)
    element = (target_element or "").strip().upper()
    peak_columns = ["transition_index", "photon_energy_eV", "oscillator_strength"]
    peaks = [dict(zip(peak_columns, (i + 1, p["energy_eV"], p["osc_strength"])))
             for i, p in enumerate(lines)]

    energies = [p["energy_eV"] for p in lines]
    e_min, e_max = xanes_window(element, energies)
    if element in XANES_ENERGY_RANGE and energies and not any(
        e_min <= energy <= e_max for energy in energies
    ):
        listed = ", ".join(f"{energy:g}" for energy in energies)
        raise ValueError(
            f"XANES transitions [{listed}] eV all fall outside the fixed "
            f"{element} window {e_min:g}-{e_max:g} eV"
        )
    n = int(round((e_max - e_min) / XANES_ENERGY_STEP)) + 1
    x = make_grid(e_min, e_max, n=n)

    bright = [p for p in lines if p["osc_strength"] >= OSCILLATOR_STRENGTH_THRESHOLD]
    y_raw = gaussian_broaden(x, [p["energy_eV"] for p in bright],
                             [p["osc_strength"] for p in bright], FWHM_XANES)
    ok = bool(bright) and has_valid_signal(y_raw, SIGNAL_EPS)
    warnings = [] if (ok or not bright) else [
        _out_of_window_warning("XANES", e_min, e_max, "eV")]

    # The edge is hard-coded to K in this version (expert-confirmed 0810): a
    # XANES task carries the absorbing ELEMENT but no edge label, so the title
    # is generated as "{element} K-edge XANES Spectrum". A future version may
    # add an OPTIONAL task field `edge` (default "K", explicit "L" for L edges)
    # that selects that edge's default energy window and FWHM; the title must
    # then REUSE that field right here -- parameter mapping only, no separate
    # title branch and no change to the plotting logic.
    edge = f"{element} K-edge " if element else ""
    return _result(
        spec="xanes", peaks=peaks, peak_columns=peak_columns,
        x=x, y=normalize_curve(y_raw), y_raw=y_raw,
        spectrum_columns=["photon_energy_eV", "raw_oscillator_strength",
                          "normalized_intensity"],
        x_label="Photon Energy / eV",
        y_label="Normalized X-ray Absorption / a.u.",
        title_template=f"{edge}XANES Spectrum ({{molecule}})",
        x_lim=(float(e_min), float(e_max)), y_lim=(0.0, Y_LIM_TOP),
        has_signal=ok, message="" if ok else NO_SIGNAL_MESSAGE["xanes"],
        warnings=warnings,
        meta={
            "broadening": f"Gaussian FWHM={FWHM_XANES:g} eV",
            "y_raw_unit": "oscillator strength (un-normalized)",
            "y_normalized_note": "0-1 normalized, identical to plot",
            "jacobi_correction": "not applied (linear energy axis)",
            "oscillator_threshold": OSCILLATOR_STRENGTH_THRESHOLD,
            "edge_element": element or "unknown",
            "energy_range_eV": f"{e_min:g}-{e_max:g}",
            "num_transitions": len(lines),
        },
    )


# NMR sticks are drawn at a fixed fraction of the (0, 1) y-axis.
_NMR_STICK_HEIGHT = 0.7

_NMR_TITLE = {
    "1H": r"$^{1}$H NMR Spectrum ({molecule})",
    "13C": r"$^{13}$C NMR Spectrum ({molecule})",
}
_NMR_XLABEL = {
    "1H": r"$^{1}$H Chemical Shift / ppm",
    "13C": r"$^{13}$C Chemical Shift / ppm",
}


def build_nmr(text: str, target_nucleus=None, system_type=None,
              basis_set=None, **_) -> Dict[str, Any]:
    """NMR: isotropic shieldings -> chemical shifts, rendered as a STICK plot.

    ``delta = sigma_ref - sigma`` with ``sigma_ref`` the TMS reference for the
    task's ``system_type`` and actual shielding ``basis_set``. ``target_nucleus``
    ("1H" / "13C") selects ONE nucleus per figure, on that nucleus's conventional
    reversed ppm window.

    The spectrum is neither broadened nor normalized: each shift is one
    equal-height stick (``render == "stick"``), ``x`` holds the positions and
    ``y`` the equal heights, so the uniform dict still applies. ``y_raw`` stays
    empty — there is no continuous curve, hence no spectrum CSV for NMR.
    """
    nucleus_to_element = {"1H": "H", "13C": "C"}
    want_el = nucleus_to_element.get(target_nucleus)
    tms = tms_reference_for(system_type, basis_set)
    ref_used = tms.get(want_el) if want_el else None

    peak_columns = ["peak_index", "atom_index", "shielding_sigma_ppm",
                    "chemical_shift_delta_ppm"]
    peaks: List[Dict[str, Any]] = []
    shifts: List[float] = []
    for s in parse_nmr(text):
        el = s["element"]
        if want_el is not None and el != want_el:
            continue  # single-nucleus plot: skip the other element
        ref = tms.get(el)
        if ref is None:
            continue
        shift = ref - s["shielding"]
        shifts.append(shift)
        peaks.append(dict(zip(peak_columns,
                              (len(peaks) + 1, s["nucleus"], s["shielding"], shift))))

    fixed_xlim = {"1H": NMR_XLIM_1H, "13C": NMR_XLIM_13C}.get(target_nucleus)
    if fixed_xlim is not None:
        x_lim = tuple(fixed_xlim)
    elif shifts:
        x_lim = (float(max(shifts) + 5.0), float(min(shifts) - 5.0))
    else:
        x_lim = (12.0, 0.0)

    x = np.asarray(shifts, dtype=float)
    return _result(
        spec="nmr", peaks=peaks, peak_columns=peak_columns,
        x=x, y=np.full(x.shape, _NMR_STICK_HEIGHT, dtype=float),
        spectrum_columns=[],  # stick spectrum: no continuous curve, no spectrum CSV
        x_label=_NMR_XLABEL.get(target_nucleus, "Chemical Shift / ppm"),
        y_label="",
        title_template=_NMR_TITLE.get(target_nucleus, "NMR Spectrum ({molecule})"),
        x_lim=x_lim, y_lim=(0.0, 1.0),
        render="stick", normalized=False,
        has_signal=bool(peaks),
        message="" if peaks else NO_SIGNAL_MESSAGE["nmr"],
        meta={
            "broadening": "none (stick spectrum)",
            "target_nucleus": target_nucleus or "",
            "system_type": nmr_system_label(system_type),
            "shielding_basis": basis_set or "legacy_default",
            "TMS_reference_sigma_ppm": ref_used if ref_used is not None else "",
            "formula": "delta = sigma_TMS - sigma_sample",
            "num_nuclei": len(peaks),
        },
    )


_BUILDERS = {
    "ir": build_ir, "raman": build_raman, "uvvis": build_uvvis,
    "fluo": build_fluo, "nmr": build_nmr, "xanes": build_xanes,
}


def build_spectrum(spec: str, text: str, **kwargs) -> Dict[str, Any]:
    """Dispatch to the per-spectrum builder for ``spec`` (raises on unknown spec).

    Extra keyword args are forwarded to the builder; each builder reads only the
    ones it needs (``target_nucleus`` / ``system_type`` / ``basis_set`` for NMR,
    ``target_element`` for XANES) and swallows the rest via ``**_``, so callers
    may pass the same kwargs uniformly regardless of spec.
    """
    key = (spec or "").strip().lower()
    if key not in _BUILDERS:
        raise NotImplementedError(f"no output builder for spec={spec!r} "
                                  f"(supported: {', '.join(sorted(_BUILDERS))})")
    return _BUILDERS[key](text, **kwargs)
