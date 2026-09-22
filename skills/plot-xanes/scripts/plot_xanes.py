#!/usr/bin/env python3
"""plot-xanes — XANES spectrum plotter (Part-3, stage_id=plot, spec=xanes).

Type-C (plot) MSA skill. For every task whose ``target_spectra == "xanes"`` it
reads the ORCA ``.out`` produced by orca-xanes, delegates ALL parsing +
broadening + axis conventions to ``msa_shared.orca_output.build_spectrum`` (the
single verified source of truth for what a spectrum looks like), draws the curve
with the shared ``msa_shared.spectrum_style`` figure, and writes both PNG and PDF.

This skill hard-codes no ORCA-output grammar, no FWHM, and no matplotlib style of
its own: parsing/broadening live in orca_output, FWHM in constants/plot_defaults,
figure style in spectrum_style. A task whose ``target_spectra`` is not xanes is
passed through UNTOUCHED so the other five plot-{spec} skills can run over the same
batch without clobbering the shared ``plot_status`` field.

Input : upstream task_list JSON (--input)
Output: task_list JSON with plot_status / plot_png / plot_pdf appended
        (plot_error on failure; plot_signal="none" + plot_message when the
        calculation converged but has nothing to draw) (--output)
"""
import os
import sys
import argparse

# ---- msa_shared bootstrap (DO NOT MODIFY) ----
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from msa_shared.common_utils import (
    load_task_list, dump_task_list,
    mark_success, mark_failed, mark_skipped, mark_no_signal, add_warning,
    upstream_ok, spectrum_kwargs,
    log_startup, read_shared_version, ensure_deps,
)
from msa_shared import naming
import msa_shared.spectrum_style as spectrum_style  # side-effect: applies rcParams
from msa_shared import orca_output

MSA_CONTRACT_VERSION = "2.0"
MSA_REQUIRES = [('numpy', 'numpy>=1.24'), ('matplotlib', 'matplotlib>=3.7')]
STAGE_ID = "plot"
UPSTREAM_STAGE = "calc"
SPEC = "xanes"
# ---- end bootstrap ----


def parse_and_plot(task: dict) -> dict:
    """Read the .out, build the spectrum, save PNG+PDF. Returns the appended fields.

    All chemistry-facing logic (which ORCA block, which columns, which FWHM, which
    axis, which title) is owned by ``orca_output.build_spectrum``; this function
    only turns the returned (x, y, labels, title_template) into a styled figure via
    ``spectrum_style``. Raises on a missing/empty .out; ``process_task`` converts
    any raise into plot_status=failed.

    "No valid signal" is NOT an error and NOT a blank figure: when the builder
    reports ``has_signal`` False the calculation converged but there is nothing to
    draw, so NO file is written and the ``_has_signal`` flag tells ``process_task``
    to record ``plot_signal="none"`` instead of a figure path.
    """
    out_file = task.get("out_file")
    if not out_file or not os.path.isfile(out_file):
        raise FileNotFoundError(f"out_file missing or not on disk: {out_file!r}")
    with open(out_file, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    # spectrum_kwargs is the single source of truth for task-field -> builder-arg
    # mapping, so this skill never hand-picks e.g. the NMR nucleus itself.
    spectrum = orca_output.build_spectrum(SPEC, text, **spectrum_kwargs(task))

    # Leading underscore = control fields consumed by process_task, never written
    # into the task_list.
    fields = {
        "_has_signal": bool(spectrum.get("has_signal", True)),
        "_message": spectrum.get("message", ""),
        "_warnings": list(spectrum.get("warnings") or []),
        "plot_png": "",
        "plot_pdf": "",
    }
    if not fields["_has_signal"]:
        return fields

    fig, ax = spectrum_style.new_figure()
    spectrum_style.render_spectrum(ax, spectrum)
    # title_name prefers the English canonical name resolved by name-to-smiles.
    spectrum_style.set_title(ax, spectrum, naming.title_name(task))

    # spec_variant keeps a second nucleus / absorption edge of the same molecule
    # from overwriting the first figure.
    base = naming.spectrum_png(
        task["molecule_name"], SPEC, variant=naming.spec_variant(task)
    ).rsplit(".", 1)[0]
    fields["plot_png"], fields["plot_pdf"] = spectrum_style.save(fig, base)
    return fields


def process_task(task: dict) -> dict:
    """Single-task orchestrator. Never raises; enforces the fail-soft contract.

    Spec routing is a *pass-through*, not a skip: a task whose ``target_spectra``
    is not xanes is returned untouched (no ``plot_status`` written) so the other
    five plot-{spec} skills can run over the same batch without clobbering this
    field. ``plot_status="skipped"`` is reserved for a xanes task whose upstream
    ``calc`` stage did not succeed; a converged calculation with nothing to draw is
    ``success`` + ``plot_signal="none"``, never ``failed``.
    """
    if task.get("target_spectra") != SPEC:
        return task  # not our spectrum; leave the task completely untouched
    if not upstream_ok(task, UPSTREAM_STAGE):
        return mark_skipped(task, STAGE_ID, UPSTREAM_STAGE)
    try:
        fields = parse_and_plot(task)
    except Exception as e:  # noqa: BLE001 — fail soft, never abort the batch
        return mark_failed(task, STAGE_ID, f"{type(e).__name__}: {e}")

    has_signal = fields.pop("_has_signal", True)
    message = fields.pop("_message", "")
    warnings = fields.pop("_warnings", [])
    if has_signal:
        mark_success(task, STAGE_ID, **fields)
    else:
        mark_no_signal(task, STAGE_ID, message, **fields)
    # Warnings ride along with BOTH outcomes (imaginary frequencies are still
    # worth reporting on a figure that was drawn).
    for w in warnings:
        add_warning(task, STAGE_ID, w)
    return task


def main() -> None:
    parser = argparse.ArgumentParser(description="plot-xanes — plot XANES spectrum")
    parser.add_argument("--input", required=True, help="input task_list JSON path")
    parser.add_argument("--output", required=True, help="output task_list JSON path")
    args = parser.parse_args()

    log_startup("plot-xanes")
    ensure_deps(MSA_REQUIRES, "plot-xanes")
    shared_v = read_shared_version()
    if shared_v != MSA_CONTRACT_VERSION:
        print(
            f"ERROR: msa_contract_version mismatch (skill={MSA_CONTRACT_VERSION}, shared={shared_v})",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        tasks = load_task_list(args.input)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: cannot load task_list from {args.input}: {e}", file=sys.stderr)
        sys.exit(1)

    tasks = [process_task(t) for t in tasks]
    dump_task_list(tasks, args.output)

    n_ok = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "success")
    n_nosig = sum(1 for t in tasks if t.get(f"{STAGE_ID}_signal") == "none")
    n_fail = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "failed")
    n_skip = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "skipped")
    print(
        f"SUCCESS: plot-xanes processed {len(tasks)} tasks "
        f"(ok={n_ok} [no_signal={n_nosig}], failed={n_fail}, skipped={n_skip}) "
        f"-> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
