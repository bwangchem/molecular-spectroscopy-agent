#!/usr/bin/env python3
"""verify_param_recommendation.py — acceptance harness owned by the main session.

Runs three gates against the outsource-produced param-recommendation skill:
  1. static lint  (msa-skill-spec validator)
  2. smoke        (--help exits 0, no traceback)
  3. golden       (hand-crafted upstream task_list in, checked field-by-field)

param-recommendation (P1-5) reads the per-spectrum recipe from
msa_shared.constants.functional_basis and the solvent model from
msa_shared.constants.solvents, then appends `system_type` + a per-step `params`
dict (calc_type / functional / basis_set / zora / solvent_model) to every task.

What the golden gate pins:
  * SETTLED spectra (IR / UV-Vis / Fluo) — exact functional/basis/calc_type,
    because those recipes are frozen for v1.0.
  * PROVISIONAL spectra (NMR / XANES) — only structure/types (step present with
    the right calc_type, functional/basis are non-empty strings, zora is a bool),
    so the planned Part 2 PBE0+D4 swap will not churn this test.
  * solvent_model plumbing: gas phase -> None; a CPCM solvent -> that string.
  * fail-soft on an unknown target_spectra; skip when upstream xyz failed;
    strict append-only (no upstream field or task_id mutated).

exit 0 = all three gates green; non-zero = at least one failure.
"""

import json
import os
import subprocess
import sys
import tempfile
from typing import List, Dict, Any


SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import _functional_basis as functional_basis


def _resolve_repo_root() -> str:
    # Self-locate the repo root from this test's own path; no env var needed.
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    if not os.path.isdir(os.path.join(root, "msa_shared")):
        sys.stderr.write(f"ERROR: repo root '{root}' has no msa_shared/.\n")
        sys.exit(3)
    return root


REPO_ROOT = _resolve_repo_root()
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "param-recommendation")
ENTRY = os.path.join(SKILL_DIR, "scripts", "param_recommendation.py")
SPEC_VALIDATOR = os.path.join(REPO_ROOT, "meta", "msa-skill-spec", "scripts", "validate_skill.py")


def run(cmd: List[str], env=None, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env, cwd=cwd)


def step_lint() -> Dict[str, Any]:
    r = run(["python3", SPEC_VALIDATOR, SKILL_DIR])
    return {"step": "lint", "passed": r.returncode == 0, "returncode": r.returncode,
            "stderr_tail": r.stderr.strip().splitlines()[-4:]}


def step_help() -> Dict[str, Any]:
    r = run(["python3", ENTRY, "--help"])
    passed = r.returncode == 0 and "Traceback" not in (r.stderr + r.stdout)
    return {"step": "help", "passed": passed, "returncode": r.returncode}


def _input_tasks() -> List[Dict[str, Any]]:
    def t(tid, mol, smi, spec, solvent_std, xyz_status="success"):
        return {
            "task_id": tid, "molecule_name": mol, "target_spectra": spec,
            "solvent_raw": "", "solvent_std": solvent_std, "extra_params": {},
            "recognition_status": "success",
            "smiles": smi, "smiles_source": "dict", "smiles_status": "success",
            "xyz_file": f"./local_workspace/{mol}/{mol}_initial.xyz",
            "num_atoms": 12, "xyz_status": xyz_status,
        }
    return [
        t("task_01_苯_ir", "苯", "c1ccccc1", "ir", ""),
        t("task_02_对硝基苯酚_uvvis", "对硝基苯酚", "O=[N+]([O-])c1ccc(O)cc1", "uvvis", "CPCM(Water)"),
        t("task_03_蒽_fluo", "蒽", "c1ccc2cc3ccccc3cc2c1", "fluo", "CPCM(Cyclohexane)"),
        t("task_04_苯甲醛_nmr", "苯甲醛", "O=Cc1ccccc1", "nmr", "CPCM(Chloroform)"),
        t("task_05_吡啶_xanes", "吡啶", "c1ccncc1", "xanes", ""),
        # upstream (xyz) failed -> must skip, no params
        t("task_06_苯_ir", "苯", "c1ccccc1", "ir", "", xyz_status="failed"),
        # unknown spectrum but upstream success -> fail-soft
        t("task_07_苯_bogus", "苯", "c1ccccc1", "bogus", ""),
    ]


def check(cond: bool, msg: str, failures: List[str]) -> None:
    if not cond:
        failures.append(msg)


def _find_step(params: Dict[str, Any], calc_type_substr: str):
    """Return the first step dict whose calc_type contains the substring, or None."""
    if not isinstance(params, dict):
        return None
    for _, step in sorted(params.items()):
        if isinstance(step, dict) and calc_type_substr in str(step.get("calc_type", "")):
            return step
    return None


def step_expert_rules() -> Dict[str, Any]:
    failures: List[str] = []

    zora_cases = {
        "Brc1ccccc1": True,
        "Ic1ccccc1": False,
        "Brc1ccc(I)cc1": False,
        "c1ccccc1": False,
    }
    for smiles, expected in zora_cases.items():
        _, actual = functional_basis.detect_system_type(smiles)
        check(actual is expected, f"ZORA {smiles}: got {actual}, want {expected}", failures)

    neutral_i = functional_basis.recipe_for(
        "nmr", system_type="neutral", contains_iodine=True,
        solvent_model="CPCM(Chloroform)", target_nucleus="13C",
    )
    ionic_i = functional_basis.recipe_for(
        "nmr", system_type="anion", contains_iodine=True,
        solvent_model="CPCM(Chloroform)", target_nucleus="13C",
    )
    check(neutral_i["step1"]["basis_set"] == "def2-SVP",
          "iodine neutral NMR opt step must use def2-SVP", failures)
    check(neutral_i["step2"]["basis_set"] == "def2-TZVP",
          "iodine neutral NMR must use def2-TZVP", failures)
    check(ionic_i["step1"]["basis_set"] == "ma-def2-SVP",
          "iodine ionic NMR opt step must use ma-def2-SVP", failures)
    check(ionic_i["step2"]["basis_set"] == "ma-def2-TZVP",
          "iodine ionic NMR must use ma-def2-TZVP", failures)

    def fallbacks(spec, system_type="anion", contains_iodine=False):
        return functional_basis.fallback_recipes_for(
            spec, system_type=system_type, need_zora=False,
            contains_iodine=contains_iodine, solvent_model="CPCM(Water)",
            target_nucleus="13C",
        )

    check(fallbacks("ir") == [], "IR must not have a fallback", failures)
    check(fallbacks("raman", "neutral") == [], "neutral Raman must not have a fallback", failures)
    raman = fallbacks("raman")
    check(len(raman) == 1 and raman[0]["changed_steps"] == ["step2"],
          "ionic Raman must lower step2 only", failures)
    check(raman[0]["params"]["step2"]["basis_set"] == "ma-def2-SVP",
          "ionic Raman fallback step2 must use ma-def2-SVP", failures)

    uvvis = fallbacks("uvvis")
    check(len(uvvis) == 1 and uvvis[0]["changed_steps"] == ["step1", "step2"],
          "ionic UV-Vis must lower both steps", failures)
    check(all(uvvis[0]["params"][s]["basis_set"] == "ma-def2-SVP" for s in ("step1", "step2")),
          "ionic UV-Vis fallback must use ma-def2-SVP", failures)

    fluo = fallbacks("fluo")
    check(len(fluo) == 1 and fluo[0]["changed_steps"] == ["step3"],
          "ionic fluorescence must lower step3 only", failures)
    check(fluo[0]["params"]["step3"]["basis_set"] == "ma-def2-SVP",
          "ionic fluorescence fallback step3 must use ma-def2-SVP", failures)

    xanes_neutral = fallbacks("xanes", "neutral")
    check(len(xanes_neutral) == 1 and xanes_neutral[0]["changed_steps"] == ["step2", "step3"],
          "neutral XANES must lower step2 and step3", failures)
    check(all(xanes_neutral[0]["params"][s]["basis_set"] == "def2-SVP" for s in ("step2", "step3")),
          "neutral XANES fallback must use def2-SVP", failures)

    nmr = fallbacks("nmr")
    check(len(nmr) == 1 and nmr[0]["fallback_kind"] == "scf_threshold",
          "non-iodine ionic NMR must have one threshold fallback", failures)
    check(nmr[0]["params"]["step2"]["scf_thresh"] == "1e-7",
          "NMR threshold fallback must use 1e-7", failures)

    nmr_i = fallbacks("nmr", contains_iodine=True)
    check(len(nmr_i) == 2,
          "iodine ionic NMR must have two ordered fallbacks", failures)
    check(nmr_i[0]["fallback_kind"] == "scf_threshold" and
          nmr_i[1]["fallback_kind"] == "basis",
          "iodine ionic NMR fallback order must be threshold then basis", failures)
    check(nmr_i[1]["params"]["step2"]["basis_set"] == "ma-def2-SVP" and
          nmr_i[1]["params"]["step2"]["scf_thresh"] == "1e-7",
          "iodine ionic NMR basis fallback must retain 1e-7", failures)
    check(all(item["params"]["step1"]["basis_set"] == "ma-def2-SVP" for item in nmr_i),
          "iodine ionic NMR fallbacks must leave the opt step at ma-def2-SVP", failures)

    return {"step": "expert_rules", "passed": not failures, "failures": failures}


def step_manifest_report() -> Dict[str, Any]:
    failures: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.json")
        out_path = os.path.join(td, "out.json")
        manifest_path = os.path.join(td, "manifest.csv")
        report_path = os.path.join(td, "validation.csv")
        tasks = [
            {"task_id": "i_ir", "mol_id": "mol-I", "molecule_name": "mol-I",
             "smiles": "Ic1ccccc1", "target_spectra": "ir", "xyz_status": "success"},
            {"task_id": "i_nmr", "mol_id": "mol-I", "molecule_name": "mol-I",
             "smiles": "Ic1ccccc1", "target_spectra": "nmr", "xyz_status": "success"},
            {"task_id": "br_ir", "mol_id": "mol-Br", "molecule_name": "mol-Br",
             "smiles": "Brc1ccccc1", "target_spectra": "ir", "xyz_status": "success"},
            {"task_id": "anion_ir", "mol_id": "mol-anion", "molecule_name": "mol-anion",
             "smiles": "CC(=O)[O-]", "target_spectra": "ir", "xyz_status": "success"},
        ]
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump({"task_list": tasks}, f)
        with open(manifest_path, "w", encoding="utf-8", newline="") as f:
            f.write("mol_id,smiles,charge,multiplicity,needs_ZORA\n")
            f.write("mol-I,Ic1ccccc1,0,1,true\n")
            f.write("mol-Br,Brc1ccccc1,0,1,false\n")
            f.write("mol-anion,CC(=O)[O-],0,1,false\n")

        r = run(["python3", ENTRY, "--input", in_path, "--output", out_path,
                 "--manifest", manifest_path, "--validation-report", report_path], cwd=td)
        check(r.returncode == 0, f"manifest report exited {r.returncode}: {r.stderr[-300:]}", failures)
        if os.path.exists(report_path):
            import csv
            with open(report_path, encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            check(list(rows[0]) == ["mol_id", "field", "manifest_value", "pipeline_value",
                                    "classification", "reason"],
                  "manifest report columns are not fixed", failures)
            check(len(rows) == 3, f"manifest report expected 3 differences, got {len(rows)}", failures)
            iodine_rows = [row for row in rows if row["mol_id"] == "mol-I"]
            check(len(iodine_rows) == 1 and iodine_rows[0]["classification"] == "expected_rule_change",
                  "iodine-only ZORA difference must be expected_rule_change and deduplicated", failures)
            check("Br-or-I" in iodine_rows[0]["reason"] and "Br-only" in iodine_rows[0]["reason"],
                  "expected rule-change reason must identify the old and new rules", failures)
            check(sum(row["classification"] == "mismatch" for row in rows) == 2,
                  "all other differences must be mismatch", failures)
        else:
            failures.append("manifest validation report was not created")
        check("checked=3" in r.stderr and "expected_rule_change=1" in r.stderr and "mismatch=2" in r.stderr,
              "manifest summary counts missing from stderr", failures)

        pair = run(["python3", ENTRY, "--input", in_path, "--output", out_path,
                    "--manifest", manifest_path], cwd=td)
        check(pair.returncode == 1 and "--manifest" in pair.stderr and "--validation-report" in pair.stderr,
              "manifest/report CLI options must be supplied together", failures)

    return {"step": "manifest_report", "passed": not failures, "failures": failures}


def step_golden() -> Dict[str, Any]:
    failures: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.json")
        out_path = os.path.join(td, "out.json")
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump({"parse_batch_id": "parse_test", "task_list": _input_tasks(),
                       "parse_warnings": [], "parse_errors": []}, f, ensure_ascii=False)

        env = dict(os.environ)
        r = run(["python3", ENTRY, "--input", in_path, "--output", out_path], env=env, cwd=td)

        if r.returncode != 0:
            return {"step": "golden", "passed": False, "n_pass": 0, "n_total": 1,
                    "fatal": f"skill exited {r.returncode}", "stderr_tail": r.stderr[-600:]}
        try:
            payload = json.load(open(out_path, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            return {"step": "golden", "passed": False, "fatal": f"cannot read output: {e}",
                    "stderr_tail": r.stderr[-600:]}

        tasks = payload["task_list"] if isinstance(payload, dict) and "task_list" in payload else payload
        by_id = {t["task_id"]: t for t in tasks}

        check(len(tasks) == 7, f"expected 7 tasks out, got {len(tasks)}", failures)

        # ---- G1 IR (settled, gas phase): exact recipe, solvent_model None ----
        t1 = by_id.get("task_01_苯_ir", {})
        check(t1.get("params_status") == "success",
              f"IR: params_status={t1.get('params_status')} (want success)", failures)
        check(bool(t1.get("system_type")), "IR: empty system_type", failures)
        check(t1.get("params_fallbacks") == [],
              f"IR params_fallbacks={t1.get('params_fallbacks')} (want empty list)", failures)
        p1 = t1.get("params", {})
        s1 = p1.get("step1", {}) if isinstance(p1, dict) else {}
        check(s1.get("calc_type") == "opt_freq", f"IR step1 calc_type={s1.get('calc_type')} (want opt_freq)", failures)
        check(s1.get("functional") == "r2SCAN-3c", f"IR functional={s1.get('functional')} (want r2SCAN-3c)", failures)
        check(s1.get("basis_set") is None, f"IR basis_set={s1.get('basis_set')} (want None for composite)", failures)
        check(s1.get("zora") is False, f"IR zora={s1.get('zora')} (want False)", failures)
        check(s1.get("solvent_model") is None, f"IR solvent_model={s1.get('solvent_model')} (want None in gas)", failures)
        # append-only
        check(t1.get("molecule_name") == "苯", "IR: molecule_name mutated", failures)
        check(t1.get("target_spectra") == "ir", "IR: target_spectra mutated", failures)
        check(t1.get("xyz_status") == "success", "IR: xyz_status mutated", failures)

        # ---- G2 UV-Vis (settled, aqueous): opt + tddft, solvent propagated ----
        t2 = by_id.get("task_02_对硝基苯酚_uvvis", {})
        check(t2.get("params_status") == "success",
              f"UVVis: params_status={t2.get('params_status')} (want success)", failures)
        p2 = t2.get("params", {})
        opt2 = _find_step(p2, "opt")
        tddft2 = _find_step(p2, "tddft")
        check(opt2 is not None, "UVVis: missing opt step", failures)
        check(tddft2 is not None, "UVVis: missing tddft step", failures)
        if tddft2:
            check(tddft2.get("functional") == "CAM-B3LYP",
                  f"UVVis tddft functional={tddft2.get('functional')} (want CAM-B3LYP)", failures)
            check(tddft2.get("basis_set") == "def2-TZVP",
                  f"UVVis tddft basis_set={tddft2.get('basis_set')} (want def2-TZVP)", failures)
            check(tddft2.get("solvent_model") == "CPCM(Water)",
                  f"UVVis tddft solvent_model={tddft2.get('solvent_model')} (want CPCM(Water))", failures)

        # ---- G3 Fluo (settled): 3 steps, wB97X-D3, SVP (gs opt + es opt) then TZVP (emission) ----
        t3 = by_id.get("task_03_蒽_fluo", {})
        check(t3.get("params_status") == "success",
              f"Fluo: params_status={t3.get('params_status')} (want success)", failures)
        p3 = t3.get("params", {})
        check(isinstance(p3, dict) and len(p3) == 3, f"Fluo: expected 3 steps, got {len(p3) if isinstance(p3, dict) else 'n/a'}", failures)
        if isinstance(p3, dict):
            for name, st in p3.items():
                check(st.get("functional") == "wB97X-D3",
                      f"Fluo {name} functional={st.get('functional')} (want wB97X-D3)", failures)
            check(p3.get("step1", {}).get("basis_set") == "def2-SVP",
                  f"Fluo step1 basis_set={p3.get('step1', {}).get('basis_set')} (want def2-SVP)", failures)
            check(p3.get("step2", {}).get("basis_set") == "def2-SVP",
                  f"Fluo step2 basis_set={p3.get('step2', {}).get('basis_set')} (want def2-SVP)", failures)
            check(p3.get("step3", {}).get("basis_set") == "def2-TZVP",
                  f"Fluo step3 basis_set={p3.get('step3', {}).get('basis_set')} (want def2-TZVP)", failures)

        # ---- G4 NMR (PROVISIONAL): structure/types only ----
        t4 = by_id.get("task_04_苯甲醛_nmr", {})
        check(t4.get("params_status") == "success",
              f"NMR: params_status={t4.get('params_status')} (want success)", failures)
        nmr_step = _find_step(t4.get("params", {}), "nmr")
        check(nmr_step is not None, "NMR: missing a step with calc_type containing 'nmr'", failures)
        if nmr_step:
            check(isinstance(nmr_step.get("functional"), str) and nmr_step["functional"].strip() != "",
                  "NMR: functional must be a non-empty string", failures)
            check(isinstance(nmr_step.get("basis_set"), str) and nmr_step["basis_set"].strip() != "",
                  "NMR: basis_set must be a non-empty string", failures)
            check(isinstance(nmr_step.get("zora"), bool), "NMR: zora must be a bool", failures)
            check(nmr_step.get("solvent_model") == "CPCM(Chloroform)",
                  f"NMR solvent_model={nmr_step.get('solvent_model')} (want CPCM(Chloroform))", failures)

        # ---- G5 XANES (v2.0): PBE0 recipe present; ZORA is orthogonal (Br/I only) ----
        t5 = by_id.get("task_05_吡啶_xanes", {})
        check(t5.get("params_status") == "success",
              f"XANES: params_status={t5.get('params_status')} (want success)", failures)
        xas_step = _find_step(t5.get("params", {}), "xanes_tddft")
        check(xas_step is not None, "XANES: missing a step with calc_type 'xanes_tddft'", failures)
        if xas_step:
            check(isinstance(xas_step.get("functional"), str) and xas_step["functional"].strip() != "",
                  "XANES: functional must be a non-empty string", failures)
            # pyridine has no Br/I, so ZORA must be OFF (v2.0 makes ZORA orthogonal,
            # no longer always-on for XANES).
            check(xas_step.get("zora") is False, f"XANES zora={xas_step.get('zora')} (want False for pyridine, no Br/I)", failures)

        # ---- G6 upstream xyz failed -> skipped, no params ----
        t6 = by_id.get("task_06_苯_ir", {})
        check(t6.get("params_status") == "skipped",
              f"upstream-failed task: params_status={t6.get('params_status')} (want skipped)", failures)
        check(not t6.get("params"), "skipped task must not carry params", failures)
        check(not t6.get("system_type"), "skipped task must not carry system_type", failures)

        # ---- G7 unknown spectrum -> fail-soft, batch survives ----
        t7 = by_id.get("task_07_苯_bogus", {})
        check(t7.get("params_status") == "failed",
              f"unknown spectrum: params_status={t7.get('params_status')} (want failed)", failures)
        check(bool(t7.get("params_error")), "failed task must carry params_error", failures)

    return {"step": "golden", "passed": not failures, "n_pass": 0 if failures else 1,
            "n_total": 1, "failures": failures}


def main() -> None:
    report = {
        "lint": step_lint(),
        "help": step_help(),
        "expert_rules": step_expert_rules(),
        "manifest_report": step_manifest_report(),
        "golden": step_golden(),
    }
    ok = all(step["passed"] for step in report.values())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not ok:
        print("\nFAIL — see failures above", file=sys.stderr)
        sys.exit(1)
    print("\nPASS — all three gates green", file=sys.stderr)


if __name__ == "__main__":
    main()
