"""
Tests for the onboarding module — DB and storage operations used by the handlers.
(Telegram interaction itself requires a live bot token; tested manually per checklist.)
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ta_bot_onboard_test_")
os.environ["STORAGE_ROOT"] = _TMP
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1234567")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import core.db as db
import core.storage as storage


ADMIN_ID = 1234567


class TestAddUser(unittest.TestCase):
    def setUp(self):
        db.init_db()
        db.seed_defaults()
        db.add_user(ADMIN_ID, "Dr Admin", "users/1234567", is_admin=True)

    def test_add_creates_user_and_folder(self):
        folder_rel = storage.ensure_user_folder(99001)
        db.add_user(99001, "Dr New", folder_rel)
        self.assertTrue(db.is_whitelisted(99001))
        folder_abs = Path(_TMP).resolve() / folder_rel
        self.assertTrue(folder_abs.exists())

    def test_add_is_idempotent_on_active_user(self):
        db.add_user(99002, "Dr Same", "users/99002")
        db.add_user(99002, "Dr Same Updated", "users/99002")
        user = db.get_user(99002)
        self.assertEqual(user["display_name"], "Dr Same Updated")
        self.assertEqual(user["status"], "active")

    def test_add_restores_archived_user(self):
        db.add_user(99003, "Dr Archived", "users/99003")
        db.archive_user(99003)
        self.assertFalse(db.is_whitelisted(99003))
        db.add_user(99003, "Dr Restored", "users/99003")
        self.assertTrue(db.is_whitelisted(99003))

    def test_birthday_stored_correctly(self):
        db.add_user(99004, "Dr Birthday", "users/99004", birthday="25-12")
        user = db.get_user(99004)
        self.assertEqual(user["birthday"], "25-12")

    def test_admin_flag(self):
        db.add_user(99005, "Dr Co-Admin", "users/99005", is_admin=True)
        self.assertTrue(db.is_admin(99005))

    def test_non_admin_not_admin(self):
        db.add_user(99006, "Dr Normal", "users/99006", is_admin=False)
        self.assertFalse(db.is_admin(99006))


class TestRemoveUser(unittest.TestCase):
    def setUp(self):
        db.init_db()
        db.seed_defaults()
        db.add_user(ADMIN_ID, "Dr Admin", "users/1234567", is_admin=True)

    def test_archive_revokes_whitelist(self):
        storage.ensure_user_folder(88001)
        db.add_user(88001, "Dr Gone", "users/88001")
        db.archive_user(88001)
        self.assertFalse(db.is_whitelisted(88001))

    def test_archive_preserves_record(self):
        storage.ensure_user_folder(88002)
        db.add_user(88002, "Dr History", "users/88002")
        db.archive_user(88002)
        user = db.get_user(88002)
        self.assertIsNotNone(user)
        self.assertEqual(user["status"], "archived")
        self.assertIsNotNone(user["archived_at"])

    def test_remove_nonexistent_is_safe(self):
        db.archive_user(99999)  # should not raise

    def test_folder_archived(self):
        storage.ensure_user_folder(88003)
        db.add_user(88003, "Dr FolderGone", "users/88003")
        storage.save_file(88003, "test.pdf", b"content")
        storage.archive_user_folder(88003)
        user_dir = Path(_TMP) / "users" / "88003"
        self.assertFalse(user_dir.exists())


class TestWhoami(unittest.TestCase):
    def setUp(self):
        db.init_db()
        db.seed_defaults()

    def test_active_user_returned(self):
        db.add_user(77001, "Dr Who", "users/77001", birthday="01-01")
        user = db.get_user(77001)
        self.assertEqual(user["display_name"], "Dr Who")
        self.assertEqual(user["status"], "active")
        self.assertEqual(user["birthday"], "01-01")

    def test_nonexistent_user_returns_none(self):
        self.assertIsNone(db.get_user(0))

    def test_not_whitelisted_after_archive(self):
        db.add_user(77002, "Dr Ex", "users/77002")
        db.archive_user(77002)
        self.assertFalse(db.is_whitelisted(77002))


class TestModuleFlag(unittest.TestCase):
    def setUp(self):
        db.init_db()
        db.seed_defaults()
        db.add_user(ADMIN_ID, "Dr Admin", "users/1234567", is_admin=True)

    def test_onboarding_enabled_by_default(self):
        from core.flags import is_module_enabled
        self.assertTrue(is_module_enabled("onboarding", ADMIN_ID))

    def test_admin_bypasses_disabled_module(self):
        db.set_module_state("onboarding", "disabled")
        from core.flags import is_module_enabled
        self.assertTrue(is_module_enabled("onboarding", ADMIN_ID))

    def test_non_admin_blocked_when_disabled(self):
        db.add_user(77003, "Dr Blocked", "users/77003")
        db.set_module_state("onboarding", "disabled")
        from core.flags import is_module_enabled
        self.assertFalse(is_module_enabled("onboarding", 77003))


if __name__ == "__main__":
    unittest.main()
