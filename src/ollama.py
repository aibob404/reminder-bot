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
  "action":      "add|done|delete|pause|list|set_timezone|unknown",
  "title":       "reminder name for add; search text for done/delete/pause; null for list/set_timezone",
  "due_at":      "ISO 8601 datetime or null",
  "level":       "low|medium|high or null",
  "pause_until": "ISO 8601 datetime or null (for pause action)",
  "timezone":    "IANA timezone string or null (for set_timezone action)",
  "reply":       "short friendly confirmation message"
}

Rules:
- Resolve ALL relative dates to absolute ISO 8601 using today's date provided below
- "tomorrow at 3pm" → calculate the actual date
- "in 2 hours" → now + 2 hours
- "pause for 1 week" → pause_until = now + 7 days
- "pause until December 1" → pause_until = {year}-12-01T00:00:00
- "pause for 5 months" → pause_until = now + 5 months
- For set_timezone: convert city/country names to IANA (e.g. "Moscow" → "Europe/Moscow", "New York" → "America/New_York")
- If no due_at mentioned for add, leave it null
- title is null only for "list" and "set_timezone"
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

        data = _extract_json(raw)
        return Intent(
            action     = data.get("action", "unknown"),
            title      = data.get("title"),
            due_at     = data.get("due_at"),
            level      = data.get("level") or "medium",
            pause_until= data.get("pause_until"),
            timezone   = data.get("timezone"),
            reply      = data.get("reply"),
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
