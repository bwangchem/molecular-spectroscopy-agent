#!/usr/bin/env python3
"""validate_skill.py — Static lint for MSA-generated skills.

Usage:
    python validate_skill.py <skill_dir>
    python validate_skill.py skills/name-to-smiles

Runs the Phase 5.1 checks from msa-skill-spec/SKILL.md, plus the Phase 5.4 field
contract check (declared appends vs PIPELINE.md §1.1) and the multi-entry rules of
ironclad rules 36–37. Exits 0 if all pass, non-zero on any violation. All findings
are printed to stderr.

Path resolution self-locates the repo root from this script's own path
(meta/msa-skill-spec/scripts/validate_skill.py -> three dirs up); no
environment variable is consulted.
"""

import argparse
import os
import re
import sys
from typing import List, Tuple

import _contract_table as ct  # spec-local


def _resolve_msa_root() -> str:
    # Self-locate the repo root from this script's path:
    # meta/msa-skill-spec/scripts/validate_skill.py -> three dirs up.
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    if not os.path.isdir(os.path.join(root, "msa_shared")):
        sys.stderr.write(
            f"ERROR: repo root '{root}' does not contain an 'msa_shared' package.\n"
        )
        sys.exit(3)
    return root


def _read_shared_version(msa_root: str) -> str:
    vp = os.path.join(msa_root, "msa_shared", "VERSION")
    try:
        with open(vp, "r", encoding="utf-8") as f:
            return f.read().strip()
    except IOError:
        return "unknown"


def _parse_frontmatter(md_text: str) -> Tuple[dict, str]:
    """Very small YAML frontmatter parser (key: value only, no nesting)."""
    if not md_text.startswith("---"):
        return {}, md_text
    end = md_text.find("\n---", 3)
    if end < 0:
        return {}, md_text
    fm_block = md_text[3:end].strip()
    body = md_text[end + 4:]
    fm = {}
    for line in fm_block.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        fm[key] = val
    return fm, body


def check_frontmatter(skill_dir: str, msa_root: str, findings: List[str]) -> Tuple[dict, str]:
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_md):
        findings.append(f"ERROR: SKILL.md not found at {skill_md}")
        return {}, ""
    with open(skill_md, "r", encoding="utf-8") as f:
        text = f.read()
    fm, body = _parse_frontmatter(text)
    required = ["name", "description", "version", "msa_contract_version"]
    for k in required:
        if k not in fm or not fm[k]:
            findings.append(f"ERROR: frontmatter missing or empty key '{k}'")

    # name == dir basename
    if fm.get("name") and fm["name"] != os.path.basename(os.path.normpath(skill_dir)):
        findings.append(
            f"ERROR: frontmatter.name='{fm['name']}' != dir name '{os.path.basename(os.path.normpath(skill_dir))}'"
        )

    # msa_contract_version matches shared
    shared_v = _read_shared_version(msa_root)
    if fm.get("msa_contract_version") and fm["msa_contract_version"] != shared_v:
        findings.append(
            f"ERROR: msa_contract_version='{fm['msa_contract_version']}' != msa_shared/VERSION='{shared_v}'"
        )

    # description must contain must-load + do-not
    desc = fm.get("description", "")
    if not re.search(r"MUST load|必须先加载", desc):
        findings.append("ERROR: description missing 'MUST load' or '必须先加载'")
    if not re.search(r"DO NOT|禁止|不要", desc):
        findings.append("ERROR: description missing 'DO NOT' / '禁止' / '不要'")

    return fm, body


def check_body_sections(body: str, findings: List[str]) -> None:
    required_headings = [
        "## Task Field Contract",
        "## Agent Behavior",
        "## Scripts",
        "## Workflow",
        "## Integration",
        "## Troubleshooting",
    ]
    for h in required_headings:
        if h not in body:
            findings.append(f"WARNING: SKILL.md body missing section '{h}'")


# Markers for the self-locating bootstrap block (see PIPELINE.md §5.2). Each
# entry script puts its own scripts/ dir (holding the vendored msa_shared copy)
# on sys.path via __file__ — no environment variable is consulted.
_BOOTSTRAP_MARKERS = [
    r"os\.path\.dirname\(os\.path\.abspath\(__file__\)\)",
    r"sys\.path\.insert\(",
    r"from\s+msa_shared\.common_utils\s+import",
    r"from\s+msa_shared\s+import\s+naming",
    r"MSA_CONTRACT_VERSION\s*=",
]


def check_python_scripts(skill_dir: str, msa_root: str, findings: List[str]) -> None:
    scripts_dir = os.path.join(skill_dir, "scripts")
    if not os.path.isdir(scripts_dir):
        findings.append("WARNING: no scripts/ directory found")
        return
    shared_v = _read_shared_version(msa_root)
    skill_name = os.path.basename(os.path.normpath(skill_dir))
    py_files = [f for f in os.listdir(scripts_dir) if f.endswith(".py") and not f.startswith("_")]
    if not py_files:
        findings.append("WARNING: scripts/ has no *.py files")
        return
    for name in py_files:
        path = os.path.join(scripts_dir, name)
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        rel = f"scripts/{name}"

        # shebang
        first = src.splitlines()[0] if src else ""
        if first.strip() != "#!/usr/bin/env python3":
            findings.append(f"ERROR: {rel} first line is not '#!/usr/bin/env python3'")

        # bootstrap markers
        for pat in _BOOTSTRAP_MARKERS:
            if not re.search(pat, src):
                findings.append(f"ERROR: {rel} missing msa_shared bootstrap marker: /{pat}/")

        # forbidden: DEFAULT_ constants
        for m in re.finditer(r"^\s*DEFAULT_[A-Z_]+\s*=", src, flags=re.MULTILINE):
            findings.append(
                f"ERROR: {rel} defines DEFAULT_* constant at {m.group(0).strip()!r} — import from msa_shared.constants.* instead"
            )

        # forbidden: matplotlib.use / rcParams.update outside spectrum_style
        if "spectrum_style" not in name:
            for _ in re.finditer(r"matplotlib\.use\(", src):
                findings.append(f"ERROR: {rel} calls matplotlib.use(...) — use `import msa_shared.spectrum_style` instead")
            for _ in re.finditer(r"plt\.rcParams\.update\(", src):
                findings.append(f"ERROR: {rel} calls plt.rcParams.update(...) — use `import msa_shared.spectrum_style` instead")

        # forbidden: bare json.load(open(...)) for task_list IO
        if re.search(r"json\.load\(open\(", src):
            findings.append(f"WARNING: {rel} uses json.load(open(...)) — prefer common_utils.load_task_list()")

        # v2.0: the MSA_CONTRACT_VERSION constant must equal msa_shared/VERSION
        m = re.search(r'^MSA_CONTRACT_VERSION\s*=\s*["\']([^"\']+)["\']', src, flags=re.MULTILINE)
        if not m:
            findings.append(f"ERROR: {rel} has no `MSA_CONTRACT_VERSION = \"<v>\"` string constant")
        elif shared_v != "unknown" and m.group(1) != shared_v:
            findings.append(
                f"ERROR: {rel} MSA_CONTRACT_VERSION='{m.group(1)}' != msa_shared/VERSION='{shared_v}'"
            )

        # v2.0: dependency preflight must be declared AND invoked
        if not re.search(r"^MSA_REQUIRES\s*=", src, flags=re.MULTILINE):
            findings.append(
                f"ERROR: {rel} missing module-scope `MSA_REQUIRES = [...]` declaration (use [] if pure-stdlib)"
            )
        if not re.search(r"ensure_deps\s*\(", src):
            findings.append(
                f"ERROR: {rel} does not call ensure_deps(MSA_REQUIRES, ...) in main()"
            )

        # v2.0: orca-* skills must resolve the cluster profile from inline HPC_*
        # env only — never hard-code it, and never expose a config-file channel
        if skill_name.startswith("orca-"):
            if not re.search(r"resolve_cluster", src):
                findings.append(f"ERROR: {rel} (orca-*) does not reference resolve_cluster()")
            if "--cluster-config" in src or "MSA_CLUSTER_CONFIG" in src:
                findings.append(
                    f"ERROR: {rel} (orca-*) must NOT expose a --cluster-config / "
                    "$MSA_CLUSTER_CONFIG file channel; credentials come only from "
                    "inline HPC_* env vars"
                )

        # v2.1: C-class artifact skills (plot-* and export-spectrum-data) share the
        # signal / variant / kwarg contract — rules 31, 34, 35.
        is_artifact_skill = skill_name.startswith("plot-") or skill_name == "export-spectrum-data"
        if is_artifact_skill:
            if not re.search(r"mark_no_signal\s*\(", src):
                findings.append(
                    f"ERROR: {rel} never calls mark_no_signal() — a converged run with "
                    "nothing to draw must be success + {stage}_signal='none' (rule 31)"
                )
            if not re.search(r"spectrum_kwargs\s*\(", src):
                findings.append(
                    f"ERROR: {rel} does not route builder arguments through "
                    "spectrum_kwargs(task) (rule 35)"
                )
            if not re.search(r"naming\.spec_variant\s*\(", src):
                findings.append(
                    f"ERROR: {rel} does not tag its output filenames with "
                    "naming.spec_variant(task) (rule 34)"
                )
            for kw in ("target_nucleus", "target_element"):
                if re.search(rf"{kw}\s*=\s*(?!None)", src):
                    findings.append(
                        f"ERROR: {rel} hand-maps {kw}= into the builder — use "
                        "spectrum_kwargs(task) instead (rule 35)"
                    )

        # v2.1: a plot-* figure must carry a title, sourced from naming.title_name
        # (rule 33). export-* is exempt: its CSV header keeps molecule AND
        # molecule_en on separate lines instead of collapsing them.
        if skill_name.startswith("plot-") and not re.search(r"naming\.title_name\s*\(", src):
            findings.append(
                f"ERROR: {rel} does not title the figure via naming.title_name(task) (rule 33)"
            )


_ORCA_DIAGNOSTIC_LITERALS = (
    "SCF NOT CONVERGED AFTER",
    "RPA/TD-DFT did not converge!",
    "ORCA finished by error termination in CIS",
    "THE OPTIMIZATION HAS CONVERGED",
    "Numerical calculation ISN'T COMPLETE",
    "failed to read Hessian file!",
)
_ORCA_HAND_BUILT_JOB_PATH = re.compile(
    r"(?:f|rf|fr)[\"'][^\"'\n]*\{[^}]+\}[^\"'\n]*\.(?:inp|out|sh)\b"
)


def check_orca_runtime_constraints(skill_dir: str, findings: List[str]) -> None:
    """Enforce ironclad rule 37 on non-vendored orca-* business modules."""
    skill_name = os.path.basename(os.path.normpath(skill_dir))
    if not skill_name.startswith("orca-"):
        return
    scripts_dir = os.path.join(skill_dir, "scripts")
    if not os.path.isdir(scripts_dir):
        return

    for name in sorted(f for f in os.listdir(scripts_dir) if f.endswith(".py")):
        path = os.path.join(scripts_dir, name)
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        rel = f"scripts/{name}"

        if any(literal in src for literal in _ORCA_DIAGNOSTIC_LITERALS) or re.search(
            r"Calculation\\s\+.*of\\s\+.*is missing", src
        ):
            findings.append(
                f"ERROR: {rel} embeds an ORCA terminal diagnostic — import and call "
                "msa_shared.orca_diagnostics instead (rule 37)"
            )
        if _ORCA_HAND_BUILT_JOB_PATH.search(src):
            findings.append(
                f"ERROR: {rel} hand-builds an .inp/.out/.sh filename — use naming.* "
                "instead (rule 37)"
            )
        if re.search(r"^\s*def\s+_suffixed\s*\(", src, flags=re.MULTILINE):
            findings.append(
                f"ERROR: {rel} defines _suffixed() — represent the job variant through "
                "naming.* instead (rule 37)"
            )


def check_task_field_contract(skill_dir: str, body: str, msa_root: str,
                             findings: List[str]) -> None:
    """Check the skill's declared appends against PIPELINE.md §1.1 (Phase 5.4).

    PIPELINE.md §1.1 is the single source of truth for field ownership, so it is
    parsed here rather than restated: a skill may append only what its own row
    declares, and must declare every non-optional field of that row. This is what
    keeps "skill says X" and "contract says X" from diverging silently.
    """
    skill_name = os.path.basename(os.path.normpath(skill_dir))
    try:
        rows = ct.parse_pipeline_table(ct.pipeline_md_path(msa_root))
    except (IOError, ValueError) as e:
        findings.append(f"ERROR: cannot read PIPELINE.md §1.1 as judgement data: {e}")
        return

    row = ct.row_for_skill(rows, skill_name)
    if row is None:
        findings.append(
            f"ERROR: skill '{skill_name}' has no producer row in PIPELINE.md §1.1; "
            "add the stage there before shipping the skill"
        )
        return

    declared = ct.parse_skill_appends(body)
    if declared is None:
        findings.append(
            "ERROR: '## Task Field Contract' has no '**Appends' marker, so the "
            f"declared field set cannot be checked against §1.1 row '{row.stage_label}'"
        )
        return

    extra = declared - row.all_fields
    if extra:
        findings.append(
            f"ERROR: SKILL.md declares append field(s) {sorted(extra)} that §1.1 row "
            f"'{row.stage_label}' does not grant to '{skill_name}'"
        )

    missing = row.required - declared
    if missing:
        findings.append(
            f"ERROR: SKILL.md does not declare required field(s) {sorted(missing)} "
            f"from §1.1 row '{row.stage_label}'"
        )


def check_entry_points(skill_dir: str, body: str, findings: List[str]) -> None:
    """Check the multi-entry rules (ironclad rule 36).

    A stage may expose more than one entry script, one per shape of input
    (PIPELINE.md §0). Two things must then hold, and both are decidable statically:

    1. every entry script is documented in SKILL.md — an entry the Agent cannot
       find in the skill's own docs will simply never be invoked;
    2. all entries of the skill declare the same STAGE_ID — differing stage ids
       would make one skill write two different `{stage}_status` fields, and every
       downstream `upstream_ok` gate would then read the wrong one.

    What this cannot check is field-for-field equivalence of the emitted tasks;
    that needs execution and lives in the skill's own test (Phase 5.4).
    """
    scripts_dir = os.path.join(skill_dir, "scripts")
    if not os.path.isdir(scripts_dir):
        return
    entries = sorted(
        f for f in os.listdir(scripts_dir)
        if f.endswith(".py") and not f.startswith("_")
    )

    stage_ids = {}
    for name in entries:
        with open(os.path.join(scripts_dir, name), "r", encoding="utf-8") as f:
            src = f.read()
        m = re.search(r'^STAGE_ID\s*=\s*["\']([^"\']+)["\']', src, flags=re.MULTILINE)
        if m:
            stage_ids[name] = m.group(1)

        if body and name not in body:
            findings.append(
                f"ERROR: entry script scripts/{name} is not mentioned anywhere in "
                "SKILL.md — every entry point needs its own documented invocation "
                "(rule 36)"
            )

    distinct = set(stage_ids.values())
    if len(distinct) > 1:
        findings.append(
            "ERROR: entry scripts of one skill declare different STAGE_ID values "
            f"({', '.join(f'{k}={v}' for k, v in sorted(stage_ids.items()))}) — all "
            "entries of a stage share one stage id (rule 36)"
        )


def validate(skill_dir: str, msa_root: str) -> int:
    findings: List[str] = []
    if not os.path.isdir(skill_dir):
        print(f"ERROR: not a directory: {skill_dir}", file=sys.stderr)
        return 2

    fm, body = check_frontmatter(skill_dir, msa_root, findings)
    if body:
        check_body_sections(body, findings)
        check_task_field_contract(skill_dir, body, msa_root, findings)
    check_python_scripts(skill_dir, msa_root, findings)
    check_entry_points(skill_dir, body, findings)
    check_orca_runtime_constraints(skill_dir, findings)

    errors = [x for x in findings if x.startswith("ERROR")]
    warnings = [x for x in findings if x.startswith("WARNING")]

    for line in findings:
        print(line, file=sys.stderr)

    print(
        f"\n{'FAIL' if errors else 'PASS'}: {len(errors)} error(s), {len(warnings)} warning(s) in {skill_dir}",
        file=sys.stderr,
    )
    return 1 if errors else 0


def main():
    p = argparse.ArgumentParser(description="Validate an MSA skill against msa-skill-spec Phase 5.1")
    p.add_argument("skill_dir", help="path to skill directory (contains SKILL.md and scripts/)")
    args = p.parse_args()
    msa_root = _resolve_msa_root()
    sys.exit(validate(os.path.expanduser(args.skill_dir), msa_root))


if __name__ == "__main__":
    main()
