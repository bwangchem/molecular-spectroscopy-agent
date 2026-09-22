"""Default plotting parameters. Applied by plot-{spec} skills AND by
export-spectrum-data, so a CSV and its PNG always describe the same curve.

v2.1 re-baseline (expert feedback 2026-08-10):
  * IR / Raman are broadened with an AREA-NORMALIZED LORENTZIAN (was Gaussian).
    FWHM stays 12 / 10 cm^-1. Because the kernel integrates to 1, the raw curve
    carries the peak unit multiplied by the x unit (km*mol^-1*cm for IR,
    A^4/amu*cm for Raman); the plotted curve is that raw curve normalized to 1.
  * UV-Vis / fluorescence / XANES keep the PEAK-HEIGHT GAUSSIAN — the first two
    broadened in the wavelength (nm) domain, XANES in eV.
  * The three electronic spectra share one zero-intensity cutoff,
    ``OSCILLATOR_STRENGTH_THRESHOLD``; a spectrum with nothing above it yields a
    "no valid signal" result (see NO_SIGNAL_MESSAGE) instead of a blank figure.
  * XANES uses a FIXED per-element energy window (XANES_ENERGY_RANGE) at a
    0.1 eV step; the auto-fit window is only a fallback for unlisted elements.
  * NMR stays a STICK plot: no broadening, no normalization.
"""

# --------------------------------------------------------------------------- #
# broadening widths (full width at half maximum)
# --------------------------------------------------------------------------- #
FWHM_IR = 12.0        # cm^-1, Lorentzian
FWHM_RAMAN = 10.0     # cm^-1, Lorentzian
FWHM_UVVIS_NM = 20.0  # nm,    Gaussian on the wavelength axis
FWHM_FLUO_NM = 25.0   # nm,    Gaussian on the wavelength axis
FWHM_XANES = 1.0      # eV,    Gaussian on the energy axis

# --------------------------------------------------------------------------- #
# signal thresholds
# --------------------------------------------------------------------------- #
# Oscillator strengths below this count as zero (dark state) for ALL THREE
# electronic spectra (UV-Vis / fluorescence / XANES). Such transitions stay in
# the peaks CSV but are excluded from broadening.
OSCILLATOR_STRENGTH_THRESHOLD = 1e-6

# A broadened curve whose maximum does not exceed this is treated as all-zero
# (guards against float noise before deciding "no valid signal").
SIGNAL_EPS = 1e-12

# --------------------------------------------------------------------------- #
# axis conventions
# --------------------------------------------------------------------------- #
IR_XLIM = (4000, 400)       # cm^-1, reversed (high to low)
RAMAN_XLIM = (0, 4000)      # cm^-1
UVVIS_XLIM_NM = (200, 800)  # nm
FLUO_XLIM_NM = (300, 700)   # nm — near-UV to visible emission window

# NMR chemical-shift windows (ppm), reversed so shift decreases left-to-right
# (low field on the left) per NMR convention. Keyed by target nucleus.
NMR_XLIM_1H = (12, 0)
NMR_XLIM_13C = (220, 0)

# XANES: fixed K-edge windows per absorbing element (eV). Unlisted elements fall
# back to [min(E) - 5, max(E) + 10].
#
# Keyed by ELEMENT ALONE because this version hard-codes the K edge. A future
# version may add an optional `edge` task field (default "K", explicit "L"); the
# window would then be keyed by element TOGETHER WITH edge and carry that edge's
# own FWHM alongside FWHM_XANES above. Nothing else changes -- the title reuses
# the same field (see build_xanes in orca_output.py), so this stays a pure
# parameter mapping.
XANES_ENERGY_RANGE = {
    "C": (275.0, 320.0),
    "N": (390.0, 430.0),
    "O": (520.0, 560.0),
    "F": (675.0, 720.0),
}
XANES_ENERGY_STEP = 0.1              # eV per grid point
XANES_FALLBACK_MARGIN = (5.0, 10.0)  # (below min, above max) for unlisted elements

# Headroom above the normalized maximum on every non-stick spectrum.
Y_LIM_TOP = 1.1

# --------------------------------------------------------------------------- #
# CSV precision (export-spectrum-data)
# --------------------------------------------------------------------------- #
# The peaks table is written UNFORMATTED (raw parsed values). Only the broadened
# curve is rounded: x to the decimals below, both y columns to Y_DECIMALS.
X_DECIMALS = {"ir": 0, "raman": 0, "uvvis": 0, "fluo": 0, "xanes": 1}
Y_DECIMALS = 6

# --------------------------------------------------------------------------- #
# fixed result messages (expert-specified wording; do not paraphrase)
# --------------------------------------------------------------------------- #
NO_SIGNAL_MESSAGE = {
    "ir": ("No real vibrational modes found (all frequencies are imaginary or "
           "absent). Spectrum plot not generated."),
    "raman": ("No real vibrational modes found (all frequencies are imaginary or "
              "absent). Spectrum plot not generated."),
    "uvvis": ("No valid UV-Vis transitions with non-zero oscillator strength in "
              "200-800 nm range. Spectrum plot not generated."),
    "fluo": ("S1 state is dark (oscillator strength < 1e-6), no valid fluorescence "
             "signal under Kasha's rule. Spectrum plot not generated."),
    "xanes": ("No valid XANES transitions with non-zero oscillator strength found. "
              "Spectrum plot not generated."),
    "nmr": ("No nuclei of the requested type found in the chemical shielding "
            "summary. Spectrum plot not generated."),
}
