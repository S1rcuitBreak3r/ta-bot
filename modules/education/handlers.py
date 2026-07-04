"""
Education digest module — admin-only.

Commands:
  /previewdigest   — generate a digest draft and send for admin approval
  /addsource <url> [name] — add an RSS/web source
  /listsources     — list configured sources
  /removesource <id> — deactivate a source
  /digesthistory   — show recent approved digests

Inline keyboard handlers (top-level, not in ConversationHandler):
  digest_approve_<id>  — approve and send to audience
  digest_discard_<id>  — discard the draft

The approval keyboard works for both manually generated previews (/previewdigest)
and scheduler-generated previews (job_weekly_digest, job_monthly_digest).
"""
from __future__ import annotations

import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import core.db as db
import core.flags as flags
from core.digest_fetcher import build_digest

logger = logging.getLogger(__name__)

MODULE = "education"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def _is_admin(update: Update) -> bool:
    return bool(update.effective_user) and db.is_admin(update.effective_user.id)


# --------------------------------------------------------------------------- #
# Keyboard
# --------------------------------------------------------------------------- #

def _approval_keyboard(digest_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✓ Send to team", callback_data=f"digest_approve_{digest_id}"),
        InlineKeyboardButton("✗ Discard", callback_data=f"digest_discard_{digest_id}"),
    ]])


# --------------------------------------------------------------------------- #
# Helper — send digest preview to admin
# --------------------------------------------------------------------------- #

async def _send_preview(bot, chat_id: int, digest_id: int, digest_text: str,
                         source_names: list[str], failed_names: list[str]):
    """Send a digest draft to admin with approve/discard keyboard."""
    header = "📚 *Education Digest Draft*\n\n"
    footer_parts = []
    if source_names:
        footer_parts.append(f"Sources: {', '.join(source_names)}")
    if failed_names:
        footer_parts.append(f"⚠️ Failed sources: {', '.join(failed_names)}")
    footer = "\n\n_" + " | ".join(footer_parts) + "_" if footer_parts else ""

    preview = header + digest_text + footer

    await bot.send_message(
        chat_id=chat_id,
        text=preview,
        parse_mode="Markdown",
        reply_markup=_approval_keyboard(digest_id),
    )


# --------------------------------------------------------------------------- #
# /previewdigest
# --------------------------------------------------------------------------- #

async def cmd_previewdigest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not flags.is_module_enabled(MODULE, update.effective_user.id):
        await update.message.reply_text(flags.module_maintenance_message())
        return

    await update.message.reply_text("Generating digest — this may take a moment...")

    sources = db.list_education_sources(active_only=True)
    try:
        digest_text, source_names, failed_names = await build_digest(sources, period="weekly")
    except Exception as exc:
        logger.error("Digest generation failed: %s", exc)
        await update.message.reply_text(f"Digest generation failed: {exc}")
        return

    digest_id = db.save_digest_draft(digest_text, source_names)
    await _send_preview(
        context.bot,
        update.effective_chat.id,
        digest_id,
        digest_text,
        source_names,
        failed_names,
    )


# --------------------------------------------------------------------------- #
# Inline callback: approve / discard
# --------------------------------------------------------------------------- #

async def callback_digest_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not db.is_admin(update.effective_user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    digest_id = int(query.data.split("_")[-1])
    digest = db.get_digest(digest_id)
    if not digest or digest["status"] != "draft":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.answer("This digest is no longer pending.", show_alert=True)
        return

    # Determine audience from current module flag
    module_state = db.get_module_state(MODULE)
    if module_state == "all_users":
        recipients = db.list_active_users()
        sent_to = "all_users"
    else:
        recipients = [db.get_user(update.effective_user.id)]
        sent_to = "admin_only"

    digest_text = digest["digest_text"]
    send_count = 0
    for user in recipients:
        if not user:
            continue
        try:
            await context.bot.send_message(
                chat_id=user["telegram_id"],
                text=f"📚 *Weekly Education Digest*\n\n{digest_text}",
                parse_mode="Markdown",
            )
            send_count += 1
        except Exception as exc:
            logger.warning("Could not deliver digest to %s: %s", user["telegram_id"], exc)

    db.approve_digest(digest_id, sent_to, send_count)
    db.log_action(update.effective_user.id, "approve_digest",
                  detail=f"#{digest_id} → {sent_to} ({send_count} recipients)",
                  module_name=MODULE)

    await query.edit_message_reply_markup(reply_markup=None)
    audience_label = "all active users" if sent_to == "all_users" else "admin only"
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ Digest #{digest_id} sent to {audience_label} ({send_count} recipients).",
    )


async def callback_digest_discard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not db.is_admin(update.effective_user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    digest_id = int(query.data.split("_")[-1])
    db.discard_digest(digest_id)
    db.log_action(update.effective_user.id, "discard_digest",
                  detail=f"#{digest_id}", module_name=MODULE)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.edit_message_text(f"Digest #{digest_id} discarded.")


# --------------------------------------------------------------------------- #
# /addsource, /listsources, /removesource
# --------------------------------------------------------------------------- #

async def cmd_addsource(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /addsource <url> [optional name]\n\n"
            "Example: /addsource https://www.nejm.org/action/showFeed?type=etoc&feed=rss&jc=nejm NEJM"
        )
        return

    url = args[0]
    name = " ".join(args[1:]) if len(args) > 1 else url.split("//")[-1].split("/")[0]
    source_type = "rss" if any(k in url for k in ("rss", "feed", "atom", "xml")) else "html"

    db.add_education_source(name, url, source_type)
    db.log_action(update.effective_user.id, "add_education_source",
                  detail=f"{name} ({url})", module_name=MODULE)
    await update.message.reply_text(f"✅ Source added: {name} ({source_type.upper()})")


async def cmd_listsources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    sources = db.list_education_sources(active_only=False)
    if not sources:
        await update.message.reply_text(
            "No sources configured. Use /addsource <url> to add one."
        )
        return
    lines = ["Education sources:"]
    for s in sources:
        status = "✅" if s["active"] else "⛔"
        lines.append(f"{status} [{s['id']}] {s['name']} ({s['source_type'].upper()})\n   {s['url']}")
    await update.message.reply_text("\n\n".join(lines))


async def cmd_removesource(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /removesource <id>")
        return
    try:
        source_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Source ID must be a number.")
        return
    db.remove_education_source(source_id)
    db.log_action(update.effective_user.id, "remove_education_source",
                  detail=f"id={source_id}", module_name=MODULE)
    await update.message.reply_text(f"Source #{source_id} deactivated.")


# --------------------------------------------------------------------------- #
# /digesthistory
# --------------------------------------------------------------------------- #

async def cmd_digesthistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    digests = db.list_recent_digests(limit=5)
    if not digests:
        await update.message.reply_text("No digests in history yet.")
        return
    lines = ["Recent digests (last 5):"]
    for d in digests:
        status_icon = {"approved": "✅", "discarded": "✗", "draft": "📝"}.get(d["status"], "?")
        sent_info = f" → {d['sent_to']} ({d['recipients_count']} recipients)" if d["sent_to"] else ""
        created = d["created_at"][:16]
        sources = json.loads(d["sources_used"] or "[]")
        src_label = ", ".join(sources) if sources else "AI only"
        lines.append(
            f"{status_icon} #{d['id']} — {created}{sent_info}\n"
            f"   Sources: {src_label}\n"
            f"   {d['digest_text'][:120]}…"
        )
    await update.message.reply_text("\n\n".join(lines))


# --------------------------------------------------------------------------- #
# Register
# --------------------------------------------------------------------------- #

def register_handlers(application):
    application.add_handler(CommandHandler("previewdigest", cmd_previewdigest))
    application.add_handler(CommandHandler("addsource", cmd_addsource))
    application.add_handler(CommandHandler("listsources", cmd_listsources))
    application.add_handler(CommandHandler("removesource", cmd_removesource))
    application.add_handler(CommandHandler("digesthistory", cmd_digesthistory))

    application.add_handler(
        CallbackQueryHandler(callback_digest_approve, pattern=r"^digest_approve_\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(callback_digest_discard, pattern=r"^digest_discard_\d+$")
    )
