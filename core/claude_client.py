"""
Thin wrapper around the Anthropic API.
  1. Retries on transient failures (network, rate limits, 5xx).
  2. Makes JSON responses reliable — strips markdown fences and nudges on parse failure.

The anthropic SDK is synchronous; python-telegram-bot and APScheduler run on asyncio.
ask() / ask_json() push the blocking call into a thread via asyncio.to_thread().
"""
import asyncio
import json
import logging
import time

import anthropic

from core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_RETRIES, RETRY_BACKOFF_SECONDS

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class ClaudeError(RuntimeError):
    """Raised when Claude could not be reached or parsed after all retries."""


def _call_raw_sync(system: str, user_message: str, max_tokens: int) -> str:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = _client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            return "".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("Claude API call failed (attempt %s/%s): %s", attempt, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise ClaudeError(f"Claude API call failed after {MAX_RETRIES} attempts: {last_exc}")


async def ask(system: str, user_message: str, max_tokens: int = 2000) -> str:
    return await asyncio.to_thread(_call_raw_sync, system, user_message, max_tokens)


def _extract_json_block(text: str) -> str:
    """Strip ```json ... ``` fences or surrounding prose."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not (text.startswith("{") or text.startswith("[")):
        start_candidates = [i for i in (text.find("{"), text.find("[")) if i != -1]
        if start_candidates:
            text = text[min(start_candidates):]
    return text


async def ask_json(system: str, user_message: str, max_tokens: int = 2000):
    """Ask Claude for JSON and parse it, retrying with a corrective nudge on parse failure."""
    strict_system = (
        system
        + "\n\nIMPORTANT: Respond with ONLY valid JSON. No markdown fences, no commentary."
    )
    current_user_message = user_message
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        raw = await ask(strict_system, current_user_message, max_tokens)
        candidate = _extract_json_block(raw)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            logger.warning("Claude JSON parse failed (attempt %s/%s): %s", attempt, MAX_RETRIES, exc)
            current_user_message = (
                user_message
                + f"\n\nYour previous reply could not be parsed as JSON ({exc}). "
                "Reply again with ONLY a single valid JSON object/array and nothing else."
            )
    raise ClaudeError(f"Claude did not return valid JSON after {MAX_RETRIES} attempts: {last_error}")
