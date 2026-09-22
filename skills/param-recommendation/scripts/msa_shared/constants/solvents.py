"""CPCM implicit-solvent map — single source of truth (PIPELINE.md §4.1, iron rule 21).

task-recognition (P1-1) already resolves a user's solvent phrase into the
canonical ``solvent_std`` string (e.g. "CPCM(Water)") or "" for the gas phase, so
downstream skills normally pass ``solvent_std`` straight through. This module
exists so any skill that must (re)map a raw solvent name to a CPCM keyword shares
one dictionary rather than defining its own.

The keys mirror task-recognition/dicts/solvents.json verbatim (domain data); the
gas-phase sentinels ("气相"/"真空"/"无溶剂") map to "" which callers treat as None.
"""

# Chinese / English / abbreviation -> ORCA CPCM keyword ("" == gas phase).
CPCM_SOLVENT_MAP = {
    "气相": "",
    "真空": "",
    "无溶剂": "",
    "水": "CPCM(Water)",
    "氯仿": "CPCM(Chloroform)",
    "三氯甲烷": "CPCM(Chloroform)",
    "CDCl3": "CPCM(Chloroform)",
    "环己烷": "CPCM(Cyclohexane)",
    "甲醇": "CPCM(Methanol)",
    "乙醇": "CPCM(Ethanol)",
    "二氯甲烷": "CPCM(CH2Cl2)",
    "DCM": "CPCM(CH2Cl2)",
    "乙腈": "CPCM(Acetonitrile)",
    "丙酮": "CPCM(Acetone)",
    "四氢呋喃": "CPCM(THF)",
    "THF": "CPCM(THF)",
    "甲苯": "CPCM(Toluene)",
    "苯": "CPCM(Benzene)",
    "二甲亚砜": "CPCM(DMSO)",
    "DMSO": "CPCM(DMSO)",
    "乙酸乙酯": "CPCM(EthylAcetate)",
}


def solvent_model_from_std(solvent_std):
    """Normalize a task's ``solvent_std`` into a params ``solvent_model`` value.

    ``solvent_std`` is already a CPCM keyword (or "" for gas / None). Returns the
    CPCM string for an implicit-solvent calculation, or None for the gas phase, so
    that a params step carries ``solvent_model=None`` in vacuum. Never raises.
    """
    s = (solvent_std or "").strip()
    return s or None
