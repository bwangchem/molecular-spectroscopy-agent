#!/usr/bin/env python3
"""verify_plot_fluo.py — acceptance harness for the plot-fluo skill.

Three gates: static lint (msa-skill-spec validator), smoke (--help exits 0), and
golden (in-process). The golden gate feeds a canned ORCA `.out` carrying a real
Fluorescence block and checks the skill produces a PNG+PDF, passes non-fluo tasks
through untouched, skips upstream-failed tasks, and fails soft on a missing .out.
Fully offline — plotting uses the Agg backend, no cluster or display needed.

exit 0 = all three gates green; non-zero = at least one failure.
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
from typing import List, Dict, Any


def _resolve_repo_root() -> str:
    # Self-locate the repo root from this test's own path; no env var needed.
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    if not os.path.isdir(os.path.join(root, "msa_shared")):
        sys.stderr.write(f"ERROR: repo root '{root}' has no msa_shared/.\n")
        sys.exit(3)
    return root


REPO_ROOT = _resolve_repo_root()
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "plot-fluo")
SCRIPTS_DIR = os.path.join(SKILL_DIR, "scripts")
ENTRY = os.path.join(SCRIPTS_DIR, "plot_fluo.py")
SPEC_VALIDATOR = os.path.join(REPO_ROOT, "meta", "msa-skill-spec", "scripts", "validate_skill.py")

CANNED_OUT = """ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS
Transition      Energy     Energy  Wavelength fosc(D2)      D2        DX        DY        DZ
                 (eV)      (cm-1)    (nm)                 (au**2)    (au)      (au)      (au)
0-1A  ->  1-1A    2.800000   22600.0   442.9   0.250000   1.9  -1.3   0.0  -0.0
0-1A  ->  2-1A    3.600000   29000.0   344.4   0.500000   2.5  -1.7   0.0  -0.0

****ORCA TERMINATED NORMALLY****
"""
# A converged calculation with nothing to draw. "No valid signal" is a physical
# outcome, not an execution error, so the skill must still report success.
CANNED_DARK = """ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS
Transition      Energy     Energy  Wavelength fosc(D2)      D2        DX        DY        DZ
                 (eV)      (cm-1)    (nm)                 (au**2)    (au)      (au)      (au)
0-1A  ->  1-1A    2.800000   22600.0   442.9   0.000000   0.0   0.0   0.0  -0.0
0-1A  ->  2-1A    3.600000   29000.0   344.4   0.500000   2.5  -1.7   0.0  -0.0

****ORCA TERMINATED NORMALLY****
"""


def run(cmd: List[str], env=None) -> subprocess.CompletedProcess:
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
    return importlib.import_module("plot_fluo")


def _task(task_id: str, out_file: str, calc_status: str = "success") -> Dict[str, Any]:
    return {"task_id": task_id, "molecule_name": "\u82ef", "target_spectra": "fluo",
            "out_file": out_file, "calc_status": calc_status}


def step_golden() -> Dict[str, Any]:
    failures: List[str] = []
    skill = _import_skill()
    with tempfile.TemporaryDirectory() as td:
        cwd0 = os.getcwd(); os.chdir(td)
        try:
            out_path = os.path.join(td, "benzene-fluo.out")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(CANNED_OUT)

            # G1: a real fluo task succeeds and produces PNG + PDF on disk
            out = skill.process_task(_task("t1_ok", out_path))
            check(out.get("plot_status") == "success",
                  f"G1 plot_status={out.get('plot_status')!r} err={out.get('plot_error')!r}", failures)
            check(out.get("plot_png") and os.path.isfile(out["plot_png"]), "G1 plot_png missing on disk", failures)
            check(out.get("plot_pdf") and os.path.isfile(out["plot_pdf"]), "G1 plot_pdf missing on disk", failures)
            check(out.get("task_id") == "t1_ok" and out.get("calc_status") == "success",
                  "G1 upstream field / task_id mutated", failures)

            # G2: non-fluo task passed through UNTOUCHED (no plot_status)
            other = {"task_id": "t2_other", "target_spectra": "zzz", "calc_status": "success"}
            out2 = skill.process_task(dict(other))
            check("plot_status" not in out2,
                  f"G2 non-fluo task must be untouched, got plot_status={out2.get('plot_status')!r}", failures)

            # G3: upstream calc not ok -> skipped
            out3 = skill.process_task(_task("t3_skip", out_path, calc_status="failed"))
            check(out3.get("plot_status") == "skipped", f"G3 plot_status={out3.get('plot_status')!r} (want skipped)", failures)

            # G4: missing .out -> failed (fail soft)
            out4 = skill.process_task(_task("t4_missing", os.path.join(td, "nope.out")))
            check(out4.get("plot_status") == "failed", f"G4 plot_status={out4.get('plot_status')!r} (want failed)", failures)

            # G5: Kasha — the peaks table is the UNFILTERED record of what ORCA
            # produced (every state), while only S1 is broadened into the curve, on
            # the 300-700 nm emission window.
            fluo = skill.orca_output.build_fluo(CANNED_OUT)
            check(len(fluo["peaks"]) == 2,
                  f"G5 peaks table must keep every state, got {fluo['peaks']}", failures)
            check(abs(fluo["peaks"][0]["wavelength_nm"] - 442.9) < 0.5,
                  f"G5 S1 must head the peaks table: {fluo['peaks']}", failures)
            x_peak = float(fluo["x"][int(fluo["y"].argmax())])
            check(abs(x_peak - 442.9) < 5.0,
                  f"G5 curve must come from S1 alone, but peaks at {x_peak:.1f} nm "
                  f"(S2 is the brighter state at 344.4 nm)", failures)
            check(tuple(fluo["x_lim"]) == (300, 700),
                  f"G5 build_fluo x_lim must be (300,700), got {fluo.get('x_lim')}", failures)

            # G-nosig: a converged run with nothing to draw stays SUCCESS, records
            # plot_signal="none" + a message, and writes NO figure at all.
            dark_path = os.path.join(td, "dark-fluo.out")
            with open(dark_path, "w", encoding="utf-8") as f:
                f.write(CANNED_DARK)
            outd = skill.process_task(_task("t5_no_signal", dark_path))
            check(outd.get("plot_status") == "success",
                  f"G-nosig status={outd.get('plot_status')!r} (want success, "
                  f"no-signal is not a failure)", failures)
            check(outd.get("plot_signal") == "none",
                  f"G-nosig plot_signal={outd.get('plot_signal')!r} (want 'none')", failures)
            check(outd.get("plot_png") == "" and outd.get("plot_pdf") == "",
                  f"G-nosig must not claim a figure: png={outd.get('plot_png')!r}", failures)
            check(bool(outd.get("plot_message")), "G-nosig plot_message missing", failures)
            check("plot_error" not in outd, "G-nosig must not set plot_error", failures)

            # G-title: the figure title uses the English canonical name resolved by
            # name-to-smiles, not the (possibly Chinese) molecule_name, AND the
            # wording is the one the expert confirmed on 0810 (third round). Both
            # halves are locked here because a title string has no other guard.
            seen: List[tuple] = []
            real_set_title = skill.spectrum_style.set_title
            skill.spectrum_style.set_title = (
                lambda ax, spectrum, name: seen.append((name, spectrum.get("title_template")))
            )
            try:
                t_en = _task("t6_title", out_path)
                t_en["molecule_name_en"] = "Benzene"
                skill.process_task(t_en)
            finally:
                skill.spectrum_style.set_title = real_set_title
            check(seen == [("Benzene", "Fluorescence Emission Spectrum ({molecule})")],
                  f"G-title name/wording pair must be the expert-confirmed one, got {seen}", failures)
        finally:
            os.chdir(cwd0)
    n_total = 7
    return {"step": "golden", "passed": not failures,
            "n_pass": n_total - len(failures), "n_total": n_total, "failures": failures}


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
