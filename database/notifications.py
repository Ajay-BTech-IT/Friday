"""In-app inbox (PC). FCM/push for phones is held for the mobile phase."""
from __future__ import annotations

from uuid import UUID

from database import db
from timeutil import local_today, tz


async def add(title: str, body: str | None = None, kind: str = "reminder", urgent: bool = False, reminder_id: UUID | None = None) -> dict:
    return await db.fetchrow(
        "INSERT INTO notifications(user_id,kind,title,body,reminder_id,urgent) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
        db.owner_id(), kind, title, body, reminder_id, urgent,
    )


async def unseen(limit: int = 50) -> list[dict]:
    return await db.fetch(
        "SELECT * FROM notifications WHERE user_id=%s AND seen_at IS NULL ORDER BY created_at LIMIT %s",
        db.owner_id(), limit,
    )


async def recent(limit: int = 30) -> list[dict]:
    return await db.fetch(
        "SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT %s", db.owner_id(), limit
    )


async def mark_seen(ids: list[UUID] | None = None) -> int:
    if ids:
        return await db.execute(
            "UPDATE notifications SET seen_at=now() WHERE user_id=%s AND id = ANY(%s) AND seen_at IS NULL", db.owner_id(), ids
        )
    return await db.execute("UPDATE notifications SET seen_at=now() WHERE user_id=%s AND seen_at IS NULL", db.owner_id())


async def count_today(tzname: str, kinds: tuple[str, ...] = ("reminder", "alert", "email")) -> int:
    """Non-urgent notifications already sent today (owner-local day). Used for the daily cap."""
    return await db.fetchval(
        "SELECT count(*) FROM notifications WHERE user_id=%s AND kind = ANY(%s) AND NOT urgent "
        "AND (created_at AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date",
        db.owner_id(), list(kinds), tzname, tzname,
    )
