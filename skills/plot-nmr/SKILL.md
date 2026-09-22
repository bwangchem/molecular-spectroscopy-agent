---
name: plot-nmr
description: NMR spectrum plotting sub-agent (Part-3, stage_id=plot, spec=nmr). For each nmr task it reads the ORCA `.out` from orca-nmr and delegates peak parsing, TMS referencing (no broadening — a stick plot) and every axis convention to msa_shared.orca_output, then writes a uniform-style PNG+PDF via msa_shared.spectrum_style. Triggered when the user asks to "plot the NMR spectrum", "draw 1H NMR", or "plot-nmr". The Agent MUST load this skill BEFORE changing any plot parameter; DO NOT define a custom matplotlib style (import msa_shared.spectrum_style); DO NOT hard-code peak parsing, the broadening kernel or the FWHM (they come from msa_shared.orca_output / constants/plot_defaults); DO NOT hand-build an output filename (use msa_shared.naming); DO NOT report "no valid signal" as a failure.
version: 0.2.0
msa_contract_version: 2.0
---

# plot-nmr

## What this skill does

NMR spectrum plotting sub-agent. For every task whose `target_spectra` is
`"nmr"` it reads the ORCA `.out` produced by orca-nmr, asks
`msa_shared.orca_output.build_spectrum` for the peak table, the equal-height stick positions
and the axis conventions, then renders it with the shared
`msa_shared.spectrum_style` figure (8.0 x 4.5 in, 300 dpi, pure black, title not
bold, left+bottom spines only, no grid, no legend) and writes both a PNG and a
PDF.

For NMR the builder applies NO broadening at all: `delta = sigma_TMS - sigma`
against the TMS reference matching both the task's `system_type` and its actual
`calc_basis_final`, rendered as equal-height STICKS on the conventional reversed
ppm window of the target nucleus (12-0 for 1H, 220-0 for 13C).

Every chemistry-facing choice is delegated, so the six plot-{spec} skills stay
identical and a parameter changes in exactly one place: the ORCA grammar and the
broadening live in `msa_shared.orca_output`, the widths and windows in
`constants/plot_defaults.py`, the figure style in `spectrum_style`, and the
output paths in `naming`. This skill hard-codes none of them.

## Agent Behavior

**CRITICAL: Always interact in the same language the user is using.**

Rules for defaults / overrides:
- Defaults: DO NOT pass FWHM / xlim; let `orca_output` read them from
  `constants/plot_defaults.py`.
- Overrides: ONLY when the user explicitly asks for a different broadening.
- NEVER silently change font size / color / DPI (they come from `spectrum_style`).

Task-source rule: accept only the task_list produced upstream by orca-nmr. A
task whose `target_spectra` is not `nmr` is passed through **untouched** so the
sibling plot-{spec} skills can run over the same batch without clobbering the
shared `plot_status` field.

Four outcomes, and only four:
- a figure was written -> `plot_status="success"` with `plot_png` / `plot_pdf`;
- the calculation converged but there is nothing to draw (the shielding summary contains no nucleus of the requested type) ->
  `plot_status="success"` **plus** `plot_signal="none"` and `plot_message`, with
  NO file written and both path fields left as `""`. This is a legitimate
  physical result, NOT a failure: reporting it as `failed` would wrongly stop the
  downstream export, which must still write the raw peaks table;
- the upstream `calc` stage did not succeed -> `plot_status="skipped"`;
- anything raised -> `plot_status="failed"` with `plot_error`; the batch keeps
  going.

## Task Field Contract

This skill sits at the **plotting** stage (stage_id=`plot`, upstream=`calc`,
spec=`nmr`).

**Consumes:**
- `target_spectra`: str, must equal `"nmr"` (otherwise the task is passed through)
- `out_file`: str, ORCA `.out` path (must exist on disk)
- `molecule_name`: str, used for the output filenames
- `molecule_name_en`: str, optional — the English canonical name resolved by
  name-to-smiles. It is what goes into the figure title; an absent or empty value
  falls back to `molecule_name`.
- `extra_params.target_nucleus`: str, `"1H"` or `"13C"` — selects the nucleus, the ppm window AND the filename variant
- `system_type`: str, picks the TMS charge regime (`neutral` / `anion_zwitterion`)
- `calc_basis_final`: str, actual shielding basis after fallback; together with
  `system_type` selects the expert-confirmed TMS reference. Legacy tasks without
  this field fall back to the historical basis for their charge regime.

**Appends (only on a nmr task this skill actually runs):**
- `plot_status`: "success" | "failed" | "skipped"
- `plot_png` / `plot_pdf`: str, local paths — `""` when there was no valid signal
- `plot_signal`: str, present ONLY as `"none"` (converged, nothing to draw)
- `plot_message`: str, the fixed user-facing sentence that accompanies
  `plot_signal="none"`
- `plot_warnings`: list[str], diagnostics attached to a figure that WAS produced
  (deduplicated; the NMR builder currently emits none, so this field is normally absent)
- `plot_error`: str (only when failed / skipped)

Rewriting any upstream field is forbidden; appending fields outside this
whitelist is forbidden; a non-nmr task must be returned unchanged.

## Output files

`{slug}_nmr_1H_spectrum.png` / `.pdf` (`_13C` for carbon), written into `local_workspace/<run_id>/<slug>/` by
`naming.spectrum_png(molecule_name, "nmr", variant=naming.spec_variant(task))`.
The filename variant is the target nucleus, so a 13C figure never overwrites the 1H figure of the same molecule.

NEVER hand-build this path: `naming` is the single source of truth, and the same
`spec_variant` value is what `export-spectrum-data` puts on the CSV.

## Scripts

`scripts/plot_nmr.py` — CLI entry point

```
python3 scripts/plot_nmr.py --input local_workspace/<run_id>/task_list.json --output local_workspace/<run_id>/task_list.json
```

## Workflow

1. Load task_list.
2. For each task: pass through if `target_spectra != "nmr"`; skip if
   `upstream_ok(task, "calc")` is false.
3. Read `out_file`; `orca_output.build_spectrum("nmr", text,
   **spectrum_kwargs(task))` -> peaks, the `(x, y)` series, axis labels/limits,
   `title_template`, `has_signal`, `message`, `warnings`.
4. If `has_signal` is false: write NO file and `mark_no_signal(task, "plot",
   message, plot_png="", plot_pdf="")`.
5. Otherwise `spectrum_style.new_figure()` -> `render_spectrum(ax, spectrum)` ->
   `set_title(ax, spectrum, naming.title_name(task))` (English name first).
6. `spectrum_style.save(fig, base)` with `base` from `naming.spectrum_png(...)`
   minus its extension -> writes PNG + PDF; then
   `mark_success(task, "plot", plot_png=..., plot_pdf=...)`.
7. Replay every builder warning through `add_warning(task, "plot", ...)`, then
   dump the task_list.

## Integration

1. task-recognition
2. name-to-smiles
3. smiles-to-xyz
4. structure-plot
5. param-recommendation
6. time-estimation
7. orca-nmr
8. **plot-nmr (this skill)**
9. export-spectrum-data

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| plot_status=failed, error="FileNotFoundError: out_file ..." | upstream `.out` path missing | re-run orca-nmr; check `out_file` |
| plot_status=success but `plot_signal="none"` and no PNG | the shielding summary contains no nucleus of the requested type | expected — read `plot_message`; the peaks CSV is still exported |
| plot_warnings is non-empty | the NMR builder emits no warnings, so this should not happen | investigate: a warning here means something outside the builder wrote it |
| figure title shows the Chinese name | `molecule_name_en` was not resolved upstream | check name-to-smiles (`smiles_warnings`); the fallback is by design |
| wrong plot font / color / DPI | `spectrum_style` not imported, or matplotlib touched directly | it is imported at the top of the script; never call `rcParams` here |
| plot_status=skipped | upstream `calc` did not succeed | fix the ORCA calculation first |
| task passed through with no plot_status | `target_spectra != "nmr"` | expected — another plot-{spec} skill owns that task |
