"""orca-xanes skill-local helpers — core-orbital-window parsing and optimized-
geometry extraction from ORCA output.

XANES cannot be run as one chained input file: the third (core-excitation
TD-DFT) job needs a ``%tddft orbwin[0] = a, b, -1, -1`` donor window whose two
integers are the *orbital numbers* of the target element's 1s core orbitals, and
those numbers are only known after the second (single-point, ``KeepDens``) job
has printed its ORBITAL ENERGIES table. This module owns the two pure-text
parsing steps the orca-xanes runner performs between cluster submissions:

* ``parse_core_orbital_range`` — read the last ORBITAL ENERGIES table and return
  the ``(a, b)`` core-orbital-number window for the requested K-edge element.
* ``extract_last_geometry`` / ``geometry_to_xyz`` — recover the optimized geometry
  from the last ``CARTESIAN COORDINATES (ANGSTROEM)`` block so the TD-DFT job can
  be submitted as a fresh single point on the optimized structure.

Everything here is pure string parsing over text the caller passes in, so it is
fully unit-testable offline with no cluster access.
"""
import re

# K-edge 1s core-orbital energy windows (eV), with a safety margin around the
# element's 1s eigenvalue. Only C/N/O/F K-edges are in scope (matches the expert
# Part-2 reference); L-edges and hydrogen are unsupported.
CORE_ORBITAL_ENERGY_WINDOWS = {
    "C": (-310.0, -270.0),
    "N": (-420.0, -380.0),
    "O": (-550.0, -510.0),
    "F": (-710.0, -670.0),
}

SUPPORTED_XANES_ELEMENTS = tuple(CORE_ORBITAL_ENERGY_WINDOWS)

# One ORBITAL ENERGIES row: "NO   OCC   E(Eh)   E(eV)". Strict 4-column numeric
# match anchored to the full line so downstream sections (Mulliken/Mayer, which
# carry element letters) never match.
_ORB_ROW = re.compile(
    r"^\s*(\d+)\s+([\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*$", re.MULTILINE
)

_COORD_HEADER = "CARTESIAN COORDINATES (ANGSTROEM)"
_COORD_LINE = re.compile(
    r"^\s*([A-Za-z]{1,2})\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s*$"
)


class XanesParseError(ValueError):
    """Raised when an ORCA .out lacks the block/values the XANES step needs."""


def parse_core_orbital_range(out_text, target_element):
    """Return the ``(orb_start, orb_end)`` core 1s orbital-number window.

    Scans the *last* ORBITAL ENERGIES table in ``out_text`` (in a chained
    opt+single-point run that is the step2 single point, whose numbering the
    TD-DFT job reuses) and collects every occupied orbital (OCC > 0.5) whose
    energy in eV falls inside ``CORE_ORBITAL_ENERGY_WINDOWS[target_element]``.
    Returns the smallest orbital number as ``a`` and the largest as ``b``, feeding
    ``orbwin[0] = a, b, -1, -1``.

    Raises ``XanesParseError`` if the element is unsupported, no ORBITAL ENERGIES
    table is present, or no occupied core orbital lies in the window.
    """
    element = (target_element or "").strip().upper()
    if element not in CORE_ORBITAL_ENERGY_WINDOWS:
        raise XanesParseError(
            f"unsupported XANES target element {target_element!r}; "
            f"supported K-edges: {', '.join(SUPPORTED_XANES_ELEMENTS)}"
        )
    idx = out_text.rfind("ORBITAL ENERGIES")
    if idx == -1:
        raise XanesParseError("no 'ORBITAL ENERGIES' table found in ORCA output")

    section = out_text[idx: idx + 60000]
    e_min, e_max = CORE_ORBITAL_ENERGY_WINDOWS[element]
    core = []
    for m in _ORB_ROW.finditer(section):
        occ = float(m.group(2))
        e_ev = float(m.group(4))
        if occ > 0.5 and e_min <= e_ev <= e_max:
            core.append(int(m.group(1)))
    if not core:
        raise XanesParseError(
            f"no occupied core orbital for {element} in energy window "
            f"[{e_min}, {e_max}] eV (last ORBITAL ENERGIES table)"
        )
    return min(core), max(core)


def extract_last_geometry(out_text):
    """Return the last geometry as a list of ``(element, x, y, z)`` tuples.

    Parses the final ``CARTESIAN COORDINATES (ANGSTROEM)`` block — after an OPT
    that is the optimized structure. Raises ``XanesParseError`` if no coordinate
    block is present or the block is empty.
    """
    idx = out_text.rfind(_COORD_HEADER)
    if idx == -1:
        raise XanesParseError(
            "no 'CARTESIAN COORDINATES (ANGSTROEM)' block in ORCA output"
        )
    atoms = []
    # lines[0] is the header itself; the next line is the dashed underline.
    for line in out_text[idx:].splitlines()[1:]:
        stripped = line.strip()
        if not stripped or set(stripped) <= set("-"):
            if atoms:
                break
            continue
        m = _COORD_LINE.match(line)
        if not m:
            if atoms:
                break
            continue
        atoms.append(
            (m.group(1), float(m.group(2)), float(m.group(3)), float(m.group(4)))
        )
    if not atoms:
        raise XanesParseError("empty CARTESIAN COORDINATES block in ORCA output")
    return atoms


def geometry_to_xyz(atoms, comment="xanes optimized geometry"):
    """Serialize ``(element, x, y, z)`` tuples into standard ``.xyz`` text."""
    lines = [str(len(atoms)), comment]
    for el, x, y, z in atoms:
        lines.append(f"{el:<2s} {x:>18.10f} {y:>18.10f} {z:>18.10f}")
    return "\n".join(lines) + "\n"
