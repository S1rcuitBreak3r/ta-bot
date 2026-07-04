"""
Tests for the education digest module.

Network and Claude calls are mocked. DB uses a real SQLite temp dir.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_TMP = tempfile.mkdtemp(prefix="ta_bot_edu_test_")
os.environ["STORAGE_ROOT"] = _TMP
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["CHROMA_PATH"] = os.path.join(_TMP, "chroma")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1234567")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import core.db as db
from core.config import DB_PATH as _ACTUAL_DB_PATH

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
# Education sources
# --------------------------------------------------------------------------- #

class TestEducationSources(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_add_and_list_source(self):
        db.add_education_source("NEJM", "https://nejm.org/rss", "rss")
        sources = db.list_education_sources(active_only=True)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["name"], "NEJM")
        self.assertEqual(sources[0]["source_type"], "rss")

    def test_add_duplicate_url_updates_name(self):
        db.add_education_source("Old Name", "https://nejm.org/rss", "rss")
        db.add_education_source("NEJM Updated", "https://nejm.org/rss", "rss")
        sources = db.list_education_sources()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["name"], "NEJM Updated")

    def test_remove_source_deactivates(self):
        db.add_education_source("BMJ", "https://bmj.com/rss", "rss")
        sources = db.list_education_sources()
        source_id = sources[0]["id"]
        db.remove_education_source(source_id)
        active = db.list_education_sources(active_only=True)
        self.assertEqual(len(active), 0)
        all_sources = db.list_education_sources(active_only=False)
        self.assertEqual(len(all_sources), 1)
        self.assertFalse(all_sources[0]["active"])

    def test_list_empty(self):
        sources = db.list_education_sources()
        self.assertEqual(sources, [])


# --------------------------------------------------------------------------- #
# Digest history
# --------------------------------------------------------------------------- #

class TestDigestHistory(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_save_draft(self):
        digest_id = db.save_digest_draft("Weekly highlights: ...", ["NEJM", "BMJ"])
        self.assertIsNotNone(digest_id)
        digest = db.get_digest(digest_id)
        self.assertEqual(digest["status"], "draft")
        self.assertEqual(json.loads(digest["sources_used"]), ["NEJM", "BMJ"])

    def test_approve_digest(self):
        digest_id = db.save_digest_draft("This week in medicine...", [])
        db.approve_digest(digest_id, "admin_only", recipients_count=1)
        digest = db.get_digest(digest_id)
        self.assertEqual(digest["status"], "approved")
        self.assertEqual(digest["sent_to"], "admin_only")
        self.assertEqual(digest["recipients_count"], 1)
        self.assertIsNotNone(digest["sent_at"])

    def test_discard_digest(self):
        digest_id = db.save_digest_draft("Draft to discard", [])
        db.discard_digest(digest_id)
        digest = db.get_digest(digest_id)
        self.assertEqual(digest["status"], "discarded")

    def test_list_recent_digests(self):
        for i in range(7):
            db.save_digest_draft(f"Digest {i}", [])
        recent = db.list_recent_digests(limit=5)
        self.assertEqual(len(recent), 5)

    def test_get_nonexistent_returns_none(self):
        self.assertIsNone(db.get_digest(99999))

    def test_draft_remains_pending_after_approve_of_different_digest(self):
        id1 = db.save_digest_draft("Draft 1", [])
        id2 = db.save_digest_draft("Draft 2", [])
        db.approve_digest(id2, "admin_only", 1)
        d1 = db.get_digest(id1)
        self.assertEqual(d1["status"], "draft")


# --------------------------------------------------------------------------- #
# Digest generation (Claude mocked)
# --------------------------------------------------------------------------- #

class TestDigestGeneration(unittest.TestCase):
    def setUp(self):
        _reset_db()

    @patch("core.digest_fetcher.ask", new_callable=AsyncMock)
    def test_generate_no_sources(self, mock_ask):
        import asyncio
        mock_ask.return_value = "**Week's Highlights**\n• Item 1\n• Item 2"

        from core.digest_fetcher import generate_digest
        result = asyncio.get_event_loop().run_until_complete(
            generate_digest([], period="weekly")
        )
        self.assertIn("Highlights", result)
        mock_ask.assert_called_once()

    @patch("core.digest_fetcher.ask", new_callable=AsyncMock)
    def test_generate_with_content(self, mock_ask):
        import asyncio
        mock_ask.return_value = "Clinical updates this week..."

        from core.digest_fetcher import generate_digest
        content = [{"name": "NEJM", "content": "New drug approved for..."}]
        result = asyncio.get_event_loop().run_until_complete(
            generate_digest(content, period="weekly")
        )
        self.assertIsNotNone(result)
        call_args = mock_ask.call_args[1]["user_message"]
        self.assertIn("NEJM", call_args)


# --------------------------------------------------------------------------- #
# Source fetching (HTTP mocked)
# --------------------------------------------------------------------------- #

class TestFetchSources(unittest.TestCase):
    def setUp(self):
        _reset_db()

    @patch("core.digest_fetcher.httpx.AsyncClient")
    def test_failed_source_skipped(self, mock_client_class):
        import asyncio
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("Connection refused")
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value = mock_client

        from core.digest_fetcher import fetch_all_sources
        sources = [{"name": "Broken Source", "url": "http://broken.example.com", "source_type": "rss"}]
        content_items, failed = asyncio.get_event_loop().run_until_complete(
            fetch_all_sources(sources)
        )
        self.assertEqual(len(content_items), 0)
        self.assertEqual(failed, ["Broken Source"])

    @patch("core.digest_fetcher.feedparser")
    @patch("core.digest_fetcher.httpx.AsyncClient")
    def test_successful_rss_fetch(self, mock_client_class, mock_feedparser):
        import asyncio
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = "<rss><channel><item><title>Test</title></item></channel></rss>"
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value = mock_client

        mock_entry = MagicMock()
        mock_entry.get = lambda k, d="": {"title": "Test Article", "summary": "Summary here"}.get(k, d)
        mock_feedparser.parse.return_value = MagicMock(entries=[mock_entry])

        from core.digest_fetcher import fetch_all_sources
        sources = [{"name": "Test Feed", "url": "http://example.com/rss", "source_type": "rss"}]
        content_items, failed = asyncio.get_event_loop().run_until_complete(
            fetch_all_sources(sources)
        )
        self.assertEqual(len(content_items), 1)
        self.assertEqual(failed, [])
        self.assertIn("Test Article", content_items[0]["content"])


# --------------------------------------------------------------------------- #
# Module flag
# --------------------------------------------------------------------------- #

class TestModuleFlag(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_education_is_admin_only_by_default(self):
        state = db.get_module_state("education")
        self.assertEqual(state, "admin_only")

    def test_non_admin_blocked_by_flag(self):
        db.add_user(33001, "Dr NonAdmin", "users/33001", is_admin=False)
        from core.flags import is_module_enabled
        self.assertFalse(is_module_enabled("education", 33001))

    def test_admin_bypasses_admin_only(self):
        from core.flags import is_module_enabled
        self.assertTrue(is_module_enabled("education", ADMIN_ID))

    def test_enable_for_all_users(self):
        db.set_module_state("education", "all_users")
        db.add_user(33002, "Dr Regular", "users/33002", is_admin=False)
        from core.flags import is_module_enabled
        self.assertTrue(is_module_enabled("education", 33002))


if __name__ == "__main__":
    unittest.main()
