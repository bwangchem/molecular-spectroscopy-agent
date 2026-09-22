#!/usr/bin/env python3
"""Focused tests for the computation-fallback meta-contract rules."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load(name: str):
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MetaContractTests(unittest.TestCase):
    def test_schema_examples_include_ordered_fallbacks_and_attempts(self):
        schema = _load("dump_task_schema")
        params = schema.STAGE_SCHEMA["params"]["append_fields"]
        calc = schema.STAGE_SCHEMA["calc"]["append_fields"]

        self.assertIsInstance(params["params_fallbacks"], list)
        self.assertEqual(params["params_fallbacks"][0]["fallback_kind"], "basis")
        self.assertIsInstance(calc["calc_attempts"], list)
        self.assertFalse(calc["calc_fallback_applied"])
        self.assertEqual(calc["calc_basis_fallback"], "none")

    def test_orca_runtime_constraints_reject_inline_diagnostics_and_paths(self):
        validator = _load("validate_skill")
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "orca-demo"
            scripts = skill / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "run_demo.py").write_text(
                'msg = "SCF NOT CONVERGED AFTER"\n'
                'inp = f"{mol}-ir.inp"\n'
                'def _suffixed(path):\n    return path\n',
                encoding="utf-8",
            )
            findings = []
            validator.check_orca_runtime_constraints(str(skill), findings)

        self.assertTrue(any("orca_diagnostics" in item for item in findings))
        self.assertTrue(any("naming.*" in item for item in findings))
        self.assertTrue(any("_suffixed" in item for item in findings))

    def test_orca_runtime_constraints_accept_shared_helpers(self):
        validator = _load("validate_skill")
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "orca-demo"
            scripts = skill / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "run_demo.py").write_text(
                "from msa_shared import naming\n"
                "from msa_shared.orca_diagnostics import classify_orca_output\n"
                "path = naming.orca_inp('benzene', 'ir')\n",
                encoding="utf-8",
            )
            findings = []
            validator.check_orca_runtime_constraints(str(skill), findings)

        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
