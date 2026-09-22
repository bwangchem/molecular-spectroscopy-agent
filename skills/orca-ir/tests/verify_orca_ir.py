#!/usr/bin/env python3
"""verify_orca_ir.py — acceptance harness owned by the main session.

Runs three gates against the orca-ir skill (Part-2, stage_id=calc, spec=ir):
  1. static lint  (msa-skill-spec validator)
  2. smoke        (--help exits 0, no traceback)
  3. golden       (in-process: the paramiko transport is monkeypatched so the
                   skill's logic is exercised offline, with no real cluster)

Why the golden gate runs in-process: orca-ir's real work is remote (SFTP upload,
sbatch, squeue poll, SFTP download). We cannot reach the HPC from a test, so we
import the skill module and replace common_utils' ``submit_slurm`` / ``poll_slurm``
/ ``download_results`` (as bound on the skill module) with fakes that record the
call and, on "download", drop a canned ``.out`` carrying ORCA's normal-termination
sentinel. That lets us assert every part the skill actually owns:

  * the ORCA ``.inp`` is built from ``task["params"]`` — correct functional
    (r2SCAN-3c), IR keyword (Opt Freq), CPCM solvent when solvated, ``xyzfile``
    line — and nothing is hard-coded;
  * the SLURM ``submit.sh`` is written and the inp / sh / xyz trio is uploaded;
  * success stamps calc_status="success" + out_file + slurm_job_id + int
    calc_wallclock, and does NOT rewrite an upstream field or task_id;
  * a non-IR task is passed through UNTOUCHED (no calc_status) so sibling calc
    skills don't clobber each other;
  * an IR task with params_status != success is marked calc_status="skipped";
  * a missing ``.out`` and an abnormal-termination ``.out`` both fail soft with
    calc_status="failed".

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
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "orca-ir")
SCRIPTS_DIR = os.path.join(SKILL_DIR, "scripts")
ENTRY = os.path.join(SCRIPTS_DIR, "run_orca_ir.py")
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
    """Import run_orca_ir as a module (self-locates its vendored msa_shared)."""
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    return importlib.import_module("run_orca_ir")


def _ir_task(task_id: str, solvent_model, xyz_path: str, params_status: str = "success") -> Dict[str, Any]:
    """A well-formed IR task as param-recommendation would emit it."""
    return {
        "task_id": task_id,
        "molecule_name": "苯",
        "target_spectra": "ir",
        "solvent_std": "CPCM(Water)" if solvent_model else "",
        "smiles": "c1ccccc1",
        "xyz_file": xyz_path,
        "num_atoms": 12,
        "system_type": "neutral",
        "params": {"step1": {"calc_type": "opt_freq", "functional": "r2SCAN-3c",
                             "basis_set": None, "zora": False, "solvent_model": solvent_model}},
        "smiles_status": "success",
        "xyz_status": "success",
        "params_status": params_status,
    }


def step_golden() -> Dict[str, Any]:
    failures: List[str] = []
    skill = _import_skill()

    with tempfile.TemporaryDirectory() as td:
        cwd0 = os.getcwd()
        os.chdir(td)  # naming writes to ./local_workspace relative to CWD
        try:
            # A real initial XYZ on disk (build_ir_inputs checks it exists).
            xyz_path = os.path.join(td, "benzene_initial.xyz")
            with open(xyz_path, "w", encoding="utf-8") as f:
                f.write("2\n\nC 0 0 0\nC 0 0 1.4\n")

            # ---- transport fakes (record calls; behavior is per-test switchable) ----
            calls: Dict[str, Any] = {}
            mode = {"download": "normal"}  # normal | missing | abnormal

            def fake_submit(work_dir, script="submit.sh", upload_files=None, **conn):
                calls["work_dir"] = work_dir
                calls["script"] = script
                calls["upload_files"] = list(upload_files or [])
                return "99887"

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

            # ---- G1: solvated IR task succeeds; .inp correct; upload trio; no mutation ----
            mode["download"] = "normal"
            t = _ir_task("t1_ok", "CPCM(Water)", xyz_path)
            before_keys = set(t.keys())
            out = skill.process_task(dict(t), FAKE_CLUSTER)
            check(out.get("calc_status") == "success",
                  f"G1 calc_status={out.get('calc_status')!r} (want success); err={out.get('calc_error')!r}", failures)
            check(out.get("slurm_job_id") == "99887", f"G1 slurm_job_id={out.get('slurm_job_id')!r}", failures)
            check(isinstance(out.get("calc_wallclock"), int), f"G1 calc_wallclock not int: {out.get('calc_wallclock')!r}", failures)
            check(out.get("out_file") and os.path.isfile(out["out_file"]), "G1 out_file missing on disk", failures)
            check(out.get("task_id") == "t1_ok" and out.get("params_status") == "success",
                  "G1 upstream field / task_id mutated", failures)
            # uploaded exactly inp + sh + xyz
            ups = [os.path.basename(p) for p in calls.get("upload_files", [])]
            check(any(p.endswith("_ir.inp") for p in ups) and any(p.endswith("_ir.sh") for p in ups)
                  and any(p.endswith("_initial.xyz") for p in ups),
                  f"G1 upload trio wrong: {ups}", failures)
            # inp content honest + correct
            inp_path = next((p for p in calls["upload_files"] if p.endswith("_ir.inp")), None)
            inp_txt = open(inp_path, encoding="utf-8").read() if inp_path else ""
            check("r2SCAN-3c" in inp_txt, "G1 .inp missing functional r2SCAN-3c", failures)
            check("OPT FREQ" in inp_txt, "G1 .inp missing 'OPT FREQ' (IR keyword)", failures)
            check("CPCM(Water)" in inp_txt, "G1 .inp missing CPCM(Water) solvent", failures)
            check("xyzfile 0 1 benzene_initial.xyz" in inp_txt, f"G1 .inp xyzfile line wrong:\n{inp_txt}", failures)

            # ---- G2: gas-phase IR task -> no CPCM in .inp ----
            t2 = _ir_task("t2_gas", None, xyz_path)
            out2 = skill.process_task(dict(t2), FAKE_CLUSTER)
            check(out2.get("calc_status") == "success", f"G2 calc_status={out2.get('calc_status')!r}", failures)
            inp2 = next((p for p in calls["upload_files"] if p.endswith("_ir.inp")), None)
            inp2_txt = open(inp2, encoding="utf-8").read() if inp2 else ""
            check("CPCM" not in inp2_txt, f"G2 gas-phase .inp must have no CPCM:\n{inp2_txt}", failures)

            # ---- G3: non-IR task passed through UNTOUCHED ----
            uvvis = {"task_id": "t3_uvvis", "target_spectra": "uvvis", "params_status": "success",
                     "molecule_name": "对硝基苯酚"}
            out3 = skill.process_task(dict(uvvis), FAKE_CLUSTER)
            check("calc_status" not in out3, f"G3 non-IR task must be untouched, got calc_status={out3.get('calc_status')!r}", failures)

            # ---- G4: IR task with params not ok -> skipped ----
            t4 = _ir_task("t4_skip", "CPCM(Water)", xyz_path, params_status="failed")
            out4 = skill.process_task(dict(t4), FAKE_CLUSTER)
            check(out4.get("calc_status") == "skipped", f"G4 calc_status={out4.get('calc_status')!r} (want skipped)", failures)

            # ---- G5: .out never produced -> failed ----
            mode["download"] = "missing"
            t5 = _ir_task("t5_missing", "CPCM(Water)", xyz_path)
            out5 = skill.process_task(dict(t5), FAKE_CLUSTER)
            check(out5.get("calc_status") == "failed", f"G5 calc_status={out5.get('calc_status')!r} (want failed)", failures)

            # ---- G6: abnormal ORCA termination -> failed ----
            mode["download"] = "abnormal"
            t6 = _ir_task("t6_abnormal", "CPCM(Water)", xyz_path)
            out6 = skill.process_task(dict(t6), FAKE_CLUSTER)
            check(out6.get("calc_status") == "failed", f"G6 calc_status={out6.get('calc_status')!r} (want failed)", failures)
            check(out6.get("calc_error") == "optimization_not_converged",
                  f"G6 calc_error={out6.get('calc_error')!r}", failures)

            # ---- G7: resolve_cluster reads the profile from inline HPC_* env
            #          (secret-free, file-free contract) ----
            hpc_env = {"HPC_HOST": "h", "HPC_USER": "u", "HPC_PASSWORD": "p",
                       "HPC_REMOTE_ROOT": "/r", "HPC_ORCA_BIN": "/o",
                       "HPC_MPI_PREFIX": "/m"}
            saved7 = {k: os.environ.get(k) for k in hpc_env}
            os.environ.update(hpc_env)
            try:
                prof = skill.resolve_cluster()
                check(prof.get("host") == "h" and prof.get("orca_bin") == "/o"
                      and prof.get("qos") == "normal",
                      f"G7 resolve_cluster from HPC_* env wrong: {prof!r}", failures)
            finally:
                for k, v in saved7.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

            # ---- G8: no host/user/auth/paths -> actionable ClusterConfigError
            #          pointing ONLY at the inline HPC_* channel (no file channel) ----
            saved = {k: os.environ.pop(k, None) for k in
                     ("HPC_HOST", "HPC_USER", "HPC_PASSWORD", "HPC_KEY_FILE",
                      "HPC_REMOTE_ROOT", "HPC_ORCA_BIN", "HPC_MPI_PREFIX")}
            try:
                raised = False
                try:
                    skill.resolve_cluster()
                except skill.ClusterConfigError as e:
                    raised = "HPC_HOST" in str(e) and "--cluster-config" not in str(e)
                check(raised, "G8 resolve_cluster must raise an actionable HPC_*-only ClusterConfigError", failures)
            finally:
                for k, v in saved.items():
                    if v is not None:
                        os.environ[k] = v

            _ = before_keys  # (kept for clarity; per-field mutation checks done above)
        finally:
            os.chdir(cwd0)

    n_total = 8
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
