"""
Onboarding module — /adduser, /removeuser, /whoami.

All multi-step flows use ConversationHandler with /cancel at every step.
State is persisted via PicklePersistence so Railway restarts don't strand users mid-flow.
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

logger = logging.getLogger(__name__)

MODULE = "onboarding"

# ConversationHandler state constants
(
    ADDUSER_AWAITING_ID,
    ADDUSER_AWAITING_NAME,
    ADDUSER_AWAITING_BIRTHDAY,
    REMOVEUSER_AWAITING_ID,
    REMOVEUSER_AWAITING_CONFIRM,
) = range(5)


def _is_admin(update: Update) -> bool:
    return bool(update.effective_user) and db.is_admin(update.effective_user.id)


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
            "Your session timed out. Please start the command again if you still need to."
        )
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# /adduser
# --------------------------------------------------------------------------- #

async def cmd_adduser_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update):
        return ConversationHandler.END
    if not flags.is_module_enabled(MODULE, update.effective_user.id):
        await update.message.reply_text(flags.module_maintenance_message())
        return ConversationHandler.END
    await update.message.reply_text(
        "Adding a new team member.\n\n"
        "Step 1 of 3 — Telegram ID: Ask them to message @userinfobot to get it, "
        "then send the number here.\n\n"
        "Send /cancel at any time to abort."
    )
    return ADDUSER_AWAITING_ID


async def adduser_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        telegram_id = int(text)
    except ValueError:
        await update.message.reply_text(
            "That doesn't look like a valid Telegram ID — it should be a number.\n"
            "Please try again, or /cancel."
        )
        return ADDUSER_AWAITING_ID

    existing = db.get_user(telegram_id)
    if existing and existing["status"] == "active":
        await update.message.reply_text(
            f"{existing['display_name']} (ID: {telegram_id}) is already active in the system.\n"
            "Nothing has changed. /cancel to exit."
        )
        return ConversationHandler.END

    context.user_data["new_telegram_id"] = telegram_id
    await update.message.reply_text(
        f"Telegram ID: {telegram_id} ✓\n\n"
        "Step 2 of 3 — Display name: What should we call them? (e.g. Dr. Jane Lee)"
    )
    return ADDUSER_AWAITING_NAME


async def adduser_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    display_name = update.message.text.strip()
    if not display_name:
        await update.message.reply_text(
            "Name can't be blank. Please try again, or /cancel."
        )
        return ADDUSER_AWAITING_NAME

    context.user_data["new_display_name"] = display_name
    await update.message.reply_text(
        f"Name: {display_name} ✓\n\n"
        "Step 3 of 3 — Birthday (optional): Send it as DD-MM (e.g. 15-03 for 15 March).\n"
        "Send /skip if you don't want to set a birthday now."
    )
    return ADDUSER_AWAITING_BIRTHDAY


async def adduser_got_birthday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    birthday = update.message.text.strip()
    try:
        parts = birthday.split("-")
        assert len(parts) == 2
        day, month = int(parts[0]), int(parts[1])
        assert 1 <= day <= 31 and 1 <= month <= 12
    except (ValueError, AssertionError):
        await update.message.reply_text(
            "Birthday should be in DD-MM format (e.g. 15-03).\n"
            "Please try again, or send /skip."
        )
        return ADDUSER_AWAITING_BIRTHDAY
    return await _finish_adduser(update, context, birthday=birthday)


async def adduser_skip_birthday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _finish_adduser(update, context, birthday=None)


async def _finish_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           birthday: str | None) -> int:
    telegram_id: int = context.user_data.get("new_telegram_id")
    display_name: str = context.user_data.get("new_display_name")
    try:
        folder_rel = storage.ensure_user_folder(telegram_id)
        db.add_user(telegram_id, display_name, folder_rel, birthday=birthday)
        db.log_action(
            update.effective_user.id, "add_user",
            detail=f"{display_name} (id={telegram_id})", module_name=MODULE,
        )
    except Exception as exc:
        logger.error("add_user failed for %s: %s", telegram_id, exc)
        await update.message.reply_text(f"Something went wrong adding the user: {exc}")
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text(
        f"✅ {display_name} has been added.\n"
        f"Telegram ID: {telegram_id}\n"
        f"Birthday: {birthday or '(not set)'}"
    )
    try:
        await context.bot.send_message(
            chat_id=telegram_id,
            text=(
                f"Welcome to T.A. Medical Group's team bot, {display_name}!\n\n"
                "You now have access. Send /help to see what's available."
            ),
        )
    except Exception:
        await update.message.reply_text(
            f"(Note: couldn't send a welcome message to {display_name} directly — "
            "they may need to start the bot themselves first.)"
        )

    context.user_data.clear()
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# /removeuser
# --------------------------------------------------------------------------- #

async def cmd_removeuser_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update):
        return ConversationHandler.END
    if not flags.is_module_enabled(MODULE, update.effective_user.id):
        await update.message.reply_text(flags.module_maintenance_message())
        return ConversationHandler.END
    await update.message.reply_text(
        "Removing a team member.\n\n"
        "Step 1 of 2 — Telegram ID: Send the ID of the person to remove.\n\n"
        "Send /cancel to abort."
    )
    return REMOVEUSER_AWAITING_ID


async def removeuser_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        telegram_id = int(text)
    except ValueError:
        await update.message.reply_text(
            "That doesn't look like a valid Telegram ID. Please send a number, or /cancel."
        )
        return REMOVEUSER_AWAITING_ID

    user = db.get_user(telegram_id)
    if not user or user["status"] != "active":
        await update.message.reply_text(
            f"No active user found with Telegram ID {telegram_id}.\n"
            "Check the ID and try again, or /cancel."
        )
        return REMOVEUSER_AWAITING_ID

    context.user_data["remove_telegram_id"] = telegram_id
    context.user_data["remove_display_name"] = user["display_name"]
    await update.message.reply_text(
        f"About to remove: {user['display_name']} (ID: {telegram_id})\n\n"
        "Their access and documents will be archived — nothing is permanently deleted.\n\n"
        "Step 2 of 2: Reply Yes to confirm, or /cancel to abort."
    )
    return REMOVEUSER_AWAITING_CONFIRM


async def removeuser_got_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.strip().lower() != "yes":
        await update.message.reply_text(
            "Reply Yes (exactly) to confirm, or /cancel to abort."
        )
        return REMOVEUSER_AWAITING_CONFIRM

    telegram_id: int = context.user_data.get("remove_telegram_id")
    display_name: str = context.user_data.get("remove_display_name")
    try:
        db.archive_user(telegram_id)
        storage.archive_user_folder(telegram_id)
        db.log_action(
            update.effective_user.id, "remove_user",
            detail=f"{display_name} (id={telegram_id})", module_name=MODULE,
        )
    except Exception as exc:
        logger.error("remove_user failed for %s: %s", telegram_id, exc)
        await update.message.reply_text(f"Something went wrong: {exc}")
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text(
        f"✅ {display_name} has been removed. Their records are safely archived."
    )
    context.user_data.clear()
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# /whoami
# --------------------------------------------------------------------------- #

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return
    if not db.is_whitelisted(uid):
        await update.message.reply_text(
            "You are not registered with this bot. Contact Dr. Tan to be added."
        )
        return
    user = db.get_user(uid)
    if not user or user["status"] == "archived":
        await update.message.reply_text(
            "Your access has been removed. Contact Dr. Tan if you believe this is an error."
        )
        return
    role = "Admin" if user["is_admin"] else "Team member"
    birthday = user["birthday"] or "not set"
    await update.message.reply_text(
        f"Name: {user['display_name']}\n"
        f"Role: {role}\n"
        f"Birthday: {birthday}\n"
        f"Status: Active"
    )


# --------------------------------------------------------------------------- #
# Register
# --------------------------------------------------------------------------- #

def register_handlers(application):
    adduser_conv = ConversationHandler(
        entry_points=[CommandHandler("adduser", cmd_adduser_start)],
        states={
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, conv_timeout)],
            ADDUSER_AWAITING_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adduser_got_id),
            ],
            ADDUSER_AWAITING_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adduser_got_name),
            ],
            ADDUSER_AWAITING_BIRTHDAY: [
                CommandHandler("skip", adduser_skip_birthday),
                MessageHandler(filters.TEXT & ~filters.COMMAND, adduser_got_birthday),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        name="adduser_conv",
        persistent=True,
    )

    removeuser_conv = ConversationHandler(
        entry_points=[CommandHandler("removeuser", cmd_removeuser_start)],
        states={
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, conv_timeout)],
            REMOVEUSER_AWAITING_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, removeuser_got_id),
            ],
            REMOVEUSER_AWAITING_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, removeuser_got_confirm),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        name="removeuser_conv",
        persistent=True,
    )

    application.add_handler(adduser_conv)
    application.add_handler(removeuser_conv)
    application.add_handler(CommandHandler("whoami", cmd_whoami))
