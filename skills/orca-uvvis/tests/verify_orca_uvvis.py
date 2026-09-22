#!/usr/bin/env python3
"""verify_orca_uvvis.py — acceptance harness owned by the main session.

Runs three gates against the orca-uvvis skill (Part-2, stage_id=calc, spec=uvvis):
  1. static lint  (msa-skill-spec validator)
  2. smoke        (--help exits 0, no traceback)
  3. golden       (in-process: the paramiko transport is monkeypatched so the
                   skill's logic is exercised offline, with no real cluster)

Same offline strategy as verify_orca_ir / verify_orca_raman: import the skill
module and replace common_utils' submit_slurm / poll_slurm / download_results (as
bound on the skill module) with fakes; the "download" fake drops a canned .out
with ORCA's normal-termination sentinel. UV-Vis is the pipeline's first MULTI-STEP
recipe: params carry step1 (calc_type="opt", r2SCAN-3c) then step2
(calc_type="tddft", CAM-B3LYP/def2-TZVP, nroots), chained by orca_input into one
input file with a "$new_job" separator, the tddft job inheriting the optimized
geometry via a filename-less "* xyzfile" line.

Checks: the .inp carries BOTH steps' functionals (r2SCAN-3c opt + CAM-B3LYP), the
"$new_job" separator, the "%tddft nroots 30 end" block (nroots plumbed from
params, not hard-coded), the initial-geometry line for step1 and the filename-less
carry-over line for step2, CPCM on both steps when solvated / none in gas; the
inp/sh/xyz trio is uploaded; success stamps the whitelist fields without touching
upstream/task_id; a non-UV-Vis task is passed through untouched; params-not-ok ->
skipped; missing / abnormal .out -> failed.

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
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "orca-uvvis")
SCRIPTS_DIR = os.path.join(SKILL_DIR, "scripts")
ENTRY = os.path.join(SCRIPTS_DIR, "run_orca_uvvis.py")
SPEC_VALIDATOR = os.path.join(REPO_ROOT, "meta", "msa-skill-spec", "scripts", "validate_skill.py")

# A well-formed cluster profile for the in-process golden gate (offline; the
# transport is faked, so these values are never dialed).
FAKE_CLUSTER = {
    "host": "cluster.example.invalid", "user": "tester",
    "password": "unused-in-tests", "key_file": None,
    "remote_root": "/scratch/tester/orca",
    "orca_bin": "/opt/orca/orca", "mpi_prefix": "/opt/openmpi",
    "partition": "test", "qos": "normal",
}


def run(cmd: List[str], env=None, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env, cwd=cwd)


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
    return importlib.import_module("run_orca_uvvis")


def _uvvis_task(task_id: str, solvent_model, xyz_path: str, params_status: str = "success") -> Dict[str, Any]:
    """A well-formed two-step UV-Vis task as param-recommendation would emit it."""
    return {
        "task_id": task_id,
        "molecule_name": "苯",
        "target_spectra": "uvvis",
        "solvent_std": "CPCM(Water)" if solvent_model else "",
        "smiles": "c1ccccc1",
        "xyz_file": xyz_path,
        "num_atoms": 12,
        "system_type": "neutral",
        "params": {
            "step1": {"calc_type": "opt_freq", "functional": "CAM-B3LYP",
                      "basis_set": "def2-TZVP", "ri_acceleration": "RIJCOSX",
                      "auxiliary_basis": "AutoAux", "zora": False,
                      "solvent_model": solvent_model},
            "step2": {"calc_type": "uvvis_tddft", "functional": "CAM-B3LYP",
                      "basis_set": "def2-TZVP", "ri_acceleration": "RIJCOSX",
                      "auxiliary_basis": "AutoAux", "zora": False,
                      "solvent_model": solvent_model, "nroots": 30, "tda": False,
                      "maxdim": 60},
        },
        "smiles_status": "success",
        "xyz_status": "success",
        "params_status": params_status,
    }


def step_golden() -> Dict[str, Any]:
    failures: List[str] = []
    skill = _import_skill()

    with tempfile.TemporaryDirectory() as td:
        cwd0 = os.getcwd()
        os.chdir(td)
        try:
            xyz_path = os.path.join(td, "benzene_initial.xyz")
            with open(xyz_path, "w", encoding="utf-8") as f:
                f.write("2\n\nC 0 0 0\nC 0 0 1.4\n")

            calls: Dict[str, Any] = {}
            mode = {"download": "normal"}  # normal | missing | abnormal

            def fake_submit(work_dir, script="submit.sh", upload_files=None, **conn):
                calls["work_dir"] = work_dir
                calls["script"] = script
                calls["upload_files"] = list(upload_files or [])
                return "55443"

            def fake_poll(job_id, interval=30, timeout=7200, **conn):
                calls["polled"] = job_id
                return "COMPLETED"

            def fake_download(remote_dir, files, local_dir, **conn):
                os.makedirs(local_dir, exist_ok=True)
                if mode["download"] == "missing":
                    return [], list(files)
                body = ("normal run\nTHE OPTIMIZATION HAS CONVERGED\n"
                        + skill.orca_input.ORCA_NORMAL_TERMINATION + "\n"
                        if mode["download"] == "normal" else "run died early\n")
                for name in files:
                    with open(os.path.join(local_dir, name), "w", encoding="utf-8") as f:
                        f.write(body)
                return list(files), []

            skill.submit_slurm = fake_submit
            skill.poll_slurm = fake_poll
            skill.download_results = fake_download

            # G1: solvated UV-Vis task succeeds; .inp correct; upload trio; no mutation
            mode["download"] = "normal"
            t = _uvvis_task("t1_ok", "CPCM(Water)", xyz_path)
            out = skill.process_task(dict(t), FAKE_CLUSTER)
            check(out.get("calc_status") == "success",
                  f"G1 calc_status={out.get('calc_status')!r} (want success); err={out.get('calc_error')!r}", failures)
            check(out.get("slurm_job_id") == "55443", f"G1 slurm_job_id={out.get('slurm_job_id')!r}", failures)
            check(isinstance(out.get("calc_wallclock"), int), f"G1 calc_wallclock not int: {out.get('calc_wallclock')!r}", failures)
            check(out.get("out_file") and os.path.isfile(out["out_file"]), "G1 out_file missing on disk", failures)
            check(out.get("task_id") == "t1_ok" and out.get("params_status") == "success",
                  "G1 upstream field / task_id mutated", failures)
            ups = [os.path.basename(p) for p in calls.get("upload_files", [])]
            check(any(p.endswith("_uvvis.inp") for p in ups) and any(p.endswith("_uvvis.sh") for p in ups)
                  and any(p.endswith("_initial.xyz") for p in ups),
                  f"G1 upload trio wrong: {ups}", failures)
            inp_path = next((p for p in calls["upload_files"] if p.endswith("_uvvis.inp")), None)
            inp_txt = open(inp_path, encoding="utf-8").read() if inp_path else ""
            check("OPT FREQ" in inp_txt, "G1 .inp missing 'OPT FREQ' on the ground-state step", failures)
            check(inp_txt.count("CAM-B3LYP") == 2, "G1 .inp should carry CAM-B3LYP on BOTH steps", failures)
            check("def2-TZVP" in inp_txt, "G1 .inp missing basis def2-TZVP", failures)
            check(inp_txt.count("CPCM(Water)") == 2, f"G1 .inp should carry CPCM(Water) on BOTH steps:\n{inp_txt}", failures)
            check("xyzfile 0 1 benzene_initial.xyz" in inp_txt, f"G1 .inp step1 xyzfile line wrong:\n{inp_txt}", failures)

            # G7: two-step chaining structure (the thing that distinguishes UV-Vis)
            check("$new_job" in inp_txt, f"G7 .inp missing $new_job separator:\n{inp_txt}", failures)
            check("%tddft nroots 30 tda false maxdim 60 end" in inp_txt, f"G7 .inp missing %tddft nroots+tda+maxdim block:\n{inp_txt}", failures)
            check("* xyzfile 0 1\n" in inp_txt, f"G7 .inp missing filename-less geometry carry-over line:\n{inp_txt}", failures)
            check("OPT FREQ CAM-B3LYP" in inp_txt, f"G7 .inp step1 must be an OPT FREQ with CAM-B3LYP:\n{inp_txt}", failures)

            # G2: gas-phase UV-Vis task -> no CPCM in .inp
            t2 = _uvvis_task("t2_gas", None, xyz_path)
            out2 = skill.process_task(dict(t2), FAKE_CLUSTER)
            check(out2.get("calc_status") == "success", f"G2 calc_status={out2.get('calc_status')!r}", failures)
            inp2 = next((p for p in calls["upload_files"] if p.endswith("_uvvis.inp")), None)
            inp2_txt = open(inp2, encoding="utf-8").read() if inp2 else ""
            check("CPCM" not in inp2_txt, f"G2 gas-phase .inp must have no CPCM:\n{inp2_txt}", failures)
            check("%tddft nroots 30 tda false maxdim 60 end" in inp2_txt, f"G2 gas-phase .inp still needs the tddft block:\n{inp2_txt}", failures)

            # G3: non-UV-Vis task passed through UNTOUCHED
            ir = {"task_id": "t3_ir", "target_spectra": "ir", "params_status": "success",
                  "molecule_name": "苯"}
            out3 = skill.process_task(dict(ir), FAKE_CLUSTER)
            check("calc_status" not in out3, f"G3 non-UV-Vis task must be untouched, got calc_status={out3.get('calc_status')!r}", failures)

            # G4: UV-Vis task with params not ok -> skipped
            t4 = _uvvis_task("t4_skip", "CPCM(Water)", xyz_path, params_status="failed")
            out4 = skill.process_task(dict(t4), FAKE_CLUSTER)
            check(out4.get("calc_status") == "skipped", f"G4 calc_status={out4.get('calc_status')!r} (want skipped)", failures)

            # G5: .out never produced -> failed
            mode["download"] = "missing"
            t5 = _uvvis_task("t5_missing", "CPCM(Water)", xyz_path)
            out5 = skill.process_task(dict(t5), FAKE_CLUSTER)
            check(out5.get("calc_status") == "failed", f"G5 calc_status={out5.get('calc_status')!r} (want failed)", failures)

            # G6: abnormal ORCA termination -> failed
            mode["download"] = "abnormal"
            t6 = _uvvis_task("t6_abnormal", "CPCM(Water)", xyz_path)
            out6 = skill.process_task(dict(t6), FAKE_CLUSTER)
            check(out6.get("calc_status") == "failed", f"G6 calc_status={out6.get('calc_status')!r} (want failed)", failures)
            check(out6.get("calc_error") == "optimization_not_converged",
                  f"G6 calc_error={out6.get('calc_error')!r}", failures)
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
