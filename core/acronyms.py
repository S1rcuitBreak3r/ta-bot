"""
Team acronym expansion for knowledge searches.

The embedding model can't know that "LSCS" means caesarean section or that
"GC" means gastroscopy + colonoscopy, so queries are augmented before search:
the original text is kept and the expansion appended in parentheses, e.g.
    "lscs fee"  ->  "lscs (lower segment caesarean section) fee"

Single-letter shorthands (G, C, T, A) are only expanded for TOSP searches,
where they are unambiguous — expanding them in general /ask queries would
mangle ordinary sentences.
"""
from __future__ import annotations

import re

# Multi-letter acronyms — safe to expand in any search.
# Values use the terminology as printed in the MOH TOSP tables so the
# expansion embeds close to the real row.
ACRONYMS: dict[str, str] = {
    # Obstetrics / gynaecology
    "lscs": "lower segment caesarean section",
    "cs": "caesarean section",
    "csect": "caesarean section",
    "c-sect": "caesarean section",
    "tah": "total abdominal hysterectomy",
    "d&c": "dilatation and curettage of uterus",
    "d+c": "dilatation and curettage of uterus",
    # Endoscopy / GI
    "ogd": "gastroscopy upper gi endoscopy",
    "gc": "gastroscopy and colonoscopy upper gi endoscopy with colonoscopy",
    "g&c": "gastroscopy and colonoscopy upper gi endoscopy with colonoscopy",
    "g+c": "gastroscopy and colonoscopy upper gi endoscopy with colonoscopy",
    "ercp": "endoscopic retrograde cholangiopancreatography",
    "lap chole": "laparoscopic cholecystectomy gallbladder removal",
    "appendix": "appendicectomy",
    # ENT
    "t&a": "tonsillectomy and adenoidectomy tonsils removal with adenoidectomy",
    "t+a": "tonsillectomy and adenoidectomy tonsils removal with adenoidectomy",
    "fess": "functional endoscopic sinus surgery nasal sinuses",
    # Urology
    "turp": "transurethral resection of prostate",
    "turbt": "transurethral resection of bladder tumour",
    "pcnl": "percutaneous nephrolithotomy kidney calculus",
    "eswl": "extra corporeal shockwave lithotripsy",
    "circ": "circumcision",
    # Orthopaedics
    "orif": "open reduction internal fixation",
    "tkr": "total knee replacement arthroplasty",
    "thr": "total hip replacement arthroplasty",
    # Cardiac / general
    "cabg": "coronary artery bypass graft",
    "eua": "examination under anaesthesia",
}

# Single-letter shorthands — TOSP searches only.
TOSP_ONLY_ACRONYMS: dict[str, str] = {
    "g": "gastroscopy",
    "c": "colonoscopy",
    "t": "tonsillectomy tonsils removal",
    "a": "adenoidectomy adenoids removal",
}

# "T and A" spelled out with single letters — treat like T&A.
_T_AND_A = re.compile(r"\bt\s+(?:and|n)\s+a\b", re.IGNORECASE)

# Exact keywords (as printed in the MOH TOSP tables) for lexical fallback
# when semantic search comes up short. Each entry is a tuple of terms that
# must ALL appear in the chunk text (case-insensitive).
TOSP_KEYWORDS: dict[str, list[tuple[str, ...]]] = {
    "lscs":  [("Caesarean Section",)],
    "cs":    [("Caesarean Section",)],
    "csect": [("Caesarean Section",)],
    "tah":   [("Total Abdominal Hysterectomy",)],
    "d&c":   [("Uterus", "Curettage")],
    "d+c":   [("Uterus", "Curettage")],
    "ogd":   [("Gastroscopy",), ("Upper GI Endoscopy",)],
    "g":     [("Gastroscopy",), ("Upper GI Endoscopy",)],
    "c":     [("Colonoscopy",)],
    "gc":    [("Upper GI Endoscopy", "Colonoscopy")],
    "g&c":   [("Upper GI Endoscopy", "Colonoscopy")],
    "g+c":   [("Upper GI Endoscopy", "Colonoscopy")],
    "ercp":  [("Cholangiopancreatography",)],
    "t":     [("Tonsils",)],
    "a":     [("Adenoids",)],
    "t&a":   [("Tonsils", "Adenoidectomy")],
    "t+a":   [("Tonsils", "Adenoidectomy")],
    "circ":  [("Circumcision",)],
    "turp":  [("Transurethral Resection", "Prostate")],
    "turbt": [("Transurethral Resection", "Bladder")],
    "pcnl":  [("Nephrolithotomy",)],
    "eswl":  [("Shockwave Lithotripsy",)],
    "orif":  [("Open Reduction Internal Fixation",)],
    "cabg":  [("Coronary Artery Bypass",)],
    "eua":   [("Examination Under Anaesthesia",)],
}


def tosp_keywords(query_text: str) -> list[tuple[str, ...]]:
    """Return lexical keyword groups for any known acronyms in the query."""
    lower = query_text.lower()
    groups: list[tuple[str, ...]] = []
    if _T_AND_A.search(lower):
        groups.extend(TOSP_KEYWORDS["t&a"])
    for key in sorted(TOSP_KEYWORDS, key=len, reverse=True):
        if re.search(rf"(?<![\w&+]){re.escape(key)}(?![\w&+])", lower):
            for grp in TOSP_KEYWORDS[key]:
                if grp not in groups:
                    groups.append(grp)
    return groups


def expand_query(query_text: str, tosp: bool = False) -> str:
    """
    Expand known acronyms in the query.

    TOSP mode: the acronym is REPLACED by its expansion — leaving a token the
    embedding model doesn't understand ("circ", "gc") dilutes the embedding
    and drags similarity below the threshold.

    General mode: the expansion is appended in parentheses, preserving the
    original wording so exact-term glossary matches still work.
    """
    expanded = query_text

    if _T_AND_A.search(expanded):
        expanded = _T_AND_A.sub(ACRONYMS["t&a"], expanded) if tosp \
            else expanded + f" ({ACRONYMS['t&a']})"

    table = dict(ACRONYMS)
    if tosp:
        table.update(TOSP_ONLY_ACRONYMS)

    # Longest keys first so "lap chole" wins over a hypothetical "lap".
    for key in sorted(table, key=len, reverse=True):
        pattern = re.compile(rf"(?<![\w&+]){re.escape(key)}(?![\w&+])", re.IGNORECASE)
        if pattern.search(expanded):
            expansion = table[key]
            if expansion.lower() in expanded.lower():
                continue
            if tosp:
                expanded = pattern.sub(expansion, expanded)
            else:
                expanded += f" ({expansion})"

    return expanded
