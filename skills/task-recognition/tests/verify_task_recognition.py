#!/usr/bin/env python3
"""verify_task_recognition.py — independent acceptance test for task-recognition.

Usage:
    python3 verify_task_recognition.py

Does four things in order:
1. Re-run the msa-skill-spec static lint against the task-recognition skill.
2. Smoke test (both entry points: --help must exit 0).
3. Independently run the 8 golden cases and compare against their checks.
4. Exercise the manifest entry point, including a direct assertion that the two
   entry points are equivalent (msa-skill-spec rule 36).

Exit 0 means everything passed; non-zero means at least one failure.

Path resolution self-locates the repo root from this test's own path
root. The golden-case INPUTS are Chinese natural-language user queries and are
domain data — they are intentionally preserved verbatim.

Manifest fixtures are generated into a temporary directory rather than committed:
a CSV that looks like a manifest sitting in the repo would eventually be mistaken
for the real curated dataset.
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
TASK_RECOG = os.path.join(REPO_ROOT, "skills", "task-recognition")
SPEC_VALIDATOR = os.path.join(REPO_ROOT, "meta", "msa-skill-spec", "scripts", "validate_skill.py")
ENTRY = os.path.join(TASK_RECOG, "scripts", "task_recognition.py")
MANIFEST_ENTRY = os.path.join(TASK_RECOG, "scripts", "manifest_to_tasks.py")


def run(cmd: List[str], input_text: str = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=60)


def step_lint() -> Dict[str, Any]:
    r = run(["python3", SPEC_VALIDATOR, TASK_RECOG])
    passed = r.returncode == 0
    return {"step": "lint", "passed": passed, "returncode": r.returncode, "stderr_tail": r.stderr.strip().splitlines()[-5:]}


def step_help() -> Dict[str, Any]:
    """Every entry point of the skill must at least start (rule 36: each is real)."""
    results = {}
    passed = True
    for label, script in (("task_recognition", ENTRY), ("manifest_to_tasks", MANIFEST_ENTRY)):
        r = run(["python3", script, "--help"])
        ok = r.returncode == 0 and "Traceback" not in (r.stderr + r.stdout)
        results[label] = {"returncode": r.returncode, "passed": ok}
        passed = passed and ok
    return {"step": "help", "passed": passed, "entries": results}


def _extract_task_list(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(payload, dict) and "task_list" in payload:
        return payload["task_list"]
    if isinstance(payload, list):
        return payload
    return []


def _parse_stdout(stdout: str) -> Dict[str, Any]:
    """Try both a raw list and a wrapped dict."""
    stdout = stdout.strip()
    if not stdout:
        return {"task_list": [], "parse_warnings": [], "parse_errors": []}
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        # find first { or [
        start = min([i for i in (stdout.find("{"), stdout.find("[")) if i >= 0], default=-1)
        if start < 0:
            return {}
        obj = json.loads(stdout[start:])
    if isinstance(obj, list):
        return {"task_list": obj, "parse_warnings": [], "parse_errors": []}
    return obj


def run_case(text: str) -> Dict[str, Any]:
    r = run(["python3", ENTRY, "--text", text])
    if r.returncode != 0:
        return {"error": f"exit {r.returncode}", "stderr": r.stderr[-400:], "stdout": r.stdout[-400:]}
    return _parse_stdout(r.stdout)


def check(cond: bool, msg: str, failures: List[str]) -> None:
    if not cond:
        failures.append(msg)


def t1(out: Dict[str, Any]) -> List[str]:
    f: List[str] = []
    tl = _extract_task_list(out)
    check(len(tl) == 1, f"T1: expected 1 task, got {len(tl)}", f)
    if tl:
        check(tl[0].get("molecule_name") == "苯", f"T1: molecule_name={tl[0].get('molecule_name')}", f)
        check(tl[0].get("target_spectra") == "ir", f"T1: target_spectra={tl[0].get('target_spectra')}", f)
    check(out.get("parse_warnings", []) == [], f"T1: parse_warnings={out.get('parse_warnings')}", f)
    check(out.get("parse_errors", []) == [], f"T1: parse_errors={out.get('parse_errors')}", f)
    return f


def t2(out: Dict[str, Any]) -> List[str]:
    f: List[str] = []
    tl = _extract_task_list(out)
    check(len(tl) == 5, f"T2: expected 5 tasks, got {len(tl)}", f)
    key_set = {(t.get("molecule_name"), t.get("target_spectra"), t.get("extra_params", {}).get("target_nucleus")) for t in tl}
    wanted_any = {("苯甲醛", "nmr", "1H"), ("苯甲醛", "nmr", "13C"), ("苯甲醛", "uvvis", None),
                  ("苯胺", "nmr", "1H"), ("苯胺", "nmr", "13C"), ("苯胺", "uvvis", None),
                  ("对硝基苯酚", "uvvis", None)}
    covered = key_set & wanted_any
    check(len(covered) >= 5, f"T2: covered only {len(covered)} of expected combinations: {covered}", f)
    check(out.get("parse_warnings", []) == [], f"T2: parse_warnings should be empty, got {out.get('parse_warnings')}", f)
    check(out.get("parse_errors", []) == [], f"T2: parse_errors should be empty, got {out.get('parse_errors')}", f)
    return f


def t3(out: Dict[str, Any]) -> List[str]:
    f: List[str] = []
    tl = _extract_task_list(out)
    check(len(tl) == 1, f"T3: expected 1 task, got {len(tl)}", f)
    if tl:
        check(tl[0].get("molecule_name") == "甲苯", f"T3: molecule_name={tl[0].get('molecule_name')}", f)
        check(tl[0].get("target_spectra") == "raman", f"T3: target_spectra={tl[0].get('target_spectra')}", f)
        check(tl[0].get("solvent_raw", "") == "", f"T3: solvent_raw should be empty, got {tl[0].get('solvent_raw')}", f)
        check(tl[0].get("solvent_std", "") == "", f"T3: solvent_std should be empty, got {tl[0].get('solvent_std')}", f)
    return f


def t4(out: Dict[str, Any]) -> List[str]:
    f: List[str] = []
    tl = _extract_task_list(out)
    check(len(tl) == 1, f"T4: expected 1 task, got {len(tl)}", f)
    if tl:
        check(tl[0].get("molecule_name") == "苯甲醛", f"T4: molecule_name={tl[0].get('molecule_name')}", f)
        check(tl[0].get("target_spectra") == "uvvis", f"T4: target_spectra={tl[0].get('target_spectra')}", f)
        check(tl[0].get("solvent_raw") == "乙醇", f"T4: solvent_raw={tl[0].get('solvent_raw')}", f)
        check(tl[0].get("solvent_std") == "CPCM(Ethanol)", f"T4: solvent_std={tl[0].get('solvent_std')}", f)
    return f


def t5(out: Dict[str, Any]) -> List[str]:
    f: List[str] = []
    tl = _extract_task_list(out)
    check(len(tl) == 1, f"T5: expected 1 task, got {len(tl)}", f)
    if tl:
        check(tl[0].get("solvent_raw") == "CDCl3", f"T5: solvent_raw={tl[0].get('solvent_raw')}", f)
        check(tl[0].get("solvent_std") == "CPCM(Chloroform)", f"T5: solvent_std={tl[0].get('solvent_std')}", f)
        check(tl[0].get("extra_params", {}).get("target_nucleus") == "1H", f"T5: target_nucleus={tl[0].get('extra_params',{}).get('target_nucleus')}", f)
    return f


def t6(out: Dict[str, Any]) -> List[str]:
    f: List[str] = []
    tl = _extract_task_list(out)
    check(len(tl) >= 1, f"T6: expected >=1 task, got {len(tl)}", f)
    if tl:
        check(tl[0].get("target_spectra") == "xanes", f"T6: target_spectra={tl[0].get('target_spectra')}", f)
        check(tl[0].get("extra_params", {}).get("target_element") == "C", f"T6: target_element={tl[0].get('extra_params',{}).get('target_element')}", f)
    return f


def t7(out: Dict[str, Any]) -> List[str]:
    f: List[str] = []
    tl = _extract_task_list(out)
    check(len(tl) == 0, f"T7: expected 0 tasks for '帮我', got {len(tl)}", f)
    warns = out.get("parse_warnings", [])
    check(any("未识别" in w for w in warns), f"T7: expected a '未识别' warning, got {warns}", f)
    return f


def t8(out: Dict[str, Any]) -> List[str]:
    f: List[str] = []
    tl = _extract_task_list(out)
    check(len(tl) == 3, f"T8: expected 3 tasks, got {len(tl)}", f)
    key_set = {(t.get("molecule_name"), t.get("target_spectra")) for t in tl}
    want = {("苯", "ir"), ("甲苯", "raman"), ("对硝基苯酚", "uvvis")}
    missing = want - key_set
    check(not missing, f"T8: missing combinations: {missing}", f)
    check(out.get("parse_warnings", []) == [], f"T8: parse_warnings should be empty, got {out.get('parse_warnings')}", f)
    return f


def t9(out: Dict[str, Any]) -> List[str]:
    """v2.0 open-shell guard: a triplet block is intercepted and reported, while
    the closed-shell block in the same request survives untouched."""
    f: List[str] = []
    tl = _extract_task_list(out)
    check(len(tl) == 1, f"T9: expected 1 surviving task, got {len(tl)}", f)
    if tl:
        check(tl[0].get("molecule_name") == "苯", f"T9: surviving molecule_name={tl[0].get('molecule_name')}", f)
        check(tl[0].get("target_spectra") == "ir", f"T9: surviving target_spectra={tl[0].get('target_spectra')}", f)
    errs = out.get("parse_errors", [])
    check(any("开壳层" in e for e in errs), f"T9: expected an open-shell parse_error, got {errs}", f)
    return f


GOLDEN: List[Dict[str, Any]] = [
    {"id": "T1", "text": "帮我算一下苯的红外光谱", "checker": t1},
    {"id": "T2", "text": "帮我分别计算苯甲醛和苯胺的1H NMR、13C NMR、以及紫外光谱，还有对硝基苯酚的紫外光谱", "checker": t2},
    {"id": "T3", "text": "计算甲苯的拉曼光谱", "checker": t3},
    {"id": "T4", "text": "在乙醇中计算苯甲醛的紫外光谱", "checker": t4},
    {"id": "T5", "text": "苯甲醛在 CDCl3 中的 1H NMR", "checker": t5},
    {"id": "T6", "text": "对硝基苯酚的 C K-edge XANES 光谱", "checker": t6},
    {"id": "T7", "text": "帮我", "checker": t7},
    {"id": "T8", "text": "苯的 IR；甲苯的 Raman；对硝基苯酚的 UV-Vis", "checker": t8},
    {"id": "T9", "text": "计算苯的红外；计算三重态萘的紫外", "checker": t9},
]


def step_golden() -> Dict[str, Any]:
    results = []
    n_pass = 0
    for case in GOLDEN:
        out = run_case(case["text"])
        failures = case["checker"](out)
        if not failures:
            n_pass += 1
        results.append({
            "id": case["id"],
            "text": case["text"],
            "task_count": len(_extract_task_list(out)) if isinstance(out, dict) else 0,
            "failures": failures,
            "passed": not failures,
        })
    return {
        "step": "golden",
        "passed": n_pass == len(GOLDEN),
        "n_pass": n_pass,
        "n_total": len(GOLDEN),
        "cases": results,
    }


# ---------- step 4: the manifest entry point ----------
#
# The fixture is five molecules chosen so that every branch of the
# physical-existence filter (PIPELINE.md §2.6) fires at least once: CCl4 and CF4
# carry no hydrogen, and between them the five rows are missing 14 of the
# 5 x 10 = 50 conceivable molecule x spectrum combinations. `charge` and
# `category` are present precisely because the entry must NOT read them for any
# chemistry decision.
MANIFEST_HEADER = ["mol_id", "name", "smiles", "formula", "elements",
                   "multiplicity", "category", "charge"]
MANIFEST_ROWS = [
    ["mol_000001", "Benzene", "c1ccccc1", "C6H6", "C", "1", "core", "0"],
    ["mol_000002", "Ethanol", "CCO", "C2H6O", "C,O", "1", "core", "0"],
    ["mol_000003", "Tetrachloromethane", "ClC(Cl)(Cl)Cl", "CCl4", "C,Cl", "1", "extra", "0"],
    ["mol_000004", "Tetrafluoromethane", "FC(F)(F)F", "CF4", "C,F", "1", "extra", "0"],
    ["mol_000005", "Aniline", "Nc1ccccc1", "C6H7N", "C,N", "1", "core", "0"],
]
SHARD_RUN_ID = "run_20260908_120000_abcdef"


def _write_manifest(path: str, rows: List[List[str]], header: List[str] = None) -> str:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header if header is not None else MANIFEST_HEADER)
        for row in rows:
            w.writerow(row)
    return path


def _run_manifest(args: List[str]):
    r = run(["python3", MANIFEST_ENTRY] + args)
    return r, (_parse_stdout(r.stdout) if r.returncode == 0 else {})


def _read_report(path: str):
    """Read the §2.6 bypass report; returns (columns, rows)."""
    if not os.path.isfile(path):
        return [], []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = list(reader.fieldnames or [])
        return cols, list(reader)


def _variant(task: Dict[str, Any]) -> str:
    ep = task.get("extra_params", {}) or {}
    return ep.get("target_nucleus") or ep.get("target_element") or ""


def _rdkit_available() -> bool:
    try:
        import rdkit  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def m1(tmp: str) -> List[str]:
    """Full expansion: nothing may be lost, nothing invented."""
    f: List[str] = []
    path = _write_manifest(os.path.join(tmp, "m1.csv"), MANIFEST_ROWS)
    report = os.path.join(tmp, "m1_skipped.csv")
    r, out = _run_manifest(["--manifest", path, "--skipped-report", report])
    check(r.returncode == 0, f"M1: exit {r.returncode}: {r.stderr[-300:]}", f)
    tl = _extract_task_list(out)
    _cols, skipped = _read_report(report)
    check(len(tl) == 36, f"M1: expected 36 tasks, got {len(tl)}", f)
    check(len(skipped) == 14, f"M1: expected 14 bypassed combinations, got {len(skipped)}", f)
    check(len(tl) + len(skipped) == 50,
          f"M1: {len(tl)} tasks + {len(skipped)} bypassed != 5 molecules x 10 combinations", f)
    per_spec: Dict[str, int] = {}
    for t in tl:
        per_spec[t["target_spectra"]] = per_spec.get(t["target_spectra"], 0) + 1
    want = {"ir": 5, "raman": 5, "uvvis": 5, "fluo": 5, "nmr": 8, "xanes": 8}
    check(per_spec == want, f"M1: per-spectrum counts {per_spec} != {want}", f)
    # Decision 4: a manifest says which molecules exist, not in which solvent.
    check(all(t.get("solvent_raw") == "" and t.get("solvent_std") == "" for t in tl),
          "M1: the manifest entry must not invent a solvent value", f)
    check(all(t.get("recognition_status") == "success" for t in tl),
          "M1: every emitted task must carry recognition_status=success", f)
    return f


def m2(tmp: str) -> List[str]:
    """No hydrogen, no 1H — but carbon still gets its 13C (judged from `formula`)."""
    f: List[str] = []
    path = _write_manifest(os.path.join(tmp, "m2.csv"), MANIFEST_ROWS)
    report = os.path.join(tmp, "m2_skipped.csv")
    r, out = _run_manifest(["--manifest", path, "--spectra", "nmr",
                            "--skipped-report", report])
    check(r.returncode == 0, f"M2: exit {r.returncode}: {r.stderr[-300:]}", f)
    got = {(t["molecule_name"], _variant(t)) for t in _extract_task_list(out)}
    for mol in ("mol_000003", "mol_000004"):  # CCl4 / CF4
        check((mol, "1H") not in got, f"M2: {mol} has no hydrogen yet got a 1H task", f)
        check((mol, "13C") in got, f"M2: {mol} contains carbon but got no 13C task", f)
    check(("mol_000001", "1H") in got, "M2: benzene lost its 1H task", f)
    # Judging H from `elements` instead of `formula` would drop ALL 1H tasks; this
    # count is what makes that regression visible.
    check(sum(1 for _m, v in got if v == "1H") == 3,
          f"M2: expected 3 surviving 1H tasks, got {sorted(got)}", f)
    _cols, skipped = _read_report(report)
    bypassed = {(s["mol_id"], s["variant"]) for s in skipped}
    check(bypassed == {("mol_000003", "1H"), ("mol_000004", "1H")},
          f"M2: bypassed 1H set {sorted(bypassed)}", f)
    return f


def m3(tmp: str) -> List[str]:
    """XANES: an edge exists iff the element is present; the rest go to the report."""
    f: List[str] = []
    path = _write_manifest(os.path.join(tmp, "m3.csv"), MANIFEST_ROWS)
    report = os.path.join(tmp, "m3_skipped.csv")
    r, out = _run_manifest(["--manifest", path, "--spectra", "xanes",
                            "--skipped-report", report])
    check(r.returncode == 0, f"M3: exit {r.returncode}: {r.stderr[-300:]}", f)
    got = {(t["molecule_name"], _variant(t)) for t in _extract_task_list(out)}
    want = {("mol_000001", "C"),
            ("mol_000002", "C"), ("mol_000002", "O"),
            ("mol_000003", "C"),
            ("mol_000004", "C"), ("mol_000004", "F"),
            ("mol_000005", "C"), ("mol_000005", "N")}
    check(got == want, f"M3: edge tasks {sorted(got)} != {sorted(want)}", f)
    cols, skipped = _read_report(report)
    check(cols == ["mol_id", "target_spectra", "variant", "reason"],
          f"M3: bypass report columns {cols} != PIPELINE.md §2.6 columns", f)
    check(len(skipped) == 12, f"M3: expected 12 absent edges, got {len(skipped)}", f)
    check(all(s["reason"] for s in skipped),
          "M3: every bypass row must state a reason", f)
    return f


def m4(tmp: str) -> List[str]:
    """Sharding partitions the manifest: union == whole, intersection == empty."""
    f: List[str] = []
    path = _write_manifest(os.path.join(tmp, "m4.csv"), MANIFEST_ROWS)
    _r, full = _run_manifest(["--manifest", path, "--spectra", "ir"])
    ids_full = {t["molecule_name"] for t in _extract_task_list(full)}
    shards: List[set] = []
    for k in (1, 2):
        r, out = _run_manifest(["--manifest", path, "--spectra", "ir",
                                "--run-id", SHARD_RUN_ID, "--shard", f"{k}/2"])
        check(r.returncode == 0, f"M4: shard {k}/2 exit {r.returncode}: {r.stderr[-300:]}", f)
        shards.append({t["molecule_name"] for t in _extract_task_list(out)})
        check(out.get("run_id") == SHARD_RUN_ID,
              f"M4: shard {k}/2 run_id={out.get('run_id')} — all shards share one run", f)
    union = shards[0] | shards[1]
    overlap = shards[0] & shards[1]
    check(union == ids_full,
          f"M4: shard union {sorted(union)} != full run {sorted(ids_full)}", f)
    check(not overlap, f"M4: shards overlap on {sorted(overlap)}", f)
    return f


def m5(tmp: str) -> List[str]:
    """One bad row refuses the whole batch, naming its CSV line (decision 8)."""
    f: List[str] = []
    bad_rows = [
        ["mol_000001", "Benzene", "c1ccccc1", "C6H6", "C", "1", "core", "0"],      # line 2 ok
        ["", "Ghost", "CCO", "C2H6O", "C,O", "1", "core", "0"],                    # line 3 empty mol_id
        ["mol_000004", "Methane", "", "CH4", "C", "1", "core", "0"],               # line 4 empty smiles
        ["mol_000001", "Benzene", "c1ccccc1", "C6H6", "C", "1", "core", "0"],      # line 5 duplicate of line 2
        ["mol_000006", "Triplet", "CCO", "C2H6O", "C,O", "3", "core", "0"],        # line 6 open shell
        ["mol_000007", "Broken", "not-a-smiles((", "C2H6O", "C,O", "1", "core", "0"],  # line 7 unparseable
    ]
    path = _write_manifest(os.path.join(tmp, "m5.csv"), bad_rows)
    r, _out = _run_manifest(["--manifest", path])
    check(r.returncode == 1, f"M5: expected exit 1, got {r.returncode}", f)
    for token in ("line 3", "line 4", "line 5", "line 6"):
        check(token in r.stderr, f"M5: stderr does not name '{token}': {r.stderr[-400:]}", f)
    check("first seen on line 2" in r.stderr,
          f"M5: a duplicate must point at its first occurrence: {r.stderr[-400:]}", f)
    if _rdkit_available():
        check("line 7" in r.stderr,
              f"M5: rdkit is installed, so an unparseable SMILES must be caught: {r.stderr[-400:]}", f)
    check("task_list" not in r.stdout,
          "M5: a refused batch must emit no task_list at all", f)
    return f


def m6(tmp: str) -> List[str]:
    """Three ways to be told 'no' before any work happens."""
    f: List[str] = []
    path = _write_manifest(os.path.join(tmp, "m6.csv"), MANIFEST_ROWS)

    # A shard without a shared run_id would split one dataset across N runs.
    r, _ = _run_manifest(["--manifest", path, "--shard", "1/2"])
    check(r.returncode == 1, f"M6: --shard without --run-id should exit 1, got {r.returncode}", f)
    check("--run-id" in r.stderr, f"M6: the --shard refusal must name --run-id: {r.stderr[-300:]}", f)

    r, _ = _run_manifest(["--manifest", path, "--spectra", "ftir"])
    check(r.returncode == 1, f"M6: unknown spectrum should exit 1, got {r.returncode}", f)
    check("ftir" in r.stderr, f"M6: the refusal must quote the unknown value: {r.stderr[-300:]}", f)

    nocol = _write_manifest(
        os.path.join(tmp, "m6_nocol.csv"),
        [["mol_000001", "c1ccccc1"]],
        header=["mol_id", "smiles"],  # `formula` is required — H/C presence depends on it
    )
    r, _ = _run_manifest(["--manifest", nocol])
    check(r.returncode == 1, f"M6: missing required column should exit 1, got {r.returncode}", f)
    check("formula" in r.stderr, f"M6: the refusal must name the missing column: {r.stderr[-300:]}", f)

    header_only = os.path.join(tmp, "m6_empty.csv")
    _write_manifest(header_only, [])
    r, _ = _run_manifest(["--manifest", header_only])
    check(r.returncode == 1, f"M6: header-only manifest should exit 1, got {r.returncode}", f)
    return f


# The rule-36 case. Equivalence is guaranteed structurally (both entries call the
# same constructors in task_recognition.py), and asserted here so that any future
# divergence — a field one entry stops writing, a spelling one entry changes —
# fails a test instead of silently producing two dialects of the same task_list.
_EQUIV_MOL = "苯"
_EQUIV_MANIFEST_ROW = [_EQUIV_MOL, "Benzene", "c1ccccc1", "C6H6", "C", "1", "core", "0"]
_EQUIV_PAIRS = [
    # (label, natural-language text, --spectra value, expected task count)
    ("ir", "计算苯的红外光谱", "ir", 1),
    ("nmr", "苯的核磁", "nmr", 2),
    # XANES needs an explicit edge on the natural-language side (there is no way to
    # guess one from a name), so the fixture's `elements` column holds C alone.
    ("xanes", "苯的XANES，C K边", "xanes", 1),
]
# run_id is per batch and task_id is per position, so rule 36 exempts exactly these.
_EQUIV_EXEMPT = ("task_id", "run_id")


def _strip(task: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in task.items() if k not in _EQUIV_EXEMPT}


def m7(tmp: str) -> List[str]:
    """Rule 36: both entry points emit field-for-field identical tasks."""
    f: List[str] = []
    path = _write_manifest(os.path.join(tmp, "m7.csv"), [_EQUIV_MANIFEST_ROW])
    for label, text, spectra, n_expected in _EQUIV_PAIRS:
        nl = run_case(text)
        r, mf = _run_manifest(["--manifest", path, "--spectra", spectra])
        check(r.returncode == 0, f"M7[{label}]: manifest entry exit {r.returncode}: {r.stderr[-300:]}", f)
        nl_tasks = _extract_task_list(nl)
        mf_tasks = _extract_task_list(mf)
        check(len(nl_tasks) == n_expected,
              f"M7[{label}]: natural language gave {len(nl_tasks)} tasks, expected {n_expected}", f)
        check(len(mf_tasks) == n_expected,
              f"M7[{label}]: manifest gave {len(mf_tasks)} tasks, expected {n_expected}", f)
        left = {_variant(t): _strip(t) for t in nl_tasks}
        right = {_variant(t): _strip(t) for t in mf_tasks}
        if set(left) != set(right):
            check(False, f"M7[{label}]: variants differ: {sorted(left)} vs {sorted(right)}", f)
            continue
        for variant in sorted(left):
            a, b = left[variant], right[variant]
            if a == b:
                continue
            diff = {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}
            check(False, f"M7[{label}/{variant or '-'}]: fields differ (nl, manifest): {diff}", f)
    return f


MANIFEST_CASES = [
    {"id": "M1", "what": "full expansion: 36 tasks + 14 bypassed == 5 x 10", "fn": m1},
    {"id": "M2", "what": "no hydrogen -> no 1H, carbon still gets 13C", "fn": m2},
    {"id": "M3", "what": "XANES edges follow element presence; §2.6 report shape", "fn": m3},
    {"id": "M4", "what": "shards partition the manifest and share one run_id", "fn": m4},
    {"id": "M5", "what": "any bad row refuses the batch, naming its CSV line", "fn": m5},
    {"id": "M6", "what": "argument / header refusals all exit 1", "fn": m6},
    {"id": "M7", "what": "rule 36: the two entry points are equivalent", "fn": m7},
]


def step_manifest() -> Dict[str, Any]:
    results = []
    n_pass = 0
    with tempfile.TemporaryDirectory(prefix="msa_manifest_verify_") as tmp:
        for case in MANIFEST_CASES:
            try:
                failures = case["fn"](tmp)
            except Exception as e:  # noqa: BLE001 — a crashing case is a failing case
                failures = [f"{case['id']}: raised {type(e).__name__}: {e}"]
            if not failures:
                n_pass += 1
            results.append({
                "id": case["id"],
                "what": case["what"],
                "failures": failures,
                "passed": not failures,
            })
    return {
        "step": "manifest",
        "passed": n_pass == len(MANIFEST_CASES),
        "n_pass": n_pass,
        "n_total": len(MANIFEST_CASES),
        "rdkit_available": _rdkit_available(),
        "cases": results,
    }


def main() -> None:
    report = {
        "lint": step_lint(),
        "help": step_help(),
        "golden": step_golden(),
        "manifest": step_manifest(),
    }
    ok = (report["lint"]["passed"] and report["help"]["passed"]
          and report["golden"]["passed"] and report["manifest"]["passed"])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not ok:
        print("\nFAIL — see failures above", file=sys.stderr)
        sys.exit(1)
    print("\nPASS — all four steps green", file=sys.stderr)


if __name__ == "__main__":
    main()
