#!/usr/bin/env python3
"""verify_orca_xanes.py — acceptance harness owned by the main session.

Runs three gates against the orca-xanes skill (Part-2, stage_id=calc, spec=xanes):
  1. static lint  (msa-skill-spec validator)
  2. smoke        (--help exits 0, no traceback)
  3. golden       (in-process: the paramiko transport is monkeypatched so the
                   skill's logic is exercised offline, with no real cluster)

Same offline strategy as the sibling orca-* harnesses: import the skill module and
replace common_utils' submit_slurm / poll_slurm / download_results (as bound on the
skill module) with fakes; the "download" fake drops a canned .out with ORCA's
normal-termination sentinel.

v2.0 XANES is a THREE-step recipe the runner splits into TWO cluster submissions
with a local parse in between:

  A. optsp submission — step1 (calc_type="opt") + step2 (calc_type="xanes_sp", a
     KeepDens single point), chained in "<mol>-xanes_<el>_optsp.inp" with a "$new_job"
     separator. Its .out yields the ORBITAL ENERGIES table and the optimized
     geometry, which the skill parses locally to resolve the core-orbital donor
     window (a, b) for the K-edge target element and to write "<mol>_opt.xyz".
  B. tddft submission — step3 (calc_type="xanes_tddft") as a fresh single point on
     the optimized geometry, in the canonical "<mol>-xanes_<el>.inp" (no "$new_job"),
     carrying "%tddft nroots N tda false MaxIter K orbwin[0] = a, b, -1, -1 end"
     with the resolved window.

ZORA is orthogonal in v2.0 (Br/I only), so benzene's every step is zora=False and
NO "ZORA" keyword appears. Checks split by submission: the optsp .inp carries the
opt keyword line (OPT PBE0 D4 def2-SVP), the SP step (SP ... DecontractAux
KeepDens), the "$new_job" separator, the initial-geometry xyzfile line, and CPCM
on both steps when solvated; the tddft .inp carries the "%tddft ... orbwin[0] ="
block with MaxIter and nroots from params (window resolved from the parsed table,
not hard-coded), the opt-geometry upload, and no "$new_job". Success stamps the
whitelist fields (incl. xanes_orbwin / xanes_target_element) without touching
upstream/task_id; a non-XANES task is passed through untouched; params-not-ok ->
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
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "orca-xanes")
SCRIPTS_DIR = os.path.join(SKILL_DIR, "scripts")
ENTRY = os.path.join(SCRIPTS_DIR, "run_orca_xanes.py")
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
    return importlib.import_module("run_orca_xanes")


def _xanes_task(task_id: str, solvent_model, xyz_path: str, params_status: str = "success") -> Dict[str, Any]:
    """A well-formed three-step XANES task as param-recommendation would emit it.

    v2.0 XANES is a THREE-step recipe (opt -> SP KeepDens -> core-excitation
    TD-DFT); the runner splits it into two cluster submissions and resolves the
    ``orb_win`` donor window itself from the SP job's ORBITAL ENERGIES table, so
    ``step3["orb_win"]`` arrives as None. ZORA is orthogonal (Br/I only) — benzene
    carries none, so every step has ``zora=False``. The K-edge target element lives
    in ``extra_params`` (carbon here).
    """
    return {
        "task_id": task_id,
        "molecule_name": "苯",
        "target_spectra": "xanes",
        "solvent_std": "CPCM(Water)" if solvent_model else "",
        "smiles": "c1ccccc1",
        "xyz_file": xyz_path,
        "num_atoms": 12,
        "system_type": "neutral",
        "extra_params": {"target_element": "C"},
        "params": {
            "step1": {"calc_type": "opt", "functional": "PBE0", "dispersion": "D4",
                      "basis_set": "def2-SVP", "ri_acceleration": "RIJCOSX",
                      "auxiliary_basis": "AutoAux", "zora": False, "solvent_model": solvent_model},
            "step2": {"calc_type": "xanes_sp", "functional": "PBE0", "dispersion": "D4",
                      "basis_set": "def2-TZVP", "ri_acceleration": "RIJCOSX",
                      "auxiliary_basis": "AutoAux", "decontract_aux": True, "keepdens": True,
                      "zora": False, "solvent_model": solvent_model},
            "step3": {"calc_type": "xanes_tddft", "functional": "PBE0", "dispersion": "D4",
                      "basis_set": "def2-TZVP", "ri_acceleration": "RIJCOSX",
                      "auxiliary_basis": "AutoAux", "decontract_aux": True, "keepdens": True,
                      "zora": False, "solvent_model": solvent_model, "nroots": 50,
                      "tda": False, "tddft_maxiter": 500, "orb_win": None},
        },
        "smiles_status": "success",
        "xyz_status": "success",
        "params_status": params_status,
    }


def step_golden() -> Dict[str, Any]:
    failures: List[str] = []
    skill = _import_skill()

    # A canned optsp .out: an ORBITAL ENERGIES table whose first two occupied
    # orbitals sit in the carbon K-edge window (-310..-270 eV) so the skill
    # resolves orb_win=(0, 1), a CARTESIAN COORDINATES block for the optimized
    # geometry, and the normal-termination sentinel.
    def _normal_body() -> str:
        return (
            "normal run\n"
            "THE OPTIMIZATION HAS CONVERGED\n"
            "ORBITAL ENERGIES\n"
            "----------------\n"
            "  NO   OCC          E(Eh)            E(eV)\n"
            "   0   2.0000     -10.600000       -290.000000\n"
            "   1   2.0000     -10.500000       -285.000000\n"
            "   2   2.0000      -0.400000        -10.884000\n"
            "\n"
            "CARTESIAN COORDINATES (ANGSTROEM)\n"
            "---------------------------------\n"
            "  C     0.000000    0.000000    0.000000\n"
            "  C     0.000000    0.000000    1.400000\n"
            "\n"
            + skill.orca_input.ORCA_NORMAL_TERMINATION + "\n"
        )

    def _inp_for(submissions, suffix):
        """Return the .inp text uploaded by the submission whose inp ends in suffix."""
        for sub in submissions:
            for p in sub["upload_files"]:
                if p.endswith(suffix):
                    return open(p, encoding="utf-8").read()
        return ""

    with tempfile.TemporaryDirectory() as td:
        cwd0 = os.getcwd()
        os.chdir(td)
        try:
            xyz_path = os.path.join(td, "benzene_initial.xyz")
            with open(xyz_path, "w", encoding="utf-8") as f:
                f.write("2\n\nC 0 0 0\nC 0 0 1.4\n")

            calls: Dict[str, Any] = {"submissions": []}
            mode = {"download": "normal"}  # normal | missing | abnormal

            def fake_submit(work_dir, script="submit.sh", upload_files=None, **conn):
                calls["submissions"].append({
                    "work_dir": work_dir,
                    "script": script,
                    "upload_files": list(upload_files or []),
                })
                return "66778"

            def fake_poll(job_id, interval=30, timeout=7200, **conn):
                calls["polled"] = job_id
                return "COMPLETED"

            def fake_download(remote_dir, files, local_dir, **conn):
                os.makedirs(local_dir, exist_ok=True)
                if mode["download"] == "missing":
                    return [], list(files)
                body = _normal_body() if mode["download"] == "normal" else "run died early\n"
                for name in files:
                    with open(os.path.join(local_dir, name), "w", encoding="utf-8") as f:
                        f.write(body)
                return list(files), []

            skill.submit_slurm = fake_submit
            skill.poll_slurm = fake_poll
            skill.download_results = fake_download

            # G1: solvated XANES task succeeds; two submissions; no mutation
            mode["download"] = "normal"
            calls["submissions"] = []
            t = _xanes_task("t1_ok", "CPCM(Water)", xyz_path)
            out = skill.process_task(dict(t), FAKE_CLUSTER)
            check(out.get("calc_status") == "success",
                  f"G1 calc_status={out.get('calc_status')!r} (want success); err={out.get('calc_error')!r}", failures)
            check(out.get("slurm_job_id") == "66778", f"G1 slurm_job_id={out.get('slurm_job_id')!r}", failures)
            check(isinstance(out.get("calc_wallclock"), int), f"G1 calc_wallclock not int: {out.get('calc_wallclock')!r}", failures)
            check(out.get("out_file") and os.path.isfile(out["out_file"]), "G1 out_file missing on disk", failures)
            check(out.get("task_id") == "t1_ok" and out.get("params_status") == "success",
                  "G1 upstream field / task_id mutated", failures)
            check(out.get("xanes_orbwin") == [0, 1, -1, -1],
                  f"G1 xanes_orbwin={out.get('xanes_orbwin')!r} (want [0, 1, -1, -1])", failures)
            check(out.get("xanes_target_element") == "C",
                  f"G1 xanes_target_element={out.get('xanes_target_element')!r}", failures)
            check(len(calls["submissions"]) == 2,
                  f"G1 expected 2 submissions (optsp + tddft), got {len(calls['submissions'])}", failures)

            # Submission A — optsp: opt + SP KeepDens, chained. Job files carry
            # the edge element (naming.job_variant, iron rule 34).
            ups_a = [os.path.basename(p) for p in (calls["submissions"][0]["upload_files"] if calls["submissions"] else [])]
            check(any(p.endswith("_xanes_C_optsp.inp") for p in ups_a)
                  and any(p.endswith("_xanes_C_optsp.sh") for p in ups_a)
                  and any(p.endswith("_initial.xyz") for p in ups_a),
                  f"G1 optsp upload trio wrong: {ups_a}", failures)
            optsp_txt = _inp_for(calls["submissions"], "_xanes_C_optsp.inp")
            check("OPT PBE0 D4 def2-SVP" in optsp_txt, f"G1 optsp .inp missing opt keyword line:\n{optsp_txt}", failures)
            check("SP PBE0 D4 def2-TZVP" in optsp_txt, f"G1 optsp .inp missing SP KeepDens step:\n{optsp_txt}", failures)
            check("DecontractAux" in optsp_txt and "KeepDens" in optsp_txt,
                  f"G1 optsp .inp missing DecontractAux/KeepDens on SP step:\n{optsp_txt}", failures)
            check("$new_job" in optsp_txt, f"G1 optsp .inp missing $new_job separator:\n{optsp_txt}", failures)
            check(optsp_txt.count("CPCM(Water)") == 2,
                  f"G1 optsp .inp should carry CPCM(Water) on BOTH steps:\n{optsp_txt}", failures)
            check("xyzfile 0 1 benzene_initial.xyz" in optsp_txt,
                  f"G1 optsp .inp step1 xyzfile line wrong:\n{optsp_txt}", failures)
            check("ZORA" not in optsp_txt, f"G1 optsp .inp must NOT carry ZORA (orthogonal, Br/I only):\n{optsp_txt}", failures)

            # Submission B — tddft: core-excitation single point on the opt geometry
            ups_b = [os.path.basename(p) for p in (calls["submissions"][1]["upload_files"] if len(calls["submissions"]) > 1 else [])]
            check(any(p.endswith("_xanes_C.inp") and not p.endswith("_optsp.inp") for p in ups_b)
                  and any(p.endswith("_xanes_C.sh") and not p.endswith("_optsp.sh") for p in ups_b)
                  and any(p.endswith("_opt.xyz") for p in ups_b),
                  f"G1 tddft upload trio wrong: {ups_b}", failures)
            check(os.path.basename(out.get("out_file") or "").endswith("_xanes_C.out"),
                  f"G1 out_file must carry the edge element: {out.get('out_file')!r}", failures)
            tddft_txt = ""
            for sub in calls["submissions"]:
                for p in sub["upload_files"]:
                    if p.endswith("_xanes_C.inp") and not p.endswith("_optsp.inp"):
                        tddft_txt = open(p, encoding="utf-8").read()
            check("%tddft" in tddft_txt and "orbwin[0] =" in tddft_txt,
                  f"G1 tddft .inp missing %tddft orbwin block:\n{tddft_txt}", failures)
            check("orbwin[0] = 0, 1, -1, -1" in tddft_txt,
                  f"G1 tddft .inp orbwin window not resolved from parsed table:\n{tddft_txt}", failures)
            check("MaxIter 500" in tddft_txt, f"G1 tddft .inp missing MaxIter 500:\n{tddft_txt}", failures)
            check("nroots 50 tda false" in tddft_txt, f"G1 tddft .inp missing nroots 50 tda false:\n{tddft_txt}", failures)
            check("$new_job" not in tddft_txt, f"G1 tddft .inp must be a single job (no $new_job):\n{tddft_txt}", failures)
            check("xyzfile 0 1 " in tddft_txt and "_opt.xyz" in tddft_txt,
                  f"G1 tddft .inp must reference the optimized geometry:\n{tddft_txt}", failures)
            check("ZORA" not in tddft_txt, f"G1 tddft .inp must NOT carry ZORA (orthogonal, Br/I only):\n{tddft_txt}", failures)

            # G2: gas-phase XANES task -> no CPCM in either .inp
            calls["submissions"] = []
            t2 = _xanes_task("t2_gas", None, xyz_path)
            out2 = skill.process_task(dict(t2), FAKE_CLUSTER)
            check(out2.get("calc_status") == "success", f"G2 calc_status={out2.get('calc_status')!r}", failures)
            optsp2 = _inp_for(calls["submissions"], "_xanes_C_optsp.inp")
            tddft2 = ""
            for sub in calls["submissions"]:
                for p in sub["upload_files"]:
                    if p.endswith("_xanes_C.inp") and not p.endswith("_optsp.inp"):
                        tddft2 = open(p, encoding="utf-8").read()
            check("CPCM" not in optsp2, f"G2 gas-phase optsp .inp must have no CPCM:\n{optsp2}", failures)
            check("CPCM" not in tddft2, f"G2 gas-phase tddft .inp must have no CPCM:\n{tddft2}", failures)
            check("orbwin[0] =" in tddft2, f"G2 gas-phase tddft .inp still needs the orbwin block:\n{tddft2}", failures)

            # G3: non-XANES task passed through UNTOUCHED
            ir = {"task_id": "t3_ir", "target_spectra": "ir", "params_status": "success",
                  "molecule_name": "苯"}
            out3 = skill.process_task(dict(ir), FAKE_CLUSTER)
            check("calc_status" not in out3, f"G3 non-XANES task must be untouched, got calc_status={out3.get('calc_status')!r}", failures)

            # G4: XANES task with params not ok -> skipped
            t4 = _xanes_task("t4_skip", "CPCM(Water)", xyz_path, params_status="failed")
            out4 = skill.process_task(dict(t4), FAKE_CLUSTER)
            check(out4.get("calc_status") == "skipped", f"G4 calc_status={out4.get('calc_status')!r} (want skipped)", failures)

            # G5: .out never produced -> failed
            mode["download"] = "missing"
            t5 = _xanes_task("t5_missing", "CPCM(Water)", xyz_path)
            out5 = skill.process_task(dict(t5), FAKE_CLUSTER)
            check(out5.get("calc_status") == "failed", f"G5 calc_status={out5.get('calc_status')!r} (want failed)", failures)

            # G6: abnormal ORCA termination -> failed
            mode["download"] = "abnormal"
            t6 = _xanes_task("t6_abnormal", "CPCM(Water)", xyz_path)
            out6 = skill.process_task(dict(t6), FAKE_CLUSTER)
            check(out6.get("calc_status") == "failed", f"G6 calc_status={out6.get('calc_status')!r} (want failed)", failures)
            check(out6.get("calc_error") == "XANES opt/SP step: optimization_not_converged",
                  f"G6 calc_error={out6.get('calc_error')!r}", failures)
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
