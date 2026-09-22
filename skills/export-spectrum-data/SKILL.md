---
name: export-spectrum-data
description: Spectrum-data export sub-agent (Part-3, stage_id=export, upstream=calc). For every spectrum it reads the ORCA `.out` and writes metadata-headed CSVs — leading `# key: value` comment lines (molecule, units, broadening, normalization, TMS reference, imaginary-frequency / dark-state diagnostics) followed by the column header and rows. The CSV layout is owned by the skill-local `_export_format` module, which delegates every chemistry decision (parsing, filtering, broadening, column names, no-signal wording) to `msa_shared.orca_output.build_spectrum` — the SAME source of truth the plot-{spec} skills use. The five continuous spectra get a three-column curve CSV (x, un-normalized raw, 0-1 normalized); NMR gets a peaks-only stick CSV (delta = sigma_TMS - sigma_sample). Triggered when the user asks to "export spectrum data", "export the spectrum", or "give me the CSV". The Agent MUST load this skill BEFORE changing any CSV column name; DO NOT invent column names (they come from `orca_output`); DO NOT hand-parse the `.out` or pick an FWHM (they come from `msa_shared.orca_output` / `constants/plot_defaults`).
version: 0.2.0
msa_contract_version: 2.0
---

# export-spectrum-data

## What this skill does

Spectrum-data export sub-agent. For every task whose upstream `calc` stage
succeeded it reads the ORCA `.out` and asks the skill-local `_export_format`
module for the CSV artifacts. `_export_format` owns only CSV presentation — the
metadata header lines, which builder fact lands in which file, and the numeric
precision — and delegates every chemistry decision to
`msa_shared.orca_output.build_spectrum`: parsing, imaginary/dark-state filtering,
broadening, axis conventions, column names and the fixed no-signal wording. That
is the SAME source of truth the plot-{spec} skills use, so a CSV and its PNG
always describe identical peaks and curves.

Each CSV begins with `# key: value` metadata comment lines (molecule, units,
broadening, normalization, Jacobi note, TMS reference, calculation basis/fallback,
imaginary-frequency count, dark-state flag, source file), then the column header
row, then the data.

- **Peaks CSV** — every row ORCA produced, UNFILTERED and UNFORMATTED: imaginary
  frequencies and zero-oscillator-strength transitions are kept verbatim, because
  this file is the raw record of the run.
- **Spectrum CSV** — three columns for the five continuous spectra: x, the
  un-normalized raw curve (physical unit preserved) and the 0-1 normalized curve
  that is identical to the plotted one. NMR is a stick spectrum and has none.

Unlike the plot-{spec} family, this single skill handles all six spectra; it
branches on `target_spectra` internally rather than owning one spectrum. It
hard-codes no ORCA-output grammar, no FWHM, and no column names of its own.

## Agent Behavior

**CRITICAL: Always interact in the same language the user is using.**

Rules for defaults / overrides:
- Defaults: DO NOT pass FWHM; `orca_output` reads it from `constants/plot_defaults.py`.
- Overrides: ONLY when the user explicitly asks for a different broadening.
- NEVER silently change column names or metadata keys — column names are owned by
  `orca_output`, matching the expert output spec verbatim.

Task-source rule: a task whose upstream `calc` stage did not succeed is marked
`export_status="skipped"`. This skill exports every spectrum, so there is no
spec pass-through.

**"No valid signal" is NOT a failure.** A calculation can finish correctly and
still have nothing to broaden (all modes imaginary, a dark S1, every oscillator
strength below threshold, no nuclei of the requested type). The export then still
succeeds: the peaks CSV is written in full, the spectrum CSV keeps its metadata
and header row with no data rows, and the task carries `export_signal="none"` plus
the fixed reason in `export_message`. Report that reason to the user; do NOT
re-run the calculation on account of it.

## Task Field Contract

This skill sits at the **data export** stage (stage_id=`export`, upstream=`calc`).

**Consumes:**
- `target_spectra`: str, one of ir / raman / uvvis / fluo / nmr / xanes
- `out_file`: str, ORCA `.out` path (must exist on disk)
- `molecule_name`: str, used for the output filenames
- `molecule_name_en`: str, optional; written to the metadata header as
  `molecule_en` when it differs from `molecule_name`
- `extra_params.target_nucleus`: str, optional (NMR only); when set (`1H` / `13C`)
  the NMR export keeps only that element's nuclei and the nucleus goes into the
  filename
- `xanes_target_element`: str, optional (XANES only); selects the fixed K-edge
  energy window and goes into the filename
- `system_type`: str, optional (NMR only); selects the TMS reference regime
- `calc_basis_original`, `calc_basis_final`, `calc_basis_fallback`,
  `calc_scf_thresh`, `calc_fallback_reason`, `calc_fallback_steps`: optional calc
  audit summary; `calc_basis_final` also selects the matching basis-specific TMS
  reference through `constants/nmr_ref.tms_reference_for`

Builder arguments are never re-derived here: they come from
`common_utils.spectrum_kwargs(task)`, so this skill and the plot skills can never
disagree about, say, where the XANES edge element lives.

**Appends:**
- `export_status`: "success" | "failed" | "skipped"
- `export_signal`: "none" — present only when the run had nothing to broaden
  (status stays `success`)
- `export_message`: str — the fixed reason accompanying `export_signal="none"`
- `export_warnings`: list[str] — diagnostics that do NOT invalidate the export
  (imaginary frequencies present, transitions outside the plotted window)
- `export_error`: str (only when failed / skipped)
- `peaks_csv`: str, local peaks-CSV path
- `spectrum_csv`: str, local broadened-curve CSV path — written for every
  spectrum EXCEPT NMR (NMR is a peaks-only stick plot, so no `spectrum_csv`)

**Filenames** come from `naming.peaks_csv` / `naming.spectrum_csv` with
`variant=naming.spec_variant(task)`, so an NMR file carries its nucleus
(`benzene_nmr_1H_peaks.csv`) and a XANES file its edge element
(`benzene_xanes_C_peaks.csv`); two nuclei or two edges of the same molecule can
never overwrite each other.

**CSV column contract (owned by `orca_output`, written by `_export_format`):**

Every CSV starts with leading `# key: value` metadata comment lines, then the
column header below, then rows. All files include `basis_original`, `basis_final`,
`basis_fallback` and `fallback_reason`; ionic NMR also includes `scf_thresh`.
XANES whose fallback changed step2 but not the production step3 additionally
includes `basis_fallback_step: step2`.

| spec | peaks CSV columns | continuous-spectrum CSV columns |
|---|---|---|
| ir | `peak_index, frequency_cm-1, ir_intensity_km/mol` | `wavenumber_cm-1, raw_intensity_km·mol⁻¹·cm, normalized_intensity` |
| raman | `peak_index, raman_shift_cm-1, raman_activity_A4/amu` | `raman_shift_cm-1, raw_activity_A4/amu·cm, normalized_intensity` |
| uvvis | `transition_index, wavelength_nm, oscillator_strength, epsilon_jacobi` | `wavelength_nm, raw_intensity_jacobi, normalized_intensity` |
| fluo | `state_index, wavelength_nm, oscillator_strength, intensity_jacobi` | `wavelength_nm, raw_intensity_jacobi, normalized_intensity` |
| xanes | `transition_index, photon_energy_eV, oscillator_strength` | `photon_energy_eV, raw_oscillator_strength, normalized_intensity` |
| nmr | `peak_index, atom_index, shielding_sigma_ppm, chemical_shift_delta_ppm` | (none — peaks-only) |

**Precision** (from `constants/plot_defaults`): the peaks table is not formatted
at all; the curve's x column is an integer for IR / Raman / UV-Vis / fluorescence
and carries 1 decimal for XANES (`X_DECIMALS`), and both y columns are written as
fixed 6-decimal values (`Y_DECIMALS`). IR rows run high-wavenumber-first, matching
the reversed IR plot axis.

Rewriting any upstream field is forbidden; appending fields outside this
whitelist is forbidden.

## Scripts

`scripts/export_spectrum_data.py` — CLI entry point

```
python scripts/export_spectrum_data.py --input local_workspace/<run_id>/task_list.json --output local_workspace/<run_id>/task_list.json
```

## Workflow

1. Load task_list.
2. For each task: skip if `upstream_ok(task, "calc")` is false.
3. Read `out_file`; call `_export_format.build_artifacts(target_spectra, text,
   molecule, source_file, molecule_en=..., **spectrum_kwargs(task))` -> a tuple
   `(artifacts, result)`. Each artifact is `{"role": "peaks"|"spectrum", "meta":
   [...], "columns": [...], "rows": [...]}` (NMR returns a single peaks artifact;
   every other spectrum returns a peaks artifact and a spectrum artifact);
   `result` is the raw builder dict carrying `has_signal` / `message` /
   `warnings`. Raises `NotImplementedError` on an unknown spec.
4. For each artifact, resolve the filename via `naming.peaks_csv` /
   `naming.spectrum_csv` with `variant=naming.spec_variant(task)` and write it
   with `_write_csv` — leading `# key: value` meta lines, then the header, then
   rows.
5. `mark_success` when the builder had a signal, otherwise `mark_no_signal`
   (status still `success`, plus `export_signal="none"` and `export_message`);
   then `add_warning` for each builder warning.
6. Dump task_list.

## Integration

1. task-recognition
2. name-to-smiles
3. smiles-to-xyz
4. structure-plot
5. param-recommendation
6. time-estimation
7. orca-{spec}
8. plot-{spec}
9. **export-spectrum-data (this skill)**

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| export_status=failed, error="FileNotFoundError: out_file ..." | upstream `.out` path missing | re-run orca-{spec}; check `out_file` |
| export_status=failed, error="NotImplementedError: no CSV format ..." | `target_spectra` is not one of the six specs | fix task-recognition output |
| export_status=success but `export_signal="none"` | the run finished with nothing to broaden | read `export_message`; this is a chemistry result, not a bug |
| spectrum CSV has metadata + header but no data rows | same as above (`export_signal="none"`) | the peaks CSV still holds the full raw data |
| peaks CSV has zero rows | the `.out` lacked the expected block | check whether the upstream calc succeeded |
| `export_warnings` mentions imaginary frequencies | the optimized geometry is not a true minimum | re-optimize; the peaks table still lists the imaginary modes |
| `export_warnings` mentions transitions outside the plotted window | the requested XANES edge does not match the job | check `xanes_target_element` against the element actually excited |
| wrong CSV column names | someone bypassed `orca_output` | column names are owned by `orca_output.build_spectrum`; do not hand-write them |
| export_status=skipped | upstream `calc` did not succeed | fix the ORCA calculation first |
