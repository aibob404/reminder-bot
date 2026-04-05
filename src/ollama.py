import json
import os
import re
import logging
from datetime import datetime, timezone

import httpx

from .models import Intent

logger = logging.getLogger(__name__)

OLLAMA_URL   = os.environ.get("OLLAMA_URL",   "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")

_SYSTEM = """\
You are a reminder management assistant. Parse the user message and return ONLY a valid JSON object — no extra text, no markdown.

Actions:
- "add"          : user wants to create a reminder
- "done"         : user wants to mark a reminder as completed
- "delete"       : user wants to delete a reminder
- "pause"        : user wants to pause a reminder for a custom duration
- "list"         : user wants to see their reminders
- "set_timezone" : user wants to set their timezone
- "unknown"      : cannot understand

Reminder levels (extract from message, default "medium"):
- "high"   : urgent, hourly re-notify (keywords: urgent, asap, every hour, critical)
- "medium" : normal, daily re-notify (default)
- "low"    : low priority, weekly re-notify (keywords: low, sometime, weekly, whenever)

JSON format:
{
  "action":        "add|done|delete|pause|list|set_timezone|unknown",
  "title":         "reminder name for add; search text for done/delete/pause; null for list/set_timezone/reminder_num",
  "due_at":        "ISO 8601 datetime or null",
  "level":         "low|medium|high or null",
  "pause_until":   "ISO 8601 datetime or null (for pause action)",
  "timezone":      "IANA timezone string or null (for set_timezone action)",
  "reminder_num":  "integer if user references a reminder by #N (e.g. '#3 done', 'delete #5'), else null",
  "language":      "detected language of user message: 'ru' for Russian, 'en' for English",
  "reply":         "short friendly confirmation message IN THE SAME LANGUAGE as the user message"
}

Rules:
- Resolve ALL relative dates to absolute ISO 8601 using today's date provided below
- "tomorrow at 3pm" → calculate the actual date
- "in 2 hours" → now + 2 hours
- "pause for 1 week" → pause_until = now + 7 days
- "pause until December 1" → pause_until = {year}-12-01T00:00:00
- "pause for 5 months" → pause_until = now + 5 months
- For set_timezone: convert city/country names to IANA (e.g. "Moscow" → "Europe/Moscow", "New York" → "America/New_York")
- If user says "#3 done", "#2 delete", "pause #4 for 2 weeks": set reminder_num and action, title can be null
- If no due_at mentioned for add, leave it null
- title is null only for "list", "set_timezone", and when reminder_num is set
- ALWAYS return a complete JSON object with ALL fields, never return empty {}
- Detect language from user message and set "language" field accordingly
- Write "reply" in the SAME language as the user message
- Understand Russian date expressions: "завтра" (tomorrow), "через час" (in 1 hour), "через неделю" (in 1 week), "послезавтра" (day after tomorrow), "в 15:00" (at 3pm)

Examples:
User: "list" → {"action":"list","title":null,"due_at":null,"level":null,"pause_until":null,"timezone":null,"reminder_num":null,"language":"en","reply":"Here are your reminders!"}
User: "покажи напоминания" → {"action":"list","title":null,"due_at":null,"level":null,"pause_until":null,"timezone":null,"reminder_num":null,"language":"ru","reply":"Вот ваши напоминания!"}
User: "что у меня есть" → {"action":"list","title":null,"due_at":null,"level":null,"pause_until":null,"timezone":null,"reminder_num":null,"language":"ru","reply":"Вот ваши напоминания!"}
User: "#3 done" → {"action":"done","title":null,"due_at":null,"level":null,"pause_until":null,"timezone":null,"reminder_num":3,"language":"en","reply":"Marked #3 as done!"}
User: "#3 готово" → {"action":"done","title":null,"due_at":null,"level":null,"pause_until":null,"timezone":null,"reminder_num":3,"language":"ru","reply":"Напоминание #3 выполнено!"}
User: "удали #2" → {"action":"delete","title":null,"due_at":null,"level":null,"pause_until":null,"timezone":null,"reminder_num":2,"language":"ru","reply":"Напоминание #2 удалено!"}
User: "remind me to call John tomorrow at 3pm" → {"action":"add","title":"Call John","due_at":"2026-04-06T15:00:00Z","level":"medium","pause_until":null,"timezone":null,"reminder_num":null,"language":"en","reply":"Got it! I'll remind you to call John tomorrow at 3pm."}
User: "напомни позвонить Ивану завтра в 15:00" → {"action":"add","title":"Позвонить Ивану","due_at":"2026-04-06T15:00:00Z","level":"medium","pause_until":null,"timezone":null,"reminder_num":null,"language":"ru","reply":"Хорошо! Напомню позвонить Ивану завтра в 15:00."}
"""


async def parse_intent(user_message: str) -> Intent:
    now = datetime.now(timezone.utc)
    system = _SYSTEM + f"\nCurrent UTC datetime: {now.isoformat()}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model":  OLLAMA_MODEL,
                    "system": system,
                    "prompt": user_message,
                    "stream": False,
                    "format": "json",
                },
            )
            resp.raise_for_status()
            raw = resp.json()["response"]

        logger.debug("Ollama raw response: %s", raw)
        data = _extract_json(raw)
        if not data.get("action"):
            logger.warning("Ollama returned no action for: %r — raw: %s", user_message, raw)
        return Intent(
            action       = data.get("action", "unknown"),
            title        = data.get("title"),
            due_at       = data.get("due_at"),
            level        = data.get("level") or "medium",
            pause_until  = data.get("pause_until"),
            timezone     = data.get("timezone"),
            reminder_num = data.get("reminder_num"),
            language     = data.get("language") or "en",
            reply        = data.get("reply"),
        )

    except Exception as e:
        logger.error("Ollama error: %s", e)
        return Intent(action="unknown", reply="Sorry, I couldn't understand that. Please try again.")


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return json.loads(match.group())
        raise ValueError(f"No JSON found in: {text!r}")
