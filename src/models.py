from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class User:
    id: int
    chat_id: int
    username: Optional[str]
    timezone: str
    created_at: datetime


@dataclass
class Reminder:
    id: int
    user_id: int
    title: str
    level: str          # low | medium | high
    status: str         # active | done | deleted | paused
    due_at: Optional[datetime]
    next_notify_at: Optional[datetime]
    last_notified: Optional[datetime]
    notify_count: int
    paused_until: Optional[datetime]
    created_at: datetime
    updated_at: datetime


@dataclass
class Intent:
    action: str                         # add | done | delete | pause | list | set_timezone | unknown
    title: Optional[str] = None
    due_at: Optional[str] = None        # ISO 8601 string from Ollama
    level: Optional[str] = None         # low | medium | high
    pause_until: Optional[str] = None   # ISO 8601 string from Ollama
    timezone: Optional[str] = None      # IANA timezone string
    reply: Optional[str] = None


# Re-notify intervals by level (seconds)
LEVEL_INTERVALS = {
    "high":   3600,        # 1 hour
    "medium": 86400,       # 24 hours
    "low":    604800,      # 7 days
}

NOTIFY_WINDOW_START = 8   # 8:00 AM
NOTIFY_WINDOW_END   = 21  # 9:00 PM
