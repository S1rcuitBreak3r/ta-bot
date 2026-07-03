"""
Tests for core/db.py — schema, seed, and key data operations.
"""
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="ta_bot_db_test_")
os.environ["STORAGE_ROOT"] = _TMP
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1234567")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import core.db as db


class TestInitAndSeed(unittest.TestCase):
    def setUp(self):
        db.init_db()
        db.seed_defaults()

    def test_all_module_flags_exist(self):
        states = db.list_module_states()
        names = {m["module_name"] for m in states}
        self.assertEqual(names, {"onboarding", "knowledge_bank", "expiry_tracker",
                                 "education", "form_filler"})

    def test_default_states(self):
        for module in ("onboarding", "knowledge_bank", "expiry_tracker"):
            self.assertEqual(db.get_module_state(module), "all_users")
        for module in ("education", "form_filler"):
            self.assertEqual(db.get_module_state(module), "admin_only")

    def test_seed_is_idempotent(self):
        db.seed_defaults()
        db.seed_defaults()
        states = db.list_module_states()
        self.assertEqual(len(states), 5)


class TestUsers(unittest.TestCase):
    def setUp(self):
        db.init_db()
        db.seed_defaults()

    def test_add_and_get_user(self):
        db.add_user(111, "Dr Test", "users/111")
        user = db.get_user(111)
        self.assertIsNotNone(user)
        self.assertEqual(user["display_name"], "Dr Test")
        self.assertEqual(user["status"], "active")

    def test_whitelist_check(self):
        db.add_user(222, "Dr Two", "users/222")
        self.assertTrue(db.is_whitelisted(222))
        self.assertFalse(db.is_whitelisted(999))

    def test_archive_user(self):
        db.add_user(333, "Dr Three", "users/333")
        db.archive_user(333)
        self.assertFalse(db.is_whitelisted(333))
        user = db.get_user(333)
        self.assertEqual(user["status"], "archived")

    def test_readd_archived_user(self):
        db.add_user(444, "Dr Four", "users/444")
        db.archive_user(444)
        db.add_user(444, "Dr Four Renewed", "users/444")
        self.assertTrue(db.is_whitelisted(444))


class TestModuleFlags(unittest.TestCase):
    def setUp(self):
        db.init_db()
        db.seed_defaults()

    def test_set_and_get_state(self):
        db.set_module_state("education", "all_users")
        self.assertEqual(db.get_module_state("education"), "all_users")

    def test_disable_module(self):
        db.set_module_state("knowledge_bank", "disabled")
        self.assertEqual(db.get_module_state("knowledge_bank"), "disabled")

    def test_unknown_module_returns_disabled(self):
        self.assertEqual(db.get_module_state("nonexistent"), "disabled")


class TestTrackedDocuments(unittest.TestCase):
    def setUp(self):
        db.init_db()
        db.seed_defaults()
        db.add_user(555, "Dr Five", "users/555")

    def test_insert_and_get_document(self):
        doc_id = db.insert_document(555, "PASSPORT", "users/555/passport.pdf",
                                    "2028-01-01", "Valid until 01 Jan 2028", "high")
        doc = db.get_active_document(555, "PASSPORT")
        self.assertIsNotNone(doc)
        self.assertEqual(doc["id"], doc_id)

    def test_archive_on_re_upload(self):
        db.insert_document(555, "INDEMNITY_INSURANCE", "users/555/ins_v1.pdf",
                           "2026-06-01", None, "low")
        doc_v1 = db.get_active_document(555, "INDEMNITY_INSURANCE")
        db.archive_document(doc_v1["id"])
        db.insert_document(555, "INDEMNITY_INSURANCE", "users/555/ins_v2.pdf",
                           "2027-06-01", None, "high")
        doc_active = db.get_active_document(555, "INDEMNITY_INSURANCE")
        self.assertEqual(doc_active["local_file_path"], "users/555/ins_v2.pdf")


class TestSchedulerLog(unittest.TestCase):
    def setUp(self):
        db.init_db()

    def test_log_start_and_done(self):
        log_id = db.log_scheduler_start("test_job")
        self.assertIsNotNone(log_id)
        db.log_scheduler_done(log_id, success=True)
        last = db.get_last_scheduler_run("test_job")
        self.assertIsNotNone(last)
        self.assertEqual(last["success"], 1)

    def test_failed_job_not_returned_as_last_success(self):
        log_id = db.log_scheduler_start("fail_job")
        db.log_scheduler_done(log_id, success=False, error_detail="boom")
        last = db.get_last_scheduler_run("fail_job")
        self.assertIsNone(last)


if __name__ == "__main__":
    unittest.main()
