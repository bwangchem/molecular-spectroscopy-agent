#!/usr/bin/env python3
"""verify_export_spectrum_data.py — acceptance harness for export-spectrum-data.

Three gates: static lint (msa-skill-spec validator), smoke (--help exits 0), and
golden (in-process). The golden gate feeds canned ORCA `.out` files carrying the
verified IR / absorption / NMR blocks and checks the skill writes v2.1 CSVs:

  * ``# key: value`` metadata lines, then the expert's verbatim column header,
    then data;
  * five continuous spectra in THREE columns (x, un-normalized raw, 0-1
    normalized), NMR peaks-only;
  * peaks tables unfiltered (imaginary frequencies and zero-oscillator-strength
    transitions kept) and unformatted;
  * the curve's x at X_DECIMALS precision and both y columns at 6 decimals, IR
    written high-wavenumber-first;
  * "no valid signal" is a SUCCESS carrying ``export_signal="none"``, a full
    peaks CSV and a data-free spectrum CSV;
  * variant-aware filenames (NMR nucleus / XANES edge element);
  * upstream-failed tasks skip, and a missing .out / unknown spec fails soft.

Fully offline, no matplotlib, no cluster.

exit 0 = all three gates green; non-zero = at least one failure.
"""
import csv
import importlib
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Tuple


def _resolve_repo_root() -> str:
    # Self-locate the repo root from this test's own path; no env var needed.
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    if not os.path.isdir(os.path.join(root, "msa_shared")):
        sys.stderr.write(f"ERROR: repo root '{root}' has no msa_shared/.\n")
        sys.exit(3)
    return root


REPO_ROOT = _resolve_repo_root()
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "export-spectrum-data")
SCRIPTS_DIR = os.path.join(SKILL_DIR, "scripts")
ENTRY = os.path.join(SCRIPTS_DIR, "export_spectrum_data.py")
SPEC_VALIDATOR = os.path.join(REPO_ROOT, "meta", "msa-skill-spec", "scripts", "validate_skill.py")

# One imaginary mode + two real ones: exercises the "kept in the peaks table but
# excluded from the curve" rule and the imaginary-frequency warning.
IR_OUT = """-----------
IR SPECTRUM
-----------

 Mode   freq       eps      Int      T**2         TX        TY        TZ
       cm**-1   L/(mol*cm) km/mol    a.u.
----------------------------------------------------------------------------
  6:    -42.10   0.000102    0.51  0.000031  (-0.000000 -0.009640  0.000000)
  7:   1146.68   0.000341    1.73  0.000093  (-0.000000 -0.009640  0.000000)
  9:   1806.49   0.020286  102.51  0.003504  ( 0.000000 -0.000000  0.059197)

****ORCA TERMINATED NORMALLY****
"""

# Second transition is below OSCILLATOR_STRENGTH_THRESHOLD: kept in the peaks
# table, excluded from the broadened curve.
ABS_OUT = """ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS
Transition      Energy     Energy  Wavelength fosc(D2)      D2        DX        DY        DZ
                 (eV)      (cm-1)    (nm)                 (au**2)    (au)      (au)      (au)
0-1A  ->  1-1A    4.500000   36000.0   275.5   0.120000   1.9  -1.3   0.0  -0.0
0-1A  ->  2-1A    5.200000   41900.0   238.7   0.000000   0.0  -0.0   0.0  -0.0

****ORCA TERMINATED NORMALLY****
"""

# Dark S1 (the bright state is S2): under Kasha's rule this is "no valid signal".
DARK_S1_OUT = """ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS
Transition      Energy     Energy  Wavelength fosc(D2)      D2        DX        DY        DZ
                 (eV)      (cm-1)    (nm)                 (au**2)    (au)      (au)      (au)
0-1A  ->  1-1A    2.800000   22600.0   442.5   0.000000   0.0  -0.0   0.0  -0.0
0-1A  ->  2-1A    3.900000   31500.0   317.5   0.250000   2.1  -1.4   0.0  -0.0

****ORCA TERMINATED NORMALLY****
"""

# Carbon K-edge core excitations (~285 eV -> ~4.3 nm).
XANES_OUT = """ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS
Transition      Energy     Energy  Wavelength fosc(D2)      D2        DX        DY        DZ
                 (eV)      (cm-1)    (nm)                 (au**2)    (au)      (au)      (au)
0-1A  ->  1-1A  285.000000 2299000.0    4.32   0.014000   0.1  -0.1   0.0  -0.0
0-1A  ->  2-1A  288.500000 2327000.0    4.27   0.006000   0.1  -0.1   0.0  -0.0

****ORCA TERMINATED NORMALLY****
"""

NMR_OUT = """CHEMICAL SHIELDING SUMMARY (ppm)
  Nucleus  Element    Isotropic     Anisotropy
     0       C          150.000          7.061
     1       H           24.000          2.000
     2       H           23.500          2.100

****ORCA TERMINATED NORMALLY****
"""


def run(cmd, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)


def step_lint() -> Dict[str, Any]:
    r = run(["python3", SPEC_VALIDATOR, SKILL_DIR])
    return {"step": "lint", "passed": r.returncode == 0, "returncode": r.returncode,
            "stderr_tail": r.stderr.strip().splitlines()[-4:]}


def step_help() -> Dict[str, Any]:
    env = dict(os.environ)
    r = run(["python3", ENTRY, "--help"], env=env)
    passed = r.returncode == 0 and "Traceback" not in (r.stderr + r.stdout)
    return {"step": "help", "passed": passed, "returncode": r.returncode}


def check(cond: bool, msg: str, failures: List[str]) -> None:
    if not cond:
        failures.append(msg)


def _import_skill():
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    return importlib.import_module("export_spectrum_data")


def _write_out(td: str, name: str, body: str) -> str:
    p = os.path.join(td, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return p


def _task(task_id: str, spec: str, out_file: str, calc_status: str = "success",
          **extra) -> Dict[str, Any]:
    t = {"task_id": task_id, "molecule_name": "benzene", "target_spectra": spec,
         "out_file": out_file, "calc_status": calc_status}
    t.update(extra)
    return t


def _read_csv(path: str) -> Tuple[Dict[str, str], List[str], List[List[str]]]:
    """Parse a metadata-headed CSV into (meta, warnings, rows).

    ``# key: value`` comment lines become the meta dict; the ``warning`` key may
    repeat, so those values are collected into a list instead. Everything else is
    the CSV body (header row + data rows).
    """
    meta: Dict[str, str] = {}
    warnings: List[str] = []
    body: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                kv = line[1:].strip()
                if ":" in kv:
                    k, v = kv.split(":", 1)
                    k, v = k.strip(), v.strip()
                    (warnings.append(v) if k == "warning" else meta.update({k: v}))
            else:
                body.append(line)
    return meta, warnings, list(csv.reader(body))


def step_golden() -> Dict[str, Any]:
    failures: List[str] = []
    skill = _import_skill()
    with tempfile.TemporaryDirectory() as td:
        cwd0 = os.getcwd(); os.chdir(td)
        try:
            ir_out = _write_out(td, "b-ir.out", IR_OUT)
            abs_out = _write_out(td, "b-uv.out", ABS_OUT)
            dark_out = _write_out(td, "b-fluo.out", DARK_S1_OUT)
            xanes_out = _write_out(td, "b-xanes.out", XANES_OUT)
            nmr_out = _write_out(td, "b-nmr.out", NMR_OUT)

            # G1: IR peaks CSV — verbatim header, imaginary mode KEPT, warning
            #     recorded in both the metadata header and the task.
            out = skill.process_task(_task("t1_ir", "ir", ir_out))
            check(out.get("export_status") == "success",
                  f"G1 export_status={out.get('export_status')!r} err={out.get('export_error')!r}", failures)
            pmeta, pwarn, prows = _read_csv(out["peaks_csv"])
            check(pmeta.get("molecule") == "benzene", f"G1 peaks meta molecule wrong: {pmeta!r}", failures)
            check(pmeta.get("x_unit") == "cm^-1", f"G1 peaks meta x_unit wrong: {pmeta!r}", failures)
            check(prows[0] == ["peak_index", "frequency_cm-1", "ir_intensity_km/mol"],
                  f"G1 peaks header wrong: {prows[0]!r}", failures)
            check(len(prows) - 1 == 3, f"G1 peaks must keep the imaginary mode: {len(prows) - 1} rows", failures)
            check(prows[1][1] == "-42.1", f"G1 first peak must be the imaginary one: {prows[1]!r}", failures)
            check(pmeta.get("n_imaginary_freqs") == "1", f"G1 peaks meta n_imaginary_freqs: {pmeta!r}", failures)
            check(len(pwarn) == 1 and "imaginary" in pwarn[0],
                  f"G1 peaks CSV must carry the imaginary-frequency warning: {pwarn!r}", failures)
            check(any("imaginary" in w for w in out.get("export_warnings") or []),
                  f"G1 task must carry export_warnings: {out.get('export_warnings')!r}", failures)

            # G2: IR spectrum CSV — three columns, high wavenumber first, 6-decimal y.
            cmeta, _cw, crows = _read_csv(out["spectrum_csv"])
            check(crows[0] == ["wavenumber_cm-1", "raw_intensity_km·mol⁻¹·cm", "normalized_intensity"],
                  f"G2 curve header wrong: {crows[0]!r}", failures)
            check(len(crows) - 1 == 3601, f"G2 curve length wrong: {len(crows) - 1}", failures)
            check(crows[1][0] == "4000" and crows[-1][0] == "400",
                  f"G2 IR curve must run high->low: {crows[1][0]!r}..{crows[-1][0]!r}", failures)
            check(all(len(c.split(".")[1]) == 6 for c in crows[1][1:]),
                  f"G2 y columns must be 6-decimal fixed point: {crows[1]!r}", failures)
            check("Lorentzian" in (cmeta.get("broadening") or ""),
                  f"G2 curve meta missing Lorentzian broadening: {cmeta!r}", failures)
            check(max(float(r[2]) for r in crows[1:]) == 1.0,
                  "G2 normalized column must peak at 1.0", failures)

            # G3: UV-Vis — 4-column peaks keeping the dark transition, 3-column curve.
            out2 = skill.process_task(_task("t2_uv", "uvvis", abs_out))
            check(out2.get("export_status") == "success",
                  f"G3 export_status={out2.get('export_status')!r} err={out2.get('export_error')!r}", failures)
            m2, _w2, rows2 = _read_csv(out2["peaks_csv"])
            check(rows2[0] == ["transition_index", "wavelength_nm", "oscillator_strength", "epsilon_jacobi"],
                  f"G3 peaks header wrong: {rows2[0]!r}", failures)
            check(len(rows2) - 1 == 2, f"G3 peaks must keep the zero-fosc transition: {len(rows2) - 1}", failures)
            check(m2.get("num_transitions") == "2", f"G3 peaks meta num_transitions: {m2!r}", failures)
            _cm2, _cw2, crows2 = _read_csv(out2["spectrum_csv"])
            check(crows2[0] == ["wavelength_nm", "raw_intensity_jacobi", "normalized_intensity"],
                  f"G3 curve header wrong: {crows2[0]!r}", failures)

            # G4: fluorescence with a dark S1 -> success + signal=none, full peaks
            #     table, spectrum CSV with metadata + header but NO data rows.
            out3 = skill.process_task(_task("t3_dark", "fluo", dark_out))
            check(out3.get("export_status") == "success",
                  f"G4 a no-signal export is still a success: {out3.get('export_status')!r}", failures)
            check(out3.get("export_signal") == "none",
                  f"G4 export_signal={out3.get('export_signal')!r} (want none)", failures)
            check("dark" in (out3.get("export_message") or ""),
                  f"G4 export_message must name the dark S1: {out3.get('export_message')!r}", failures)
            m3, _w3, rows3 = _read_csv(out3["peaks_csv"])
            check(len(rows3) - 1 == 2, f"G4 peaks table must stay complete: {len(rows3) - 1}", failures)
            check(m3.get("signal") == "none", f"G4 peaks meta must record signal: {m3!r}", failures)
            _cm3, _cw3, crows3 = _read_csv(out3["spectrum_csv"])
            check(len(crows3) - 1 == 0, f"G4 no-signal curve must have no data rows: {len(crows3) - 1}", failures)

            # G5: XANES — variant filename carries the edge element, x at 1 decimal.
            out4 = skill.process_task(_task(
                "t4_xanes", "xanes", xanes_out,
                xanes_target_element="C",
                calc_basis_original="def2-TZVP",
                calc_basis_final="def2-TZVP",
                calc_basis_fallback="none",
                calc_fallback_steps=["step2"],
                calc_fallback_reason="scf_not_converged",
            ))
            check(out4.get("export_status") == "success",
                  f"G5 export_status={out4.get('export_status')!r} err={out4.get('export_error')!r}", failures)
            check(os.path.basename(out4["peaks_csv"]) == "benzene_xanes_C_peaks.csv",
                  f"G5 peaks filename must carry the edge element: {out4['peaks_csv']!r}", failures)
            m4, _w4, rows4 = _read_csv(out4["peaks_csv"])
            check(rows4[0] == ["transition_index", "photon_energy_eV", "oscillator_strength"],
                  f"G5 peaks header wrong: {rows4[0]!r}", failures)
            check(m4.get("edge_element") == "C", f"G5 peaks meta edge_element: {m4!r}", failures)
            check(m4.get("basis_original") == "def2-TZVP" and
                  m4.get("basis_final") == "def2-TZVP" and
                  m4.get("basis_fallback") == "none",
                  f"G5 XANES production basis metadata wrong: {m4!r}", failures)
            check(m4.get("basis_fallback_step") == "step2",
                  f"G5 XANES step2-only fallback must be explicit: {m4!r}", failures)
            check(m4.get("fallback_reason") == "scf_not_converged",
                  f"G5 fallback reason missing: {m4!r}", failures)
            _cm4, _cw4, crows4 = _read_csv(out4["spectrum_csv"])
            check(crows4[1][0] == "275.0" and crows4[-1][0] == "320.0",
                  f"G5 XANES x must be the fixed C window at 1 decimal: "
                  f"{crows4[1][0]!r}..{crows4[-1][0]!r}", failures)

            # G6: NMR — peaks-only, per-nucleus filename, 1-based index, no float noise.
            out5 = skill.process_task(_task(
                "t5_nmr", "nmr", nmr_out,
                extra_params={"target_nucleus": "1H"},
                system_type="anion",
                calc_basis_original="ma-def2-TZVP",
                calc_basis_final="ma-def2-TZVP",
                calc_basis_fallback="none",
                calc_scf_thresh="1e-7",
                calc_fallback_reason="scf_not_converged",
            ))
            check(out5.get("export_status") == "success",
                  f"G6 export_status={out5.get('export_status')!r} err={out5.get('export_error')!r}", failures)
            check(os.path.basename(out5["peaks_csv"]) == "benzene_nmr_1H_peaks.csv",
                  f"G6 peaks filename must carry the nucleus: {out5['peaks_csv']!r}", failures)
            m5, _w5, rows5 = _read_csv(out5["peaks_csv"])
            check(rows5[0] == ["peak_index", "atom_index", "shielding_sigma_ppm",
                               "chemical_shift_delta_ppm"],
                  f"G6 peaks header wrong: {rows5[0]!r}", failures)
            check(len(rows5) - 1 == 2, f"G6 must keep only the two H nuclei: {len(rows5) - 1}", failures)
            check(rows5[1][0] == "1", f"G6 peak_index must be 1-based: {rows5[1]!r}", failures)
            check(rows5[1][3] == "7.44", f"G6 delta must use final-basis TMS 31.44-24.0: {rows5[1]!r}", failures)
            check(m5.get("basis_original") == "ma-def2-TZVP" and
                  m5.get("basis_final") == "ma-def2-TZVP" and
                  m5.get("basis_fallback") == "none",
                  f"G6 threshold-only retry must not claim basis fallback: {m5!r}", failures)
            check(m5.get("scf_thresh") == "1e-7" and
                  m5.get("fallback_reason") == "scf_not_converged",
                  f"G6 NMR retry metadata wrong: {m5!r}", failures)
            check("delta = sigma_TMS - sigma_sample" in (m5.get("formula") or ""),
                  f"G6 peaks meta missing shift formula: {m5!r}", failures)
            check(not out5.get("spectrum_csv"),
                  f"G6 NMR must be peaks-only, got {out5.get('spectrum_csv')!r}", failures)

            # G7: upstream calc not ok -> skipped
            out6 = skill.process_task(_task("t6_skip", "ir", ir_out, calc_status="failed"))
            check(out6.get("export_status") == "skipped",
                  f"G7 export_status={out6.get('export_status')!r} (want skipped)", failures)

            # G8: missing .out -> failed; unknown spec -> failed
            out7 = skill.process_task(_task("t7_missing", "ir", os.path.join(td, "nope.out")))
            check(out7.get("export_status") == "failed",
                  f"G8 export_status={out7.get('export_status')!r} (want failed)", failures)
            out8 = skill.process_task(_task("t8_unknown", "zzz", ir_out))
            check(out8.get("export_status") == "failed",
                  f"G8 export_status={out8.get('export_status')!r} (want failed)", failures)
            check("NotImplementedError" in (out8.get("export_error") or ""),
                  f"G8 export_error should mention NotImplementedError: {out8.get('export_error')!r}", failures)

            # G9: task_id / upstream fields not mutated on success
            check(out.get("task_id") == "t1_ir" and out.get("calc_status") == "success",
                  "G9 upstream field / task_id mutated on success", failures)
        finally:
            os.chdir(cwd0)

    n_total = 9
    return {"step": "golden", "passed": not failures,
            "n_pass": max(0, n_total - len(failures)), "n_total": n_total,
            "failures": failures}


def main() -> None:
    report = {"lint": step_lint(), "help": step_help(), "golden": step_golden()}
    ok = report["lint"]["passed"] and report["help"]["passed"] and report["golden"]["passed"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not ok:
        print("\nFAIL — see failures above", file=sys.stderr)
        sys.exit(1)
    print("\nPASS — all three gates green", file=sys.stderr)


if __name__ == "__main__":
    main()
