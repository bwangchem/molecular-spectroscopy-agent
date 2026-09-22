#!/usr/bin/env python3
"""_contract_table.py — Parse PIPELINE.md §1.1 as machine-readable judgement data.

PIPELINE.md §1.1 (the field-append table) is the single source of truth for which
stage owns which task field. This module turns that markdown table into structured
data so the spec tooling can enforce it instead of trusting a hand-kept copy:

  * dump_task_schema.py --check  — guards its STAGE_SCHEMA mirror against drift
  * validate_skill.py            — checks a skill's declared appends against the row

Both consumers self-locate the repo root, so no environment variable is consulted.

Field notation in §1.1: fields are backtick-quoted; a trailing `?` marks the field
as optional (e.g. `calc_error?`). Multi-spectrum producers are written as a brace
list (`orca-{ir,raman,uvvis,nmr,fluo,xanes}`) which expands to concrete skill names.
"""

import os
import re
import sys
from typing import Dict, List, Optional, Set

SECTION_HEADING = "### 1.1 Field-append table by stage"


class StageRow:
    """One row of PIPELINE.md §1.1."""

    def __init__(self, stage_label: str, producer_raw: str, consumes: str,
                 required: Set[str], optional: Set[str]):
        self.stage_label = stage_label
        self.producer_raw = producer_raw
        self.consumes = consumes
        self.required = required
        self.optional = optional

    @property
    def all_fields(self) -> Set[str]:
        return self.required | self.optional

    @property
    def producer_skills(self) -> List[str]:
        """Concrete skill names this row covers (brace lists expanded)."""
        return expand_producer(self.producer_raw)

    def __repr__(self) -> str:
        return f"<StageRow {self.stage_label!r} producer={self.producer_raw!r}>"


def resolve_repo_root(script_path: str) -> str:
    """Self-locate the repo root from a meta/msa-skill-spec/scripts/*.py path."""
    here = os.path.dirname(os.path.abspath(script_path))
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    if not os.path.isdir(os.path.join(root, "msa_shared")):
        sys.stderr.write(
            f"ERROR: repo root '{root}' does not contain an 'msa_shared' package.\n"
        )
        sys.exit(3)
    return root


def pipeline_md_path(msa_root: str) -> str:
    return os.path.join(msa_root, "msa_shared", "PIPELINE.md")


def expand_producer(producer_raw: str) -> List[str]:
    """`orca-{ir,raman}` -> ['orca-ir', 'orca-raman']; plain names pass through."""
    m = re.match(r"^([A-Za-z0-9_\-]+)\{([^}]*)\}$", producer_raw.strip())
    if not m:
        return [producer_raw.strip()]
    prefix, inner = m.group(1), m.group(2)
    return [f"{prefix}{part.strip()}" for part in inner.split(",") if part.strip()]


def _extract_fields(cell: str) -> (Set[str], Set[str]):
    """Pull backtick-quoted field names out of an Appends cell.

    Parenthesised prose (e.g. "(per-step functional/basis_set/...)") carries no
    backticks and is therefore ignored by construction.
    """
    required, optional = set(), set()
    for name, opt in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)(\??)`", cell):
        (optional if opt else required).add(name)
    return required, optional


def parse_pipeline_table(pipeline_path: str) -> List[StageRow]:
    """Parse §1.1 into StageRow objects, in table order."""
    with open(pipeline_path, "r", encoding="utf-8") as f:
        text = f.read()

    if SECTION_HEADING not in text:
        raise ValueError(
            f"{pipeline_path}: section heading '{SECTION_HEADING}' not found; "
            "the contract table cannot be located."
        )
    section = text.split(SECTION_HEADING, 1)[1]
    # The table ends at the next heading of any level.
    section = re.split(r"^#{1,6} ", section, maxsplit=1, flags=re.MULTILINE)[0]

    rows: List[StageRow] = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|- :"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if cells[0].lower() == "stage":  # header row
            continue
        required, optional = _extract_fields(cells[3])
        rows.append(StageRow(cells[0], cells[1], cells[2], required, optional))

    if not rows:
        raise ValueError(f"{pipeline_path}: §1.1 parsed to zero data rows.")
    return rows


def row_for_skill(rows: List[StageRow], skill_name: str) -> Optional[StageRow]:
    """Find the §1.1 row that owns `skill_name`, expanding brace lists."""
    for row in rows:
        if skill_name in row.producer_skills:
            return row
    return None


def skill_index(rows: List[StageRow]) -> Dict[str, StageRow]:
    """Map every concrete skill name to its row."""
    out: Dict[str, StageRow] = {}
    for row in rows:
        for skill in row.producer_skills:
            out[skill] = row
    return out


def parse_skill_appends(skill_md_text: str) -> Optional[Set[str]]:
    """Extract the field names a SKILL.md declares under its `**Appends` marker.

    Returns None when the marker is absent (the caller decides how to report it).

    Only the bullet list immediately following the marker is read, and only the
    part of each bullet before its first colon — so a field's prose description
    (which may itself contain backticks, e.g. `plot_signal="none"`) cannot leak in,
    and a later unrelated list (e.g. the orca-* "Cluster profile" fields) is out of
    range. A bullet may declare two fields at once: `plot_png` / `plot_pdf`.
    """
    lines = skill_md_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("**Appends"):
            start = i + 1
            break
    if start is None:
        return None

    fields: Set[str] = set()
    seen_bullet = False
    blank_after_list = False
    for line in lines[start:]:
        if line.startswith("## ") or line.startswith("**"):
            break
        if not line.strip():
            if seen_bullet:
                blank_after_list = True
            continue
        if line.startswith("- "):
            if blank_after_list:
                break  # a separate list further down the section
            seen_bullet = True
            head = line[2:].split(":", 1)[0]
            for name in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", head):
                fields.add(name)
        elif line[:1] in (" ", "\t"):
            continue  # continuation of the previous bullet
        else:
            break  # prose ends the list block
    return fields
