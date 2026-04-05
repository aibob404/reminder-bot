import os
import asyncpg
from datetime import datetime, timezone
from typing import Optional
from .models import User, Reminder

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=os.environ["PGHOST"],
            port=int(os.environ.get("PGPORT", 5432)),
            database=os.environ["PGDATABASE"],
            user=os.environ["PGUSER"],
            password=os.environ["PGPASSWORD"],
            min_size=1,
            max_size=5,
        )
    return _pool


async def init_schema():
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Base tables
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         SERIAL PRIMARY KEY,
                chat_id    BIGINT UNIQUE NOT NULL,
                username   TEXT,
                timezone   TEXT NOT NULL DEFAULT 'UTC',
                language   TEXT NOT NULL DEFAULT 'en',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'en'
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id             SERIAL PRIMARY KEY,
                user_id        INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                user_seq       INT NOT NULL DEFAULT 0,
                title          TEXT NOT NULL,
                level          VARCHAR(10) NOT NULL DEFAULT 'medium',
                status         VARCHAR(20) NOT NULL DEFAULT 'active',
                due_at         TIMESTAMPTZ,
                next_notify_at TIMESTAMPTZ,
                last_notified  TIMESTAMPTZ,
                notify_count   INT DEFAULT 0,
                paused_until   TIMESTAMPTZ,
                created_at     TIMESTAMPTZ DEFAULT NOW(),
                updated_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Migration: add user_seq if missing (existing deployments)
        await conn.execute("""
            ALTER TABLE reminders ADD COLUMN IF NOT EXISTS user_seq INT NOT NULL DEFAULT 0
        """)
        # Backfill user_seq for any rows still at 0
        await conn.execute("""
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at, id) AS seq
                FROM reminders WHERE user_seq = 0
            )
            UPDATE reminders r SET user_seq = ranked.seq
            FROM ranked WHERE r.id = ranked.id
        """)
        # Indexes (after column is guaranteed to exist)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminders_user_status
                ON reminders(user_id, status)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminders_next_notify
                ON reminders(next_notify_at)
                WHERE status = 'active'
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_user_seq
                ON reminders(user_id, user_seq)
        """)


# ── Users ──────────────────────────────────────────────────────────────────────

async def get_or_create_user(chat_id: int, username: Optional[str]) -> User:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE chat_id = $1", chat_id
        )
        if not row:
            row = await conn.fetchrow(
                """INSERT INTO users (chat_id, username)
                   VALUES ($1, $2) RETURNING *""",
                chat_id, username,
            )
        return _user(row)


async def update_language(chat_id: int, lang: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET language = $1 WHERE chat_id = $2", lang, chat_id
        )


async def update_timezone(chat_id: int, tz: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET timezone = $1 WHERE chat_id = $2", tz, chat_id
        )


async def get_all_users() -> list[User]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users")
        return [_user(r) for r in rows]


# ── Reminders ─────────────────────────────────────────────────────────────────

async def add_reminder(
    user_id: int,
    title: str,
    level: str,
    due_at: Optional[datetime],
    next_notify_at: Optional[datetime],
) -> Reminder:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO reminders (user_id, user_seq, title, level, due_at, next_notify_at)
               VALUES (
                 $1,
                 (SELECT COALESCE(MAX(user_seq), 0) + 1 FROM reminders WHERE user_id = $1),
                 $2, $3, $4, $5
               ) RETURNING *""",
            user_id, title, level, due_at, next_notify_at,
        )
        return _reminder(row)


async def get_reminder_by_seq(user_id: int, user_seq: int) -> Optional[Reminder]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM reminders
               WHERE user_id = $1 AND user_seq = $2
               AND status NOT IN ('deleted', 'done')""",
            user_id, user_seq,
        )
        return _reminder(row) if row else None


async def get_active_reminders(user_id: int, search: Optional[str] = None) -> list[Reminder]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if search:
            rows = await conn.fetch(
                """SELECT * FROM reminders
                   WHERE user_id = $1 AND status NOT IN ('deleted', 'done')
                   AND title ILIKE $2
                   ORDER BY user_seq ASC""",
                user_id, f"%{search}%",
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM reminders
                   WHERE user_id = $1 AND status NOT IN ('deleted', 'done')
                   ORDER BY user_seq ASC""",
                user_id,
            )
        return [_reminder(r) for r in rows]


async def set_reminder_done(reminder_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE reminders
               SET status = 'done', next_notify_at = NULL, updated_at = NOW()
               WHERE id = $1""",
            reminder_id,
        )


async def set_reminder_deleted(reminder_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE reminders
               SET status = 'deleted', next_notify_at = NULL, updated_at = NOW()
               WHERE id = $1""",
            reminder_id,
        )


async def set_reminder_paused(reminder_id: int, paused_until: datetime) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE reminders
               SET status = 'paused', paused_until = $1, next_notify_at = NULL, updated_at = NOW()
               WHERE id = $2""",
            paused_until, reminder_id,
        )


async def get_due_reminders() -> list[tuple[Reminder, User]]:
    """All active reminders whose next_notify_at is in the past."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT r.*, u.chat_id AS u_chat_id, u.timezone AS u_timezone, u.language AS u_language
               FROM reminders r
               JOIN users u ON u.id = r.user_id
               WHERE r.status = 'active'
                 AND r.next_notify_at IS NOT NULL
                 AND r.next_notify_at <= NOW()"""
        )
        result = []
        for row in rows:
            reminder = _reminder(row)
            user = User(
                id=row["user_id"],
                chat_id=row["u_chat_id"],
                username=None,
                timezone=row["u_timezone"],
                language=row["u_language"],
                created_at=datetime.now(timezone.utc),
            )
            result.append((reminder, user))
        return result


async def update_next_notify(reminder_id: int, next_notify_at: Optional[datetime]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE reminders
               SET next_notify_at = $1,
                   last_notified = NOW(),
                   notify_count = notify_count + 1,
                   updated_at = NOW()
               WHERE id = $2""",
            next_notify_at, reminder_id,
        )


async def get_expired_pauses() -> list[tuple[Reminder, User]]:
    """Paused reminders whose paused_until has passed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT r.*, u.chat_id AS u_chat_id, u.timezone AS u_timezone, u.language AS u_language
               FROM reminders r
               JOIN users u ON u.id = r.user_id
               WHERE r.status = 'paused'
                 AND r.paused_until IS NOT NULL
                 AND r.paused_until <= NOW()"""
        )
        result = []
        for row in rows:
            reminder = _reminder(row)
            user = User(
                id=row["user_id"],
                chat_id=row["u_chat_id"],
                username=None,
                timezone=row["u_timezone"],
                language=row["u_language"],
                created_at=datetime.now(timezone.utc),
            )
            result.append((reminder, user))
        return result


async def reactivate_reminder(reminder_id: int, next_notify_at: datetime) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE reminders
               SET status = 'active', paused_until = NULL,
                   next_notify_at = $1, updated_at = NOW()
               WHERE id = $2""",
            next_notify_at, reminder_id,
        )


# ── Row mappers ───────────────────────────────────────────────────────────────

def _user(row) -> User:
    return User(
        id=row["id"],
        chat_id=row["chat_id"],
        username=row["username"],
        timezone=row["timezone"],
        language=row["language"],
        created_at=row["created_at"],
    )


def _reminder(row) -> Reminder:
    return Reminder(
        id=row["id"],
        user_id=row["user_id"],
        user_seq=row["user_seq"],
        title=row["title"],
        level=row["level"],
        status=row["status"],
        due_at=row["due_at"],
        next_notify_at=row["next_notify_at"],
        last_notified=row["last_notified"],
        notify_count=row["notify_count"],
        paused_until=row["paused_until"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
