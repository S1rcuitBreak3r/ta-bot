"""
Document ingestion pipeline.

Text extraction  pdfplumber (native text); pytesseract OCR fallback for scanned PDFs.
Chunking         fixed-size with overlap (CHUNK_SIZE / CHUNK_OVERLAP from config).
Consistency      if ChromaDB write fails → SQLite rows are archived (rolled back).
                 if SQLite write fails → ChromaDB chunks are deleted.
"""
from __future__ import annotations

import io
import logging
import uuid

import pdfplumber

import core.chroma_client as chroma
import core.db as db
import core.storage as storage
from core.config import CHUNK_OVERLAP, CHUNK_SIZE

logger = logging.getLogger(__name__)

CATEGORIES = ("sop", "tosp", "antibiotics", "glossary", "general")


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #

def extract_text(file_bytes: bytes, filename: str) -> tuple[str, bool]:
    """
    Extract text. Returns (text, low_confidence).
    low_confidence=True when OCR was needed or very little text was found.
    """
    text = ""
    low_confidence = False

    if filename.lower().endswith(".pdf"):
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages).strip()
        except Exception as exc:
            logger.warning("pdfplumber failed on %s: %s", filename, exc)

        if len(text) < 100:
            low_confidence = True
            try:
                import pytesseract  # noqa: PLC0415
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    if pdf.pages:
                        img = pdf.pages[0].to_image(resolution=200).original
                        text = pytesseract.image_to_string(img).strip()
                logger.info("OCR used for %s", filename)
            except Exception as exc:
                logger.warning("OCR fallback failed for %s: %s", filename, exc)
    else:
        try:
            text = file_bytes.decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        low_confidence = len(text) < 100

    return text, low_confidence


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

def chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks of CHUNK_SIZE characters."""
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        piece = text[start : start + CHUNK_SIZE].strip()
        if piece:
            chunks.append(piece)
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


# --------------------------------------------------------------------------- #
# Document ingest
# --------------------------------------------------------------------------- #

def ingest_document(
    category: str, filename: str, file_bytes: bytes
) -> tuple[int, int, bool]:
    """
    Ingest a document. Returns (source_id, num_chunks, low_confidence).
    Archives any existing active source with the same category + filename first.
    Rolls back on partial failure.
    """
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category: {category!r}")

    # Archive previous version if it exists
    existing = db.get_active_source(category, filename)
    if existing:
        old_ids = _source_chroma_ids(existing["id"])
        db.archive_source_and_chunks(existing["id"])
        try:
            chroma.delete_by_ids(old_ids)
        except Exception as exc:
            logger.error("Failed to delete old ChromaDB chunks (source %s): %s", existing["id"], exc)

    rel_path = storage.save_knowledge_file(category, filename, file_bytes)
    text, low_confidence = extract_text(file_bytes, filename)
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError(f"No extractable text in {filename!r}")

    source_id = db.insert_knowledge_source(category, filename, rel_path)

    chroma_ids = [str(uuid.uuid4()) for _ in chunks]
    metadatas = [
        {"source_type": "document", "category": category,
         "source_id": source_id, "filename": filename}
        for _ in chunks
    ]

    # Write to ChromaDB — rollback SQLite on failure
    try:
        chroma.add_chunks(chroma_ids, chunks, metadatas)
    except Exception as exc:
        db.archive_source_and_chunks(source_id)
        raise RuntimeError(f"ChromaDB ingest failed for {filename!r}: {exc}") from exc

    # Write chunks to SQLite — rollback ChromaDB on failure
    try:
        for cid, chunk in zip(chroma_ids, chunks):
            db.insert_chunk(source_id, cid, chunk)
    except Exception as exc:
        chroma.delete_by_ids(chroma_ids)
        db.archive_source_and_chunks(source_id)
        raise RuntimeError(f"SQLite chunk insert failed: {exc}") from exc

    return source_id, len(chunks), low_confidence


# --------------------------------------------------------------------------- #
# Manual answer embedding
# --------------------------------------------------------------------------- #

def embed_manual_answer(
    answer_id: int, trigger_query: str, answer_text: str,
    category: str | None,
) -> str:
    """
    Embed a manual answer into ChromaDB. Returns the chroma_id.
    Raises RuntimeError if ChromaDB write fails.
    """
    chroma_id = str(uuid.uuid4())
    text = f"{trigger_query}\n{answer_text}"
    metadata = {
        "source_type": "manual_answer",
        "category": category or "general",
        "source_id": answer_id,
        "filename": "(admin-provided)",
    }
    chroma.add_chunks([chroma_id], [text], [metadata])
    return chroma_id


def reindex_unembedded(admin_id: int) -> tuple[int, int]:
    """
    Re-embed all manual_answers rows where chroma_id IS NULL.
    Returns (fixed, failed).
    """
    rows = db.get_unembedded_manual_answers()
    fixed = failed = 0
    for row in rows:
        try:
            cid = embed_manual_answer(
                row["id"], row["trigger_query"], row["answer_text"], row.get("category")
            )
            db.update_manual_answer_chroma_id(row["id"], cid)
            fixed += 1
        except Exception as exc:
            logger.error("Reindex failed for manual_answer %s: %s", row["id"], exc)
            failed += 1
    return fixed, failed


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _source_chroma_ids(source_id: int) -> list[str]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT chroma_id FROM knowledge_chunks WHERE source_id = ?", (source_id,)
        ).fetchall()
    return [r["chroma_id"] for r in rows]
