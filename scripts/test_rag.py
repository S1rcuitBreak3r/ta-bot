"""
Local RAG test script — no Telegram needed.

Usage:
    .venv/bin/python scripts/test_rag.py <pdf_file> "query 1" "query 2" ...

Example:
    .venv/bin/python scripts/test_rag.py docs/glossary.pdf "what is RSI" "TOSP code for appendix"

The script:
  1. Creates a fresh temp DB + ChromaDB
  2. Ingests the PDF into the 'glossary' category (auto-detected from filename,
     or use --category sop/tosp/antibiotics/glossary/general)
  3. Runs each query and prints raw scores + matched text
  4. Cleans up temp files when done
"""
import argparse
import os
import sys
import tempfile
import shutil

# ── Bootstrap env before any core imports ────────────────────────────────────
_TMP = tempfile.mkdtemp(prefix="ta_rag_test_")
os.environ["STORAGE_ROOT"] = _TMP
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["CHROMA_PATH"] = os.path.join(_TMP, "chroma")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "604492237")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.db as db
import core.ingestion as ingestion
import core.chroma_client as chroma
import core.search as search
from core.acronyms import expand_query
from core.config import SIMILARITY_THRESHOLD

_DISTANCE_CUTOFF = 1.0 - SIMILARITY_THRESHOLD

SEP = "─" * 60


def detect_category(filename: str) -> str:
    name = os.path.basename(filename).lower()
    for cat in ("tosp", "antibiotics", "glossary", "sop"):
        if cat in name:
            return cat
    return "general"


def main():
    parser = argparse.ArgumentParser(description="Test RAG ingestion and search locally.")
    parser.add_argument("pdf", help="Path to the PDF file to ingest")
    parser.add_argument("queries", nargs="+", help="One or more queries to test")
    parser.add_argument("--category", default=None,
                        choices=("sop", "tosp", "antibiotics", "glossary", "general"),
                        help="Category (default: auto-detect from filename)")
    args = parser.parse_args()

    if not os.path.exists(args.pdf):
        print(f"ERROR: file not found: {args.pdf}")
        sys.exit(1)

    category = args.category or detect_category(args.pdf)
    filename = os.path.basename(args.pdf)

    try:
        # Init DB
        db.init_db()
        db.seed_defaults()

        # Ingest
        print(f"\n{SEP}")
        print(f"Ingesting: {filename}")
        print(f"Category:  {category}")
        print(SEP)

        with open(args.pdf, "rb") as f:
            file_bytes = f.read()

        source_id, num_chunks, low_confidence = ingestion.ingest_document(
            category, filename, file_bytes
        )

        total_in_db = chroma.count()
        print(f"✅ Done — {num_chunks} chunks ingested ({total_in_db} total in ChromaDB)")
        if low_confidence:
            print("⚠️  Low-confidence extraction — document may be scanned or image-based.")

        # Query each question
        for query_text in args.queries:
            expanded = expand_query(query_text, tosp=(category == "tosp"))
            print(f"\n{SEP}")
            print(f"Query: {query_text!r}")
            if expanded != query_text:
                print(f"Expanded: {expanded!r}")
            print(SEP)

            raw = chroma.query(expanded, n_results=5, category=None)
            ids_list = raw.get("ids", [[]])[0]
            docs     = raw.get("documents", [[]])[0]
            metas    = raw.get("metadatas", [[]])[0]
            dists    = raw.get("distances", [[]])[0]

            if not ids_list:
                print("No results returned from ChromaDB at all.")
                continue

            print(f"Top {len(ids_list)} raw results (threshold={SIMILARITY_THRESHOLD}, cutoff distance={_DISTANCE_CUTOFF:.2f}):\n")
            for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), 1):
                similarity = 1.0 - dist
                passed = "✅ PASS" if dist <= _DISTANCE_CUTOFF else "❌ FILTERED"
                print(f"  [{i}] {passed}  similarity={similarity:.3f}  dist={dist:.3f}  file={meta.get('filename','?')}")
                # Show first 200 chars of matched text
                preview = doc[:200].replace("\n", " ")
                print(f"      Text: {preview!r}")
                print()

            # For TOSP: show exactly what the bot would reply (semantic +
            # keyword fallback, up to 3 results)
            if category == "tosp":
                bot_results = search.search_tosp(query_text)
                print(f"  BOT WOULD REPLY WITH ({min(len(bot_results), 3)} result(s)):")
                if not bot_results:
                    print("    (nothing — escalated to admin queue)")
                for r in bot_results[:3]:
                    via = "semantic" if r["distance"] is not None else "keyword"
                    first_lines = " | ".join(r["text"].split("\n")[:3])
                    print(f"    [{via}] {first_lines[:160]}")
                print()

    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
        print(f"{SEP}")
        print("Temp files cleaned up.")


if __name__ == "__main__":
    main()
