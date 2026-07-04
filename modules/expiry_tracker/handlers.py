"""
Expiry tracker module handlers.

Commands:
  /upload       — track an expiry document (doc type → file → date confirm)
  /myexpiry     — view current tracked documents and their expiry dates
  /birthday DD-MM — save or update your birthday
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

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
import core.storage as storage
from core.config import CONVERSATION_TIMEOUT_SECONDS
from core.date_extractor import extract_from_document, format_date, parse_user_date

logger = logging.getLogger(__name__)

MODULE = "expiry_tracker"

# ConversationHandler states
UPLOAD_AWAITING_TYPE = 40
UPLOAD_AWAITING_FILE = 41
UPLOAD_AWAITING_CONFIRM = 42
UPLOAD_AWAITING_DATE_EDIT = 43

DOC_TYPES = (
    "MEDICAL_REGISTRATION",
    "INDEMNITY_INSURANCE",
    "BCLS",
    "ACLS",
    "WORK_PASS",
    "PASSPORT",
    "HOSPITAL_CREDENTIALING",
)

DOC_TYPE_LABELS = {
    "MEDICAL_REGISTRATION": "Medical Registration",
    "INDEMNITY_INSURANCE": "Indemnity Insurance",
    "BCLS": "BCLS",
    "ACLS": "ACLS",
    "WORK_PASS": "Work Pass",
    "PASSPORT": "Passport",
    "HOSPITAL_CREDENTIALING": "Hospital Credentialing",
}


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
# /upload
# --------------------------------------------------------------------------- #

def _doc_type_keyboard() -> InlineKeyboardMarkup:
    rows = []
    items = list(DOC_TYPE_LABELS.items())
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(label, callback_data=f"upl_{dtype}")
               for dtype, label in items[i:i + 2]]
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✓ Yes, save it", callback_data="exp_confirm"),
        InlineKeyboardButton("✗ Enter manually", callback_data="exp_edit"),
    ]])


async def cmd_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_whitelisted(update):
        return ConversationHandler.END
    if not flags.is_module_enabled(MODULE, update.effective_user.id):
        await update.message.reply_text(flags.module_maintenance_message())
        return ConversationHandler.END
    await update.message.reply_text(
        "Uploading an expiry document.\n\n"
        "Step 1 of 3 — What type of document is this?",
        reply_markup=_doc_type_keyboard(),
    )
    return UPLOAD_AWAITING_TYPE


async def upload_got_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    doc_type = query.data.split("_", 1)[1]
    label = DOC_TYPE_LABELS.get(doc_type, doc_type)
    context.user_data["upl_doc_type"] = doc_type
    await query.edit_message_text(
        f"Document type: {label} ✓\n\n"
        "Step 2 of 3 — Send the document file (PDF or image).\n\n"
        "Send /cancel to abort."
    )
    return UPLOAD_AWAITING_FILE


async def upload_got_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.document and not update.message.photo:
        await update.message.reply_text(
            "Please send a PDF or image file, or /cancel."
        )
        return UPLOAD_AWAITING_FILE

    doc_type = context.user_data.get("upl_doc_type", "")
    uid = update.effective_user.id

    # Download file
    if update.message.document:
        tg_file_obj = update.message.document
        filename = tg_file_obj.file_name or "document.pdf"
        file_id = tg_file_obj.file_id
    else:
        # Photo — use largest size
        tg_file_obj = update.message.photo[-1]
        filename = "photo.jpg"
        file_id = tg_file_obj.file_id

    tg_file = await context.bot.get_file(file_id)
    file_bytes = bytes(await tg_file.download_as_bytearray())

    # Save file to user's storage folder
    rel_path = storage.save_file(uid, filename, file_bytes)
    context.user_data["upl_file_path"] = rel_path
    context.user_data["upl_filename"] = filename

    await update.message.reply_text("File received. Extracting expiry date...")

    # Extract date
    try:
        expiry_date, ctx_snippet, low_conf = await extract_from_document(
            file_bytes, filename, doc_type
        )
    except Exception as exc:
        logger.error("Date extraction failed: %s", exc)
        expiry_date, ctx_snippet, low_conf = None, None, True

    context.user_data["upl_low_conf"] = low_conf

    if expiry_date:
        context.user_data["upl_expiry_date"] = expiry_date.isoformat()
        context.user_data["upl_ctx_snippet"] = ctx_snippet or ""

        label = DOC_TYPE_LABELS.get(doc_type, doc_type)
        msg = f"Step 3 of 3 — I found this date:\n\n"
        if ctx_snippet:
            msg += f'"{ctx_snippet}"\n\n'
        msg += f"→ *{format_date(expiry_date)}*\n\n"
        if low_conf:
            msg += "⚠️ Low confidence (scanned document or AI extraction) — please check carefully.\n\n"
        msg += f"Is this the correct expiry date for your {label}?"

        await update.message.reply_text(
            msg, parse_mode="Markdown", reply_markup=_confirm_keyboard()
        )
        return UPLOAD_AWAITING_CONFIRM
    else:
        label = DOC_TYPE_LABELS.get(doc_type, doc_type)
        await update.message.reply_text(
            f"I couldn't find an expiry date in this {label} document automatically.\n\n"
            "Please type the expiry date (DD-MM-YYYY, e.g. 31-12-2026):"
        )
        return UPLOAD_AWAITING_DATE_EDIT


async def upload_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "exp_edit":
        await query.edit_message_text(
            "Please type the correct expiry date (DD-MM-YYYY, e.g. 31-12-2026):"
        )
        return UPLOAD_AWAITING_DATE_EDIT

    # Confirmed — save to DB
    return await _save_document(query, context, update.effective_user.id)


async def upload_got_manual_date(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    date_str = update.message.text.strip()
    d = parse_user_date(date_str)
    if not d or d <= date.today():
        await update.message.reply_text(
            "That doesn't look like a valid future date.\n"
            "Please use DD-MM-YYYY format (e.g. 31-12-2026), or /cancel."
        )
        return UPLOAD_AWAITING_DATE_EDIT

    context.user_data["upl_expiry_date"] = d.isoformat()
    context.user_data["upl_ctx_snippet"] = "(manually entered)"
    return await _save_document(update.message, context, update.effective_user.id)


async def _save_document(reply_target, context, uid: int) -> int:
    doc_type = context.user_data["upl_doc_type"]
    rel_path = context.user_data["upl_file_path"]
    expiry_iso = context.user_data["upl_expiry_date"]
    ctx_snippet = context.user_data.get("upl_ctx_snippet", "")
    low_conf = context.user_data.get("upl_low_conf", False)
    confidence = "low" if low_conf else "high"

    # Archive previous active doc of same type (UNIQUE index enforces one active per user/type)
    existing = db.get_active_document(uid, doc_type)
    if existing:
        db.archive_document(existing["id"])
        old_rel = existing.get("local_file_path", "")
        if old_rel:
            try:
                storage.archive_file(uid, old_rel)
            except Exception as exc:
                logger.warning("Could not archive old file %s: %s", old_rel, exc)

    db.insert_document(
        telegram_id=uid,
        doc_type=doc_type,
        local_file_path=rel_path,
        extracted_expiry_date=expiry_iso,
        extraction_context=ctx_snippet,
        extraction_confidence=confidence,
    )

    label = DOC_TYPE_LABELS.get(doc_type, doc_type)
    expiry_date = date.fromisoformat(expiry_iso)
    days_left = (expiry_date - date.today()).days

    msg = (
        f"✅ {label} saved.\n"
        f"Expiry: {format_date(expiry_date)} ({days_left} days away)"
    )
    if days_left < 30:
        msg += "\n\n⚠️ This expires in less than 30 days — please renew soon."

    db.log_action(uid, "upload_document", detail=f"{doc_type} expires {expiry_iso}",
                  module_name=MODULE)

    if hasattr(reply_target, "edit_message_text"):
        await reply_target.edit_message_text(msg)
    else:
        await reply_target.reply_text(msg)

    context.user_data.clear()
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# /myexpiry
# --------------------------------------------------------------------------- #

async def cmd_myexpiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update):
        return
    if not flags.is_module_enabled(MODULE, update.effective_user.id):
        await update.message.reply_text(flags.module_maintenance_message())
        return

    uid = update.effective_user.id
    docs = db.list_user_documents(uid)

    if not docs:
        await update.message.reply_text(
            "You have no tracked documents yet.\n"
            "Use /upload to track a document's expiry date."
        )
        return

    today = date.today()
    lines = ["Your tracked documents:\n"]
    for doc in docs:
        label = DOC_TYPE_LABELS.get(doc["doc_type"], doc["doc_type"])
        if doc["extracted_expiry_date"]:
            expiry = date.fromisoformat(doc["extracted_expiry_date"])
            days_left = (expiry - today).days
            if days_left < 0:
                urgency = " ❌ EXPIRED"
            elif days_left <= 7:
                urgency = f" 🔴 {days_left} days"
            elif days_left <= 30:
                urgency = f" 🟠 {days_left} days"
            elif days_left <= 60:
                urgency = f" 🟡 {days_left} days"
            else:
                urgency = f" ✅ {days_left} days"
            lines.append(f"📋 {label}\n   Expires: {format_date(expiry)}{urgency}")
        else:
            lines.append(f"📋 {label}\n   Expiry date not set")

    await update.message.reply_text("\n\n".join(lines))


# --------------------------------------------------------------------------- #
# /birthday DD-MM
# --------------------------------------------------------------------------- #

async def cmd_birthday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update):
        return

    uid = update.effective_user.id
    args = context.args or []
    if not args:
        user = db.get_user(uid)
        birthday = user["birthday"] if user else None
        if birthday:
            await update.message.reply_text(f"Your birthday is set to {birthday}.")
        else:
            await update.message.reply_text(
                "Your birthday is not set.\n"
                "Set it with /birthday DD-MM (e.g. /birthday 25-12 for 25 December)."
            )
        return

    raw = args[0].strip()
    try:
        parts = raw.split("-")
        assert len(parts) == 2
        day, month = int(parts[0]), int(parts[1])
        assert 1 <= day <= 31 and 1 <= month <= 12
        birthday_str = f"{day:02d}-{month:02d}"
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "Birthday should be in DD-MM format (e.g. /birthday 25-12 for 25 December)."
        )
        return

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE users SET birthday = ? WHERE telegram_id = ?",
            (birthday_str, uid),
        )

    db.log_action(uid, "set_birthday", detail=birthday_str, module_name=MODULE)
    await update.message.reply_text(f"Birthday saved: {birthday_str}.")


# --------------------------------------------------------------------------- #
# Register
# --------------------------------------------------------------------------- #

def register_handlers(application):
    upload_conv = ConversationHandler(
        entry_points=[CommandHandler("upload", cmd_upload_start)],
        states={
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, conv_timeout)],
            UPLOAD_AWAITING_TYPE: [
                CallbackQueryHandler(upload_got_type, pattern=r"^upl_"),
            ],
            UPLOAD_AWAITING_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, upload_got_file),
            ],
            UPLOAD_AWAITING_CONFIRM: [
                CallbackQueryHandler(upload_confirm, pattern=r"^exp_"),
            ],
            UPLOAD_AWAITING_DATE_EDIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, upload_got_manual_date),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        name="upload_conv",
        persistent=True,
    )

    application.add_handler(upload_conv)
    application.add_handler(CommandHandler("myexpiry", cmd_myexpiry))
    application.add_handler(CommandHandler("birthday", cmd_birthday))
