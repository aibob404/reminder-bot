import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
from telegram import Update
from telegram.ext import ContextTypes

from . import db
from .models import Intent, Reminder, NOTIFY_WINDOW_START
from .ollama import parse_intent

logger = logging.getLogger(__name__)

LEVEL_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


# ── Session state ─────────────────────────────────────────────────────────────

@dataclass
class Session:
    state: str = "idle"                          # idle | awaiting_selection
    pending_action: Optional[str] = None
    pending_intent: Optional[Intent] = None
    pending_reminders: list[Reminder] = field(default_factory=list)


_sessions: dict[int, Session] = {}


def _session(chat_id: int) -> Session:
    if chat_id not in _sessions:
        _sessions[chat_id] = Session()
    return _sessions[chat_id]


# ── Main handler ──────────────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id  = update.effective_chat.id
    username = update.effective_user.username
    text     = update.message.text.strip()
    session  = _session(chat_id)

    user = await db.get_or_create_user(chat_id, username)

    if session.state == "awaiting_selection":
        await _handle_selection(update, session, text, user)
        return

    await update.message.chat.send_action("typing")

    intent = await parse_intent(text)

    match intent.action:
        case "add":
            await _do_add(update, user, intent)
        case "list":
            await _do_list(update, user)
        case "set_timezone":
            await _do_set_timezone(update, user, intent)
        case "done" | "delete" | "pause":
            await _do_modify(update, session, user, intent)
        case _:
            await update.message.reply_text(
                intent.reply or
                "I didn't understand. You can:\n"
                "• Add: \"Remind me to call John tomorrow at 3pm\"\n"
                "• List: \"Show my reminders\"\n"
                "• Complete: \"Done with #3\" or \"Done with the John call\"\n"
                "• Delete: \"Delete #2\" or \"Delete the dentist reminder\"\n"
                "• Pause: \"Pause #1 for 2 weeks\"\n"
                "• Timezone: \"Set my timezone to Moscow\""
            )


# ── Actions ───────────────────────────────────────────────────────────────────

async def _do_add(update, user, intent) -> None:
    due_at      = _parse_dt(intent.due_at)
    level       = intent.level or "medium"
    next_notify = _calc_first_notify(due_at, user.timezone)

    reminder = await db.add_reminder(
        user_id        = user.id,
        title          = intent.title,
        level          = level,
        due_at         = due_at,
        next_notify_at = next_notify,
    )

    due_str = f"\n📅 {_fmt_dt(due_at, user.timezone)}" if due_at else ""
    await update.message.reply_text(
        f"✅ Reminder #{reminder.user_seq} added!\n"
        f"{LEVEL_EMOJI[level]} [{level.upper()}] {reminder.title}"
        f"{due_str}"
    )


async def _do_list(update, user) -> None:
    reminders = await db.get_active_reminders(user.id)
    if not reminders:
        await update.message.reply_text("You have no active reminders.")
        return

    lines = []
    for r in reminders:
        due    = f"\n   📅 Due: {_fmt_dt(r.due_at, user.timezone)}" if r.due_at else ""
        next_n = f"\n   🔔 Next: {_fmt_dt(r.next_notify_at, user.timezone)}" if r.next_notify_at else ""
        pause  = f"\n   ⏸ Until: {_fmt_dt(r.paused_until, user.timezone)}" if r.paused_until else ""
        lines.append(f"#{r.user_seq} {LEVEL_EMOJI[r.level]} {r.title}{due}{next_n}{pause}")

    await update.message.reply_text("Your reminders:\n\n" + "\n\n".join(lines))


async def _do_set_timezone(update, user, intent) -> None:
    tz_name = intent.timezone
    if not tz_name:
        await update.message.reply_text("I couldn't identify that timezone. Try: \"Set timezone to Europe/Moscow\"")
        return

    try:
        pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        await update.message.reply_text(
            f"Unknown timezone: {tz_name!r}\n"
            "Use IANA format, e.g.: Europe/Moscow, America/New_York, Asia/Tokyo"
        )
        return

    await db.update_timezone(user.chat_id, tz_name)
    now_local = datetime.now(pytz.timezone(tz_name)).strftime("%H:%M")
    await update.message.reply_text(
        f"✅ Timezone set to {tz_name}\n"
        f"Your current local time: {now_local}"
    )


async def _do_modify(update, session, user, intent) -> None:
    # Direct lookup by #N if Ollama extracted a number
    if intent.reminder_num is not None:
        reminder = await db.get_reminder_by_seq(user.id, intent.reminder_num)
        if not reminder:
            await update.message.reply_text(f"No active reminder found with #{intent.reminder_num}.")
            return
        await _execute_action(update, intent.action, reminder, user, intent)
        return

    # Search by title
    reminders = await db.get_active_reminders(user.id, search=intent.title)

    if not reminders:
        await update.message.reply_text(
            f"No active reminders found matching \"{intent.title}\"."
        )
        return

    if len(reminders) == 1:
        await _execute_action(update, intent.action, reminders[0], user, intent)
        return

    # Multiple matches — show list and ask user to pick by #N
    session.state             = "awaiting_selection"
    session.pending_action    = intent.action
    session.pending_intent    = intent
    session.pending_reminders = reminders

    lines = [f"#{r.user_seq} {LEVEL_EMOJI[r.level]} {r.title}" for r in reminders]
    await update.message.reply_text(
        "Which reminder do you mean?\n\n" + "\n".join(lines) +
        "\n\nReply with the # number (e.g. #2)."
    )


async def _handle_selection(update, session, text, user) -> None:
    # Accept "#3" or just "3"
    match = re.match(r"^#?(\d+)$", text.strip())
    if not match:
        session.state = "idle"
        await update.message.reply_text("Cancelled.")
        return

    seq = int(match.group(1))
    reminder = next((r for r in session.pending_reminders if r.user_seq == seq), None)

    # Also accept list-position index (1, 2, 3...) as fallback
    if reminder is None and 1 <= seq <= len(session.pending_reminders):
        reminder = session.pending_reminders[seq - 1]

    if reminder is None:
        session.state = "idle"
        await update.message.reply_text("Invalid number. Cancelled.")
        return

    action = session.pending_action
    intent = session.pending_intent or Intent(action=action)
    session.state = "idle"
    session.pending_action = None
    session.pending_intent = None
    session.pending_reminders = []

    await _execute_action(update, action, reminder, user, intent)


async def _execute_action(update, action: str, reminder: Reminder, user, intent) -> None:
    match action:
        case "done":
            await db.set_reminder_done(reminder.id)
            await update.message.reply_text(f"✅ Done: #{reminder.user_seq} \"{reminder.title}\"")

        case "delete":
            await db.set_reminder_deleted(reminder.id)
            await update.message.reply_text(f"🗑 Deleted: #{reminder.user_seq} \"{reminder.title}\"")

        case "pause":
            paused_until = _parse_dt(intent.pause_until)
            if not paused_until:
                await update.message.reply_text(
                    f"How long should I pause #{reminder.user_seq}?\n"
                    "E.g. \"pause for 2 weeks\" or \"pause until December 1\""
                )
                return
            await db.set_reminder_paused(reminder.id, paused_until)
            await update.message.reply_text(
                f"⏸ Paused #{reminder.user_seq} \"{reminder.title}\" until "
                f"{_fmt_dt(paused_until, user.timezone)}"
            )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _fmt_dt(dt: Optional[datetime], tz_name: str) -> str:
    if not dt:
        return "—"
    try:
        tz  = pytz.timezone(tz_name)
        loc = dt.astimezone(tz)
        return loc.strftime("%d %b %Y %H:%M %Z")
    except Exception:
        return dt.strftime("%d %b %Y %H:%M UTC")


def _calc_first_notify(due_at: Optional[datetime], tz_name: str) -> Optional[datetime]:
    if due_at:
        return due_at
    try:
        tz       = pytz.timezone(tz_name)
        now      = datetime.now(tz)
        next_8am = now.replace(hour=NOTIFY_WINDOW_START, minute=0, second=0, microsecond=0)
        if now.hour >= NOTIFY_WINDOW_START:
            next_8am += timedelta(days=1)
        return next_8am.astimezone(timezone.utc)
    except Exception:
        return None
