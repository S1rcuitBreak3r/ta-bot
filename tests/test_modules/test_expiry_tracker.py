"""
Tests for the expiry tracker module.

Claude API calls are mocked. DB uses a real SQLite in-memory temp dir.
"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

_TMP = tempfile.mkdtemp(prefix="ta_bot_expiry_test_")
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
from core.date_extractor import extract_regex, format_date, parse_user_date

ADMIN_ID = 1234567


def _reset_db():
    for suffix in ("", "-wal", "-shm"):
        p = _ACTUAL_DB_PATH + suffix
        if os.path.exists(p):
            os.unlink(p)
    db.init_db()
    db.seed_defaults()
    db.add_user(ADMIN_ID, "Dr Admin", "users/1234567", is_admin=True)


# --------------------------------------------------------------------------- #
# Date extraction — regex
# --------------------------------------------------------------------------- #

class TestExtractRegex(unittest.TestCase):
    def test_dd_mm_yyyy_after_expiry_keyword(self):
        text = "This certificate is valid until 31/12/2030."
        d, ctx = extract_regex(text)
        self.assertIsNotNone(d)
        self.assertEqual(d.year, 2030)
        self.assertEqual(d.month, 12)
        self.assertEqual(d.day, 31)

    def test_month_name_format(self):
        text = "Policy expires 15 March 2028."
        d, ctx = extract_regex(text)
        self.assertIsNotNone(d)
        self.assertEqual(d, date(2028, 3, 15))

    def test_abbreviated_month(self):
        text = "Renewal date: 01 Jan 2027"
        d, ctx = extract_regex(text)
        self.assertIsNotNone(d)
        self.assertEqual(d.month, 1)

    def test_no_keyword_returns_none(self):
        text = "Issued on 01/01/2020. Date of birth: 15/05/1990."
        d, ctx = extract_regex(text)
        self.assertIsNone(d)

    def test_past_date_ignored(self):
        text = "Valid until 01/01/2000. Expiry date: 01/01/2000."
        d, ctx = extract_regex(text)
        self.assertIsNone(d)

    def test_context_snippet_returned(self):
        text = "Renewal date: 30 Jun 2030 — please renew before this date."
        d, ctx = extract_regex(text)
        self.assertIsNotNone(d)
        self.assertIsNotNone(ctx)
        self.assertIn("2030", ctx)


# --------------------------------------------------------------------------- #
# Date parsing (user input)
# --------------------------------------------------------------------------- #

class TestParseUserDate(unittest.TestCase):
    def test_dd_mm_yyyy_dash(self):
        d = parse_user_date("31-12-2026")
        self.assertEqual(d, date(2026, 12, 31))

    def test_dd_mm_yyyy_slash(self):
        d = parse_user_date("15/03/2027")
        self.assertEqual(d, date(2027, 3, 15))

    def test_dd_mon_yyyy(self):
        d = parse_user_date("01 Jan 2028")
        self.assertEqual(d, date(2028, 1, 1))

    def test_full_month_name(self):
        d = parse_user_date("25 December 2029")
        self.assertEqual(d, date(2029, 12, 25))

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_user_date("not a date"))
        self.assertIsNone(parse_user_date(""))
        self.assertIsNone(parse_user_date("99-99-9999"))

    def test_format_date(self):
        self.assertEqual(format_date(date(2026, 3, 5)), "5 Mar 2026")
        self.assertEqual(format_date(date(2030, 12, 31)), "31 Dec 2030")


# --------------------------------------------------------------------------- #
# Document DB operations
# --------------------------------------------------------------------------- #

class TestTrackedDocuments(unittest.TestCase):
    def setUp(self):
        _reset_db()
        db.add_user(55001, "Dr Expiry", "users/55001")
        storage.ensure_user_folder(55001)

    def test_insert_and_retrieve(self):
        doc_id = db.insert_document(
            55001, "INDEMNITY_INSURANCE", "users/55001/ins.pdf",
            extracted_expiry_date="2027-06-30",
            extraction_context="valid until 30 June 2027",
            extraction_confidence="high",
        )
        self.assertIsNotNone(doc_id)
        docs = db.list_user_documents(55001)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["doc_type"], "INDEMNITY_INSURANCE")
        self.assertEqual(docs[0]["extracted_expiry_date"], "2027-06-30")

    def test_archive_on_reupload(self):
        doc_id_1 = db.insert_document(
            55001, "PASSPORT", "users/55001/pass_v1.pdf",
            extracted_expiry_date="2026-01-01",
        )
        existing = db.get_active_document(55001, "PASSPORT")
        self.assertIsNotNone(existing)

        db.archive_document(existing["id"])
        doc_id_2 = db.insert_document(
            55001, "PASSPORT", "users/55001/pass_v2.pdf",
            extracted_expiry_date="2028-01-01",
        )

        # Only second doc is active
        docs = db.list_user_documents(55001)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["extracted_expiry_date"], "2028-01-01")

    def test_unique_active_doc_per_type(self):
        """UNIQUE partial index prevents two active rows for same user+type."""
        db.insert_document(55001, "BCLS", "users/55001/bcls.pdf",
                           extracted_expiry_date="2027-01-01")
        import sqlite3
        with self.assertRaises((sqlite3.IntegrityError, Exception)):
            db.insert_document(55001, "BCLS", "users/55001/bcls2.pdf",
                               extracted_expiry_date="2028-01-01")

    def test_get_documents_expiring_on_day(self):
        target = date.today() + timedelta(days=30)
        db.insert_document(
            55001, "ACLS", "users/55001/acls.pdf",
            extracted_expiry_date=target.isoformat(),
        )
        docs = db.get_documents_expiring_on_day(30)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["doc_type"], "ACLS")

    def test_expiring_on_different_day_not_returned(self):
        target = date.today() + timedelta(days=31)
        db.insert_document(
            55001, "WORK_PASS", "users/55001/wp.pdf",
            extracted_expiry_date=target.isoformat(),
        )
        docs = db.get_documents_expiring_on_day(30)
        self.assertEqual(len(docs), 0)

    def test_update_expiry_date(self):
        doc_id = db.insert_document(
            55001, "MEDICAL_REGISTRATION", "users/55001/reg.pdf",
            extracted_expiry_date="2026-06-01",
        )
        db.update_document_expiry(doc_id, "2028-06-01", reconfirmed=True)
        docs = db.list_user_documents(55001)
        self.assertEqual(docs[0]["extracted_expiry_date"], "2028-06-01")
        self.assertIsNotNone(docs[0]["last_reconfirmed_at"])


# --------------------------------------------------------------------------- #
# Birthday
# --------------------------------------------------------------------------- #

class TestBirthday(unittest.TestCase):
    def setUp(self):
        _reset_db()
        db.add_user(66001, "Dr Birthday", "users/66001", birthday="25-12")

    def test_birthday_stored(self):
        user = db.get_user(66001)
        self.assertEqual(user["birthday"], "25-12")

    def test_birthday_today_query(self):
        today_ddmm = date.today().strftime("%d-%m")
        db.add_user(66002, "Dr Today", "users/66002", birthday=today_ddmm)
        users = db.get_users_with_birthday_today(today_ddmm)
        ids = [u["telegram_id"] for u in users]
        self.assertIn(66002, ids)

    def test_birthday_other_day_not_returned(self):
        users = db.get_users_with_birthday_today("01-01")
        ids = [u["telegram_id"] for u in users]
        self.assertNotIn(66001, ids)


# --------------------------------------------------------------------------- #
# Module flag enforcement
# --------------------------------------------------------------------------- #

class TestModuleFlag(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_expiry_tracker_enabled_by_default(self):
        from core.flags import is_module_enabled
        self.assertTrue(is_module_enabled("expiry_tracker", ADMIN_ID))

    def test_non_admin_blocked_when_disabled(self):
        db.add_user(44001, "Dr Blocked", "users/44001")
        db.set_module_state("expiry_tracker", "disabled")
        from core.flags import is_module_enabled
        self.assertFalse(is_module_enabled("expiry_tracker", 44001))


if __name__ == "__main__":
    unittest.main()
