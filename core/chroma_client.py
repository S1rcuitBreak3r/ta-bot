"""
ChromaDB wrapper — single collection for all knowledge content.

Collection:  ta_knowledge
Space:       cosine  (distance = 1 − cosine_similarity; 0 = identical)
Per-chunk metadata:
  source_type  "document" | "manual_answer"
  category     "sop" | "tosp" | "antibiotics" | "glossary" | "general"
  source_id    int  (knowledge_sources.id or manual_answers.id)
  filename     str  (for citation)

The client and collection are module-level singletons, reset between
test runs via reset_client().
"""
from __future__ import annotations

import os

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

_client: chromadb.PersistentClient | None = None
_collection: chromadb.Collection | None = None

_COLLECTION_NAME = "ta_knowledge"


def _get_client() -> chromadb.PersistentClient:
    global _client
    if _client is None:
        path = os.environ.get("CHROMA_PATH", "/data/chroma")
        _client = chromadb.PersistentClient(path=path)
    return _client


def get_collection() -> chromadb.Collection:
    global _collection
    if _collection is None:
        _collection = _get_client().get_or_create_collection(
            name=_COLLECTION_NAME,
            embedding_function=DefaultEmbeddingFunction(),
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def add_chunks(chroma_ids: list[str], texts: list[str], metadatas: list[dict]):
    get_collection().add(ids=chroma_ids, documents=texts, metadatas=metadatas)


def query(query_text: str, n_results: int = 3, category: str | None = None) -> dict:
    """
    Query the collection. Returns chromadb results dict:
    {ids, documents, metadatas, distances} — each a list-of-lists.
    """
    kwargs: dict = dict(
        query_texts=[query_text],
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )
    if category:
        kwargs["where"] = {"category": category}
    return get_collection().query(**kwargs)


def delete_by_ids(chroma_ids: list[str]):
    if chroma_ids:
        get_collection().delete(ids=chroma_ids)


def count() -> int:
    return get_collection().count()


def reset_client():
    """Reset singletons — use in tests to isolate ChromaDB state."""
    global _client, _collection
    _client = None
    _collection = None
