"""
Tests for the knowledge bank module.

ChromaDB is mocked (unittest.mock.patch) so tests run without network or
GPU resources. DB operations use a real SQLite in-memory temp dir.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_TMP = tempfile.mkdtemp(prefix="ta_bot_kb_test_")
os.environ["STORAGE_ROOT"] = _TMP
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["CHROMA_PATH"] = os.path.join(_TMP, "chroma")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1234567")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import core.db as db
import core.ingestion as ingestion
import core.search as search
from core.config import DB_PATH as _ACTUAL_DB_PATH  # the path get_conn() really uses

ADMIN_ID = 1234567


def _reset_db():
    """Delete and reinitialise the DB that get_conn() actually uses."""
    for suffix in ("", "-wal", "-shm"):
        p = _ACTUAL_DB_PATH + suffix
        if os.path.exists(p):
            os.unlink(p)
    db.init_db()
    db.seed_defaults()
    db.add_user(ADMIN_ID, "Dr Admin", "users/1234567", is_admin=True)


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

class TestChunkText(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        chunks = ingestion.chunk_text("Hello world")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], "Hello world")

    def test_long_text_produces_multiple_chunks(self):
        text = "A" * 1000
        chunks = ingestion.chunk_text(text)
        self.assertGreater(len(chunks), 1)

    def test_empty_text_returns_empty(self):
        self.assertEqual(ingestion.chunk_text(""), [])

    def test_chunk_size_respects_config(self):
        from core.config import CHUNK_SIZE
        text = "X" * (CHUNK_SIZE + 50)
        chunks = ingestion.chunk_text(text)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), CHUNK_SIZE)

    def test_overlap_means_chunks_share_content(self):
        from core.config import CHUNK_OVERLAP
        text = "A" * 500
        chunks = ingestion.chunk_text(text)
        if len(chunks) >= 2:
            # Chunks overlap — end of chunk 0 == start of chunk 1
            self.assertGreater(len(chunks[0]), CHUNK_OVERLAP)


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #

class TestExtractText(unittest.TestCase):
    def test_plain_text_file(self):
        # Content must be > 100 chars to be considered high-confidence
        content = b"This is a plain text document with enough content to pass the threshold. " * 3
        text, low_conf = ingestion.extract_text(content, "notes.txt")
        self.assertIn("plain text", text)
        self.assertFalse(low_conf)

    def test_short_text_is_low_confidence(self):
        content = b"Short"
        text, low_conf = ingestion.extract_text(content, "short.txt")
        self.assertTrue(low_conf)


# --------------------------------------------------------------------------- #
# Ingest document (ChromaDB mocked)
# --------------------------------------------------------------------------- #

class TestIngestDocument(unittest.TestCase):
    def setUp(self):
        _reset_db()

    @patch("core.ingestion.chroma")
    def test_ingest_creates_source_and_chunks(self, mock_chroma):
        mock_chroma.delete_by_ids = MagicMock()
        mock_chroma.add_chunks = MagicMock()

        content = b"This is the TOSP fee table. Procedure code 123 costs $500. " * 10
        source_id, num_chunks, low_conf = ingestion.ingest_document("tosp", "fees.txt", content)

        self.assertIsNotNone(source_id)
        self.assertGreater(num_chunks, 0)

        # SQLite should have the source
        active = db.get_active_source("tosp", "fees.txt")
        self.assertIsNotNone(active)
        self.assertEqual(active["category"], "tosp")

    @patch("core.ingestion.chroma")
    def test_ingest_archives_old_version(self, mock_chroma):
        mock_chroma.delete_by_ids = MagicMock()
        mock_chroma.add_chunks = MagicMock()

        content = b"Version 1 content for testing purposes only. " * 10
        source_id_v1, _, _ = ingestion.ingest_document("sop", "protocol.txt", content)

        content2 = b"Version 2 updated content for testing purposes. " * 10
        source_id_v2, _, _ = ingestion.ingest_document("sop", "protocol.txt", content2)

        self.assertNotEqual(source_id_v1, source_id_v2)
        # Old source archived
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT status FROM knowledge_sources WHERE id = ?", (source_id_v1,)
            ).fetchone()
        self.assertEqual(row["status"], "archived")
        # New source active
        active = db.get_active_source("sop", "protocol.txt")
        self.assertEqual(active["id"], source_id_v2)

    @patch("core.ingestion.chroma")
    def test_chroma_failure_rolls_back_sqlite(self, mock_chroma):
        mock_chroma.delete_by_ids = MagicMock()
        mock_chroma.add_chunks.side_effect = RuntimeError("Chroma unavailable")

        content = b"Some content that will trigger a chroma failure. " * 10
        with self.assertRaises(RuntimeError):
            ingestion.ingest_document("general", "fail.txt", content)

        # Source should not be active after rollback
        active = db.get_active_source("general", "fail.txt")
        self.assertIsNone(active)

    def test_unknown_category_raises(self):
        with self.assertRaises(ValueError):
            ingestion.ingest_document("invalid_cat", "file.txt", b"content")


# --------------------------------------------------------------------------- #
# Manual answer embed (ChromaDB mocked)
# --------------------------------------------------------------------------- #

class TestEmbedManualAnswer(unittest.TestCase):
    def setUp(self):
        _reset_db()

    @patch("core.ingestion.chroma")
    def test_embed_returns_chroma_id(self, mock_chroma):
        mock_chroma.add_chunks = MagicMock()

        answer_id = db.insert_manual_answer(
            trigger_query="What is RSI?",
            answer_text="Rapid Sequence Induction is a method of...",
            added_by=ADMIN_ID,
            category="glossary",
        )
        chroma_id = ingestion.embed_manual_answer(
            answer_id, "What is RSI?", "Rapid Sequence Induction is a method of...", "glossary"
        )
        self.assertIsNotNone(chroma_id)
        self.assertGreater(len(chroma_id), 10)

    @patch("core.ingestion.chroma")
    def test_embed_failure_propagates(self, mock_chroma):
        mock_chroma.add_chunks.side_effect = RuntimeError("Embed failed")

        answer_id = db.insert_manual_answer(
            trigger_query="ACLS?", answer_text="Advanced Cardiac...",
            added_by=ADMIN_ID,
        )
        with self.assertRaises(RuntimeError):
            ingestion.embed_manual_answer(answer_id, "ACLS?", "Advanced Cardiac...", None)


# --------------------------------------------------------------------------- #
# Search (ChromaDB mocked)
# --------------------------------------------------------------------------- #

class TestSearch(unittest.TestCase):
    def setUp(self):
        _reset_db()

    @patch("core.search.chroma")
    def test_result_above_threshold_returned(self, mock_chroma):
        mock_chroma.count.return_value = 5
        mock_chroma.query.return_value = {
            "ids": [["abc123"]],
            "documents": [["RSI stands for Rapid Sequence Induction."]],
            "metadatas": [[{"source_type": "manual_answer", "category": "glossary",
                            "source_id": 1, "filename": "(admin-provided)"}]],
            "distances": [[0.05]],  # well below cutoff 0.18
        }
        results = search.search("What is RSI?")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_type"], "manual_answer")

    @patch("core.search.chroma")
    def test_result_below_threshold_filtered(self, mock_chroma):
        mock_chroma.count.return_value = 5
        mock_chroma.query.return_value = {
            "ids": [["abc123"]],
            "documents": [["unrelated content"]],
            "metadatas": [[{"source_type": "document", "category": "sop",
                            "source_id": 1, "filename": "sop.pdf"}]],
            "distances": [[0.95]],  # way above cutoff
        }
        results = search.search("some query")
        self.assertEqual(len(results), 0)

    @patch("core.search.chroma")
    def test_empty_collection_returns_empty(self, mock_chroma):
        mock_chroma.count.return_value = 0
        results = search.search("anything")
        self.assertEqual(results, [])

    def test_format_answer_manual_citation(self):
        result = {
            "text": "RSI is Rapid Sequence Induction.",
            "source_type": "manual_answer",
            "category": "glossary",
            "filename": "(admin-provided)",
        }
        formatted = search.format_answer(result)
        self.assertIn("RSI is Rapid Sequence Induction.", formatted)
        self.assertIn("admin-provided", formatted)
        self.assertNotIn("(admin-provided)", formatted.split("\n\n")[0])

    def test_format_answer_document_citation(self):
        result = {
            "text": "Procedure 123 costs $500.",
            "source_type": "document",
            "category": "tosp",
            "filename": "tosp_2024.pdf",
        }
        formatted = search.format_answer(result)
        self.assertIn("tosp_2024.pdf", formatted)
        self.assertIn("TOSP", formatted)


# --------------------------------------------------------------------------- #
# Queue deduplication
# --------------------------------------------------------------------------- #

class TestQueueDeduplication(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_no_duplicate_if_queue_empty(self):
        result = search.find_pending_duplicate("What is RSI?", "ask")
        self.assertIsNone(result)

    def test_finds_exact_duplicate(self):
        db.add_user(99001, "Dr One", "users/99001")
        db.insert_queue_entry(99001, "What is RSI?", "ask")
        result = search.find_pending_duplicate("What is RSI?", "ask")
        self.assertIsNotNone(result)

    def test_case_insensitive_match(self):
        db.add_user(99002, "Dr Two", "users/99002")
        db.insert_queue_entry(99002, "what is RSI?", "ask")
        result = search.find_pending_duplicate("WHAT IS RSI?", "ask")
        self.assertIsNotNone(result)

    def test_different_command_is_not_duplicate(self):
        db.add_user(99003, "Dr Three", "users/99003")
        db.insert_queue_entry(99003, "amoxicillin dose", "ask")
        result = search.find_pending_duplicate("amoxicillin dose", "antibiotics")
        self.assertIsNone(result)

    def test_add_also_asked_by(self):
        db.add_user(88001, "Dr A", "users/88001")
        db.add_user(88002, "Dr B", "users/88002")
        qid = db.insert_queue_entry(88001, "What is ACLS?", "ask")
        db.add_also_asked_by(qid, 88002)
        entry = db.get_queue_entry(qid)
        also = json.loads(entry["also_asked_by"])
        self.assertIn(88002, also)

    def test_resolve_marks_entry(self):
        db.add_user(77001, "Dr C", "users/77001")
        qid = db.insert_queue_entry(77001, "Protocol for RSI?", "ask")
        db.resolve_queue_entry(qid)
        entry = db.get_queue_entry(qid)
        self.assertEqual(entry["status"], "resolved")

    def test_dismiss_marks_entry(self):
        db.add_user(77002, "Dr D", "users/77002")
        qid = db.insert_queue_entry(77002, "Something vague?", "ask")
        db.dismiss_queue_entry(qid)
        entry = db.get_queue_entry(qid)
        self.assertEqual(entry["status"], "dismissed")


# --------------------------------------------------------------------------- #
# Reindex
# --------------------------------------------------------------------------- #

class TestReindex(unittest.TestCase):
    def setUp(self):
        _reset_db()

    @patch("core.ingestion.chroma")
    def test_reindex_fixes_null_chroma_id(self, mock_chroma):
        mock_chroma.add_chunks = MagicMock()

        answer_id = db.insert_manual_answer(
            trigger_query="What is BCLS?",
            answer_text="Basic Cardiac Life Support.",
            added_by=ADMIN_ID,
            chroma_id=None,
        )
        rows = db.get_unembedded_manual_answers()
        self.assertEqual(len(rows), 1)

        fixed, failed = ingestion.reindex_unembedded(ADMIN_ID)
        self.assertEqual(fixed, 1)
        self.assertEqual(failed, 0)

        rows_after = db.get_unembedded_manual_answers()
        self.assertEqual(len(rows_after), 0)

    @patch("core.ingestion.chroma")
    def test_reindex_reports_failure(self, mock_chroma):
        mock_chroma.add_chunks.side_effect = RuntimeError("Still broken")

        db.insert_manual_answer(
            trigger_query="What is ACLS?",
            answer_text="Advanced Cardiac Life Support.",
            added_by=ADMIN_ID,
            chroma_id=None,
        )
        fixed, failed = ingestion.reindex_unembedded(ADMIN_ID)
        self.assertEqual(fixed, 0)
        self.assertEqual(failed, 1)


if __name__ == "__main__":
    unittest.main()
