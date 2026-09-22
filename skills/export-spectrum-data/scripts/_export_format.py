#!/usr/bin/env python3
"""_export_format — CSV layout for export-spectrum-data (v2.1).

Skill-local presentation module (like orca-xanes's ``_xanes_orbitals``). It owns
ONLY the things that are specific to writing a CSV — the metadata header lines,
which builder facts land in the peaks file vs the spectrum file, and the numeric
rounding — and delegates every chemistry decision (parsing, filtering,
broadening, axis conventions, column names, no-signal wording) to
``msa_shared.orca_output.build_spectrum`` — the SAME source of truth the plot
skills use, so a CSV and its PNG always describe identical peaks and curves.

Layout mandated by the expert's Part-3 output spec (2026-08-10):
  * Peaks CSV — every row ORCA produced, UNFILTERED and UNFORMATTED: imaginary
    frequencies and zero-oscillator-strength transitions are kept verbatim.
  * Spectrum CSV — three columns for all five continuous spectra:
    x, raw (un-normalized, physical unit), normalized (0-1, identical to the
    plotted curve). NMR is a stick spectrum and yields a peaks CSV only.
  * Rounding — peaks not formatted at all; the curve's x to X_DECIMALS[spec]
    (integers except XANES at 1 decimal) and both y columns to Y_DECIMALS (6).
  * Metadata — ``# key: value`` lines ahead of the header row, carrying the
    calculation parameters, units, source file and the imaginary-frequency /
    dark-state diagnostics.
  * No valid signal is NOT a failure: the peaks CSV is written in full and the
    spectrum CSV keeps its metadata + header row with no data rows, with
    ``# signal: none`` and the fixed message naming the reason.

Public API:
    build_artifacts(spec, text, molecule, source_file, **builder_kwargs)
        -> (artifacts, result)

``artifacts`` is a list of dicts::

    {"role": "peaks" | "spectrum",
     "meta": [(key, value), ...],   # written as leading ``# key: value`` lines
     "columns": [str, ...],         # CSV header row
     "rows": [[...], ...]}          # data rows

``result`` is the raw builder dict, so the caller can propagate ``has_signal``,
``message`` and ``warnings`` onto the task without parsing anything back out.
"""
from typing import Any, Dict, List, Tuple

from msa_shared import orca_output
from msa_shared.constants.plot_defaults import X_DECIMALS, Y_DECIMALS

# --------------------------------------------------------------------------- #
# per-spectrum presentation table
# --------------------------------------------------------------------------- #
# For each spectrum: the human-readable labels of the two files, the x unit, the
# unit lines that only make sense in the peaks file, and which builder-meta keys
# belong to which file. Everything here is CSV cosmetics; nothing is chemistry.
#
# ``spectrum_label`` may contain ``{edge}``, filled from the builder's
# ``edge_element`` meta (XANES only).
_LAYOUT: Dict[str, Dict[str, Any]] = {
    "ir": {
        "peaks_label": "IR",
        "spectrum_label": "IR (Lorentzian broadened)",
        "x_unit": "cm^-1",
        "peaks_units": [("y_unit", "km/mol (ORCA original output)")],
        "peaks_keys": ("num_peaks", "n_imaginary_freqs", "imaginary_freqs_cm-1"),
        "spectrum_keys": ("y_raw_unit", "y_normalized_note", "broadening",
                          "n_imaginary_freqs"),
    },
    "raman": {
        "peaks_label": "Raman",
        "spectrum_label": "Raman (Lorentzian broadened)",
        "x_unit": "cm^-1",
        "peaks_units": [("y_unit", "A^4/amu (ORCA original output)")],
        "peaks_keys": ("num_peaks", "n_imaginary_freqs", "imaginary_freqs_cm-1"),
        "spectrum_keys": ("y_raw_unit", "y_normalized_note", "broadening",
                          "n_imaginary_freqs"),
    },
    "uvvis": {
        "peaks_label": "UV-Vis absorption",
        "spectrum_label": "UV-Vis (Gaussian broadened)",
        "x_unit": "nm",
        "peaks_units": [("y_unit_oscillator", "dimensionless"),
                        ("y_unit_epsilon", "arbitrary unit (Jacobi corrected)")],
        "peaks_keys": ("num_transitions", "jacobi_correction"),
        "spectrum_keys": ("y_raw_unit", "y_normalized_note", "broadening",
                          "jacobi_correction", "oscillator_threshold"),
    },
    "fluo": {
        "peaks_label": "Fluorescence emission",
        "spectrum_label": "Fluorescence S1 (Gaussian broadened)",
        "x_unit": "nm",
        "peaks_units": [("y_unit_oscillator", "dimensionless"),
                        ("y_unit_intensity", "arbitrary unit (Jacobi corrected)")],
        "peaks_keys": ("num_states", "kasha_rule", "s1_wavelength_nm",
                       "s1_oscillator_strength"),
        "spectrum_keys": ("y_raw_unit", "y_normalized_note", "broadening",
                          "kasha_rule", "jacobi_correction", "oscillator_threshold",
                          "s1_dark"),
    },
    "xanes": {
        "peaks_label": "XANES {edge}",
        "spectrum_label": "XANES {edge} (Gaussian broadened)",
        "x_unit": "eV",
        "peaks_units": [("y_unit", "oscillator strength (dimensionless)")],
        "peaks_keys": ("num_transitions", "edge_element", "energy_range_eV"),
        "spectrum_keys": ("y_raw_unit", "y_normalized_note", "broadening",
                          "jacobi_correction", "oscillator_threshold",
                          "edge_element", "energy_range_eV"),
    },
    "nmr": {
        "peaks_label": "NMR {nucleus}",
        "spectrum_label": "",  # stick spectrum: no continuous-curve file
        "x_unit": "ppm",
        "peaks_units": [("y_unit", "bar plot (equal height)")],
        "peaks_keys": ("target_nucleus", "system_type", "TMS_reference_sigma_ppm",
                       "formula", "num_nuclei"),
        "spectrum_keys": (),
    },
}


def _label(template: str, meta: Dict[str, Any]) -> str:
    """Fill the ``{edge}`` / ``{nucleus}`` slot of a spectrum label."""
    edge = str(meta.get("edge_element") or "").strip()
    nucleus = str(meta.get("target_nucleus") or "").strip()
    return (template
            .replace("{edge}", f"{edge}-edge" if edge and edge != "unknown" else "")
            .replace("{nucleus}", nucleus)
            .strip())


def _selected(meta: Dict[str, Any], keys) -> List[Tuple[str, Any]]:
    """The builder-meta entries named by ``keys``, in that order, skipping absent ones."""
    return [(k, meta[k]) for k in keys if k in meta]


def _diagnostics(result: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """The ``signal`` / ``message`` / ``warning`` metadata lines.

    ``warning`` intentionally repeats when several diagnostics apply — the meta
    list is a list of pairs, not a dict, so a reader can collect every
    ``# warning:`` line back into a list.
    """
    out: List[Tuple[str, Any]] = []
    if not result.get("has_signal", True):
        out.append(("signal", "none"))
        if result.get("message"):
            out.append(("message", result["message"]))
    out.extend(("warning", w) for w in result.get("warnings") or [])
    return out


def _head(molecule: str, molecule_en, label: str, x_unit: str) -> List[Tuple[str, Any]]:
    """The leading identity lines shared by every CSV."""
    head: List[Tuple[str, Any]] = [("molecule", molecule)]
    if molecule_en and str(molecule_en).strip() and str(molecule_en) != molecule:
        head.append(("molecule_en", molecule_en))
    head.append(("spectrum", label))
    head.append(("x_unit", x_unit))
    return head


def _peak_value(v: Any) -> Any:
    """A peaks-table cell, unformatted apart from IEEE noise removal.

    The expert's spec says the peaks table is written with no formatting — the raw
    record of the run. Taken literally that also writes back the binary-float
    artifacts of a derived column: an NMR shift of 6.7922 comes out of
    ``31.36 - 24.5678`` as ``6.792200000000001``. Re-rendering at 12 significant
    digits deletes the artifact (it lives around digit 16) and changes nothing a
    quantum-chemistry output could meaningfully carry.
    """
    if isinstance(v, float):
        return float(f"{v:.12g}")
    return v


def _curve_rows(result: Dict[str, Any], spec: str) -> List[List[Any]]:
    """Three-column curve rows (x, raw, normalized) at the mandated precision.

    Both y columns are written as FIXED 6-decimal strings rather than rounded
    floats: ``round(4.1e-05, 6)`` still prints as ``4.1e-05``, which is not what
    "6 位小数" asks for and forces every consumer to parse scientific notation.
    The x column is an int where X_DECIMALS is 0 (``4000``, not ``4000.0``) and a
    fixed-decimal string otherwise (XANES, 1 decimal).

    ``csv_reverse`` (IR only) emits the highest wavenumber first so the row order
    matches the reversed IR plot axis.
    """
    x_dec = X_DECIMALS.get(spec, 1)
    rows = [
        [int(round(float(x))) if x_dec == 0 else f"{float(x):.{x_dec}f}",
         f"{float(raw):.{Y_DECIMALS}f}",
         f"{float(norm):.{Y_DECIMALS}f}"]
        for x, raw, norm in zip(result["x"], result["y_raw"], result["y"])
    ]
    if result.get("csv_reverse"):
        rows.reverse()
    return rows


def build_artifacts(spec: str, text: str, molecule: str, source_file: str,
                    molecule_en=None, calculation_meta=None,
                    **builder_kwargs) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build the CSV artifacts for ``spec`` plus the builder result behind them.

    ``builder_kwargs`` is forwarded verbatim to ``orca_output.build_spectrum``
    (``target_nucleus`` / ``system_type`` for NMR, ``target_element`` for XANES);
    each builder ignores the ones it does not need. Raises NotImplementedError on
    an unknown spectrum, which the caller turns into export_status=failed.
    """
    key = (spec or "").strip().lower()
    if key not in _LAYOUT:
        raise NotImplementedError(f"no CSV format for spec={spec!r} "
                                  f"(supported: {', '.join(sorted(_LAYOUT))})")

    result = orca_output.build_spectrum(key, text, **builder_kwargs)
    layout = _LAYOUT[key]
    meta = result["meta"]
    diagnostics = _diagnostics(result)
    calculation_meta = list(calculation_meta or [])
    tail = [("source_file", source_file)]

    peaks_meta = (_head(molecule, molecule_en, _label(layout["peaks_label"], meta),
                        layout["x_unit"])
                  + calculation_meta
                  + list(layout["peaks_units"])
                  + _selected(meta, layout["peaks_keys"])
                  + diagnostics + tail)
    artifacts = [{
        "role": "peaks",
        "meta": peaks_meta,
        "columns": result["peak_columns"],
        # Unformatted on purpose: the peaks table is the raw record of the run.
        "rows": [[_peak_value(p[c]) for c in result["peak_columns"]]
                 for p in result["peaks"]],
    }]

    if result["spectrum_columns"]:
        spectrum_meta = (_head(molecule, molecule_en,
                               _label(layout["spectrum_label"], meta),
                               layout["x_unit"])
                         + calculation_meta
                         + _selected(meta, layout["spectrum_keys"])
                         + diagnostics + tail)
        artifacts.append({
            "role": "spectrum",
            "meta": spectrum_meta,
            "columns": result["spectrum_columns"],
            # No valid signal -> metadata + header only, never a column of zeros.
            "rows": _curve_rows(result, key) if result["has_signal"] else [],
        })

    return artifacts, result
