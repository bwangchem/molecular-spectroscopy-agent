"""Line-shape kernels shared by the plot and export layers. numpy only.

Both broadening kernels live here — NOT in ``spectrum_style`` — because
``export-spectrum-data`` must reproduce byte-identical curves without importing
matplotlib. ``spectrum_style`` re-exports them under the names the expert's
specification uses (``lorentzian_broadening`` / ``gaussian_broadening``), so a
plot skill and an export skill can both reach them through their own layer while
there is only ONE implementation.

Normalization conventions differ on purpose, matching the spec:

  * ``lorentzian_broaden`` is AREA-normalized: each stick contributes
    ``a * (gamma/pi) / ((x-c)^2 + gamma^2)`` with ``gamma = FWHM/2``, so
    ``integral == a``. The integrated band intensity is therefore conserved and
    the raw curve carries the peak unit multiplied by the x unit
    (km*mol^-1*cm for IR, A^4/amu*cm for Raman).
  * ``gaussian_broaden`` is PEAK-HEIGHT weighted: each stick contributes
    ``a * exp(-(x-c)^2 / 2 sigma^2)``, so the curve keeps the peak's own unit.

Both are used un-normalized for the CSV ``raw`` column; the plotted curve is the
same array divided by its maximum (``normalize_curve``).
"""

import math
from typing import Iterable

import numpy as np

_FWHM_TO_SIGMA = 1.0 / (2.0 * math.sqrt(2.0 * math.log(2.0)))


def make_grid(lo: float, hi: float, n: int = 2000) -> np.ndarray:
    """A monotonically increasing linear grid on [lo, hi] with ``n`` points."""
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        hi = lo + 1.0
    return np.linspace(lo, hi, int(n))


def gaussian_broaden(x_grid, centers: Iterable, weights: Iterable, fwhm: float) -> np.ndarray:
    """Peak-height weighted sum of Gaussians of the given FWHM, sampled on ``x_grid``.

    Each stick contributes ``weight * exp(-(x-c)^2 / 2 sigma^2)``. Empty input
    yields an all-zero curve. The result is NOT normalized.
    """
    x = np.asarray(x_grid, dtype=float)
    c = np.asarray(list(centers), dtype=float)
    w = np.asarray(list(weights), dtype=float)
    if c.size == 0:
        return np.zeros_like(x)
    sigma = max(float(fwhm) * _FWHM_TO_SIGMA, 1e-9)
    diff = x[None, :] - c[:, None]
    g = np.exp(-(diff ** 2) / (2.0 * sigma ** 2))
    return (w[:, None] * g).sum(axis=0)


def lorentzian_broaden(x_grid, centers: Iterable, weights: Iterable, fwhm: float) -> np.ndarray:
    """Area-normalized sum of Lorentzians of the given FWHM, sampled on ``x_grid``.

    Each stick contributes ``weight * (gamma/pi) / ((x-c)^2 + gamma^2)`` with
    ``gamma = fwhm/2``, so every mode's integrated intensity equals its weight.
    Empty input yields an all-zero curve. The result is NOT normalized.
    """
    x = np.asarray(x_grid, dtype=float)
    c = np.asarray(list(centers), dtype=float)
    w = np.asarray(list(weights), dtype=float)
    if c.size == 0:
        return np.zeros_like(x)
    gamma = max(float(fwhm) / 2.0, 1e-9)
    diff = x[None, :] - c[:, None]
    lor = (gamma / math.pi) / (diff ** 2 + gamma ** 2)
    return (w[:, None] * lor).sum(axis=0)


def normalize_curve(y) -> np.ndarray:
    """Scale a curve so its maximum value is 1 (no-op for an all-zero curve)."""
    y = np.asarray(y, dtype=float)
    peak = float(np.max(y)) if y.size else 0.0
    if peak > 0.0:
        return y / peak
    return y


def has_valid_signal(y_raw, threshold: float = 1e-12) -> bool:
    """True when a broadened curve carries anything above the float-noise floor."""
    y = np.asarray(y_raw, dtype=float)
    if y.size == 0:
        return False
    return bool(np.max(y) > threshold)
