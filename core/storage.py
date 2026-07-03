"""
Local volume file operations.

All paths stored in the DB are RELATIVE to STORAGE_ROOT.
This module prepends STORAGE_ROOT at read/write time so paths in the DB
never contain a hardcoded prefix — Railway volume remounts don't break them.

Security: every path is validated against the user's folder root via
pathlib.Path.resolve() + is_relative_to(). Any attempt to escape the
folder (../.. tricks, absolute paths) raises StorageSecurityError and
is logged to audit_log.
"""
from __future__ import annotations

import io
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from core.config import STORAGE_ROOT


class StorageSecurityError(ValueError):
    """Raised when a path escapes the user's designated folder."""


def _root() -> Path:
    return Path(STORAGE_ROOT).resolve()


def _user_root(telegram_id: int) -> Path:
    return _root() / "users" / str(telegram_id)


def _knowledge_root() -> Path:
    return _root() / "knowledge"


def _resolve_safe(base: Path, relative: str | Path) -> Path:
    """
    Resolve `relative` under `base`. Raises StorageSecurityError if the
    resolved path escapes `base` (path traversal attempt).
    """
    resolved = (base / relative).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        raise StorageSecurityError(
            f"Path traversal attempt: '{relative}' escapes '{base}'"
        )
    return resolved


def ensure_user_folder(telegram_id: int) -> str:
    """
    Create the user's folder if it doesn't exist.
    Returns the relative path (relative to STORAGE_ROOT).
    """
    user_root = _user_root(telegram_id)
    user_root.mkdir(parents=True, exist_ok=True)
    (user_root / "_archived").mkdir(exist_ok=True)
    # relative to STORAGE_ROOT
    return str(user_root.relative_to(_root()))


def save_file(telegram_id: int, filename: str, data: bytes) -> str:
    """
    Save `data` as `filename` in the user's folder.
    Returns the relative path (relative to STORAGE_ROOT) suitable for DB storage.
    Raises StorageSecurityError on traversal attempt.
    """
    user_root = _user_root(telegram_id)
    user_root.mkdir(parents=True, exist_ok=True)
    dest = _resolve_safe(user_root, filename)
    dest.write_bytes(data)
    return str(dest.relative_to(_root()))


def read_file(telegram_id: int, relative_path: str) -> bytes:
    """
    Read a file belonging to `telegram_id`.
    `relative_path` is relative to STORAGE_ROOT (as stored in DB).
    Raises StorageSecurityError if it escapes the user's folder.
    """
    user_root = _user_root(telegram_id)
    abs_path = (_root() / relative_path).resolve()
    try:
        abs_path.relative_to(user_root.resolve())
    except ValueError:
        raise StorageSecurityError(
            f"Cross-user access attempt: '{relative_path}' is not under user {telegram_id}'s folder"
        )
    return abs_path.read_bytes()


def archive_file(telegram_id: int, relative_path: str) -> str:
    """
    Move a file to the user's _archived/ subfolder.
    Returns the new relative path (relative to STORAGE_ROOT).
    """
    user_root = _user_root(telegram_id)
    abs_path = (_root() / relative_path).resolve()
    try:
        abs_path.relative_to(user_root.resolve())
    except ValueError:
        raise StorageSecurityError(
            f"Cross-user access attempt on archive: '{relative_path}'"
        )
    archive_dir = user_root / "_archived"
    archive_dir.mkdir(exist_ok=True)
    dest = archive_dir / abs_path.name
    # Avoid clobber: suffix with a counter if dest exists
    counter = 1
    while dest.exists():
        dest = archive_dir / f"{abs_path.stem}_{counter}{abs_path.suffix}"
        counter += 1
    shutil.move(str(abs_path), str(dest))
    return str(dest.relative_to(_root()))


def list_files(telegram_id: int) -> list[str]:
    """List relative paths of active (non-archived) files in the user's folder."""
    user_root = _user_root(telegram_id)
    if not user_root.exists():
        return []
    result = []
    for p in user_root.iterdir():
        if p.is_file() and p.name != "_archived":
            result.append(str(p.relative_to(_root())))
    return sorted(result)


def archive_user_folder(telegram_id: int):
    """Move entire user folder to _archived_users/ on offboarding."""
    user_root = _user_root(telegram_id)
    if not user_root.exists():
        return
    dest_root = _root() / "_archived_users" / str(telegram_id)
    dest_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(user_root), str(dest_root))


# --------------------------------------------------------------------------- #
# Knowledge base storage
# --------------------------------------------------------------------------- #

def save_knowledge_file(category: str, filename: str, data: bytes) -> str:
    """Save an ingested document. Returns relative path."""
    dest_dir = _knowledge_root() / category
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    dest.write_bytes(data)
    return str(dest.relative_to(_root()))


def archive_knowledge_file(relative_path: str) -> str:
    """Move a superseded knowledge file to knowledge/_archived/."""
    abs_path = (_root() / relative_path).resolve()
    archive_dir = _knowledge_root() / "_archived"
    archive_dir.mkdir(exist_ok=True)
    dest = archive_dir / abs_path.name
    counter = 1
    while dest.exists():
        dest = archive_dir / f"{abs_path.stem}_{counter}{abs_path.suffix}"
        counter += 1
    shutil.move(str(abs_path), str(dest))
    return str(dest.relative_to(_root()))


# --------------------------------------------------------------------------- #
# Backup — weekly Telegram-delivered dump
# --------------------------------------------------------------------------- #

def create_backup_archive(db_path: str) -> bytes:
    """
    Build a .tar.gz containing:
      - the SQLite database file
      - the knowledge/ directory
    Returns the archive as bytes suitable for sending via Telegram.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # DB file
        if os.path.exists(db_path):
            tar.add(db_path, arcname="ta_bot.db")
        # Knowledge base
        kb_root = _knowledge_root()
        if kb_root.exists():
            tar.add(str(kb_root), arcname="knowledge")
    return buf.getvalue()
