"""
Tests for core/storage.py.

Focuses on the security-critical path: file operations must stay inside
the user's designated folder. Traversal attempts must raise StorageSecurityError.
"""
import os
import sys
import tempfile
import unittest

# Allow running from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

# Override STORAGE_ROOT to a temp directory for all tests
_TMP_DIR = tempfile.mkdtemp(prefix="ta_bot_test_")
os.environ.setdefault("STORAGE_ROOT", _TMP_DIR)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1234567")

from core.storage import (  # noqa: E402
    StorageSecurityError,
    _resolve_safe,
    archive_file,
    ensure_user_folder,
    list_files,
    read_file,
    save_file,
)
from pathlib import Path


class TestResolveSafe(unittest.TestCase):
    def setUp(self):
        self.base = Path(_TMP_DIR) / "resolve_test"
        self.base.mkdir(exist_ok=True)

    def test_safe_filename(self):
        resolved = _resolve_safe(self.base, "document.pdf")
        self.assertTrue(str(resolved).startswith(str(self.base.resolve())))

    def test_traversal_dotdot(self):
        with self.assertRaises(StorageSecurityError):
            _resolve_safe(self.base, "../other_user/secret.pdf")

    def test_traversal_nested(self):
        with self.assertRaises(StorageSecurityError):
            _resolve_safe(self.base, "subdir/../../etc/passwd")

    def test_absolute_path(self):
        with self.assertRaises(StorageSecurityError):
            _resolve_safe(self.base, "/etc/passwd")


class TestSaveReadFile(unittest.TestCase):
    TELEGRAM_ID = 99991

    def test_save_and_read_roundtrip(self):
        data = b"test content"
        rel = save_file(self.TELEGRAM_ID, "test.txt", data)
        self.assertTrue(rel.startswith("users/"))
        back = read_file(self.TELEGRAM_ID, rel)
        self.assertEqual(back, data)

    def test_read_cross_user_blocked(self):
        other_id = 99992
        data = b"secret"
        rel = save_file(other_id, "private.txt", data)
        with self.assertRaises(StorageSecurityError):
            read_file(self.TELEGRAM_ID, rel)  # wrong user

    def test_traversal_in_save_filename(self):
        with self.assertRaises(StorageSecurityError):
            save_file(self.TELEGRAM_ID, "../other/file.pdf", b"data")


class TestListFiles(unittest.TestCase):
    TELEGRAM_ID = 99993

    def test_list_empty(self):
        ensure_user_folder(self.TELEGRAM_ID)
        files = list_files(self.TELEGRAM_ID)
        self.assertIsInstance(files, list)

    def test_list_after_save(self):
        save_file(self.TELEGRAM_ID, "doc1.pdf", b"a")
        save_file(self.TELEGRAM_ID, "doc2.pdf", b"b")
        files = list_files(self.TELEGRAM_ID)
        self.assertGreaterEqual(len(files), 2)


class TestArchiveFile(unittest.TestCase):
    TELEGRAM_ID = 99994

    def test_archive_moves_file(self):
        rel = save_file(self.TELEGRAM_ID, "insurance.pdf", b"content")
        archived_rel = archive_file(self.TELEGRAM_ID, rel)
        self.assertIn("_archived", archived_rel)
        # Original should be gone
        from core.config import STORAGE_ROOT
        self.assertFalse((Path(STORAGE_ROOT) / rel).exists())

    def test_archive_cross_user_blocked(self):
        other_id = 99995
        rel = save_file(other_id, "private.pdf", b"content")
        with self.assertRaises(StorageSecurityError):
            archive_file(self.TELEGRAM_ID, rel)


if __name__ == "__main__":
    unittest.main()
