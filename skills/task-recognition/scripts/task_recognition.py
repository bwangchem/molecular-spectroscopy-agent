#!/usr/bin/env python3
"""task-recognition — parse a user's natural-language spectroscopy request into a structured task_list JSON.

This is the entry skill (Type-A preprocessing) of the Molecular Spectroscopy
Agent (MSA) pipeline. Unlike downstream preprocessing skills it does NOT consume
an upstream task_list; its input is free natural language. It performs text-only
recognition and formatting only: it does NOT convert names to SMILES, does NOT
recommend calculation parameters, and does NOT schedule jobs. When the user does
not specify a solvent, ``solvent_std`` is left empty and the downstream
``param-recommendation`` skill fills in the default.

Input : a natural-language string (--text, or --input-file, or stdin)
Output: wrapped task_list JSON to stdout (or to --output); optional --summary
        prints a human-readable summary to stderr.

Naming:
- batch id  parse_batch_id = "parse_YYYYMMDD_HHMMSS"
- run id     run_id = "run_YYYYMMDD_HHMMSS_<6hex>" — the run-isolation namespace
            stamped onto every task; nests all local + HPC artifacts under a
            per-run directory so concurrent runs never clobber each other
- task id   task_id = "task_{NN}_{molecule_name}_{target_spectra}", NN is a
            two-digit index that increases globally within the batch
- target_spectra in {ir, raman, uvvis, nmr, fluo, xanes}

CLI:
  python task_recognition.py --text "compute the IR spectrum of benzene, solvent chloroform"
  echo "..." | python task_recognition.py
  python task_recognition.py --input-file req.txt --output result.json --summary
"""
import os
import sys
import json
import argparse
import re
import time
import secrets

# ---- msa_shared bootstrap (DO NOT MODIFY) ----
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from msa_shared.common_utils import (
    load_task_list, dump_task_list,
    mark_success, mark_failed, mark_skipped, upstream_ok,
    log_startup, read_shared_version, ensure_deps,
)
from msa_shared import naming

MSA_CONTRACT_VERSION = "2.0"
MSA_REQUIRES = []  # stdlib only
STAGE_ID = "recognition"
UPSTREAM_STAGE = None
# ---- end bootstrap ----


# ---------- dictionary loading ----------
# Dictionaries are domain data shipped inside this skill's dicts/ directory.
# They are loaded relative to this script, so the skill stays self-contained.
_dicts_dir = os.path.join(os.path.dirname(__file__), "..", "dicts")


def _load_json(name):
    with open(os.path.join(_dicts_dir, name), "r", encoding="utf-8") as f:
        return json.load(f)


def _flatten_molecules(mol_dict_by_cat):
    """molecules.json is grouped by category; flatten it to {zh_name: en_name}."""
    flat = {}
    for cat, entries in mol_dict_by_cat.items():
        if cat.startswith("_"):
            continue
        if not isinstance(entries, dict):
            continue
        for zh, en in entries.items():
            flat[zh] = en
    return flat


def _strip_meta(d):
    """Drop metadata fields whose key begins with '_' (e.g. _comment)."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


_molecule_dict = _flatten_molecules(_load_json("molecules.json"))
_spectra_synonyms = _strip_meta(_load_json("spectra_synonyms.json"))
_solvent_map = _strip_meta(_load_json("solvents.json"))
_separators = _load_json("separators.json")["separators"]


# ---------- shared warning strings (decided at BATCH level) ----------
# These two "not recognized" messages must be decided at the batch level, not
# per segment: a multi-chunk / multi-semicolon input often produces sub-chunks
# that contain only a molecule or only a spectrum, and emitting per-segment
# warnings from them is spurious noise. We therefore keep them out of the batch
# warning list whenever the batch produced at least one valid task.
_warn_no_molecule = "未识别出分子名称，请确认名称或直接提供SMILES"
_warn_no_spectra = "未识别计算目标光谱"
_warn_no_task = "未识别出任何有效任务，请检查输入是否包含分子名与光谱类型"


# ---------- open-shell scope guard (v2.0) ----------
# The pipeline supports ONLY closed-shell singlets (downstream fixes the spin
# multiplicity at 1). If the user's own wording asks for an open-shell species
# — radical / triplet / high-spin / carbene / nitrene / diradical — we intercept
# that request up front and report it as unsupported, rather than silently
# emitting a task that would be computed as a (wrong) closed-shell singlet.
# name-to-smiles independently guards the structural side (odd-electron SMILES);
# this guard covers the natural-language side.
_OPENSHELL_TERMS = (
    "自由基", "双自由基", "三重态", "三线态", "激发三重态",
    "高自旋", "开壳层", "开壳", "卡宾", "氮宾", "顺磁",
    "radical", "diradical", "biradical", "triplet", "high-spin",
    "high spin", "open-shell", "open shell", "carbene", "nitrene",
)


def _detect_openshell(text_block):
    """Return the first open-shell term found in the text (case-insensitive), else None."""
    low = text_block.lower()
    for term in _OPENSHELL_TERMS:
        # ASCII terms match case-insensitively; CJK terms are unaffected by lower().
        if term.lower() in low:
            return term
    return None


# ---------- 1. text cleaning + semantic chunking ----------

# Soft punctuation is turned into spaces. Semicolons are kept as HARD block
# delimiters so that "A的IR；B的Raman；C的UV" splits into independent segments.
_soft_punct = "，。、：？！,.:?!"


def clean_raw_text(raw_text):
    """Normalize punctuation: soft punctuation -> spaces, unify semicolons.

    Semicolons (Chinese and ASCII) are preserved as a single ';' so that the
    semantic chunker can use them as hard block boundaries.
    """
    trans = str.maketrans(_soft_punct, " " * len(_soft_punct))
    text = raw_text.translate(trans)
    text = text.replace("；", ";")
    return text


def split_semantic_blocks(clean_text):
    """Split cleaned text into independent semantic blocks.

    Two levels of splitting:
      1. hard split on semicolons (a semicolon separates independent requests);
      2. soft split on the connective separator words from separators.json
         ("分别 / 另外 / 同时 / 以及 / 还有").
    Empty sub-chunks are dropped so they cannot raise spurious per-segment
    warnings.
    """
    blocks = []
    for segment in clean_text.split(";"):
        segment = " ".join(segment.split()).strip()
        if not segment:
            continue
        sub_blocks = [segment]
        for sep in _separators:
            new_blocks = []
            for block in sub_blocks:
                parts = block.split(sep)
                new_blocks.extend([p.strip() for p in parts if p.strip()])
            sub_blocks = new_blocks
        blocks.extend(sub_blocks)
    return blocks


# ---------- 2. molecule-name recognition (three-layer strategy) ----------

_rule_patterns = [
    r"\d+[\-,]\d*[\-,]?[一二三四五六七八九十]?(氯|溴|碘|氟|硝基|氨基|羟基|甲基|乙基|甲氧基)[一二]?[苯酚烷醇酸胺酮醛酯醚]",
    r"[邻间对]?(氯|溴|碘|氟|硝基|氨基|羟基|甲|乙|丙|丁)[一二]?[苯酚烷醇酸胺酮醛]",
    r"[一二三四五六七八九十]?[甲乙丙丁戊己庚辛壬癸]+[烷烯炔醇酸胺酮醛]",
]
_ctx_patterns = [
    r"计算\s*(\S+?)\s*的",
    r"(\S+?)\s*的\s*(红外|拉曼|紫外|荧光|核磁|NMR|XANES)",
]


def extract_molecules(text_block, mask_solvent=""):
    """Recognize molecule names using a three-layer strategy.

      1. exact dictionary match (keys sorted by length desc, so "苯" cannot
         swallow "甲苯");
      2. naming-rule regex match (three patterns) on the dictionary residue;
      3. context-sentence fallback ("计算 X 的" / "X 的 <spectrum>"), enabled
         ONLY when the first two layers found nothing, with conservative
         filtering.

    mask_solvent: an already-recognized raw solvent name. If non-empty it is
    masked out of the working text before recognition so that a solvent token
    such as "氯仿" in "溶剂用氯仿" is not mistaken for a molecule.

    Returns (molecule_names, warnings).
    """
    found = []
    warnings = []
    # Solvent-token masking: the recognized solvent no longer competes as a molecule.
    working_text = text_block.replace(mask_solvent, " ") if mask_solvent else text_block
    remaining = working_text

    # Layer 1: exact dictionary match (longest key first).
    sorted_names = sorted(_molecule_dict.keys(), key=len, reverse=True)
    for name in sorted_names:
        if name in remaining:
            found.append(name)
            remaining = remaining.replace(name, " ")

    # Layer 2: naming-rule regex match (on the dictionary residue).
    rule_hits = []
    for pat in _rule_patterns:
        for m in re.finditer(pat, remaining):
            name = m.group()
            if name not in found and len(name) >= 2:
                found.append(name)
                rule_hits.append(name)
    if rule_hits:
        warnings.append(f"以下分子按命名规则识别：{', '.join(rule_hits)}")

    # Layer 3: context-sentence fallback (only when the first two layers are empty).
    if not found:
        connectives = ("和", "与", "及", "以及", "或", "跟")
        for pat in _ctx_patterns:
            for m in re.finditer(pat, working_text):
                name = m.group(1)
                if not name or len(name) < 2 or len(name) > 6:
                    continue
                if any(c in name for c in connectives):
                    continue
                if name in found:
                    continue
                found.append(name)

    if not found:
        warnings.append(_warn_no_molecule)

    # De-duplicate while preserving order.
    seen = set()
    result = []
    for mol in found:
        if mol not in seen:
            seen.add(mol)
            result.append(mol)
    return result, warnings


# ---------- 3. spectrum + extra-parameter recognition ----------

# Element edge patterns for XANES. Accepts both English ("C K-edge") and
# Chinese ("C K边" / "C边" / "碳K边") spellings.
_element_zh = {"碳": "C", "氮": "N", "氧": "O", "氟": "F"}
_edge_ascii_re = re.compile(r"([CNOF])\s*K?[\s\-]*(?:edge|边)", re.IGNORECASE)
_edge_zh_re = re.compile(r"([碳氮氧氟])\s*K?[\s\-]*(?:edge|边)", re.IGNORECASE)


def _detect_xanes_element(text_block):
    """Return the XANES target element (C/N/O/F) or None."""
    m = _edge_ascii_re.search(text_block)
    if m:
        return m.group(1).upper()
    m = _edge_zh_re.search(text_block)
    if m:
        return _element_zh.get(m.group(1))
    return None


def extract_spectra(text_block):
    """Recognize spectrum types and their extra parameters.

    - NMR: additionally extract target_nucleus (1H / 13C); if neither is given,
      default to both.
    - XANES: additionally extract target_element (C / N / O / F); if unspecified,
      warn (validate_tasks later drops such a task).

    Returns (configs, warnings).
    """
    configs = []
    warnings = []
    found_types = set()

    sorted_keys = sorted(_spectra_synonyms.keys(), key=len, reverse=True)
    for kw in sorted_keys:
        std = _spectra_synonyms[kw]
        if kw in text_block and std not in found_types:
            found_types.add(std)

            if std == "nmr":
                nuclei = []
                if "1H" in text_block or "氢谱" in text_block or "氢" in text_block:
                    nuclei.append("1H")
                if "13C" in text_block or "碳谱" in text_block or "碳" in text_block:
                    nuclei.append("13C")
                if not nuclei:
                    warnings.append("计算NMR但是未明确氢还是碳，默认两种都进行计算")
                    nuclei = ["1H", "13C"]
                for n in nuclei:
                    configs.append({"spec_std": std, "extra_params": {"target_nucleus": n}})

            elif std == "xanes":
                elem = _detect_xanes_element(text_block)
                if elem is None:
                    warnings.append("计算XANES但是未明确哪种元素，需要补充")
                configs.append({
                    "spec_std": std,
                    "extra_params": {"target_element": elem} if elem else {},
                })

            else:
                configs.append({"spec_std": std, "extra_params": {}})

    if not configs:
        warnings.append(_warn_no_spectra)
    return configs, warnings


# ---------- 4. solvent recognition + standardization ----------

_strong_solvent_tokens = ("气相", "真空", "无溶剂", "CDCl3", "DMSO", "THF", "DCM")
_solvent_ctx_re = re.compile(
    r"溶剂|溶液|条件下|"
    r"以\s*[\u4e00-\u9fa5A-Za-z0-9\-]{1,10}\s*(?:为|作为)\s*溶剂|"
    r"在\s*[\u4e00-\u9fa5A-Za-z0-9\-]{1,10}\s*(?:中|里|下)"
)


def extract_solvent(text_block):
    """Recognize the solvent and map it to an ORCA CPCM keyword.

    Strategy (context-based disambiguation):
      1) if a strong token (气相/真空/无溶剂/CDCl3/DMSO/THF/DCM) appears, match it
         directly;
      2) otherwise require an explicit solvent context ("溶剂"/"溶液"/"以…为溶剂"/
         "在…中"/"…条件下"), then match dictionary keys longest-first;
      3) if neither holds, return ("", "") so that words like 甲苯/乙醇/丙酮/氯仿,
         which are BOTH molecules and solvents, are left to molecule recognition
         instead of being swallowed as a solvent.

    气相 / 真空 / 无溶剂 -> std is "" (explicit gas phase).
    User did not mention a solvent -> ("", "") (downstream fills the default).
    """
    # 1) strong token first
    for tok in _strong_solvent_tokens:
        if tok in text_block and tok in _solvent_map:
            return tok, _solvent_map[tok]

    # 2) ordinary match requires a solvent context
    if not _solvent_ctx_re.search(text_block):
        return "", ""

    sorted_sol = sorted(_solvent_map.keys(), key=len, reverse=True)
    for name in sorted_sol:
        if name in text_block:
            return name, _solvent_map[name]
    return "", ""


# ---------- 5. task-list generation (Cartesian product) ----------

def expand_task_list(molecules, spectra_configs, raw_solvent, std_solvent):
    """Expand molecules x spectra into standardized task entries.

    task_id is numbered locally within the block; the main entry renumbers
    globally. extra_params is copied to avoid shared references.
    """
    tasks = []
    idx = 0
    for mol in molecules:
        for spec_cfg in spectra_configs:
            idx += 1
            tasks.append({
                "task_id": f"task_{idx:02d}_{mol}_{spec_cfg['spec_std']}",
                "molecule_name": mol,
                "target_spectra": spec_cfg["spec_std"],
                "solvent_raw": raw_solvent,
                "solvent_std": std_solvent,
                "extra_params": spec_cfg["extra_params"].copy(),
            })
    return tasks


# ---------- 6. task validation ----------

def validate_tasks(task_list):
    """Drop XANES tasks that have no target_element and record them as errors."""
    valid = []
    errors = []
    for t in task_list:
        if t["target_spectra"] == "xanes" and \
           t["extra_params"].get("target_element") is None:
            errors.append(f"{t['task_id']}：XANES计算未指定靶元素，跳过")
            continue
        valid.append(t)
    return valid, errors


# ---------- 7. shared batch primitives (used by EVERY entry point) ----------
# task-recognition has two entry points (PIPELINE.md §0): this script, driven by a
# user's natural language, and manifest_to_tasks.py, driven by a curated manifest
# CSV. The two must emit field-for-field identical tasks for the same molecule and
# spectrum (msa-skill-spec rule 36), so the id formats, the numbering, the status
# stamping and the batch wrapper live here once and are CALLED by both entries
# rather than restated in each. Nothing below is natural-language specific.

def new_parse_batch_id():
    """parse_batch_id of one parse — format `parse_YYYYMMDD_HHMMSS` (PIPELINE.md §0)."""
    return f"parse_{time.strftime('%Y%m%d_%H%M%S')}"


def new_run_id():
    """A fresh run-isolation namespace — format `run_YYYYMMDD_HHMMSS_<6hex>`.

    It nests all local + remote (HPC) artifacts under a per-run directory so
    concurrent runs of the same molecule/spectrum never clobber each other
    (PIPELINE.md §1.4). The random suffix defeats same-second collisions that
    parse_batch_id cannot.
    """
    return f"run_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"


def assign_task_ids(tasks, start_idx=0):
    """Stamp `task_id` on `tasks`, continuing from `start_idx`; return the last index.

    Numbering is global within the batch (PIPELINE.md §1.3), which is why the index
    is threaded through instead of restarting for each group of tasks.
    """
    idx = start_idx
    for t in tasks:
        idx += 1
        t["task_id"] = f"task_{idx:02d}_{t['molecule_name']}_{t['target_spectra']}"
    return idx


def finalize_batch(tasks, warnings, errors, batch_id, run_id):
    """Validate, stamp and wrap `tasks` into the batch object every entry returns.

    Callers pass tasks that already carry their `task_id` (see assign_task_ids):
    numbering deliberately happens BEFORE validation, so a task dropped here leaves
    a gap in the numbering instead of shifting every later id. Keeping that order
    means a task_id already printed in a log still identifies the same task after
    validation has run.
    """
    valid_tasks, val_err = validate_tasks(tasks)
    errors = list(errors) + val_err
    warnings = list(warnings)

    # Stamp the entry-stage status so every downstream skill's
    # upstream_ok(task, "recognition") gate passes. Each emitted task is, by
    # construction, a successfully parsed task, so recognition_status="success".
    # Also stamp the run-isolation namespace onto every task: load_task_list
    # unwraps the batch object and returns bare tasks, so the per-task run_id is
    # what actually reaches (and isolates) every downstream stage.
    for t in valid_tasks:
        t["run_id"] = run_id
        mark_success(t, "recognition")

    # Batch-level fallback: only when the whole batch produced no valid task and
    # no error do we emit the "nothing recognized" warning.
    if not valid_tasks and not errors:
        warnings.append(_warn_no_task)

    return {
        "parse_batch_id": batch_id,
        "run_id": run_id,
        "task_list": valid_tasks,
        "parse_warnings": sorted(set(warnings)),
        "parse_errors": sorted(set(errors)),
    }


# ---------- 8. main entry + summary ----------

def parse_user_request(raw_input):
    """Main function: natural-language request in, wrapped task_list JSON out."""
    batch_id = new_parse_batch_id()
    run_id = new_run_id()
    warnings = []
    errors = []
    all_tasks = []

    text = clean_raw_text(raw_input)
    if not text.strip():
        return finalize_batch([], [], ["输入为空白"], batch_id, run_id)

    blocks = split_semantic_blocks(text)
    global_idx = 0
    for block in blocks:
        # Open-shell scope guard: the pipeline only supports closed-shell
        # singlets. If this request explicitly asks for an open-shell species,
        # report it as unsupported and emit no task for the block.
        os_term = _detect_openshell(block)
        if os_term is not None:
            errors.append(
                f"检测到开壳层相关描述（{os_term}），当前版本仅支持闭壳层单重态，"
                f"不支持自由基/三重态/高自旋/卡宾等开壳体系，已跳过该请求"
            )
            continue

        # Recognize the solvent first, then mask its token out of molecule recognition.
        raw_sol, std_sol = extract_solvent(block)
        mols, mol_warn = extract_molecules(block, mask_solvent=raw_sol)
        specs, spec_warn = extract_spectra(block)

        # Only a productive block (both a molecule AND a spectrum) contributes
        # tasks. The two "not recognized" fallbacks (_warn_no_molecule /
        # _warn_no_spectra) only appear in non-productive blocks, so restricting
        # warning collection to productive blocks suppresses per-segment noise
        # while still surfacing informational warnings (naming-rule note, NMR
        # default nucleus, missing XANES element). This is the batch-level fix
        # for the multi-chunk spurious-warning bug (golden tests T2 / T8).
        if mols and specs:
            block_tasks = expand_task_list(mols, specs, raw_sol, std_sol)
            global_idx = assign_task_ids(block_tasks, global_idx)
            all_tasks.extend(block_tasks)
            warnings.extend(w for w in mol_warn if w != _warn_no_molecule)
            warnings.extend(w for w in spec_warn if w != _warn_no_spectra)

    return finalize_batch(all_tasks, warnings, errors, batch_id, run_id)


def generate_summary(parse_result):
    """Build a human-readable parse summary (multi-line text)."""
    total = len(parse_result["task_list"])
    lines = [
        "===== parse result =====",
        f"batch id: {parse_result['parse_batch_id']}",
        f"valid tasks: {total}",
        "",
    ]
    if parse_result["parse_warnings"]:
        lines.append("[warnings]")
        for w in parse_result["parse_warnings"]:
            lines.append(f"  - {w}")
        lines.append("")
    if parse_result["parse_errors"]:
        lines.append("[errors]")
        for e in parse_result["parse_errors"]:
            lines.append(f"  x {e}")
        lines.append("")
    if total > 0:
        lines.append("[task list]")
        for i, t in enumerate(parse_result["task_list"], 1):
            spec = t["target_spectra"].upper()
            extra = ""
            if "target_nucleus" in t["extra_params"]:
                extra = f" ({t['extra_params']['target_nucleus']})"
            if "target_element" in t["extra_params"]:
                extra = f" ({t['extra_params']['target_element']} K-edge)"
            sol = t["solvent_std"] if t["solvent_std"] else "unspecified (downstream default)"
            lines.append(f"  {i}. {t['molecule_name']} --- {spec}{extra} --- {sol}")
    return "\n".join(lines)


# ---------- CLI ----------

def _read_input(args):
    if args.text is not None:
        return args.text
    if args.input_file:
        with open(args.input_file, "r", encoding="utf-8") as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("ERROR: input must be provided via --text / --input-file / stdin")


def main():
    ap = argparse.ArgumentParser(
        description="task-recognition (MSA entry skill): natural language -> structured task_list JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--text", type=str, default=None,
                    help="natural-language request string")
    ap.add_argument("--input-file", type=str, default=None,
                    help="read the natural-language request from a UTF-8 file")
    ap.add_argument("--output", type=str, default=None,
                    help="write the JSON to this path (default: stdout)")
    ap.add_argument("--summary", action="store_true",
                    help="also print a human-readable summary to stderr")
    ap.add_argument("--indent", type=int, default=2,
                    help="JSON indent (default: 2; pass 0 for compact output)")
    args = ap.parse_args()

    log_startup("task-recognition")
    ensure_deps(MSA_REQUIRES, "task-recognition")
    shared_v = read_shared_version()
    if shared_v != MSA_CONTRACT_VERSION:
        print(
            f"ERROR: msa_contract_version mismatch (skill={MSA_CONTRACT_VERSION}, shared={shared_v})",
            file=sys.stderr,
        )
        sys.exit(2)

    raw = _read_input(args)
    result = parse_user_request(raw)

    indent = None if args.indent == 0 else args.indent

    if args.output:
        # Use the shared writer for the file path. dump_task_list serializes the
        # given object as-is; here it writes the wrapped batch object.
        dump_task_list(result, args.output, indent=indent if indent is not None else 2)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=indent))

    if args.summary:
        print(generate_summary(result), file=sys.stderr)

    n_ok = len(result["task_list"])
    print(f"SUCCESS: task-recognition parsed {n_ok} tasks (batch={result['parse_batch_id']})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
