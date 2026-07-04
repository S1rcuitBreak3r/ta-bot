"""
Tests for the form filler module.

Claude calls are mocked. Storage uses a real temp dir.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

_TMP = tempfile.mkdtemp(prefix="ta_bot_form_test_")
os.environ["STORAGE_ROOT"] = _TMP
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["CHROMA_PATH"] = os.path.join(_TMP, "chroma")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1234567")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import core.db as db
import core.storage as storage
from core.config import DB_PATH as _ACTUAL_DB_PATH
from core.form_filler import (
    PROFILE_FIELDS,
    WATERMARK,
    fill_form,
    format_profile,
    load_profile,
    save_profile,
)

ADMIN_ID = 1234567
USER_ID = 55555


def _reset_db():
    for suffix in ("", "-wal", "-shm"):
        p = _ACTUAL_DB_PATH + suffix
        if os.path.exists(p):
            os.unlink(p)
    db.init_db()
    db.seed_defaults()
    db.add_user(ADMIN_ID, "Dr Admin", "users/1234567", is_admin=True)


# --------------------------------------------------------------------------- #
# Profile I/O
# --------------------------------------------------------------------------- #

class TestProfileIO(unittest.TestCase):
    def setUp(self):
        _reset_db()
        db.add_user(USER_ID, "Dr Test", "users/55555")
        storage.ensure_user_folder(USER_ID)

    def test_save_and_load_roundtrip(self):
        profile = {
            "full_name": "Dr. Test User",
            "mmc_number": "MMC 99999",
            "clinic_name": "Test Clinic",
            "designation": "Consultant",
            "contact": "+65 9000 0000",
            "address": "1 Test Street, Singapore 000001",
        }
        save_profile(USER_ID, profile)
        loaded = load_profile(USER_ID)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["full_name"], "Dr. Test User")
        self.assertEqual(loaded["mmc_number"], "MMC 99999")
        self.assertEqual(loaded["address"], "1 Test Street, Singapore 000001")

    def test_load_nonexistent_returns_none(self):
        result = load_profile(99999)
        self.assertIsNone(result)

    def test_save_overwrites_previous(self):
        profile1 = {"full_name": "Old Name", "mmc_number": "MMC 111"}
        save_profile(USER_ID, profile1)

        profile2 = {"full_name": "New Name", "mmc_number": "MMC 222"}
        save_profile(USER_ID, profile2)

        loaded = load_profile(USER_ID)
        self.assertEqual(loaded["full_name"], "New Name")
        self.assertEqual(loaded["mmc_number"], "MMC 222")

    def test_format_profile_shows_all_fields(self):
        profile = {
            "full_name": "Dr. Example",
            "mmc_number": "MMC 12345",
            "clinic_name": "Example Clinic",
            "designation": "GP",
            "contact": "+65 0000 0000",
            "address": "1 Example Road",
        }
        formatted = format_profile(profile)
        self.assertIn("Dr. Example", formatted)
        self.assertIn("MMC 12345", formatted)
        self.assertIn("GP", formatted)

    def test_format_profile_shows_not_set_for_missing(self):
        profile = {"full_name": "Dr. Partial"}
        formatted = format_profile(profile)
        self.assertIn("(not set)", formatted)

    def test_profile_excludes_nric(self):
        """Verify no NRIC field exists in PROFILE_FIELDS."""
        field_keys = [f[0] for f in PROFILE_FIELDS]
        self.assertNotIn("nric", field_keys)
        self.assertNotIn("ic_number", field_keys)
        self.assertNotIn("identity", field_keys)


# --------------------------------------------------------------------------- #
# Form filling (Claude mocked)
# --------------------------------------------------------------------------- #

class TestFillForm(unittest.TestCase):
    SAMPLE_PROFILE = {
        "full_name": "Dr. Tan Hon Liang",
        "mmc_number": "MMC 54321",
        "clinic_name": "T.A. Medical Group",
        "designation": "Consultant Anaesthesiologist",
        "contact": "+65 9123 4567",
        "address": "10 Clinic Road, Singapore 123456",
    }

    @patch("core.form_filler.ask", new_callable=AsyncMock)
    def test_fill_native_pdf(self, mock_ask):
        import asyncio
        mock_ask.return_value = "Name → Dr. Tan Hon Liang\nMMC → MMC 54321\nDate → NOT FILLED"

        form_text = "Applicant Name: ______\nMMC Number: ______\nDate of Application: ______"
        result = asyncio.get_event_loop().run_until_complete(
            fill_form(form_text, self.SAMPLE_PROFILE, low_confidence=False)
        )
        self.assertIn("Dr. Tan Hon Liang", result)
        self.assertIn(WATERMARK, result)

    @patch("core.form_filler.ask", new_callable=AsyncMock)
    def test_fill_scanned_returns_table(self, mock_ask):
        import asyncio
        mock_ask.return_value = "Field | Value\nName | Dr. Tan Hon Liang\nNRIC | NOT FILLED"

        form_text = "blurry scanned text..."
        result = asyncio.get_event_loop().run_until_complete(
            fill_form(form_text, self.SAMPLE_PROFILE, low_confidence=True)
        )
        self.assertIn(WATERMARK, result)
        # Verify scanned-mode prompt was used (table format expected)
        call_args = mock_ask.call_args[1]["user_message"]
        self.assertIn("scanned", call_args.lower())

    @patch("core.form_filler.ask", new_callable=AsyncMock)
    def test_watermark_always_appended(self, mock_ask):
        import asyncio
        mock_ask.return_value = "Filled content"

        for low_conf in (True, False):
            result = asyncio.get_event_loop().run_until_complete(
                fill_form("some form", self.SAMPLE_PROFILE, low_confidence=low_conf)
            )
            self.assertIn(WATERMARK, result)

    @patch("core.form_filler.ask", new_callable=AsyncMock)
    def test_claude_prompt_never_invents_values(self, mock_ask):
        import asyncio
        mock_ask.return_value = "NOT FILLED"

        result = asyncio.get_event_loop().run_until_complete(
            fill_form("NRIC: ______", self.SAMPLE_PROFILE, low_confidence=False)
        )
        # The prompt should instruct Claude not to invent values
        call_args = mock_ask.call_args[1]["user_message"]
        self.assertTrue(
            "never" in call_args.lower() or "not invent" in call_args.lower()
            or "do not" in call_args.lower()
        )


# --------------------------------------------------------------------------- #
# Module flag
# --------------------------------------------------------------------------- #

class TestModuleFlag(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_form_filler_is_admin_only_by_default(self):
        state = db.get_module_state("form_filler")
        self.assertEqual(state, "admin_only")

    def test_non_admin_blocked_by_flag(self):
        db.add_user(22001, "Dr NonAdmin", "users/22001", is_admin=False)
        from core.flags import is_module_enabled
        self.assertFalse(is_module_enabled("form_filler", 22001))

    def test_admin_bypasses_admin_only(self):
        from core.flags import is_module_enabled
        self.assertTrue(is_module_enabled("form_filler", ADMIN_ID))

    def test_enable_for_all_users(self):
        db.add_user(22002, "Dr Staff", "users/22002", is_admin=False)
        db.set_module_state("form_filler", "all_users")
        from core.flags import is_module_enabled
        self.assertTrue(is_module_enabled("form_filler", 22002))


# --------------------------------------------------------------------------- #
# Profile fields coverage
# --------------------------------------------------------------------------- #

class TestProfileFields(unittest.TestCase):
    def test_six_fields_defined(self):
        self.assertEqual(len(PROFILE_FIELDS), 6)

    def test_each_field_has_key_label_hint(self):
        for item in PROFILE_FIELDS:
            self.assertEqual(len(item), 3)
            key, label, hint = item
            self.assertTrue(key)
            self.assertTrue(label)
            self.assertTrue(hint)


if __name__ == "__main__":
    unittest.main()
