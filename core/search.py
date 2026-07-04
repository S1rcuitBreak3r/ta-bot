"""
Knowledge bank search — queries ChromaDB and formats results with citation.

SIMILARITY_THRESHOLD (config) is in cosine-similarity space (0–1).
ChromaDB returns cosine distance = 1 − cosine_similarity, so we convert:
  distance_cutoff = 1.0 − SIMILARITY_THRESHOLD
Results whose distance exceeds that cutoff are discarded (below threshold).
"""
from __future__ import annotations

import logging

import core.chroma_client as chroma
import core.db as db
from core.config import SIMILARITY_THRESHOLD

logger = logging.getLogger(__name__)

_DISTANCE_CUTOFF = 1.0 - SIMILARITY_THRESHOLD

CATEGORY_LABELS = {
    "sop": "SOP",
    "tosp": "TOSP",
    "antibiotics": "Antibiotics",
    "glossary": "Glossary",
    "general": "General",
}


def search(
    query_text: str, category: str | None = None, n_results: int = 3
) -> list[dict]:
    """
    Search the knowledge base.
    Returns a list of result dicts, most relevant first, filtered to threshold.
    Each dict: text, distance, source_type, category, filename, source_id.
    Empty list means nothing was found above the threshold.
    """
    if chroma.count() == 0:
        return []
    try:
        raw = chroma.query(query_text, n_results=n_results, category=category)
    except Exception as exc:
        logger.error("ChromaDB query failed: %s", exc)
        return []

    ids_list = raw.get("ids", [[]])[0]
    docs = raw.get("documents", [[]])[0]
    metas = raw.get("metadatas", [[]])[0]
    dists = raw.get("distances", [[]])[0]

    results = []
    for chroma_id, doc, meta, dist in zip(ids_list, docs, metas, dists):
        logger.info("search hit: dist=%.3f cutoff=%.3f file=%s", dist, _DISTANCE_CUTOFF, meta.get("filename", "?"))
        if dist > _DISTANCE_CUTOFF:
            continue
        results.append(
            {
                "chroma_id": chroma_id,
                "text": doc,
                "distance": dist,
                "source_type": meta.get("source_type", "document"),
                "category": meta.get("category", ""),
                "filename": meta.get("filename", ""),
                "source_id": meta.get("source_id", ""),
            }
        )
    return results


def format_answer(result: dict) -> str:
    """Format a search result as a Telegram-ready reply."""
    text = result["text"]
    cat_label = CATEGORY_LABELS.get(result["category"], result["category"])

    if result["source_type"] == "manual_answer":
        citation = f"_{cat_label} — admin-provided_"
    else:
        citation = f"_{cat_label} — {result['filename']}_"

    return f"{text}\n\n{citation}"


def find_pending_duplicate(query_text: str, source_command: str) -> dict | None:
    """
    Return an existing pending queue entry for the exact same query+command, or None.
    Comparison is case-insensitive.
    """
    for entry in db.list_pending_queue():
        if (
            entry["source_command"] == source_command
            and entry["query_text"].strip().lower() == query_text.strip().lower()
        ):
            return entry
    return None
