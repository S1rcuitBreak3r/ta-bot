"""
T.A. Medical Group — Internal Team Bot
Entry point. Wires Telegram, APScheduler, and all module handlers together.
"""
import asyncio
import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

import core.db as db
import core.flags as flags
from core.config import ADMIN_TELEGRAM_ID, PERSISTENCE_PATH, TELEGRAM_BOT_TOKEN
from core.scheduler import build_scheduler
from core.timeutil import sgt_display, sgt_now

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Suppress httpx INFO logs — they include the bot token in the request URL.
logging.getLogger("httpx").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Auth helpers (used by all handlers)
# --------------------------------------------------------------------------- #

def _is_whitelisted(update: Update) -> bool:
    return bool(update.effective_user) and db.is_whitelisted(update.effective_user.id)


def _is_admin(update: Update) -> bool:
    return bool(update.effective_user) and db.is_admin(update.effective_user.id)


# --------------------------------------------------------------------------- #
# Core commands
# --------------------------------------------------------------------------- #

HELP_TEXT = (
    "T.A. Medical Group — Internal Bot\n\n"
    "Knowledge bank:\n"
    "  /ask <question> — search the knowledge base\n"
    "  /tosp <query> — TOSP fee lookup\n"
    "  /antibiotics <query> — antibiotic guidance\n\n"
    "Your documents:\n"
    "  /upload — track an expiry document\n"
    "  /myexpiry — view your tracked documents\n"
    "  /birthday DD-MM — save your birthday\n\n"
    "Admin commands:\n"
    "  /adduser, /removeuser, /whoami\n"
    "  /updateknowledge, /addterm, /resolve, /dismiss\n"
    "  /enablemodule, /disablemodule\n"
    "  /unanswered, /checkhealth, /jobstatus\n\n"
    "For help, contact Dr. Tan."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update):
        db.log_action(
            update.effective_user.id if update.effective_user else None,
            "start_rejected",
            detail="not whitelisted",
            success=False,
        )
        return
    await update.message.reply_text(HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_whitelisted(update):
        return
    await update.message.reply_text(HELP_TEXT)


async def cmd_enablemodule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    args = context.args
    if not args or args[0] not in flags.ALL_MODULES:
        await update.message.reply_text(
            f"Usage: /enablemodule <module> [admin_only|all_users]\n"
            f"Modules: {', '.join(flags.ALL_MODULES)}"
        )
        return
    module_name = args[0]
    target = args[1] if len(args) > 1 and args[1] in ("admin_only", "all_users") else "all_users"
    flags.enable_module(module_name, target)
    db.log_action(update.effective_user.id, "enable_module",
                  detail=f"{module_name} → {target}", module_name=module_name)
    await update.message.reply_text(f"✅ {module_name} set to '{target}'.")


async def cmd_disablemodule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    args = context.args
    if not args or args[0] not in flags.ALL_MODULES:
        await update.message.reply_text(
            f"Usage: /disablemodule <module>\n"
            f"Modules: {', '.join(flags.ALL_MODULES)}"
        )
        return
    module_name = args[0]
    flags.disable_module(module_name)
    db.log_action(update.effective_user.id, "disable_module",
                  detail=module_name, module_name=module_name)
    await update.message.reply_text(f"⛔ {module_name} is now disabled for all non-admin users.")


async def cmd_checkhealth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    lines = [f"Health check — {sgt_display(sgt_now())}"]

    # Module states
    module_states = db.list_module_states()
    lines.append("\nModules:")
    for m in module_states:
        lines.append(f"  {m['module_name']}: {m['enabled_for']}")

    # Scheduler last runs
    job_names = ["weekly_backup", "birthday_check", "expiry_reminder", "stale_queue_reminder"]
    lines.append("\nScheduler (last successful run):")
    for job in job_names:
        last = db.get_last_scheduler_run(job)
        if last:
            lines.append(f"  {job}: {last['ran_at'][:16]}")
        else:
            lines.append(f"  {job}: never run")

    # Pending unanswered queue
    pending = db.list_pending_queue()
    lines.append(f"\nUnanswered queue: {len(pending)} pending item(s)")

    await update.message.reply_text("\n".join(lines))


async def cmd_jobstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    runs = db.list_recent_scheduler_runs(limit=15)
    if not runs:
        await update.message.reply_text("No scheduler runs recorded yet.")
        return
    lines = ["Recent scheduler runs:"]
    for r in runs:
        status = "✅" if r["success"] else "❌"
        lines.append(f"  {status} {r['job_name']} — {r['ran_at'][:16]}")
        if r["error_detail"]:
            lines.append(f"     Error: {r['error_detail'][:80]}")
    await update.message.reply_text("\n".join(lines))


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silently drop messages from non-whitelisted users."""
    if not _is_whitelisted(update):
        return
    # Whitelisted users sending unrecognised text get a gentle nudge.
    await update.message.reply_text("I didn't understand that. Try /help for available commands.")


# --------------------------------------------------------------------------- #
# Application setup
# --------------------------------------------------------------------------- #

async def _post_init(application: Application):
    scheduler = build_scheduler(application.bot)
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    logger.info("Scheduler started with %d jobs.", len(scheduler.get_jobs()))


def main():
    db.init_db()
    db.seed_defaults()
    logger.info("Database initialised.")

    # PicklePersistence keeps ConversationHandler state across Railway restarts.
    persistence = PicklePersistence(filepath=PERSISTENCE_PATH)

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .post_init(_post_init)
        .build()
    )

    # Core commands (always available, whitelist-checked inside each handler)
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("enablemodule", cmd_enablemodule))
    application.add_handler(CommandHandler("disablemodule", cmd_disablemodule))
    application.add_handler(CommandHandler("checkhealth", cmd_checkhealth))
    application.add_handler(CommandHandler("jobstatus", cmd_jobstatus))

    # Module handlers
    from modules.onboarding.handlers import register_handlers as register_onboarding
    register_onboarding(application)

    from modules.knowledge_bank.handlers import register_handlers as register_kb
    register_kb(application)

    from modules.expiry_tracker.handlers import register_handlers as register_expiry
    register_expiry(application)

    from modules.education.handlers import register_handlers as register_education
    register_education(application)

    from modules.form_filler.handlers import register_handlers as register_form
    register_form(application)

    # Fallback for unrecognised input
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_unknown))

    logger.info("Bot polling started.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
