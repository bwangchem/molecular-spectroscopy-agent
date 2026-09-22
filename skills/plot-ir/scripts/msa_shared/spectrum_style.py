"""Shared plot style for all MSA spectrum plots — the single figure-style layer.

Every plot-{spec} skill imports this module and NEVER touches ``matplotlib.use``
or ``rcParams`` itself, so all six spectra come out of one style baseline
(``spectrum_style.pdf``, expert 2026-08-10):

    8.0 x 4.5 in, 300 dpi, Arial (falling back to DejaVu Sans / Helvetica),
    title 14 pt (not bold), axis labels 12 pt, ticks 10 pt,
    spectrum line 1.2 pt pure black, axis line 1.0 pt,
    only the left and bottom spines, ticks pointing OUT with minor ticks on,
    no grid, no legend, PNG + PDF written together.

Usage:
    import msa_shared.spectrum_style as spectrum_style
    fig, ax = spectrum_style.new_figure()
    spectrum_style.render_spectrum(ax, spectrum)          # from orca_output
    spectrum_style.set_title(ax, spectrum, molecule_name)
    spectrum_style.save(fig, "path/to/{mol}_ir_spectrum")  # writes .png + .pdf

The broadening kernels and the signal test are RE-EXPORTED here under the names
the expert specification uses (``lorentzian_broadening`` / ``gaussian_broadening``
/ ``has_valid_signal`` / ``OSCILLATOR_STRENGTH_THRESHOLD``) but are IMPLEMENTED in
``msa_shared.broadening``, which imports numpy only. That split is deliberate:
export-spectrum-data must reproduce byte-identical curves without pulling in
matplotlib, so the maths cannot live in the figure-style layer.
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from cycler import cycler

# Re-exported maths (implemented in msa_shared.broadening — see the module
# docstring). ``*_broadening`` are the expert's names; the ``*_broaden`` aliases
# are what the rest of the codebase calls.
from msa_shared import naming
from msa_shared.broadening import (  # noqa: F401  (re-export)
    gaussian_broaden, lorentzian_broaden, normalize_curve, make_grid,
    has_valid_signal,
)
from msa_shared.constants.plot_defaults import (  # noqa: F401  (re-export)
    OSCILLATOR_STRENGTH_THRESHOLD, SIGNAL_EPS, Y_LIM_TOP,
)

gaussian_broadening = gaussian_broaden
lorentzian_broadening = lorentzian_broaden

FIGSIZE = (8.0, 4.5)
DPI = 300
LINE_COLOR = "#000000"

# Font sizes (expert style baseline).
FONT_TITLE = 14
FONT_AXIS_LABEL = 12
FONT_TICK = 10

# Line widths.
LINEWIDTH_SPECTRUM = 1.2
LINEWIDTH_SPINE = 1.0
LINEWIDTH_MINOR_TICK = 0.5

# Tick lengths.
TICK_LEN_MAJOR = 4
TICK_LEN_MINOR = 2

# Arial with an explicit fallback chain; ``mathtext.fontset='dejavusans'`` keeps
# superscripts such as $^{13}$C rendering even where Arial is absent, and
# ``axes.unicode_minus`` off makes a negative tick label an ASCII hyphen so the
# PDF is copy-pasteable.
FONT_FAMILY = ["Arial", "DejaVu Sans", "Helvetica"]

# Backwards-compatible aliases for callers written against the v2.0 names.
LINE_WIDTH = LINEWIDTH_SPECTRUM
FONT_SIZE = FONT_AXIS_LABEL

_RCPARAMS_APPLIED = False


def _apply_rcparams() -> None:
    global _RCPARAMS_APPLIED
    if _RCPARAMS_APPLIED:
        return
    plt.rcParams.update({
        "figure.figsize": FIGSIZE,
        "figure.dpi": DPI,
        "savefig.dpi": DPI,
        "font.family": "sans-serif",
        "font.sans-serif": FONT_FAMILY,
        "mathtext.fontset": "dejavusans",
        "axes.unicode_minus": False,
        "font.size": FONT_AXIS_LABEL,
        "axes.titlesize": FONT_TITLE,
        "axes.labelsize": FONT_AXIS_LABEL,
        "xtick.labelsize": FONT_TICK,
        "ytick.labelsize": FONT_TICK,
        "axes.linewidth": LINEWIDTH_SPINE,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": False,
        "xtick.direction": "out",
        "ytick.direction": "out",
        "lines.linewidth": LINEWIDTH_SPECTRUM,
        "lines.color": LINE_COLOR,
        # Force the default color CYCLE to pure black so ``ax.plot(x, y)`` with no
        # explicit color draws black — ``lines.color`` alone is overridden by the
        # prop_cycle, which is why an un-styled plot would otherwise come out blue.
        "axes.prop_cycle": cycler(color=[LINE_COLOR]),
    })
    _RCPARAMS_APPLIED = True


def new_figure():
    """Create a fresh (fig, ax) with project defaults applied."""
    _apply_rcparams()
    fig, ax = plt.subplots(figsize=FIGSIZE, dpi=DPI)
    return fig, ax


def apply_standard_axis(ax) -> None:
    """Apply the shared axis rules: ticks out, left+bottom spines only, no grid.

    Called for you by ``render_spectrum``; exposed separately because the NMR
    stick plot additionally hides the left spine afterwards.
    """
    ax.tick_params(axis="both", which="major", labelsize=FONT_TICK,
                   length=TICK_LEN_MAJOR, width=LINEWIDTH_SPINE, direction="out")
    ax.tick_params(axis="both", which="minor",
                   length=TICK_LEN_MINOR, width=LINEWIDTH_MINOR_TICK, direction="out")
    ax.minorticks_on()
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_linewidth(LINEWIDTH_SPINE)
    ax.spines["bottom"].set_linewidth(LINEWIDTH_SPINE)
    ax.grid(False)


# Molecule-name capitalization for figure titles is IMPLEMENTED in
# ``msa_shared.naming`` (``display_name``) and only re-exported here under the
# name the plot layer historically used. It cannot live in this module: the
# preprocessing skill that resolves ``molecule_name_en`` must apply the very same
# rule without importing matplotlib.
format_molecule_name = naming.display_name


def set_title(ax, spectrum: dict, molecule_name: str) -> None:
    """Render ``spectrum["title_template"]`` with the molecule name substituted.

    ``str.replace`` — never ``str.format`` — because the NMR templates contain
    mathtext braces (``$^{13}$C``) that ``format`` would misparse as fields. An
    empty molecule name collapses the parenthetical rather than printing ``()``.
    """
    template = spectrum.get("title_template") or ""
    if not template:
        return
    name = format_molecule_name((molecule_name or "").strip())
    if name:
        title = template.replace("{molecule}", name)
    else:
        title = template.replace(" ({molecule})", "").replace("({molecule})", "").strip()
    ax.set_title(title, fontsize=FONT_TITLE, fontweight="normal")


def render_spectrum(ax, spectrum: dict) -> None:
    """Draw a uniform ``orca_output.build_spectrum`` dict onto ``ax``.

    This is the single place that turns the builder's ``render`` field into
    matplotlib calls so all six plot-{spec} skills stay identical:

      * ``render == "stick"`` (NMR): equal-height vertical sticks via ``vlines``
        with the y-axis fully hidden (no ticks, no label, no left spine) — the
        stick heights are arbitrary, only positions carry meaning.
      * anything else: a normal line plot.

    Axis labels, limits and the shared axis style are always applied. No legend
    is drawn: a single-series spectrum does not need one.
    """
    x, y = spectrum["x"], spectrum["y"]
    if spectrum.get("render") == "stick":
        # baseline 0 -> height y for each shift; empty x draws nothing (fine).
        ax.vlines(x, 0.0, y, linewidth=LINEWIDTH_SPECTRUM, color=LINE_COLOR)
    else:
        ax.plot(x, y, linewidth=LINEWIDTH_SPECTRUM, color=LINE_COLOR)

    ax.set_xlabel(spectrum["x_label"], fontsize=FONT_AXIS_LABEL)
    ax.set_ylabel(spectrum.get("y_label", ""), fontsize=FONT_AXIS_LABEL)
    if spectrum.get("x_lim"):
        ax.set_xlim(*spectrum["x_lim"])
    if spectrum.get("y_lim"):
        ax.set_ylim(*spectrum["y_lim"])

    apply_standard_axis(ax)
    if spectrum.get("render") == "stick":
        # The stick height is arbitrary, so a y scale would be misleading.
        ax.set_yticks([])
        ax.tick_params(axis="y", which="both", left=False)
        ax.spines["left"].set_visible(False)


def save(fig, path_no_ext: str) -> tuple:
    """Save a figure to both PNG and PDF, close it, and return the two paths."""
    png = f"{path_no_ext}.png"
    pdf = f"{path_no_ext}.pdf"
    fig.tight_layout()
    fig.savefig(png, dpi=DPI, bbox_inches="tight")
    fig.savefig(pdf, bbox_inches="tight")
    plt.close(fig)
    return png, pdf


# Auto-apply on import so a plot skill only needs to import this module.
_apply_rcparams()
