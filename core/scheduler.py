"""
APScheduler setup. All jobs run in SGT (Asia/Singapore).

Every job:
  1. Writes a scheduler_log row at start (success=0).
  2. Updates it on completion (success=1) or on failure (success=0, error_detail set).
  3. Never crashes the scheduler — exceptions are caught, logged, and reported to admin.

Phase 0 jobs:
  - weekly_backup: Saturday 02:00 SGT — dumps SQLite + knowledge base, sends to admin.
  - birthday_check: daily 08:00 SGT — wishes happy birthday to team members born today.
  - expiry_reminder: daily 09:00 SGT — sends 60/30/7-day warnings for expiring docs.
  - stale_queue_reminder: daily 10:00 SGT — reminds admin of unanswered queue items >24h.
"""
import io
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import core.db as db
from core.config import ADMIN_TELEGRAM_ID, DB_PATH, TIMEZONE
from core.storage import create_backup_archive
from core.timeutil import sgt_today, to_iso

logger = logging.getLogger(__name__)

EXPIRY_THRESHOLDS = [60, 30, 7]  # days before expiry to send a warning


async def _notify_admin(bot, text: str):
    try:
        await bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=text)
    except Exception as exc:
        logger.error("Failed to notify admin: %s", exc)


async def job_weekly_backup(bot):
    log_id = db.log_scheduler_start("weekly_backup")
    try:
        archive_bytes = create_backup_archive(DB_PATH)
        buf = io.BytesIO(archive_bytes)
        buf.name = f"ta_bot_backup_{to_iso(sgt_today())}.tar.gz"
        await bot.send_document(
            chat_id=ADMIN_TELEGRAM_ID,
            document=buf,
            caption=f"Weekly backup — {to_iso(sgt_today())}. Keep this somewhere safe.",
        )
        db.log_scheduler_done(log_id, success=True)
        logger.info("Weekly backup delivered successfully.")
    except Exception as exc:
        db.log_scheduler_done(log_id, success=False, error_detail=str(exc))
        logger.error("Weekly backup failed: %s", exc)
        await _notify_admin(bot, f"⚠️ Weekly backup failed: {exc}")


async def job_birthday_check(bot):
    log_id = db.log_scheduler_start("birthday_check")
    try:
        today = sgt_today()
        today_ddmm = today.strftime("%d-%m")
        users = db.get_users_with_birthday_today(today_ddmm)
        for user in users:
            try:
                await bot.send_message(
                    chat_id=user["telegram_id"],
                    text=f"🎂 Happy Birthday, {user['display_name']}! Wishing you a wonderful day.",
                )
            except Exception as exc:
                logger.warning("Could not send birthday message to %s: %s", user["telegram_id"], exc)
        db.log_scheduler_done(log_id, success=True)
    except Exception as exc:
        db.log_scheduler_done(log_id, success=False, error_detail=str(exc))
        logger.error("Birthday check failed: %s", exc)


async def job_expiry_reminder(bot):
    log_id = db.log_scheduler_start("expiry_reminder")
    try:
        # Use exact-day matching so each reminder fires once per document per threshold.
        # A doc expiring in 5 days only gets the 7-day message, not all three.
        for days in EXPIRY_THRESHOLDS:
            docs = db.get_documents_expiring_on_day(days)
            for doc in docs:
                expiry = doc["extracted_expiry_date"]
                doc_type = doc["doc_type"].replace("_", " ").title()
                try:
                    await bot.send_message(
                        chat_id=doc["telegram_id"],
                        text=(
                            f"⏰ Reminder: Your {doc_type} expires on {expiry} "
                            f"({days} days from today).\n\n"
                            "Please check /myexpiry to review your documents."
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not send expiry reminder to %s: %s", doc["telegram_id"], exc
                    )
        db.log_scheduler_done(log_id, success=True)
    except Exception as exc:
        db.log_scheduler_done(log_id, success=False, error_detail=str(exc))
        logger.error("Expiry reminder job failed: %s", exc)


async def job_stale_queue_reminder(bot):
    log_id = db.log_scheduler_start("stale_queue_reminder")
    try:
        stale = db.get_stale_pending_queue(older_than_hours=24)
        if stale:
            lines = [f"📋 {len(stale)} unanswered question(s) pending for >24 hours:"]
            for entry in stale[:10]:  # cap at 10 to keep message readable
                lines.append(f"  [{entry['id']}] {entry['query_text'][:80]}")
            if len(stale) > 10:
                lines.append(f"  … and {len(stale) - 10} more. Use /unanswered to see all.")
            lines.append("\nUse /resolve <id> or /dismiss <id> to action them.")
            await _notify_admin(bot, "\n".join(lines))
        db.log_scheduler_done(log_id, success=True)
    except Exception as exc:
        db.log_scheduler_done(log_id, success=False, error_detail=str(exc))
        logger.error("Stale queue reminder failed: %s", exc)


def build_scheduler(bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    scheduler.add_job(
        job_weekly_backup, CronTrigger(day_of_week="sat", hour=2, minute=0, timezone=TIMEZONE),
        args=[bot], id="weekly_backup", replace_existing=True,
    )
    scheduler.add_job(
        job_birthday_check, CronTrigger(hour=8, minute=0, timezone=TIMEZONE),
        args=[bot], id="birthday_check", replace_existing=True,
    )
    scheduler.add_job(
        job_expiry_reminder, CronTrigger(hour=9, minute=0, timezone=TIMEZONE),
        args=[bot], id="expiry_reminder", replace_existing=True,
    )
    scheduler.add_job(
        job_stale_queue_reminder, CronTrigger(hour=10, minute=0, timezone=TIMEZONE),
        args=[bot], id="stale_queue_reminder", replace_existing=True,
    )

    return scheduler
