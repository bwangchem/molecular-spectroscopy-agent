#!/usr/bin/env python3
"""Integration tests for bounded retries in all six ORCA runners."""

import importlib.util
import os
import sys
import tempfile
import unittest

from msa_shared.orca_diagnostics import (
    CIS_ERROR_TERMINATION,
    OPTIMIZATION_CONVERGED,
    ORCA_NORMAL_TERMINATION,
    RPA_NOT_CONVERGED,
)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLUSTER = {
    "host": "invalid", "user": "tester", "password": "unused", "key_file": None,
    "remote_root": "/scratch/test", "orca_bin": "/opt/orca", "mpi_prefix": "/opt/mpi",
}


def load_runner(spec):
    scripts = os.path.join(ROOT, "skills", f"orca-{spec}", "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    path = os.path.join(scripts, f"run_orca_{spec}.py")
    module_spec = importlib.util.spec_from_file_location(f"retry_runner_{spec}", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def step(calc_type, basis="ma-def2-TZVP", **extra):
    value = {
        "calc_type": calc_type, "functional": "PBE0", "basis_set": basis,
        "zora": False, "solvent_model": None,
    }
    value.update(extra)
    return value


def task_for(spec, xyz_path):
    params_by_spec = {
        "ir": {"step1": step("opt_freq", None)},
        "raman": {"step1": step("opt_freq", "ma-def2-SVP"),
                  "step2": step("numfreq_raman")},
        "uvvis": {"step1": step("opt_freq"),
                  "step2": step("uvvis_tddft", nroots=30, tda=False, maxdim=60)},
        "fluo": {"step1": step("opt", "ma-def2-SVP"),
                 "step2": step("fluo_es_opt", "ma-def2-SVP", nroots=5, iroot=1, tda=False),
                 "step3": step("fluo_emission", nroots=5, iroot=1, tda=False)},
        "nmr": {"step1": step("opt"),
                "step2": step("nmr", nmr_nucleus="H", scf_thresh="1e-8")},
        "xanes": {"step1": step("opt", "def2-SVP"),
                  "step2": step("xanes_sp", "def2-TZVP", decontract_aux=True, keepdens=True),
                  "step3": step("xanes_tddft", "def2-TZVP", decontract_aux=True,
                                keepdens=True, nroots=50, tda=False,
                                tddft_maxiter=500, orb_win=None)},
    }
    params = params_by_spec[spec]
    fallback = {name: dict(value) for name, value in params.items()}
    changed = []
    if spec == "raman":
        fallback["step2"]["basis_set"] = "ma-def2-SVP"
        changed = ["step2"]
    elif spec == "uvvis":
        fallback["step1"]["basis_set"] = "ma-def2-SVP"
        fallback["step2"]["basis_set"] = "ma-def2-SVP"
        changed = ["step1", "step2"]
    elif spec == "fluo":
        fallback["step3"]["basis_set"] = "ma-def2-SVP"
        changed = ["step3"]
    elif spec == "nmr":
        fallback["step2"]["scf_thresh"] = "1e-7"
        changed = ["step2"]
    elif spec == "xanes":
        fallback["step2"]["basis_set"] = "def2-SVP"
        fallback["step3"]["basis_set"] = "def2-SVP"
        changed = ["step2", "step3"]

    task = {
        "task_id": f"task-{spec}", "molecule_name": "benzene",
        "target_spectra": spec, "smiles": "c1ccccc1", "xyz_file": xyz_path,
        "charge": 0, "multiplicity": 1, "params": params,
        "params_fallbacks": ([] if spec == "ir" else [{
            "fallback_kind": "scf_threshold" if spec == "nmr" else "basis",
            "changed_steps": changed, "params": fallback,
        }]),
        "params_status": "success",
    }
    if spec == "xanes":
        task["extra_params"] = {"target_element": "C"}
    return task


def normal_output(banners):
    return (f"{OPTIMIZATION_CONVERGED}\n" * banners) + ORCA_NORMAL_TERMINATION + "\n"


def xanes_optsp_output():
    return (
        normal_output(1)
        + "ORBITAL ENERGIES\n----------------\n"
        + "  NO   OCC          E(Eh)            E(eV)\n"
        + "   0   2.0000     -10.600000       -290.000000\n"
        + "   1   2.0000     -10.500000       -285.000000\n\n"
        + "CARTESIAN COORDINATES (ANGSTROEM)\n---------------------------------\n"
        + "  C     0.000000    0.000000    0.000000\n\n"
    )


def install_transport(runner, outputs):
    state = {"submits": 0, "downloads": 0}

    def submit(*_args, **_kwargs):
        state["submits"] += 1
        return str(1000 + state["submits"])

    def poll(*_args, **_kwargs):
        return "COMPLETED"

    def download(_remote, files, local_dir, **_kwargs):
        body = outputs[state["downloads"]]
        state["downloads"] += 1
        os.makedirs(local_dir, exist_ok=True)
        for name in files:
            with open(os.path.join(local_dir, name), "w", encoding="utf-8") as f:
                f.write(body)
        return list(files), []

    runner.submit_slurm = submit
    runner.poll_slurm = poll
    runner.download_results = download
    return state


class OrcaRuntimeRetryTests(unittest.TestCase):
    def test_five_single_submission_runners_retry_only_from_upstream_recipes(self):
        cases = {
            "raman": ("Calculation 1 of 2 is missing", 1, "numfreq_incomplete"),
            "uvvis": (f"{RPA_NOT_CONVERGED}\n{CIS_ERROR_TERMINATION}", 1, "rpa_not_converged"),
            "fluo": (f"{RPA_NOT_CONVERGED}\n{CIS_ERROR_TERMINATION}", 2, "rpa_not_converged"),
            "nmr": ("SCF NOT CONVERGED AFTER 125 CYCLES", 1, "scf_not_converged"),
        }
        with tempfile.TemporaryDirectory() as td:
            cwd = os.getcwd()
            os.chdir(td)
            try:
                xyz = os.path.join(td, "benzene_initial.xyz")
                with open(xyz, "w", encoding="utf-8") as f:
                    f.write("1\n\nC 0 0 0\n")
                for spec, (failure, banners, reason) in cases.items():
                    with self.subTest(spec=spec):
                        runner = load_runner(spec)
                        state = install_transport(runner, [failure, normal_output(banners)])
                        task = task_for(spec, xyz)
                        if spec == "nmr":
                            result = runner.run_group([task], CLUSTER)[0]
                        else:
                            result = runner.process_task(task, CLUSTER)
                        self.assertEqual(result.get("calc_status"), "success")
                        self.assertEqual(state["submits"], 2)
                        self.assertEqual([a["failure_reason"] for a in result["calc_attempts"]],
                                         [reason, "none"])
                        self.assertEqual(result["calc_attempts"][1]["params_source"],
                                         "params_fallbacks[0]")
                        self.assertTrue(result["calc_fallback_applied"])
            finally:
                os.chdir(cwd)

    def test_ir_optimization_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = os.getcwd()
            os.chdir(td)
            try:
                xyz = os.path.join(td, "benzene_initial.xyz")
                with open(xyz, "w", encoding="utf-8") as f:
                    f.write("1\n\nC 0 0 0\n")
                runner = load_runner("ir")
                state = install_transport(runner, [ORCA_NORMAL_TERMINATION])
                result = runner.process_task(task_for("ir", xyz), CLUSTER)
                self.assertEqual(result.get("calc_status"), "failed")
                self.assertEqual(state["submits"], 1)
                self.assertEqual(result["calc_attempts"][0]["failure_reason"],
                                 "optimization_not_converged")
            finally:
                os.chdir(cwd)

    def test_xanes_retries_optsp_and_core_independently(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = os.getcwd()
            os.chdir(td)
            try:
                xyz = os.path.join(td, "benzene_initial.xyz")
                with open(xyz, "w", encoding="utf-8") as f:
                    f.write("1\n\nC 0 0 0\n")
                runner = load_runner("xanes")
                state = install_transport(runner, [
                    "SCF NOT CONVERGED AFTER 125 CYCLES",
                    xanes_optsp_output(),
                    f"{RPA_NOT_CONVERGED}\n{CIS_ERROR_TERMINATION}",
                    normal_output(0),
                ])
                result = runner.process_task(task_for("xanes", xyz), CLUSTER)
                self.assertEqual(result.get("calc_status"), "success")
                self.assertEqual(state["submits"], 4)
                self.assertEqual([a["submission"] for a in result["calc_attempts"]],
                                 ["optsp", "optsp", "core", "core"])
                self.assertEqual(result["calc_basis_final"], "def2-SVP")
                self.assertEqual(result["calc_fallback_steps"], ["step2", "step3"])
                self.assertNotIn("_suffixed", runner.__dict__)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
