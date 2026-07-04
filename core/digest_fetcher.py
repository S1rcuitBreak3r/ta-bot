"""
Education digest fetcher and generator.

Fetch pipeline:
  1. For each active education_source, attempt to fetch content.
  2. RSS feeds: parsed with feedparser — top 5 entry titles + summaries.
  3. HTML: raw text (Claude handles extraction).
  4. Failed sources are logged and skipped — the rest still go to Claude.

Claude generates the final digest from collected content, or from its own
training knowledge if no sources are configured / all fail.
"""
from __future__ import annotations

import logging

import httpx
import feedparser

from core.claude_client import ask

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 12  # seconds per source
_MAX_CONTENT_PER_SOURCE = 600  # chars sent to Claude per source


async def _fetch_rss(url: str) -> str:
    """Fetch and parse an RSS/Atom feed. Returns multi-line text of top entries."""
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    feed = feedparser.parse(resp.text)
    entries = feed.entries[:6]
    if not entries:
        raise ValueError(f"No entries in feed: {url}")

    lines = []
    for e in entries:
        title = e.get("title", "").strip()
        summary = e.get("summary", "").strip()
        # Strip HTML tags from summary (basic)
        import re
        summary = re.sub(r"<[^>]+>", " ", summary).strip()[:200]
        lines.append(f"• {title}. {summary}")
    return "\n".join(lines)


async def _fetch_html(url: str) -> str:
    """Fetch a webpage and return the first chunk of text content."""
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    import re
    text = re.sub(r"<[^>]+>", " ", resp.text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_CONTENT_PER_SOURCE]


async def fetch_all_sources(sources: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Attempt to fetch all active sources.
    Returns (content_items, failed_source_names).
    content_items: list of {'name': str, 'content': str}
    """
    content_items: list[dict] = []
    failed: list[str] = []

    for source in sources:
        try:
            if source.get("source_type", "rss") == "html":
                content = await _fetch_html(source["url"])
            else:
                content = await _fetch_rss(source["url"])
            content_items.append({"name": source["name"], "content": content})
            logger.info("Fetched source: %s", source["name"])
        except Exception as exc:
            logger.warning("Source '%s' failed: %s", source["name"], exc)
            failed.append(source["name"])

    return content_items, failed


async def generate_digest(
    content_items: list[dict], period: str = "weekly"
) -> str:
    """
    Call Claude to generate a medical education digest.
    period: 'weekly' | 'monthly'
    """
    period_label = "week's" if period == "weekly" else "month's"

    if content_items:
        combined = "\n\n".join(
            f"=== {c['name']} ===\n{c['content'][:_MAX_CONTENT_PER_SOURCE]}"
            for c in content_items
        )
        prompt = (
            f"Based on these recent medical news items, write a concise {period} education "
            f"digest (5–7 bullet points) for a Singapore clinic team (including "
            f"anaesthesiologists and GPs).\n"
            "Format: one intro sentence, then bullet points using *Bold Topic*: summary.\n"
            "Focus on clinical relevance — drug updates, procedural guidelines, safety alerts.\n\n"
            f"{combined}"
        )
    else:
        prompt = (
            f"Write a concise {period} medical education digest (5–7 bullet points) "
            "for a Singapore clinic team (anaesthesiologists, GPs, clinic staff).\n"
            "Format: one intro sentence, then bullet points using *Bold Topic*: summary.\n"
            "Focus on: drug interactions, procedural guidelines, safety alerts, "
            "or clinical updates relevant to Singapore practice."
        )

    return await ask(
        system=(
            "You are a medical education editor producing a concise, accurate digest "
            "for Singapore clinicians. Use Telegram-compatible Markdown."
        ),
        user_message=prompt,
        max_tokens=900,
    )


async def build_digest(
    sources: list[dict], period: str = "weekly"
) -> tuple[str, list[str], list[str]]:
    """
    Full pipeline: fetch sources → generate digest.
    Returns (digest_text, source_names_used, source_names_failed).
    """
    content_items, failed = await fetch_all_sources(sources)
    digest_text = await generate_digest(content_items, period=period)
    source_names = [c["name"] for c in content_items]
    return digest_text, source_names, failed
