"""
SQLite persistence layer.

Schema: 9 tables.
  users               - whitelisted team members
  audit_log           - every meaningful action (compliance + analytics)
  tracked_documents   - per-user expiry-tracked files (passport, insurance, etc.)
  knowledge_sources   - ingested document metadata
  knowledge_chunks    - per-chunk pointers into ChromaDB
  unanswered_queue    - queries that couldn't be answered, pending admin resolution
  manual_answers      - admin-provided answers, embedded into ChromaDB
  module_flags        - per-module enable/disable state
  scheduler_log       - record of every scheduled job run (for observability)
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from core.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    local_folder_path TEXT NOT NULL,  -- relative to STORAGE_ROOT, e.g. users/604492237
    is_admin BOOLEAN DEFAULT 0,
    status TEXT DEFAULT 'active',     -- active | archived
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    archived_at TIMESTAMP,
    birthday TEXT                     -- DD-MM format, nullable
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT,
    success BOOLEAN,
    module_name TEXT,
    outcome TEXT,        -- answered | escalated | dismissed | not_found
    response_ms INTEGER,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tracked_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    doc_type TEXT NOT NULL,           -- MEDICAL_REGISTRATION | INDEMNITY_INSURANCE | BCLS | ACLS | WORK_PASS | PASSPORT | HOSPITAL_CREDENTIALING
    local_file_path TEXT NOT NULL,    -- relative to STORAGE_ROOT
    extracted_expiry_date DATE,
    extraction_context TEXT,          -- surrounding sentence shown to user for verification
    extraction_confidence TEXT,       -- high | low
    last_reconfirmed_at TIMESTAMP,
    status TEXT DEFAULT 'active',     -- active | archived
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uidx_active_doc
    ON tracked_documents(telegram_id, doc_type)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS knowledge_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,           -- sop | tosp | antibiotics | consumables | phonebook | glossary
    filename TEXT NOT NULL,
    local_file_path TEXT NOT NULL,    -- relative to STORAGE_ROOT
    version_label TEXT,
    status TEXT DEFAULT 'active',     -- active | archived
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES knowledge_sources(id),
    chroma_id TEXT NOT NULL,          -- pointer into ChromaDB
    chunk_text TEXT NOT NULL,
    page_reference TEXT,
    status TEXT DEFAULT 'active'      -- active | archived
);

CREATE TABLE IF NOT EXISTS unanswered_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    query_text TEXT NOT NULL,
    source_command TEXT,              -- ask | tosp | antibiotics
    status TEXT DEFAULT 'pending',    -- pending | resolved | dismissed
    also_asked_by TEXT DEFAULT '[]',  -- JSON list of additional telegram_ids who asked same thing
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS manual_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER REFERENCES unanswered_queue(id),
    category TEXT,
    trigger_query TEXT NOT NULL,
    answer_text TEXT NOT NULL,
    chroma_id TEXT,                   -- NULL means embedding failed; use /reindex to fix
    added_by INTEGER NOT NULL,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS module_flags (
    module_name TEXT PRIMARY KEY,
    enabled_for TEXT NOT NULL DEFAULT 'admin_only',  -- disabled | admin_only | all_users
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scheduler_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    scheduled_for TIMESTAMP,
    ran_at TIMESTAMP NOT NULL,
    success BOOLEAN NOT NULL,
    error_detail TEXT
);
"""

_MODULE_DEFAULTS = [
    ("onboarding",     "all_users"),
    ("knowledge_bank", "all_users"),
    ("expiry_tracker", "all_users"),
    ("education",      "admin_only"),
    ("form_filler",    "admin_only"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def seed_defaults():
    """Idempotent: insert module_flags defaults if rows don't exist yet."""
    with get_conn() as conn:
        for module_name, enabled_for in _MODULE_DEFAULTS:
            conn.execute(
                "INSERT OR IGNORE INTO module_flags (module_name, enabled_for) VALUES (?, ?)",
                (module_name, enabled_for),
            )


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #

def add_user(telegram_id: int, display_name: str, local_folder_path: str,
             is_admin: bool = False, birthday: str = None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO users (telegram_id, display_name, local_folder_path, is_admin, birthday)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 display_name = excluded.display_name,
                 local_folder_path = excluded.local_folder_path,
                 status = 'active',
                 archived_at = NULL""",
            (telegram_id, display_name, local_folder_path, int(is_admin), birthday),
        )


def archive_user(telegram_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET status = 'archived', archived_at = ? WHERE telegram_id = ?",
            (_now(), telegram_id),
        )


def get_user(telegram_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return dict(row) if row else None


def list_active_users() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE status = 'active' ORDER BY display_name"
        ).fetchall()
        return [dict(r) for r in rows]


def is_whitelisted(telegram_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE telegram_id = ? AND status = 'active'", (telegram_id,)
        ).fetchone()
        return row is not None


def is_admin(telegram_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT is_admin FROM users WHERE telegram_id = ? AND status = 'active'", (telegram_id,)
        ).fetchone()
        return bool(row and row["is_admin"])


# --------------------------------------------------------------------------- #
# audit_log
# --------------------------------------------------------------------------- #

def log_action(telegram_id: int | None, action: str, detail: str = None,
               success: bool = True, module_name: str = None,
               outcome: str = None, response_ms: int = None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO audit_log
               (telegram_id, action, detail, success, module_name, outcome, response_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (telegram_id, action, detail, int(success), module_name, outcome, response_ms),
        )


# --------------------------------------------------------------------------- #
# module_flags
# --------------------------------------------------------------------------- #

def get_module_state(module_name: str) -> str:
    """Returns 'disabled', 'admin_only', or 'all_users'. Defaults to 'disabled' if unknown."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT enabled_for FROM module_flags WHERE module_name = ?", (module_name,)
        ).fetchone()
        return row["enabled_for"] if row else "disabled"


def set_module_state(module_name: str, state: str):
    assert state in ("disabled", "admin_only", "all_users"), f"Invalid state: {state}"
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO module_flags (module_name, enabled_for, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(module_name) DO UPDATE SET
                 enabled_for = excluded.enabled_for,
                 updated_at = excluded.updated_at""",
            (module_name, state, _now()),
        )


def list_module_states() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM module_flags ORDER BY module_name").fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# tracked_documents
# --------------------------------------------------------------------------- #

def get_active_document(telegram_id: int, doc_type: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tracked_documents WHERE telegram_id = ? AND doc_type = ? AND status = 'active'",
            (telegram_id, doc_type),
        ).fetchone()
        return dict(row) if row else None


def archive_document(doc_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracked_documents SET status = 'archived' WHERE id = ?", (doc_id,)
        )


def insert_document(telegram_id: int, doc_type: str, local_file_path: str,
                    extracted_expiry_date: str = None, extraction_context: str = None,
                    extraction_confidence: str = "low") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO tracked_documents
               (telegram_id, doc_type, local_file_path, extracted_expiry_date,
                extraction_context, extraction_confidence)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (telegram_id, doc_type, local_file_path, extracted_expiry_date,
             extraction_context, extraction_confidence),
        )
        return cur.lastrowid


def update_document_expiry(doc_id: int, expiry_date: str, reconfirmed: bool = False):
    with get_conn() as conn:
        reconfirmed_at = _now() if reconfirmed else None
        conn.execute(
            """UPDATE tracked_documents
               SET extracted_expiry_date = ?,
                   last_reconfirmed_at = COALESCE(?, last_reconfirmed_at),
                   extraction_confidence = 'high'
               WHERE id = ?""",
            (expiry_date, reconfirmed_at, doc_id),
        )


def list_user_documents(telegram_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tracked_documents WHERE telegram_id = ? AND status = 'active' ORDER BY doc_type",
            (telegram_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_documents_expiring_within(days: int) -> list[dict]:
    """Documents expiring within `days` days from today (SGT date)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT d.*, u.display_name, u.telegram_id as user_telegram_id
               FROM tracked_documents d
               JOIN users u ON u.telegram_id = d.telegram_id
               WHERE d.status = 'active'
                 AND d.extracted_expiry_date IS NOT NULL
                 AND date(d.extracted_expiry_date) BETWEEN date('now') AND date('now', ? || ' days')
               ORDER BY d.extracted_expiry_date ASC""",
            (str(days),),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_active_documents_summary() -> list[dict]:
    """For admin monthly summary — all users, including those with no docs."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT u.telegram_id, u.display_name,
                      COUNT(d.id) as doc_count,
                      MIN(d.extracted_expiry_date) as soonest_expiry
               FROM users u
               LEFT JOIN tracked_documents d
                 ON d.telegram_id = u.telegram_id AND d.status = 'active'
               WHERE u.status = 'active'
               GROUP BY u.telegram_id
               ORDER BY u.display_name""",
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# knowledge_sources + knowledge_chunks
# --------------------------------------------------------------------------- #

def insert_knowledge_source(category: str, filename: str, local_file_path: str,
                            version_label: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO knowledge_sources (category, filename, local_file_path, version_label)
               VALUES (?, ?, ?, ?)""",
            (category, filename, local_file_path, version_label),
        )
        return cur.lastrowid


def get_active_source(category: str, filename: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM knowledge_sources WHERE category = ? AND filename = ? AND status = 'active'",
            (category, filename),
        ).fetchone()
        return dict(row) if row else None


def archive_source_and_chunks(source_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE knowledge_sources SET status = 'archived' WHERE id = ?", (source_id,)
        )
        conn.execute(
            "UPDATE knowledge_chunks SET status = 'archived' WHERE source_id = ?", (source_id,)
        )


def insert_chunk(source_id: int, chroma_id: str, chunk_text: str,
                 page_reference: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO knowledge_chunks (source_id, chroma_id, chunk_text, page_reference)
               VALUES (?, ?, ?, ?)""",
            (source_id, chroma_id, chunk_text, page_reference),
        )
        return cur.lastrowid


def get_active_chroma_ids() -> set[str]:
    """All chroma_ids for active chunks — used for health-check sync verification."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT chroma_id FROM knowledge_chunks WHERE status = 'active'"
        ).fetchall()
        return {r["chroma_id"] for r in rows}


# --------------------------------------------------------------------------- #
# unanswered_queue
# --------------------------------------------------------------------------- #

def insert_queue_entry(telegram_id: int, query_text: str, source_command: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO unanswered_queue (telegram_id, query_text, source_command)
               VALUES (?, ?, ?)""",
            (telegram_id, query_text, source_command),
        )
        return cur.lastrowid


def get_queue_entry(queue_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM unanswered_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        return dict(row) if row else None


def add_also_asked_by(queue_id: int, telegram_id: int):
    entry = get_queue_entry(queue_id)
    if not entry:
        return
    also = json.loads(entry["also_asked_by"] or "[]")
    if telegram_id not in also:
        also.append(telegram_id)
    with get_conn() as conn:
        conn.execute(
            "UPDATE unanswered_queue SET also_asked_by = ? WHERE id = ?",
            (json.dumps(also), queue_id),
        )


def resolve_queue_entry(queue_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE unanswered_queue SET status = 'resolved', resolved_at = ? WHERE id = ?",
            (_now(), queue_id),
        )


def dismiss_queue_entry(queue_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE unanswered_queue SET status = 'dismissed', resolved_at = ? WHERE id = ?",
            (_now(), queue_id),
        )


def list_pending_queue() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM unanswered_queue WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_stale_pending_queue(older_than_hours: int = 24) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM unanswered_queue WHERE status = 'pending'
               AND created_at <= datetime('now', ? || ' hours')
               ORDER BY created_at ASC""",
            (f"-{older_than_hours}",),
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# manual_answers
# --------------------------------------------------------------------------- #

def insert_manual_answer(trigger_query: str, answer_text: str, added_by: int,
                         category: str = None, queue_id: int = None,
                         chroma_id: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO manual_answers
               (queue_id, category, trigger_query, answer_text, chroma_id, added_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (queue_id, category, trigger_query, answer_text, chroma_id, added_by),
        )
        return cur.lastrowid


def update_manual_answer_chroma_id(answer_id: int, chroma_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE manual_answers SET chroma_id = ? WHERE id = ?", (chroma_id, answer_id)
        )


def get_unembedded_manual_answers() -> list[dict]:
    """For /reindex — rows with no chroma_id."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM manual_answers WHERE chroma_id IS NULL AND status = 'active'"
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# scheduler_log
# --------------------------------------------------------------------------- #

def log_scheduler_start(job_name: str, scheduled_for: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO scheduler_log (job_name, scheduled_for, ran_at, success)
               VALUES (?, ?, ?, 0)""",
            (job_name, scheduled_for, _now()),
        )
        return cur.lastrowid


def log_scheduler_done(log_id: int, success: bool, error_detail: str = None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE scheduler_log SET success = ?, error_detail = ? WHERE id = ?",
            (int(success), error_detail, log_id),
        )


def get_last_scheduler_run(job_name: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM scheduler_log WHERE job_name = ? AND success = 1
               ORDER BY ran_at DESC LIMIT 1""",
            (job_name,),
        ).fetchone()
        return dict(row) if row else None


def list_recent_scheduler_runs(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduler_log ORDER BY ran_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# users with birthdays today (for scheduler)
# --------------------------------------------------------------------------- #

def get_users_with_birthday_today(today_ddmm: str) -> list[dict]:
    """today_ddmm format: 'DD-MM'"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE status = 'active' AND birthday = ?",
            (today_ddmm,),
        ).fetchall()
        return [dict(r) for r in rows]
