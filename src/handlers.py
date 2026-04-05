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

STRINGS = {
    "en": {
        "no_reminders":   "You have no active reminders.",
        "reminders_header": "Your reminders:",
        "which_reminder": "Which reminder do you mean?\n\n{list}\n\nReply with the # number (e.g. #2).",
        "cancelled":      "Cancelled.",
        "invalid_number": "Invalid number. Cancelled.",
        "added":          "✅ Reminder #{seq} added!\n{emoji} [{level}] {title}{due}",
        "done":           "✅ Done: #{seq} \"{title}\"",
        "deleted":        "🗑 Deleted: #{seq} \"{title}\"",
        "paused":         "⏸ Paused #{seq} \"{title}\" until {date}",
        "pause_ask":      "How long should I pause #{seq}?\nE.g. \"pause for 2 weeks\" or \"pause until December 1\"",
        "tz_not_found":   "I couldn't identify that timezone. Try: \"Set timezone to Europe/Moscow\"",
        "tz_unknown":     "Unknown timezone: {tz}\nUse IANA format, e.g.: Europe/Moscow, America/New_York",
        "tz_set":         "✅ Timezone set to {tz}\nYour current local time: {time}",
        "seq_not_found":  "No active reminder found with #{seq}.",
        "title_not_found":"No active reminders found matching \"{title}\".",
        "notification":   "🔔 Reminder #{seq}\n{emoji} [{level}] {title}",
        "resumed":        "▶️ Reminder #{seq} resumed: \"{title}\"",
        "due_label":      "📅 Due: {date}",
        "next_label":     "🔔 Next: {date}",
        "until_label":    "⏸ Until: {date}",
        "level_high":     "HIGH", "level_medium": "MEDIUM", "level_low": "LOW",
        "help": (
            "I didn't understand. You can:\n"
            "• Add: \"Remind me to call John tomorrow at 3pm\"\n"
            "• List: \"Show my reminders\"\n"
            "• Complete: \"Done with #3\" or \"Done with the John call\"\n"
            "• Delete: \"Delete #2\" or \"Delete the dentist reminder\"\n"
            "• Pause: \"Pause #1 for 2 weeks\"\n"
            "• Timezone: \"Set my timezone to Moscow\""
        ),
    },
    "ru": {
        "no_reminders":   "У вас нет активных напоминаний.",
        "reminders_header": "Ваши напоминания:",
        "which_reminder": "Какое напоминание вы имеете в виду?\n\n{list}\n\nОтветьте номером (например #2).",
        "cancelled":      "Отменено.",
        "invalid_number": "Неверный номер. Отменено.",
        "added":          "✅ Напоминание #{seq} добавлено!\n{emoji} [{level}] {title}{due}",
        "done":           "✅ Выполнено: #{seq} \"{title}\"",
        "deleted":        "🗑 Удалено: #{seq} \"{title}\"",
        "paused":         "⏸ Напоминание #{seq} \"{title}\" отложено до {date}",
        "pause_ask":      "На сколько отложить #{seq}?\nНапример: \"на 2 недели\" или \"до 1 декабря\"",
        "tz_not_found":   "Не удалось определить часовой пояс. Попробуйте: \"Установи часовой пояс Europe/Moscow\"",
        "tz_unknown":     "Неизвестный часовой пояс: {tz}\nИспользуйте формат IANA, например: Europe/Moscow, America/New_York",
        "tz_set":         "✅ Часовой пояс установлен: {tz}\nВаше местное время: {time}",
        "seq_not_found":  "Активное напоминание #{seq} не найдено.",
        "title_not_found":"Напоминания по запросу \"{title}\" не найдены.",
        "notification":   "🔔 Напоминание #{seq}\n{emoji} [{level}] {title}",
        "resumed":        "▶️ Напоминание #{seq} возобновлено: \"{title}\"",
        "due_label":      "📅 Срок: {date}",
        "next_label":     "🔔 Следующее: {date}",
        "until_label":    "⏸ До: {date}",
        "level_high":     "ВЫСОКИЙ", "level_medium": "СРЕДНИЙ", "level_low": "НИЗКИЙ",
        "help": (
            "Я не понял. Вы можете:\n"
            "• Добавить: \"Напомни позвонить Ивану завтра в 15:00\"\n"
            "• Список: \"Покажи напоминания\"\n"
            "• Выполнено: \"#3 готово\" или \"Позвонил Ивану\"\n"
            "• Удалить: \"Удали #2\" или \"Удали напоминание про врача\"\n"
            "• Отложить: \"Отложи #1 на 2 недели\"\n"
            "• Часовой пояс: \"Установи часовой пояс Москва\""
        ),
    },
}


def _t(lang: str, key: str, **kwargs) -> str:
    s = STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))
    return s.format(**kwargs) if kwargs else s


def _level_label(lang: str, level: str) -> str:
    return _t(lang, f"level_{level}")


# ── Session state ─────────────────────────────────────────────────────────────

@dataclass
class Session:
    state: str = "idle"
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

    # Update stored language if detected
    lang = intent.language or "en"
    if lang != user.language:
        await db.update_language(chat_id, lang)
        user.language = lang

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
                intent.reply or _t(lang, "help")
            )


# ── Actions ───────────────────────────────────────────────────────────────────

async def _do_add(update, user, intent) -> None:
    due_at      = _parse_dt(intent.due_at)
    level       = intent.level or "medium"
    lang        = user.language
    next_notify = _calc_first_notify(due_at, user.timezone)

    reminder = await db.add_reminder(
        user_id        = user.id,
        title          = intent.title,
        level          = level,
        due_at         = due_at,
        next_notify_at = next_notify,
    )

    due_str = f"\n{_t(lang, 'due_label', date=_fmt_dt(due_at, user.timezone))}" if due_at else ""
    await update.message.reply_text(
        _t(lang, "added",
           seq=reminder.user_seq,
           emoji=LEVEL_EMOJI[level],
           level=_level_label(lang, level),
           title=reminder.title,
           due=due_str)
    )


async def _do_list(update, user) -> None:
    lang      = user.language
    reminders = await db.get_active_reminders(user.id)
    if not reminders:
        await update.message.reply_text(_t(lang, "no_reminders"))
        return

    lines = []
    for r in reminders:
        due    = f"\n   {_t(lang, 'due_label',   date=_fmt_dt(r.due_at,        user.timezone))}" if r.due_at        else ""
        next_n = f"\n   {_t(lang, 'next_label',  date=_fmt_dt(r.next_notify_at, user.timezone))}" if r.next_notify_at else ""
        pause  = f"\n   {_t(lang, 'until_label', date=_fmt_dt(r.paused_until,  user.timezone))}" if r.paused_until  else ""
        lines.append(f"#{r.user_seq} {LEVEL_EMOJI[r.level]} {r.title}{due}{next_n}{pause}")

    await update.message.reply_text(
        _t(lang, "reminders_header") + "\n\n" + "\n\n".join(lines)
    )


async def _do_set_timezone(update, user, intent) -> None:
    lang    = user.language
    tz_name = intent.timezone
    if not tz_name:
        await update.message.reply_text(_t(lang, "tz_not_found"))
        return

    try:
        pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        await update.message.reply_text(_t(lang, "tz_unknown", tz=tz_name))
        return

    await db.update_timezone(user.chat_id, tz_name)
    now_local = datetime.now(pytz.timezone(tz_name)).strftime("%H:%M")
    await update.message.reply_text(_t(lang, "tz_set", tz=tz_name, time=now_local))


async def _do_modify(update, session, user, intent) -> None:
    lang = user.language

    if intent.reminder_num is not None:
        reminder = await db.get_reminder_by_seq(user.id, intent.reminder_num)
        if not reminder:
            await update.message.reply_text(_t(lang, "seq_not_found", seq=intent.reminder_num))
            return
        await _execute_action(update, intent.action, reminder, user, intent)
        return

    reminders = await db.get_active_reminders(user.id, search=intent.title)
    if not reminders:
        await update.message.reply_text(_t(lang, "title_not_found", title=intent.title))
        return

    if len(reminders) == 1:
        await _execute_action(update, intent.action, reminders[0], user, intent)
        return

    session.state             = "awaiting_selection"
    session.pending_action    = intent.action
    session.pending_intent    = intent
    session.pending_reminders = reminders

    lines = [f"#{r.user_seq} {LEVEL_EMOJI[r.level]} {r.title}" for r in reminders]
    await update.message.reply_text(
        _t(lang, "which_reminder", list="\n".join(lines))
    )


async def _handle_selection(update, session, text, user) -> None:
    lang  = user.language
    match = re.match(r"^#?(\d+)$", text.strip())
    if not match:
        session.state = "idle"
        await update.message.reply_text(_t(lang, "cancelled"))
        return

    seq      = int(match.group(1))
    reminder = next((r for r in session.pending_reminders if r.user_seq == seq), None)
    if reminder is None and 1 <= seq <= len(session.pending_reminders):
        reminder = session.pending_reminders[seq - 1]

    if reminder is None:
        session.state = "idle"
        await update.message.reply_text(_t(lang, "invalid_number"))
        return

    action = session.pending_action
    intent = session.pending_intent or Intent(action=action)
    session.state = "idle"
    session.pending_action = None
    session.pending_intent = None
    session.pending_reminders = []

    await _execute_action(update, action, reminder, user, intent)


async def _execute_action(update, action: str, reminder: Reminder, user, intent) -> None:
    lang = user.language
    match action:
        case "done":
            await db.set_reminder_done(reminder.id)
            await update.message.reply_text(_t(lang, "done", seq=reminder.user_seq, title=reminder.title))

        case "delete":
            await db.set_reminder_deleted(reminder.id)
            await update.message.reply_text(_t(lang, "deleted", seq=reminder.user_seq, title=reminder.title))

        case "pause":
            paused_until = _parse_dt(intent.pause_until)
            if not paused_until:
                await update.message.reply_text(_t(lang, "pause_ask", seq=reminder.user_seq))
                return
            await db.set_reminder_paused(reminder.id, paused_until)
            await update.message.reply_text(
                _t(lang, "paused", seq=reminder.user_seq, title=reminder.title,
                   date=_fmt_dt(paused_until, user.timezone))
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
