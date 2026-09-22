#!/usr/bin/env python3
"""verify_name_to_smiles.py — acceptance harness owned by the main session.

Runs the gates against the outsource-produced name-to-smiles skill:
  1. static lint  (msa-skill-spec validator)
  2. smoke        (--help exits 0, no traceback)
  3. openshell    (white-box probe of the v2.0 closed-shell scope gate)
  4. display_en   (white-box probe of the v2.1 molecule_name_en case normalizer)
  5. golden       (hand-crafted upstream task_list in, checked field-by-field —
                   smiles / smiles_source / molecule_name_en / status)
  6. manifest     (Tier 0: the curated manifest CSV passed via --manifest)

Determinism: the golden step exports MSA_N2S_DISABLE_NETWORK=1 so the skill
must NOT hit PubChem; every asserted molecule is covered by the offline dict,
and the one gibberish molecule must therefore fail-soft (not silently resolve
via network). SMILES equality is compared canonically through RDKit so the
skill is free to emit any valid representation of the correct molecule.

The manifest step deliberately runs WITHOUT the offline switch: every one of its
tasks is a Tier 0 hit, so a run that reaches the network at all has already
broken the tier order. Its fixture is written to a temporary directory rather
than committed — a CSV that looks like a manifest sitting in the repo would
eventually be mistaken for the real curated dataset.

exit 0 = all gates green; non-zero = at least one failure.
"""

import csv
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
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "name-to-smiles")
ENTRY = os.path.join(SKILL_DIR, "scripts", "name_to_smiles.py")
SPEC_VALIDATOR = os.path.join(REPO_ROOT, "meta", "msa-skill-spec", "scripts", "validate_skill.py")

# --- optional RDKit canonicalization (available in the dev env) ---
try:
    from rdkit import Chem
    from rdkit import RDLogger
    RDLogger.DisableLog("rdApp.*")

    def canonical(smi: str):
        if not smi:
            return None
        m = Chem.MolFromSmiles(smi)
        return Chem.MolToSmiles(m) if m else None

    _HAVE_RDKIT = True
except Exception:  # noqa: BLE001
    def canonical(smi: str):
        return (smi or "").strip() or None

    _HAVE_RDKIT = False


def run(cmd: List[str], env=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)


def step_lint() -> Dict[str, Any]:
    r = run(["python3", SPEC_VALIDATOR, SKILL_DIR])
    return {"step": "lint", "passed": r.returncode == 0, "returncode": r.returncode,
            "stderr_tail": r.stderr.strip().splitlines()[-4:]}


def step_help() -> Dict[str, Any]:
    r = run(["python3", ENTRY, "--help"])
    passed = r.returncode == 0 and "Traceback" not in (r.stderr + r.stdout)
    return {"step": "help", "passed": passed, "returncode": r.returncode}


def _load_skill_module():
    """Import the skill entry script as a module for white-box probes."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_n2s_probe", ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def step_openshell() -> Dict[str, Any]:
    """Functional probe of the v2.0 open-shell scope gate.

    We assert directly against the skill's _openshell_reason() rather than the
    offline dict, because every production dict entry is a closed-shell singlet
    (polluting it with a radical just to test the gate is not acceptable). The
    gate must REJECT structural open-shell species (explicit radical electrons
    or an odd total electron count) and PASS every closed-shell molecule,
    including anions and zwitterions. Requires RDKit; skipped (soft-pass) if
    RDKit is unavailable so CI on a minimal env is not blocked.
    """
    if not _HAVE_RDKIT:
        return {"step": "openshell", "passed": True, "skipped": "no rdkit", "failures": []}

    reason = _load_skill_module()._openshell_reason

    failures: List[str] = []
    # Closed-shell (incl. charged) must PASS the gate (reason is None).
    for smi in ("c1ccccc1", "Nc1ccccc1", "Oc1ccc([N+](=O)[O-])cc1",
                "CC(=O)[O-]", "[NH3+]CC(=O)[O-]", "O=O"):
        check(reason(smi) is None, f"closed-shell {smi!r} wrongly flagged open-shell", failures)
    # Structural open-shell must be REJECTED (reason is a non-empty string).
    for smi in ("[CH3]", "[NH2]", "[OH]", "[CH2]"):
        check(bool(reason(smi)), f"open-shell {smi!r} not rejected", failures)

    return {"step": "openshell", "passed": not failures, "failures": failures}


# (input, expected) for the v2.1 English display-case normalizer. The rule is
# "capitalize the head word, never touch anything else": locant / stereo
# prefixes stay lower-case, acronyms and already-cased PubChem Titles are
# passed through verbatim, and trailing words keep their case.
_DISPLAY_CASES = [
    ("benzene", "Benzene"),
    ("p-nitrophenol", "p-Nitrophenol"),      # the expert's worked example
    ("o-xylene", "o-Xylene"),
    ("n-hexane", "n-Hexane"),
    ("tert-butanol", "tert-Butanol"),
    ("1,3-butadiene", "1,3-Butadiene"),
    ("trans-2-butene", "trans-2-Butene"),
    ("2,4-dinitrotoluene", "2,4-Dinitrotoluene"),
    ("N,N-dimethylformamide", "N,N-Dimethylformamide"),
    ("N-methylaniline", "N-Methylaniline"),
    ("ethylene glycol", "Ethylene glycol"),  # only the head word is capitalized
    ("aspartic acid", "Aspartic acid"),
    ("THF", "THF"),                          # acronym untouched
    ("DMSO", "DMSO"),
    ("CH3OH", "CH3OH"),                      # formula untouched
    ("4-Nitrophenol", "4-Nitrophenol"),      # PubChem Title passes through
    ("", ""),
]


def step_display_en() -> Dict[str, Any]:
    """White-box probe of _display_en, the molecule_name_en case normalizer.

    Kept separate from the golden gate because the interesting inputs (acronyms,
    stereo prefixes, PubChem Titles) are not all reachable through the offline
    dictionary, and polluting the production dict to reach them is not acceptable.
    """
    display = _load_skill_module()._display_en
    failures: List[str] = []
    for raw, want in _DISPLAY_CASES:
        got = display(raw)
        check(got == want, f"_display_en({raw!r}) = {got!r} (want {want!r})", failures)
    return {"step": "display_en", "passed": not failures,
            "n_total": len(_DISPLAY_CASES), "failures": failures}


# Expected structures + English display names for the offline-covered molecules
# (textbook-certain). molecule_name_en must come back in PubChem Title case, so
# the dictionary's lower-case "p-nitrophenol" must surface as "p-Nitrophenol".
_EXPECTED = {
    "苯": "c1ccccc1",
    "苯甲醛": "O=Cc1ccccc1",
    "苯胺": "Nc1ccccc1",
    "对硝基苯酚": "Oc1ccc([N+](=O)[O-])cc1",
}
_EXPECTED_EN = {
    "苯": "Benzene",
    "苯甲醛": "Benzaldehyde",
    "苯胺": "Aniline",
    "对硝基苯酚": "p-Nitrophenol",
}


def _input_tasks() -> List[Dict[str, Any]]:
    def t(tid, mol, spec, rec="success", extra=None):
        return {
            "task_id": tid, "molecule_name": mol, "target_spectra": spec,
            "solvent_raw": "", "solvent_std": "", "extra_params": extra or {},
            "recognition_status": rec,
        }
    return [
        t("task_01_苯_ir", "苯", "ir"),
        t("task_02_苯甲醛_nmr", "苯甲醛", "nmr", extra={"target_nucleus": "1H"}),
        t("task_03_苯胺_uvvis", "苯胺", "uvvis"),
        t("task_04_对硝基苯酚_uvvis", "对硝基苯酚", "uvvis"),
        t("task_05_苯_raman", "苯", "raman", rec="failed"),          # upstream failed -> skip
        t("task_06_zzqqx不存在分子_ir", "zzqqx不存在分子", "ir"),      # gibberish -> fail-soft offline
    ]


def check(cond: bool, msg: str, failures: List[str]) -> None:
    if not cond:
        failures.append(msg)


def step_golden() -> Dict[str, Any]:
    failures: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.json")
        out_path = os.path.join(td, "out.json")
        # Wrapped form, mimicking task-recognition output.
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump({"parse_batch_id": "parse_test", "task_list": _input_tasks(),
                       "parse_warnings": [], "parse_errors": []}, f, ensure_ascii=False)

        env = dict(os.environ)
        env["MSA_N2S_DISABLE_NETWORK"] = "1"  # force offline determinism
        r = run(["python3", ENTRY, "--input", in_path, "--output", out_path], env=env)

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

        # append-only / cardinality
        check(len(tasks) == 6, f"expected 6 tasks out, got {len(tasks)}", failures)
        for tid in ("task_01_苯_ir", "task_06_zzqqx不存在分子_ir"):
            check(tid in by_id, f"missing task {tid} in output (batch must not drop tasks)", failures)

        # G1..G4 dict-covered successes
        for tid, mol in (("task_01_苯_ir", "苯"), ("task_02_苯甲醛_nmr", "苯甲醛"),
                         ("task_03_苯胺_uvvis", "苯胺"), ("task_04_对硝基苯酚_uvvis", "对硝基苯酚")):
            t = by_id.get(tid, {})
            check(t.get("smiles_status") == "success", f"{mol}: smiles_status={t.get('smiles_status')} (want success)", failures)
            check(t.get("smiles_source") == "dict", f"{mol}: smiles_source={t.get('smiles_source')} (want dict, offline)", failures)
            check(bool(t.get("smiles")), f"{mol}: empty smiles", failures)
            if _HAVE_RDKIT:
                check(canonical(t.get("smiles")) == canonical(_EXPECTED[mol]),
                      f"{mol}: smiles {t.get('smiles')} != canonical {_EXPECTED[mol]}", failures)
            # v2.1: English display name for figure titles / CSV metadata
            check(t.get("molecule_name_en") == _EXPECTED_EN[mol],
                  f"{mol}: molecule_name_en={t.get('molecule_name_en')!r} (want {_EXPECTED_EN[mol]!r})", failures)
            check(not t.get("smiles_warnings"),
                  f"{mol}: unexpected smiles_warnings={t.get('smiles_warnings')!r}", failures)
            # append-only: upstream fields untouched
            check(t.get("molecule_name") == mol, f"{tid}: molecule_name mutated", failures)
            check(t.get("recognition_status") == "success", f"{tid}: recognition_status mutated", failures)

        # G5 upstream failed -> skipped, no smiles
        ts = by_id.get("task_05_苯_raman", {})
        check(ts.get("smiles_status") == "skipped", f"upstream-failed task: smiles_status={ts.get('smiles_status')} (want skipped)", failures)
        check("smiles" not in ts or not ts.get("smiles"), "skipped task must not carry a smiles value", failures)
        check(not ts.get("molecule_name_en"), "skipped task must not carry molecule_name_en", failures)

        # G6 gibberish offline -> failed (fail-soft), error present, batch survived
        tg = by_id.get("task_06_zzqqx不存在分子_ir", {})
        check(tg.get("smiles_status") == "failed", f"gibberish offline: smiles_status={tg.get('smiles_status')} (want failed)", failures)
        check(bool(tg.get("smiles_error")), "failed task must carry smiles_error", failures)
        check(not tg.get("molecule_name_en"), "failed task must not carry molecule_name_en", failures)

    return {"step": "golden", "passed": not failures, "n_pass": 0 if failures else 1,
            "n_total": 1, "failures": failures, "rdkit_canonical": _HAVE_RDKIT}


# ---------- Tier 0: the curated manifest ----------
#
# `mol_000001`'s SMILES is written in the Kekulé form on purpose: RDKit would
# canonicalize it to `c1ccccc1`, so the field coming back unchanged is the proof
# that a manifest SMILES is taken verbatim (rewriting it would substitute our
# spelling for the published dataset's own, which is the one thing a manifest run
# must never do). `mol_000003` has no `name`, which is how the id-derived fallback
# title — and hence why the `name` column matters — becomes visible.
_MANIFEST_HEADER = ["mol_id", "name", "smiles", "formula"]
_MANIFEST_ROWS = [
    ["mol_000001", "Benzene", "C1=CC=CC=C1", "C6H6"],
    ["mol_000002", "(-)-2-Butanol", "CC[C@H](C)O", "C4H10O"],
    ["mol_000003", "", "CCO", "C2H6O"],
]
_MANIFEST_SMILES = {r[0]: r[2] for r in _MANIFEST_ROWS}


def _write_manifest(path: str, rows, header=None) -> str:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header if header is not None else _MANIFEST_HEADER)
        for row in rows:
            w.writerow(row)
    return path


def _manifest_task_list() -> List[Dict[str, Any]]:
    """What task-recognition's manifest entry emits: molecule_name IS the mol_id."""
    return [
        {"task_id": f"task_{i:02d}_{mol}_ir", "molecule_name": mol,
         "target_spectra": "ir", "solvent_raw": "", "solvent_std": "",
         "extra_params": {}, "recognition_status": "success"}
        for i, mol in enumerate((r[0] for r in _MANIFEST_ROWS), start=1)
    ]


def _write_batch(path: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"parse_batch_id": "parse_test", "run_id": "run_test",
                   "task_list": _manifest_task_list(),
                   "parse_warnings": [], "parse_errors": []}, fh, ensure_ascii=False)
    return path


def _tasks_of(path: str) -> Dict[str, Dict[str, Any]]:
    payload = json.load(open(path, encoding="utf-8"))
    tasks = payload["task_list"] if isinstance(payload, dict) and "task_list" in payload else payload
    return {t["molecule_name"]: t for t in tasks}


def step_manifest() -> Dict[str, Any]:
    """Tier 0 must resolve mol_id names, verbatim, and be the ONLY thing that can."""
    failures: List[str] = []
    with tempfile.TemporaryDirectory(prefix="msa_n2s_manifest_") as td:
        manifest = _write_manifest(os.path.join(td, "manifest.csv"), _MANIFEST_ROWS)
        batch = _write_batch(os.path.join(td, "in.json"))

        # N1 — Tier 0 hit. No offline switch: a Tier 0 hit must be offline by
        # construction, not because the environment forbade the network.
        out_hit = os.path.join(td, "out_hit.json")
        r = run(["python3", ENTRY, "--input", batch, "--output", out_hit,
                 "--manifest", manifest])
        if r.returncode != 0:
            return {"step": "manifest", "passed": False,
                    "fatal": f"Tier 0 run exited {r.returncode}",
                    "stderr_tail": r.stderr[-600:]}
        by_mol = _tasks_of(out_hit)
        check(len(by_mol) == len(_MANIFEST_ROWS),
              f"N1: expected {len(_MANIFEST_ROWS)} tasks out, got {len(by_mol)}", failures)
        for mol, smiles in _MANIFEST_SMILES.items():
            t = by_mol.get(mol, {})
            check(t.get("smiles_status") == "success",
                  f"N1 {mol}: smiles_status={t.get('smiles_status')} (want success)", failures)
            check(t.get("smiles_source") == "manifest",
                  f"N1 {mol}: smiles_source={t.get('smiles_source')} (want manifest)", failures)
            check(t.get("smiles") == smiles,
                  f"N1 {mol}: smiles {t.get('smiles')!r} != verbatim {smiles!r}", failures)
        # The verbatim promise, stated as the one case where it is observable.
        if _HAVE_RDKIT:
            kekule = _MANIFEST_SMILES["mol_000001"]
            check(canonical(kekule) != kekule,
                  "N1: fixture no longer proves anything — its SMILES is already canonical",
                  failures)
            check(by_mol.get("mol_000001", {}).get("smiles") == kekule,
                  "N1: a manifest SMILES must NOT be re-canonicalized", failures)
        # molecule_name_en comes from the manifest's `name` column...
        check(by_mol.get("mol_000001", {}).get("molecule_name_en") == "Benzene",
              f"N1: molecule_name_en={by_mol.get('mol_000001', {}).get('molecule_name_en')!r} (want 'Benzene')",
              failures)
        check(by_mol.get("mol_000002", {}).get("molecule_name_en") == "(-)-2-Butanol",
              f"N1: a published name must not be re-spelled, got "
              f"{by_mol.get('mol_000002', {}).get('molecule_name_en')!r}", failures)
        # ...and without it the figure title degrades to the internal id.
        check(by_mol.get("mol_000003", {}).get("molecule_name_en") == "Mol_000003",
              f"N1: nameless row should fall back to its id, got "
              f"{by_mol.get('mol_000003', {}).get('molecule_name_en')!r}", failures)

        # N2 — the same batch WITHOUT --manifest. Nothing else can resolve a
        # mol_id, so every task must fail: that is what makes passing the same
        # manifest to this stage mandatory rather than advisory.
        out_miss = os.path.join(td, "out_miss.json")
        env = dict(os.environ)
        env["MSA_N2S_DISABLE_NETWORK"] = "1"
        r = run(["python3", ENTRY, "--input", batch, "--output", out_miss], env=env)
        check(r.returncode == 0,
              f"N2: a batch of unresolvable names is fail-soft, not exit {r.returncode}", failures)
        if r.returncode == 0:
            for mol, t in _tasks_of(out_miss).items():
                check(t.get("smiles_status") == "failed",
                      f"N2 {mol}: smiles_status={t.get('smiles_status')} (want failed without --manifest)",
                      failures)
                check(bool(t.get("smiles_error")), f"N2 {mol}: missing smiles_error", failures)

        # N3 — an unreadable manifest is exit 1, not 2991 identical per-task
        # failures: the operator gets one loud error instead of a whole bad batch.
        nocol = _write_manifest(os.path.join(td, "nocol.csv"),
                                [["mol_000001", "Benzene"]], header=["mol_id", "name"])
        out_bad = os.path.join(td, "out_bad.json")
        r = run(["python3", ENTRY, "--input", batch, "--output", out_bad,
                 "--manifest", nocol])
        check(r.returncode == 1,
              f"N3: manifest missing the smiles column should exit 1, got {r.returncode}", failures)
        check("smiles" in r.stderr,
              f"N3: the refusal must name the missing column: {r.stderr[-300:]}", failures)
        check(not os.path.isfile(out_bad),
              "N3: a refused manifest must not leave an output task_list behind", failures)

    return {"step": "manifest", "passed": not failures, "failures": failures,
            "rdkit_canonical": _HAVE_RDKIT}


def main() -> None:
    report = {"lint": step_lint(), "help": step_help(),
              "openshell": step_openshell(), "display_en": step_display_en(),
              "golden": step_golden(), "manifest": step_manifest()}
    ok = (report["lint"]["passed"] and report["help"]["passed"]
          and report["openshell"]["passed"] and report["display_en"]["passed"]
          and report["golden"]["passed"] and report["manifest"]["passed"])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not ok:
        print("\nFAIL — see failures above", file=sys.stderr)
        sys.exit(1)
    print("\nPASS — all gates green", file=sys.stderr)


if __name__ == "__main__":
    main()
