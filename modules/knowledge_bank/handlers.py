"""
Knowledge bank module handlers.

Admin commands:
  /updateknowledge  — ingest a PDF/text document (category → file upload)
  /addterm          — add a manual Q&A entry (query → answer → category)
  /unanswered       — list pending queue items
  /resolve <id>     — answer a queued question (pushes answer to all askers)
  /dismiss <id>     — close a queue item without answering
  /reindex          — re-embed manual_answers rows with missing chroma_id

All-user commands (module-flag checked):
  /ask <question>   — search all categories
  /tosp <query>     — search TOSP category
  /antibiotics <q>  — search Antibiotics category
"""
from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import core.db as db
import core.flags as flags
import core.ingestion as ingestion
import core.search as search
from core.acronyms import expand_query
from core.config import ADMIN_TELEGRAM_ID, CONVERSATION_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

MODULE = "knowledge_bank"

# ConversationHandler state constants
UPDATEKNOW_AWAITING_CATEGORY = 10
UPDATEKNOW_AWAITING_FILE = 11
ADDTERM_AWAITING_QUERY = 20
ADDTERM_AWAITING_ANSWER = 21
ADDTERM_AWAITING_CATEGORY = 22
RESOLVE_AWAITING_ANSWER = 30

CATEGORIES = ingestion.CATEGORIES
CATEGORY_LABELS = search.CATEGORY_LABELS


# --------------------------------------------------------------------------- #
# Inline keyboard helpers
# --------------------------------------------------------------------------- #

def _category_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("SOP", callback_data=f"{prefix}_sop"),
         InlineKeyboardButton("TOSP", callback_data=f"{prefix}_tosp")],
        [InlineKeyboardButton("Antibiotics", callback_data=f"{prefix}_antibiotics"),
         InlineKeyboardButton("Glossary", callback_data=f"{prefix}_glossary")],
        [InlineKeyboardButton("General", callback_data=f"{prefix}_general")],
    ])


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #

def _is_admin(update: Update) -> bool:
    return bool(update.effective_user) and db.is_admin(update.effective_user.id)


def _is_whitelisted(update: Update) -> bool:
    return bool(update.effective_user) and db.is_whitelisted(update.effective_user.id)


# --------------------------------------------------------------------------- #
# Shared fallbacks
# --------------------------------------------------------------------------- #

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def conv_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.message:
        await update.message.reply_text(
            "Your session timed out. Please start the command again."
        )
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# /updateknowledge — admin document ingest
# --------------------------------------------------------------------------- #

async def cmd_updateknowledge_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not _is_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Uploading a knowledge document.\n\n"
        "Step 1 of 2 — Select the category for this document:",
        reply_markup=_category_keyboard("upd"),
    )
    return UPDATEKNOW_AWAITING_CATEGORY


async def updateknow_got_category(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    category = query.data.split("_", 1)[1]
    context.user_data["upd_category"] = category
    label = CATEGORY_LABELS.get(category, category)
    await query.edit_message_text(
        f"Category: {label} ✓\n\n"
        "Step 2 of 2 — Send the document file (PDF or .txt).\n\n"
        "Send /cancel to abort."
    )
    return UPDATEKNOW_AWAITING_FILE


async def updateknow_got_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.message.document:
        await update.message.reply_text(
            "Please send a file (PDF or .txt), or /cancel."
        )
        return UPDATEKNOW_AWAITING_FILE

    category = context.user_data.get("upd_category", "general")
    doc = update.message.document
    filename = doc.file_name or "document.pdf"

    await update.message.reply_text(f"Received {filename}. Processing...")

    tg_file = await context.bot.get_file(doc.file_id)
    file_bytes = bytes(await tg_file.download_as_bytearray())

    t0 = time.monotonic()
    try:
        source_id, num_chunks, low_confidence = ingestion.ingest_document(
            category, filename, file_bytes
        )
    except Exception as exc:
        logger.error("ingest_document failed: %s", exc)
        await update.message.reply_text(f"Ingest failed: {exc}")
        context.user_data.clear()
        return ConversationHandler.END

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    label = CATEGORY_LABELS.get(category, category)
    msg = (
        f"✅ Ingested into {label}.\n"
        f"Chunks: {num_chunks} | Time: {elapsed_ms} ms"
    )
    if low_confidence:
        msg += "\n\n⚠️ Low-confidence extraction — document may be scanned. Please verify the content was captured correctly."

    db.log_action(
        update.effective_user.id, "ingest_document",
        detail=f"{filename} → {category} ({num_chunks} chunks)",
        module_name=MODULE,
    )
    await update.message.reply_text(msg)
    context.user_data.clear()
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# /addterm — admin manual answer
# --------------------------------------------------------------------------- #

async def cmd_addterm_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not _is_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Adding a manual answer to the knowledge base.\n\n"
        "Step 1 of 3 — What is the question or term? "
        "(e.g. 'What is RSI?' or 'RSI induction')\n\n"
        "Send /cancel to abort."
    )
    return ADDTERM_AWAITING_QUERY


async def addterm_got_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query_text = update.message.text.strip()
    if not query_text:
        await update.message.reply_text("Please enter a question or term, or /cancel.")
        return ADDTERM_AWAITING_QUERY
    context.user_data["addterm_query"] = query_text
    await update.message.reply_text(
        f"Term: {query_text!r} ✓\n\n"
        "Step 2 of 3 — What is the answer? (Plain text, as much detail as useful.)"
    )
    return ADDTERM_AWAITING_ANSWER


async def addterm_got_answer(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    answer_text = update.message.text.strip()
    if not answer_text:
        await update.message.reply_text("Answer can't be blank. Please try again, or /cancel.")
        return ADDTERM_AWAITING_ANSWER
    context.user_data["addterm_answer"] = answer_text
    await update.message.reply_text(
        "Step 3 of 3 — Select the category for this answer:",
        reply_markup=_category_keyboard("term"),
    )
    return ADDTERM_AWAITING_CATEGORY


async def addterm_got_category(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    category = query.data.split("_", 1)[1]

    trigger_query = context.user_data.get("addterm_query", "")
    answer_text = context.user_data.get("addterm_answer", "")
    admin_id = update.effective_user.id

    # Save to SQLite first (chroma_id may be set later if embedding fails)
    answer_id = db.insert_manual_answer(
        trigger_query=trigger_query,
        answer_text=answer_text,
        added_by=admin_id,
        category=category,
    )

    # Embed into ChromaDB
    try:
        chroma_id = ingestion.embed_manual_answer(answer_id, trigger_query, answer_text, category)
        db.update_manual_answer_chroma_id(answer_id, chroma_id)
        embed_note = ""
    except Exception as exc:
        logger.error("embed_manual_answer failed for answer %s: %s", answer_id, exc)
        embed_note = (
            "\n\n⚠️ Embedding failed — this answer won't appear in search yet. "
            "Run /reindex to retry."
        )

    label = CATEGORY_LABELS.get(category, category)
    db.log_action(admin_id, "add_term", detail=trigger_query, module_name=MODULE)
    await query.edit_message_text(
        f"✅ Answer saved under {label}.{embed_note}"
    )
    context.user_data.clear()
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# /ask, /tosp, /antibiotics — search commands (all users)
# --------------------------------------------------------------------------- #

async def _handle_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    source_command: str, category: str | None = None,
):
    """Shared search logic for /ask, /tosp, /antibiotics."""
    if not _is_whitelisted(update):
        return
    if not flags.is_module_enabled(MODULE, update.effective_user.id):
        await update.message.reply_text(flags.module_maintenance_message())
        return

    query_text = " ".join(context.args or []).strip()
    if not query_text:
        await update.message.reply_text(
            f"Please include your question. Example:\n/{source_command} what is RSI induction?"
        )
        return

    uid = update.effective_user.id
    t0 = time.monotonic()
    if source_command == "tosp":
        results = search.search_tosp(query_text)
    else:
        results = search.search(expand_query(query_text), category=category)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if results:
        # TOSP queries can legitimately match several procedures (e.g.
        # circumcision has separate child/adult entries) — show up to 3.
        shown = results[:3] if source_command == "tosp" else results[:1]
        answer = "\n\n———\n\n".join(search.format_answer(r) for r in shown)
        db.log_action(
            uid, source_command, detail=query_text[:200],
            module_name=MODULE, outcome="answered", response_ms=elapsed_ms,
        )
        await update.message.reply_text(answer, parse_mode="Markdown")
        return

    # Nothing found — escalate to queue
    db.log_action(
        uid, source_command, detail=query_text[:200],
        module_name=MODULE, outcome="escalated", response_ms=elapsed_ms,
    )

    duplicate = search.find_pending_duplicate(query_text, source_command)
    if duplicate:
        db.add_also_asked_by(duplicate["id"], uid)
        await update.message.reply_text(
            "I don't have that answer yet — it's already been flagged for the admin "
            "and you'll be notified when it's answered."
        )
        return

    asker = db.get_user(uid)
    asker_name = asker["display_name"] if asker else f"ID {uid}"
    queue_id = db.insert_queue_entry(uid, query_text, source_command)

    await update.message.reply_text(
        "I don't have that answer yet. It's been flagged for the admin "
        "and you'll be notified when it's answered."
    )

    # Notify admin
    try:
        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=(
                f"❓ Unanswered question (Queue #{queue_id})\n"
                f"From: {asker_name}\n"
                f"Command: /{source_command}\n\n"
                f"{query_text}\n\n"
                f"Reply with /resolve {queue_id} to answer."
            ),
        )
    except Exception as exc:
        logger.error("Failed to notify admin of queue entry %s: %s", queue_id, exc)


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _handle_search(update, context, "ask", category=None)


async def cmd_tosp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _handle_search(update, context, "tosp", category="tosp")


async def cmd_antibiotics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _handle_search(update, context, "antibiotics", category="antibiotics")


# --------------------------------------------------------------------------- #
# /unanswered — admin queue view
# --------------------------------------------------------------------------- #

async def cmd_unanswered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    pending = db.list_pending_queue()
    if not pending:
        await update.message.reply_text("No pending questions — queue is clear.")
        return
    lines = [f"Pending questions ({len(pending)}):"]
    for entry in pending:
        asker = db.get_user(entry["telegram_id"])
        asker_name = asker["display_name"] if asker else f"ID {entry['telegram_id']}"
        also = len(__import__("json").loads(entry["also_asked_by"] or "[]"))
        also_str = f" +{also} others" if also else ""
        lines.append(
            f"\n#{entry['id']} — {asker_name}{also_str}\n"
            f"  [{entry['source_command']}] {entry['query_text'][:80]}"
        )
    lines.append("\nUse /resolve <id> to answer, /dismiss <id> to close.")
    await update.message.reply_text("\n".join(lines))


# --------------------------------------------------------------------------- #
# /resolve <id> — admin provides answer, notifies all askers
# --------------------------------------------------------------------------- #

async def cmd_resolve_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not _is_admin(update):
        return ConversationHandler.END

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /resolve <queue id>\nGet queue IDs from /unanswered."
        )
        return ConversationHandler.END

    try:
        queue_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Queue ID must be a number.")
        return ConversationHandler.END

    entry = db.get_queue_entry(queue_id)
    if not entry or entry["status"] != "pending":
        await update.message.reply_text(
            f"Queue entry #{queue_id} not found or not pending."
        )
        return ConversationHandler.END

    asker = db.get_user(entry["telegram_id"])
    asker_name = asker["display_name"] if asker else f"ID {entry['telegram_id']}"

    context.user_data["resolve_queue_id"] = queue_id
    context.user_data["resolve_query"] = entry["query_text"]
    context.user_data["resolve_source_command"] = entry["source_command"]

    await update.message.reply_text(
        f"Resolving Queue #{queue_id}\n"
        f"From: {asker_name}\n\n"
        f"Question: {entry['query_text']}\n\n"
        "Type your answer now. It will be saved and sent to all who asked.\n\n"
        "Send /cancel to abort."
    )
    return RESOLVE_AWAITING_ANSWER


async def resolve_got_answer(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    answer_text = update.message.text.strip()
    if not answer_text:
        await update.message.reply_text("Answer can't be blank. Please try again, or /cancel.")
        return RESOLVE_AWAITING_ANSWER

    queue_id: int = context.user_data["resolve_queue_id"]
    query_text: str = context.user_data["resolve_query"]
    source_command: str = context.user_data.get("resolve_source_command", "ask")
    admin_id = update.effective_user.id

    entry = db.get_queue_entry(queue_id)
    if not entry:
        await update.message.reply_text("Queue entry disappeared — aborting.")
        context.user_data.clear()
        return ConversationHandler.END

    # Save to manual_answers
    answer_id = db.insert_manual_answer(
        trigger_query=query_text,
        answer_text=answer_text,
        added_by=admin_id,
        category=None,
        queue_id=queue_id,
    )

    # Embed into ChromaDB
    embed_note = ""
    try:
        chroma_id = ingestion.embed_manual_answer(answer_id, query_text, answer_text, category=None)
        db.update_manual_answer_chroma_id(answer_id, chroma_id)
    except Exception as exc:
        logger.error("embed failed for answer %s: %s", answer_id, exc)
        embed_note = "\n⚠️ Embedding failed — run /reindex to make this searchable."

    # Mark queue resolved
    db.resolve_queue_entry(queue_id)
    db.log_action(admin_id, "resolve_queue", detail=f"#{queue_id}", module_name=MODULE)

    await update.message.reply_text(
        f"✅ Answer saved and Queue #{queue_id} resolved.{embed_note}"
    )

    # Notify all who asked
    import json as _json
    recipients = [entry["telegram_id"]] + _json.loads(entry["also_asked_by"] or "[]")
    admin = db.get_user(admin_id)
    admin_name = admin["display_name"] if admin else "Admin"
    notification = (
        f"Your question has been answered:\n\n"
        f"Q: {query_text}\n\n"
        f"A: {answer_text}\n\n"
        f"— {admin_name}"
    )
    for uid in recipients:
        try:
            await context.bot.send_message(chat_id=uid, text=notification)
        except Exception as exc:
            logger.warning("Could not notify uid %s: %s", uid, exc)

    context.user_data.clear()
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# /dismiss <id>
# --------------------------------------------------------------------------- #

async def cmd_dismiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /dismiss <queue id>")
        return
    try:
        queue_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Queue ID must be a number.")
        return

    entry = db.get_queue_entry(queue_id)
    if not entry or entry["status"] != "pending":
        await update.message.reply_text(f"Queue entry #{queue_id} not found or not pending.")
        return

    db.dismiss_queue_entry(queue_id)
    db.log_action(update.effective_user.id, "dismiss_queue", detail=f"#{queue_id}", module_name=MODULE)
    await update.message.reply_text(f"Queue #{queue_id} dismissed.")


# --------------------------------------------------------------------------- #
# /reindex — re-embed manual_answers with missing chroma_id
# --------------------------------------------------------------------------- #

async def cmd_reindex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    await update.message.reply_text("Reindexing unembedded answers...")
    fixed, failed = ingestion.reindex_unembedded(update.effective_user.id)
    await update.message.reply_text(
        f"Reindex complete. Fixed: {fixed} | Still failing: {failed}"
    )


# --------------------------------------------------------------------------- #
# Register
# --------------------------------------------------------------------------- #

def register_handlers(application):
    updateknow_conv = ConversationHandler(
        entry_points=[CommandHandler("updateknowledge", cmd_updateknowledge_start)],
        states={
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, conv_timeout)],
            UPDATEKNOW_AWAITING_CATEGORY: [
                CallbackQueryHandler(updateknow_got_category, pattern=r"^upd_"),
            ],
            UPDATEKNOW_AWAITING_FILE: [
                MessageHandler(filters.Document.ALL, updateknow_got_file),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        name="updateknow_conv",
        persistent=True,
    )

    addterm_conv = ConversationHandler(
        entry_points=[CommandHandler("addterm", cmd_addterm_start)],
        states={
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, conv_timeout)],
            ADDTERM_AWAITING_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addterm_got_query),
            ],
            ADDTERM_AWAITING_ANSWER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addterm_got_answer),
            ],
            ADDTERM_AWAITING_CATEGORY: [
                CallbackQueryHandler(addterm_got_category, pattern=r"^term_"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        name="addterm_conv",
        persistent=True,
    )

    resolve_conv = ConversationHandler(
        entry_points=[CommandHandler("resolve", cmd_resolve_start)],
        states={
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, conv_timeout)],
            RESOLVE_AWAITING_ANSWER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, resolve_got_answer),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        name="resolve_conv",
        persistent=True,
    )

    application.add_handler(updateknow_conv)
    application.add_handler(addterm_conv)
    application.add_handler(resolve_conv)

    application.add_handler(CommandHandler("ask", cmd_ask))
    application.add_handler(CommandHandler("tosp", cmd_tosp))
    application.add_handler(CommandHandler("antibiotics", cmd_antibiotics))
    application.add_handler(CommandHandler("unanswered", cmd_unanswered))
    application.add_handler(CommandHandler("dismiss", cmd_dismiss))
    application.add_handler(CommandHandler("reindex", cmd_reindex))
