"""
Form filler module — admin-only at launch, unlockable via /enablemodule form_filler.

Commands:
  /setprofile   — guided flow to set your profile (full name, MMC, clinic, etc.)
  /myprofile    — display your current profile
  /fillform     — upload a form → Claude fills it from your profile → AI-watermarked draft
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import core.db as db
import core.flags as flags
import core.storage as storage
from core.config import CONVERSATION_TIMEOUT_SECONDS
from core.form_filler import (
    PROFILE_FIELDS,
    fill_form,
    format_profile,
    load_profile,
    save_profile,
)
from core.ingestion import extract_text

logger = logging.getLogger(__name__)

MODULE = "form_filler"

SETPROFILE_COLLECTING = 60
FILLFORM_AWAITING_FILE = 70


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

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
# /setprofile — guided profile collection
# --------------------------------------------------------------------------- #

async def _next_profile_step(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send the next profile question, or save and finish if all fields collected."""
    idx = context.user_data.get("sp_idx", 0)

    if idx >= len(PROFILE_FIELDS):
        uid: int = context.user_data["sp_uid"]
        profile: dict = context.user_data.get("sp_draft", {})
        storage.ensure_user_folder(uid)
        save_profile(uid, profile)
        db.log_action(uid, "set_profile", module_name=MODULE)
        await message.reply_text("✅ Profile saved.\n\n" + format_profile(profile))
        context.user_data.clear()
        return ConversationHandler.END

    field_key, label, hint = PROFILE_FIELDS[idx]
    current = context.user_data.get("sp_draft", {}).get(field_key)
    current_note = f"\nCurrent: _{current}_" if current else ""
    await message.reply_text(
        f"Step {idx + 1} of {len(PROFILE_FIELDS)} — *{label}*{current_note}\n"
        f"_{hint}_\n\n"
        "Send /skip to leave unchanged, or /cancel to abort.",
        parse_mode="Markdown",
    )
    return SETPROFILE_COLLECTING


async def cmd_setprofile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_whitelisted(update):
        return ConversationHandler.END
    if not flags.is_module_enabled(MODULE, update.effective_user.id):
        await update.message.reply_text(flags.module_maintenance_message())
        return ConversationHandler.END

    uid = update.effective_user.id
    existing = load_profile(uid) or {}
    context.user_data.update(sp_uid=uid, sp_idx=0, sp_draft=dict(existing))

    await update.message.reply_text(
        "Setting up your profile for form filling.\n\n"
        "Only form-relevant fields are collected — no NRIC or financial data.\n"
        "Existing values shown in brackets; /skip to keep them.\n"
    )
    return await _next_profile_step(update.message, context)


async def setprofile_got_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text("Please enter a value, /skip, or /cancel.")
        return SETPROFILE_COLLECTING

    idx = context.user_data.get("sp_idx", 0)
    field_key = PROFILE_FIELDS[idx][0]
    context.user_data.setdefault("sp_draft", {})[field_key] = value
    context.user_data["sp_idx"] = idx + 1
    return await _next_profile_step(update.message, context)


async def setprofile_skip(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data["sp_idx"] = context.user_data.get("sp_idx", 0) + 1
    return await _next_profile_step(update.message, context)


# --------------------------------------------------------------------------- #
# /myprofile
# --------------------------------------------------------------------------- #

async def cmd_myprofile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update):
        return
    uid = update.effective_user.id
    profile = load_profile(uid)
    if not profile:
        await update.message.reply_text(
            "No profile found. Use /setprofile to set one up."
        )
        return
    await update.message.reply_text(format_profile(profile))


# --------------------------------------------------------------------------- #
# /fillform
# --------------------------------------------------------------------------- #

async def cmd_fillform(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_whitelisted(update):
        return ConversationHandler.END
    if not flags.is_module_enabled(MODULE, update.effective_user.id):
        await update.message.reply_text(flags.module_maintenance_message())
        return ConversationHandler.END

    uid = update.effective_user.id
    profile = load_profile(uid)
    if not profile:
        await update.message.reply_text(
            "No profile found. Run /setprofile first."
        )
        return ConversationHandler.END

    context.user_data["ff_uid"] = uid
    await update.message.reply_text(
        "Send the form as a PDF or image.\n\n"
        "I'll extract the fields and fill in what I can from your profile. "
        "The result is a draft — please verify every field before submitting.\n\n"
        "Send /cancel to abort."
    )
    return FILLFORM_AWAITING_FILE


async def fillform_got_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.message.document and not update.message.photo:
        await update.message.reply_text(
            "Please send a PDF or image file, or /cancel."
        )
        return FILLFORM_AWAITING_FILE

    uid = context.user_data.get("ff_uid", update.effective_user.id)
    profile = load_profile(uid)
    if not profile:
        await update.message.reply_text("Profile not found. Run /setprofile first.")
        context.user_data.clear()
        return ConversationHandler.END

    if update.message.document:
        tg_obj = update.message.document
        filename = tg_obj.file_name or "form.pdf"
        file_id = tg_obj.file_id
    else:
        tg_obj = update.message.photo[-1]
        filename = "form.jpg"
        file_id = tg_obj.file_id

    tg_file = await context.bot.get_file(file_id)
    file_bytes = bytes(await tg_file.download_as_bytearray())

    await update.message.reply_text("Processing form...")

    try:
        form_text, low_confidence = extract_text(file_bytes, filename)
    except Exception as exc:
        logger.error("Form text extraction failed: %s", exc)
        form_text, low_confidence = "", True

    if not form_text.strip():
        await update.message.reply_text(
            "I couldn't extract any text from this file. "
            "Try a clearer scan or a native PDF."
        )
        context.user_data.clear()
        return ConversationHandler.END

    try:
        result = await fill_form(form_text, profile, low_confidence)
    except Exception as exc:
        logger.error("Form fill failed: %s", exc)
        await update.message.reply_text(f"Form filling failed: {exc}")
        context.user_data.clear()
        return ConversationHandler.END

    db.log_action(uid, "fill_form", detail=filename, module_name=MODULE)

    # Send result, chunked if needed
    for i in range(0, len(result), 4000):
        await update.message.reply_text(result[i : i + 4000])

    context.user_data.clear()
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Register
# --------------------------------------------------------------------------- #

def register_handlers(application):
    setprofile_conv = ConversationHandler(
        entry_points=[CommandHandler("setprofile", cmd_setprofile)],
        states={
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, conv_timeout)],
            SETPROFILE_COLLECTING: [
                CommandHandler("skip", setprofile_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setprofile_got_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        name="setprofile_conv",
        persistent=True,
    )

    fillform_conv = ConversationHandler(
        entry_points=[CommandHandler("fillform", cmd_fillform)],
        states={
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, conv_timeout)],
            FILLFORM_AWAITING_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, fillform_got_file),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        name="fillform_conv",
        persistent=True,
    )

    application.add_handler(setprofile_conv)
    application.add_handler(fillform_conv)
    application.add_handler(CommandHandler("myprofile", cmd_myprofile))
