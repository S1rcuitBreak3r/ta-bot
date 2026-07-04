"""
Central configuration. All secrets come from environment variables — never hardcoded here.
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()  # no-op on Railway; reads local .env when present


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"FATAL: required environment variable {name} is not set.", file=sys.stderr)
        sys.exit(1)
    return value


TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
ADMIN_TELEGRAM_ID = int(_require("ADMIN_TELEGRAM_ID"))

# Storage — volume root is /data on Railway; override locally with STORAGE_ROOT
STORAGE_ROOT = os.environ.get("STORAGE_ROOT", "/data")
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(STORAGE_ROOT, "ta_bot.db"))
CHROMA_PATH = os.environ.get("CHROMA_PATH", os.path.join(STORAGE_ROOT, "chroma"))
PERSISTENCE_PATH = os.environ.get("PERSISTENCE_PATH", os.path.join(STORAGE_ROOT, "bot_persistence"))

CLAUDE_MODEL = "claude-sonnet-4-6"
TIMEZONE = "Asia/Singapore"

# --- Tunable behaviour constants ------------------------------------------------

# Cosine similarity threshold for manual_answers semantic match (0–1).
# Tune empirically during Phase 2 — do not ship a guessed number.
SIMILARITY_THRESHOLD = 0.55

# Chunk size for document ingestion (chars). Glossary always uses 1 row per chunk.
CHUNK_SIZE = 400
CHUNK_OVERLAP = 80

# Conversation timeout — incomplete multi-step flows are abandoned after this.
CONVERSATION_TIMEOUT_SECONDS = 1800  # 30 minutes

# Birthday notification: on the day only (0 = no advance reminder).
BIRTHDAY_LOOKFORWARD_DAYS = 0

# Retry behaviour for external calls (Claude API, Telegram delivery).
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3  # multiplied by attempt number: 3s, 6s, 9s
