#!/usr/bin/env python3
"""Expert-confirmed shared computation behavior."""

import os
import unittest

from msa_shared import common_utils, naming, orca_input, orca_output
from msa_shared.constants.nmr_ref import tms_reference_for
from msa_shared.orca_diagnostics import (
    classify_orca_output,
    expected_optimization_banners,
    recipe_candidates,
    summarize_attempts,
)


NORMAL = "****ORCA TERMINATED NORMALLY****"


class OrcaDiagnosticsTests(unittest.TestCase):
    def test_a2_requires_both_rpa_and_cis_lines(self):
        rpa = "RPA/TD-DFT did not converge! You may try raising the number of iterations."
        cis = "ORCA finished by error termination in CIS"
        self.assertEqual(classify_orca_output(rpa).reason, "orca_terminated_abnormally")
        self.assertEqual(classify_orca_output(cis).reason, "cis_error")
        diag = classify_orca_output(f"{rpa}\nProcess 7: {cis}")
        self.assertEqual((diag.verdict, diag.reason), ("retry", "rpa_not_converged"))

    def test_failure_priority_and_optimization_banner_count(self):
        scf = classify_orca_output("SCF NOT CONVERGED AFTER 125 CYCLES")
        self.assertEqual((scf.verdict, scf.reason), ("retry", "scf_not_converged"))
        numfreq = classify_orca_output(
            "Calculation 3 of 9 is missing", allow_numfreq=True
        )
        self.assertEqual((numfreq.verdict, numfreq.reason), ("retry", "numfreq_incomplete"))
        opt = classify_orca_output(NORMAL, expected_optimization_banners=1)
        self.assertEqual((opt.verdict, opt.reason), ("failed", "optimization_not_converged"))
        success = classify_orca_output(
            f"THE OPTIMIZATION HAS CONVERGED\n{NORMAL}",
            expected_optimization_banners=1,
        )
        self.assertEqual((success.verdict, success.reason), ("success", "none"))


class OrcaRetryContractTests(unittest.TestCase):
    def test_recipe_candidates_filter_fallbacks_by_submission_steps(self):
        task = {
            "params": {
                "step1": {"calc_type": "opt", "basis_set": "def2-SVP"},
                "step2": {"calc_type": "xanes_sp", "basis_set": "def2-TZVP"},
                "step3": {"calc_type": "xanes_tddft", "basis_set": "def2-TZVP"},
            },
            "params_fallbacks": [{
                "fallback_kind": "basis",
                "changed_steps": ["step2", "step3"],
                "params": {
                    "step1": {"calc_type": "opt", "basis_set": "def2-SVP"},
                    "step2": {"calc_type": "xanes_sp", "basis_set": "def2-SVP"},
                    "step3": {"calc_type": "xanes_tddft", "basis_set": "def2-SVP"},
                },
            }],
        }
        optsp = recipe_candidates(task, applicable_steps={"step1", "step2"})
        core = recipe_candidates(task, applicable_steps={"step3"})
        self.assertEqual([item["params_source"] for item in optsp],
                         ["params", "params_fallbacks[0]"])
        self.assertEqual([item["params_source"] for item in core],
                         ["params", "params_fallbacks[0]"])
        self.assertEqual(expected_optimization_banners(optsp[0]["params"]), 1)
        self.assertEqual(expected_optimization_banners({
            "step1": {"calc_type": "opt"},
            "step2": {"calc_type": "fluo_es_opt"},
        }), 2)

    def test_attempt_summary_uses_successful_production_recipe(self):
        task = {
            "params": {"step1": {"basis_set": "ma-def2-TZVP"}},
            "params_fallbacks": [{
                "fallback_kind": "scf_threshold",
                "changed_steps": ["step1"],
                "params": {"step1": {"basis_set": "ma-def2-TZVP", "scf_thresh": "1e-7"}},
            }],
        }
        attempts = [
            {"attempt": 0, "params_source": "params", "submission": "single",
             "changed_steps": [], "status": "failed", "failure_reason": "scf_not_converged",
             "out_file": "a.out"},
            {"attempt": 1, "params_source": "params_fallbacks[0]", "submission": "single",
             "changed_steps": ["step1"], "status": "success", "failure_reason": "none",
             "out_file": "a.out"},
        ]
        summary = summarize_attempts(task, attempts, production_submission="single")
        self.assertTrue(summary["calc_fallback_applied"])
        self.assertEqual(summary["calc_basis_original"], "ma-def2-TZVP")
        self.assertEqual(summary["calc_basis_final"], "ma-def2-TZVP")
        self.assertEqual(summary["calc_basis_fallback"], "none")
        self.assertEqual(summary["calc_fallback_steps"], ["step1"])
        self.assertEqual(summary["calc_fallback_reason"], "scf_not_converged")
        self.assertEqual(summary["calc_scf_thresh"], "1e-7")


class NmrReferenceTests(unittest.TestCase):
    def test_five_expert_reference_pairs(self):
        expected = {
            ("neutral", "pcSseg-2"): (31.36, 186.11),
            ("anion", "aug-pcSseg-1"): (31.32, 186.58),
            ("neutral", "def2-TZVP"): (31.44, 187.51),
            ("zwitterion", "ma-def2-TZVP"): (31.44, 187.39),
            ("anion", "ma-def2-SVP"): (31.32, 195.48),
        }
        for key, pair in expected.items():
            with self.subTest(key=key):
                ref = tms_reference_for(*key)
                self.assertEqual((ref["H"], ref["C"]), pair)

    def test_legacy_call_uses_historical_default_basis(self):
        self.assertEqual(tms_reference_for()["H"], 31.36)
        self.assertEqual(tms_reference_for("anion")["C"], 186.58)


class OrcaInputTests(unittest.TestCase):
    @staticmethod
    def _step(calc_type, **extra):
        return {"calc_type": calc_type, "functional": "PBE0", **extra}

    def test_opt_and_opt_freq_insert_geom_directly_after_maxcore(self):
        for calc_type in ("opt", "opt_freq"):
            with self.subTest(calc_type=calc_type):
                text = orca_input.build_inp({"step1": self._step(calc_type)}, "mol.xyz")
                lines = text.splitlines()
                maxcore = next(i for i, line in enumerate(lines) if line.startswith("%maxcore"))
                self.assertEqual(lines[maxcore + 1], "%geom MaxIter 300 cartfallback true end")

    def test_fluo_geom_is_not_duplicated_and_nmr_threshold_is_explicit(self):
        fluo = self._step("fluo_es_opt", nroots=5, iroot=1)
        text = orca_input.build_inp({"step1": fluo}, "mol.xyz")
        self.assertEqual(text.count("%geom MaxIter 300 cartfallback true end"), 1)

        nmr = self._step("nmr", scf_thresh="1e-7")
        text = orca_input.build_inp({"step1": nmr}, "mol.xyz")
        self.assertIn("%scf Thresh 1e-7 MaxIter 500 end", text)
        with self.assertRaises(ValueError):
            orca_input.build_inp(
                {"step1": self._step("nmr", scf_thresh="1e-6")}, "mol.xyz"
            )


class NamingAndOutputTests(unittest.TestCase):
    def tearDown(self):
        naming.set_run_id("")

    def test_system_separator_is_underscore_but_molecule_hyphen_survives(self):
        path = naming.orca_inp("p-nitrophenol", "ir")
        self.assertEqual(os.path.basename(path), "p-nitrophenol_ir.inp")
        self.assertEqual(os.path.basename(naming.peaks_csv("p-nitrophenol", "ir")),
                         "p-nitrophenol_ir_peaks.csv")

    def test_spectrum_kwargs_prefers_final_basis_then_params_fallback(self):
        task = {
            "target_spectra": "nmr",
            "extra_params": {"target_nucleus": "1H"},
            "calc_basis_final": "ma-def2-SVP",
            "params": {"step1": {"basis_set": "ma-def2-TZVP"},
                       "step2": {"basis_set": "ma-def2-TZVP"}},
        }
        self.assertEqual(common_utils.spectrum_kwargs(task)["basis_set"], "ma-def2-SVP")
        task.pop("calc_basis_final")
        self.assertEqual(common_utils.spectrum_kwargs(task)["basis_set"], "ma-def2-TZVP")

    def test_nmr_uses_actual_shielding_basis(self):
        text = "CHEMICAL SHIELDING SUMMARY (ppm)\n  1 H 30.000 0.0\n\n"
        spectrum = orca_output.build_nmr(
            text, target_nucleus="1H", system_type="anion", basis_set="ma-def2-SVP"
        )
        self.assertAlmostEqual(spectrum["peaks"][0]["chemical_shift_delta_ppm"], 1.32)
        self.assertEqual(spectrum["meta"]["shielding_basis"], "ma-def2-SVP")

    def test_xanes_transitions_outside_fixed_window_raise_with_energies(self):
        text = (
            "ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS\n"
            "  1 7258989.6 1.3776 0.1\n\n"
        )
        with self.assertRaisesRegex(ValueError, r"900.*275.*320"):
            orca_output.build_xanes(text, target_element="C")


if __name__ == "__main__":
    unittest.main()
