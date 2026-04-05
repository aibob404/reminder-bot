import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
from telegram.ext import Application

from . import db
from .models import LEVEL_INTERVALS, NOTIFY_WINDOW_START, NOTIFY_WINDOW_END

logger = logging.getLogger(__name__)


async def check_due_reminders(app: Application) -> None:
    """Send notifications for due reminders (runs every 5 min)."""
    try:
        due = await db.get_due_reminders()
    except Exception as e:
        logger.error("check_due_reminders DB error: %s", e)
        return

    for reminder, user in due:
        try:
            tz       = pytz.timezone(user.timezone)
            now_local = datetime.now(tz)

            if _in_window(now_local):
                await app.bot.send_message(
                    chat_id = user.chat_id,
                    text    = (
                        f"🔔 Reminder!\n"
                        f"{_level_emoji(reminder.level)} [{reminder.level.upper()}] {reminder.title}"
                    ),
                )
                next_notify = _calc_next(reminder.level, now_local, tz)
            else:
                # Outside window — push to next 8am
                next_notify = _next_window_open(now_local, tz)

            await db.update_next_notify(reminder.id, next_notify)

        except Exception as e:
            logger.error("Failed to process reminder %d: %s", reminder.id, e)


async def check_expired_pauses(app: Application) -> None:
    """Re-activate reminders whose pause period has ended (runs every 15 min)."""
    try:
        expired = await db.get_expired_pauses()
    except Exception as e:
        logger.error("check_expired_pauses DB error: %s", e)
        return

    for reminder, user in expired:
        try:
            tz        = pytz.timezone(user.timezone)
            now_local = datetime.now(tz)
            next_notify = _next_window_open(now_local, tz) if not _in_window(now_local) \
                          else datetime.now(timezone.utc)

            await db.reactivate_reminder(reminder.id, next_notify)

            await app.bot.send_message(
                chat_id = user.chat_id,
                text    = f"▶️ Reminder resumed: \"{reminder.title}\"",
            )
        except Exception as e:
            logger.error("Failed to reactivate reminder %d: %s", reminder.id, e)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _in_window(local_dt: datetime) -> bool:
    return NOTIFY_WINDOW_START <= local_dt.hour < NOTIFY_WINDOW_END


def _level_emoji(level: str) -> str:
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(level, "⏰")


def _calc_next(level: str, now_local: datetime, tz: pytz.BaseTzInfo) -> Optional[datetime]:
    """Calculate next notification time based on level, respecting the window."""
    interval = timedelta(seconds=LEVEL_INTERVALS.get(level, LEVEL_INTERVALS["medium"]))
    candidate = now_local + interval

    if not _in_window(candidate):
        candidate = _next_window_open(candidate, tz)

    return candidate.astimezone(timezone.utc)


def _next_window_open(from_dt: datetime, tz: pytz.BaseTzInfo) -> datetime:
    """Returns the next 8:00 AM in the user's timezone."""
    local = from_dt.astimezone(tz)
    next_open = local.replace(hour=NOTIFY_WINDOW_START, minute=0, second=0, microsecond=0)
    if local.hour >= NOTIFY_WINDOW_START:
        next_open += timedelta(days=1)
    return next_open.astimezone(timezone.utc)
