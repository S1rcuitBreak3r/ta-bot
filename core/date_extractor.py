"""
Expiry date extraction from document text.

Two-step pipeline:
  1. Regex — looks for a date within 100 chars of an expiry-related keyword.
  2. Claude fallback — structured prompt asking specifically for the expiry date.

Both steps return (date, context_snippet).
low_confidence is True when the Claude fallback was needed or when OCR was used
during text extraction.
"""
from __future__ import annotations

import re
from datetime import date

# ---- Keywords that signal "expiry" context --------------------------------- #
_KW = re.compile(
    r"expir\w+|valid\s+(?:until|to|through)|renewal\s+date|renew\s+by"
    r"|effective\s+until|due\s+(?:for\s+)?renewal|valid\s+from\s+\S+\s+to",
    re.IGNORECASE,
)

# ---- Date patterns (tried in order) --------------------------------------- #
_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_ABBR = ("jan", "feb", "mar", "apr", "may", "jun",
               "jul", "aug", "sep", "oct", "nov", "dec")
_MON_PATTERN = "|".join(_MONTH_NAMES + _MONTH_ABBR)
_MONTH_MAP = {m: i + 1 for i, m in enumerate(_MONTH_NAMES)}
_MONTH_MAP.update({m: i + 1 for i, m in enumerate(_MONTH_ABBR)})

_DATE_RE = [
    # DD/MM/YYYY or DD-MM-YYYY
    re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b"),
    # 31 December 2026 or 31 Dec 2026
    re.compile(
        rf"\b(\d{{1,2}})\s+({_MON_PATTERN})\s+(\d{{4}})\b", re.IGNORECASE
    ),
    # December 31, 2026 or Dec 31 2026
    re.compile(
        rf"\b({_MON_PATTERN})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE
    ),
]


def _parse_match(m: re.Match, pattern_idx: int) -> date | None:
    try:
        g = m.groups()
        if pattern_idx == 0:
            day, month, year = int(g[0]), int(g[1]), int(g[2])
        elif pattern_idx == 1:
            day, month, year = int(g[0]), _MONTH_MAP.get(g[1].lower(), 0), int(g[2])
        else:
            month, day, year = _MONTH_MAP.get(g[0].lower(), 0), int(g[1]), int(g[2])
        if month == 0:
            return None
        return date(year, month, day)
    except (ValueError, IndexError):
        return None


def parse_user_date(text: str) -> date | None:
    """
    Parse a date string entered by the user.
    Accepts: DD-MM-YYYY, DD/MM/YYYY, DD MMM YYYY, DD Month YYYY.
    Returns a date object or None.
    """
    text = text.strip()
    for sep in ("-", "/"):
        parts = text.split(sep)
        if len(parts) == 3:
            try:
                day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
                return date(year, month, day)
            except ValueError:
                pass
    for i in (1, 2):
        for m in _DATE_RE[i].finditer(text):
            d = _parse_match(m, i)
            if d:
                return d
    return None


def format_date(d: date) -> str:
    """Format a date for display: '31 Dec 2026'."""
    return f"{d.day} {d.strftime('%b')} {d.year}"


# --------------------------------------------------------------------------- #
# Regex extraction
# --------------------------------------------------------------------------- #

def extract_regex(text: str) -> tuple[date | None, str | None]:
    """
    Find an expiry date via keyword + proximity search.
    Returns (date, context_snippet) or (None, None).
    """
    today = date.today()
    for kw_match in _KW.finditer(text):
        start = max(0, kw_match.start() - 20)
        end = min(len(text), kw_match.end() + 120)
        window = text[start:end]
        for i, pat in enumerate(_DATE_RE):
            for dm in pat.finditer(window):
                d = _parse_match(dm, i)
                if d and d > today:
                    context = window.strip()[:200].replace("\n", " ")
                    return d, context
    return None, None


# --------------------------------------------------------------------------- #
# Claude fallback (async)
# --------------------------------------------------------------------------- #

async def extract_claude(text: str, doc_type: str) -> tuple[date | None, str | None]:
    """
    Ask Claude for the expiry date when regex comes up empty.
    Returns (date, context_snippet) or (None, None).
    """
    from core.claude_client import ask  # late import — avoids loading Anthropic in tests

    doc_label = doc_type.replace("_", " ").title()
    prompt = (
        f"Extract the EXPIRY, RENEWAL, or VALIDITY END date from this {doc_label} document.\n"
        "Reply with ONLY the date in DD-MM-YYYY format (e.g. 31-12-2026).\n"
        "If there is no expiry date, reply ONLY with: NOT_FOUND\n\n"
        f"Document text (first 3000 characters):\n{text[:3000]}"
    )
    try:
        reply = (
            await ask(
                system="You extract expiry dates from medical documents. Reply exactly as instructed.",
                user_message=prompt,
                max_tokens=30,
            )
        ).strip()
        if reply.upper() == "NOT_FOUND":
            return None, None
        d = parse_user_date(reply)
        if d and d > date.today():
            return d, "(date identified by AI — please verify carefully)"
    except Exception:
        pass
    return None, None


# --------------------------------------------------------------------------- #
# Full pipeline (async — calls Claude if regex fails)
# --------------------------------------------------------------------------- #

async def extract_from_document(
    file_bytes: bytes, filename: str, doc_type: str
) -> tuple[date | None, str | None, bool]:
    """
    Extract the expiry date from a document.
    Returns (date, context_snippet, low_confidence).
    low_confidence=True when OCR or Claude was needed.
    """
    from core.ingestion import extract_text  # late import

    text, text_low_conf = extract_text(file_bytes, filename)

    d, ctx = extract_regex(text)
    if d:
        return d, ctx, text_low_conf

    # Claude fallback
    d, ctx = await extract_claude(text, doc_type)
    return d, ctx, True  # always low_confidence when Claude was needed
