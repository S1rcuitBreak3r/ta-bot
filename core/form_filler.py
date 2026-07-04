"""
Form filler — profile I/O and Claude-assisted form filling.

Profile is stored as profile.json inside the user's storage folder.
Fields: full_name, mmc_number, clinic_name, designation, contact, address.
NRIC is explicitly excluded (privacy policy decision).

All form-fill outputs are returned as plaintext drafts with an AI watermark.
Claude is instructed never to invent values — unfilled fields are marked NOT FILLED.
"""
from __future__ import annotations

import json
import logging

import core.storage as storage
from core.claude_client import ask

logger = logging.getLogger(__name__)

PROFILE_FILENAME = "profile.json"

# Ordered list of (field_key, label, hint)
PROFILE_FIELDS: list[tuple[str, str, str]] = [
    ("full_name",   "Full name",                   "e.g. Dr. Tan Hon Liang"),
    ("mmc_number",  "MMC / medical reg. number",   "e.g. MMC 12345 or MCR 12345A"),
    ("clinic_name", "Clinic or hospital name",     "e.g. T.A. Medical Group"),
    ("designation", "Designation / role",          "e.g. Consultant Anaesthesiologist"),
    ("contact",     "Contact number",              "e.g. +65 9123 4567"),
    ("address",     "Clinic address",              "e.g. 123 Clinic St, Singapore 123456"),
]

WATERMARK = (
    "\n\n─────────────────────────────\n"
    "⚠️  AI-ASSISTED DRAFT — Review every field before submitting.\n"
    "Do not rely on this output without verification."
)


# --------------------------------------------------------------------------- #
# Profile I/O
# --------------------------------------------------------------------------- #

def load_profile(telegram_id: int) -> dict | None:
    """Return the user's profile dict, or None if not set."""
    rel_path = f"users/{telegram_id}/{PROFILE_FILENAME}"
    try:
        data = storage.read_file(telegram_id, rel_path)
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def save_profile(telegram_id: int, profile: dict) -> str:
    """Save profile as JSON in the user's folder. Returns relative path."""
    data = json.dumps(profile, indent=2, ensure_ascii=False).encode("utf-8")
    return storage.save_file(telegram_id, PROFILE_FILENAME, data)


def format_profile(profile: dict) -> str:
    """Format profile for display (no sensitive field leakage)."""
    lines = ["Your profile:\n"]
    for key, label, _ in PROFILE_FIELDS:
        value = profile.get(key) or "(not set)"
        lines.append(f"  {label}: {value}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Claude form filling
# --------------------------------------------------------------------------- #

async def fill_form(
    form_text: str, profile: dict, low_confidence: bool
) -> str:
    """
    Ask Claude to fill a form using the user's profile.
    Returns a plaintext filled-form response (watermarked).
    """
    profile_text = "\n".join(
        f"{label}: {profile.get(key, '(not provided)')}"
        for key, label, _ in PROFILE_FIELDS
    )

    if low_confidence:
        prompt = (
            "The text below was extracted from a scanned or image-based form "
            "and may be incomplete or garbled.\n\n"
            "USER PROFILE:\n"
            f"{profile_text}\n\n"
            "FORM TEXT (possibly imperfect):\n"
            f"{form_text[:2000]}\n\n"
            "Because this is a scanned form, DO NOT attempt to fill it in directly. "
            "Instead:\n"
            "1. List the form fields or sections you can identify.\n"
            "2. For each field, show the matching profile value if available.\n"
            "3. Mark fields with no matching profile data as: NOT FILLED\n\n"
            "Format as a two-column table: Field | Value"
        )
    else:
        prompt = (
            "Fill in the following form using ONLY the user profile data provided.\n\n"
            "USER PROFILE:\n"
            f"{profile_text}\n\n"
            "FORM FIELDS / STRUCTURE:\n"
            f"{form_text[:3000]}\n\n"
            "Rules:\n"
            "- Match each form field to the closest profile value.\n"
            "- If no matching value exists, write: NOT FILLED\n"
            "- Never invent, guess, or extrapolate values.\n"
            "- Show the result as: Field → Value\n"
            "- After all fields, add a one-line note on what was NOT FILLED."
        )

    system = (
        "You help medical professionals fill administrative forms. "
        "Use ONLY provided profile data. Never invent values. "
        "Clearly mark missing data as NOT FILLED."
    )

    result = await ask(system=system, user_message=prompt, max_tokens=1000)
    return result + WATERMARK
