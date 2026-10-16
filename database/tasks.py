"""Tasks, routines, reminders and calendar rows (structured state, read through tools)."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID

from database import audit, db
from database.profile import get_profile
from timeutil import at_local, local_now, local_today, tz, utcnow

log = logging.getLogger("tasks")

DUE_HINTS = ("morning", "before_leaving", "before_office", "afternoon", "lunch", "after_office",
             "evening", "tonight", "bedtime", "tomorrow_morning")


# ----------------------------------------------------------------------------
# due_hint -> real time
# ----------------------------------------------------------------------------
def _combine(day: date, t: time, tzname: str) -> datetime:
    return at_local(day, t, tzname)


def resolve_due_hint(hint: str | None, profile: dict, now: datetime | None = None) -> datetime | None:
    """Turn 'after_office' etc. into a UTC datetime using the owner's profile."""
    if not hint:
        return None
    hint = hint.strip().lower().replace(" ", "_")
    tzname = profile.get("timezone") or "Asia/Kolkata"
    now = now or utcnow()
    local = now.astimezone(tz(tzname))
    today = local.date()
    is_workday = local.isoweekday() in (profile.get("work_days") or [1, 2, 3, 4, 5])

    wake = profile.get("wake_time") or time(7, 0)
    sleep = profile.get("sleep_time") or time(23, 0)
    o_start = profile.get("office_start") or time(9, 30)
    o_end = profile.get("office_end") or time(18, 30)
    commute = profile.get("commute_minutes") or 0

    def shift(t: time, minutes: int, d: date) -> datetime:
        base = datetime.combine(d, t)
        return _combine(d, (base + timedelta(minutes=minutes)).time(), tzname)

    def candidate(d: date) -> datetime | None:
        wk = datetime.combine(d, time()).isoweekday() in (profile.get("work_days") or [1, 2, 3, 4, 5])
        if hint == "morning":
            return shift(wake, 30, d)
        if hint == "tomorrow_morning":
            return shift(wake, 30, d)
        if hint in ("before_leaving", "before_office"):
            return shift(o_start, -(commute + 30), d) if wk else shift(wake, 60, d)
        if hint == "lunch":
            return _combine(d, time(13, 0), tzname)
        if hint == "afternoon":
            return _combine(d, time(15, 0), tzname)
        if hint == "after_office":
            return shift(o_end, commute, d) if wk else _combine(d, time(18, 0), tzname)
        if hint == "evening":
            return _combine(d, time(19, 0), tzname)
        if hint == "tonight":
            return _combine(d, time(20, 30), tzname)
        if hint == "bedtime":
            return shift(sleep, -30, d)
        return None

    if hint == "tomorrow_morning":
        return candidate(today + timedelta(days=1))
    c = candidate(today)
    if c is None:
        return None
    if c <= now:
        # Already past today. Late-day hints fire shortly if we are still before bedtime.
        if hint in ("after_office", "evening", "tonight", "afternoon", "lunch"):
            sleep_dt = _combine(today, sleep, tzname)
            if now < sleep_dt - timedelta(minutes=30):
                return now + timedelta(minutes=10)
        return candidate(today + timedelta(days=1))
    return c


# ----------------------------------------------------------------------------
# Reminders
# ----------------------------------------------------------------------------
async def create_reminder(
    message: str,
    fire_at: datetime,
    task_id: UUID | None = None,
    routine_id: UUID | None = None,
    event_id: UUID | None = None,
    dedupe_key: str | None = None,
    channel: str = "push",
) -> UUID | None:
    row = await db.fetchrow(
        "INSERT INTO reminders(user_id,task_id,routine_id,event_id,message,fire_at,channel,dedupe_key) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (user_id, dedupe_key) DO NOTHING RETURNING id",
        db.owner_id(), task_id, routine_id, event_id, message, fire_at, channel, dedupe_key,
    )
    return row["id"] if row else None


async def due_reminders(now: datetime | None = None, limit: int = 50) -> list[dict]:
    return await db.fetch(
        "SELECT r.*, t.priority AS task_priority, t.status AS task_status FROM reminders r "
        "LEFT JOIN tasks t ON t.id = r.task_id "
        "WHERE r.user_id=%s AND r.status='pending' AND r.fire_at <= %s ORDER BY r.fire_at LIMIT %s",
        db.owner_id(), now or utcnow(), limit,
    )


async def mark_reminder(reminder_id: UUID, status: str) -> None:
    await db.execute(
        "UPDATE reminders SET status=%s, sent_at=CASE WHEN %s='sent' THEN now() ELSE sent_at END, attempts=attempts+1 WHERE id=%s",
        status, status, reminder_id,
    )


async def defer_reminder(reminder_id: UUID, new_fire_at: datetime) -> None:
    await db.execute("UPDATE reminders SET fire_at=%s WHERE id=%s", new_fire_at, reminder_id)


async def _sync_task_reminder(task: dict) -> None:
    """Keep exactly one 'due' reminder per task in step with its due_at/status."""
    key = f"task:{task['id']}:due"
    if task["status"] in ("done", "cancelled") or task["due_at"] is None:
        await db.execute(
            "UPDATE reminders SET status='cancelled' WHERE user_id=%s AND task_id=%s AND status='pending'",
            db.owner_id(), task["id"],
        )
        return
    existing = await db.fetchrow("SELECT id, status FROM reminders WHERE user_id=%s AND dedupe_key=%s", db.owner_id(), key)
    if existing:
        await db.execute(
            "UPDATE reminders SET fire_at=%s, status='pending', message=%s WHERE id=%s",
            task["due_at"], f"Reminder: {task['title']}", existing["id"],
        )
    else:
        await create_reminder(f"Reminder: {task['title']}", task["due_at"], task_id=task["id"], dedupe_key=key)


# ----------------------------------------------------------------------------
# Tasks
# ----------------------------------------------------------------------------
async def create_task(
    title: str,
    notes: str | None = None,
    due_hint: str | None = None,
    due_at: datetime | None = None,
    priority: int = 2,
    source: str = "explicit",
    source_message_id: UUID | None = None,
    source_email_id: UUID | None = None,
    calendar_event_id: UUID | None = None,
    actor: str = "agent",
    via_email: bool | None = None,
) -> dict:
    title = title.strip()
    if not title:
        raise ValueError("title is empty")
    dup = await db.fetchrow(
        "SELECT *, similarity(lower(title), lower(%s)) AS sim FROM tasks "
        "WHERE user_id=%s AND status IN ('open','snoozed') AND similarity(lower(title), lower(%s)) > 0.65 "
        "ORDER BY sim DESC LIMIT 1",
        title, db.owner_id(), title,
    )
    if dup:
        return {"task": dup, "duplicate": True}

    profile = await get_profile()
    if due_at is None and due_hint:
        due_at = resolve_due_hint(due_hint, profile)
    row = await db.fetchrow(
        "INSERT INTO tasks(user_id,title,notes,priority,due_at,due_hint,source,source_message_id,source_email_id,calendar_event_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        db.owner_id(), title, notes, max(1, min(4, priority)), due_at, due_hint, source,
        source_message_id, source_email_id, calendar_event_id,
    )
    await _sync_task_reminder(row)
    if via_email is None:  # follow the global default for things the user asked to be reminded about
        from config import settings

        via_email = settings.email_reminders_default and source in ("explicit", "extracted") and bool(settings.reminder_email)
    if via_email and row["due_at"]:
        await db.execute("UPDATE reminders SET channel='email' WHERE user_id=%s AND dedupe_key=%s", db.owner_id(), f"task:{row['id']}:due")
    await audit.record(actor, "insert", "tasks", row["id"], None, row)
    return {"task": row, "duplicate": False}


async def list_tasks(status: str | None = "open", limit: int = 30, due_before: datetime | None = None) -> list[dict]:
    return await db.fetch(
        "SELECT id,title,notes,status,priority,due_at,due_hint,created_at FROM tasks "
        "WHERE user_id=%s AND (%s::text IS NULL OR status=%s) AND (%s::timestamptz IS NULL OR due_at <= %s) "
        "ORDER BY (due_at IS NULL), due_at, priority LIMIT %s",
        db.owner_id(), status, status, due_before, due_before, limit,
    )


async def find_task(query: str, statuses: tuple[str, ...] = ("open", "snoozed")) -> dict | None:
    """Resolve a task by uuid or fuzzy title."""
    try:
        row = await db.fetchrow("SELECT * FROM tasks WHERE id=%s AND user_id=%s", UUID(query), db.owner_id())
        if row:
            return row
    except ValueError:
        pass
    return await db.fetchrow(
        "SELECT *, similarity(lower(title), lower(%s)) AS sim FROM tasks WHERE user_id=%s AND status = ANY(%s) "
        "AND (similarity(lower(title), lower(%s)) > 0.2 OR lower(title) LIKE %s) ORDER BY sim DESC LIMIT 1",
        query, db.owner_id(), list(statuses), query, f"%{query.lower()}%",
    )


async def update_task(task_id: UUID, changes: dict[str, Any], actor: str = "agent") -> dict:
    allowed = {"title", "notes", "status", "priority", "due_at", "due_hint"}
    bad = set(changes) - allowed
    if bad:
        raise ValueError(f"Cannot change {sorted(bad)}")
    before = await db.fetchrow("SELECT * FROM tasks WHERE id=%s AND user_id=%s", task_id, db.owner_id())
    if not before:
        raise ValueError("Task not found")
    changes = dict(changes)
    if changes.get("status") == "done":
        changes["completed_at"] = utcnow()
    if "due_hint" in changes and "due_at" not in changes:
        changes["due_at"] = resolve_due_hint(changes["due_hint"], await get_profile())
    cols = ", ".join(f"{c}=%s" for c in changes)
    row = await db.fetchrow(f"UPDATE tasks SET {cols} WHERE id=%s RETURNING *", *changes.values(), task_id)
    await _sync_task_reminder(row)
    await audit.record(actor, "update", "tasks", task_id, before, row)
    return row


async def snooze_task(task_id: UUID, until: datetime, actor: str = "agent") -> dict:
    return await update_task(task_id, {"status": "snoozed", "due_at": until}, actor)


async def resync_task(task_id: UUID) -> None:
    row = await db.fetchrow("SELECT * FROM tasks WHERE id=%s", task_id)
    if row:
        await _sync_task_reminder(row)


# ----------------------------------------------------------------------------
# Routines
# ----------------------------------------------------------------------------
async def create_routine(
    title: str,
    kind: str = "habit",
    days_of_week: list[int] | None = None,
    time_of_day: time | None = None,
    anchor: str | None = None,
    anchor_offset_min: int = 0,
    actor: str = "agent",
) -> dict:
    if kind not in ("workout", "meal_prep", "carry_item", "habit", "other"):
        kind = "other"
    if time_of_day is None and anchor is None:
        raise ValueError("A routine needs either time_of_day or an anchor (wake/before_office/after_office/bedtime)")
    dup = await db.fetchrow(
        "SELECT * FROM routines WHERE user_id=%s AND active AND similarity(lower(title), lower(%s)) > 0.7",
        db.owner_id(), title,
    )
    if dup:
        return {"routine": dup, "duplicate": True}
    row = await db.fetchrow(
        "INSERT INTO routines(user_id,title,kind,days_of_week,time_of_day,anchor,anchor_offset_min) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        db.owner_id(), title.strip(), kind, days_of_week or [1, 2, 3, 4, 5, 6, 7], time_of_day, anchor, anchor_offset_min,
    )
    await audit.record(actor, "insert", "routines", row["id"], None, row)
    return {"routine": row, "duplicate": False}


async def list_routines(active_only: bool = True) -> list[dict]:
    return await db.fetch(
        "SELECT * FROM routines WHERE user_id=%s AND (%s = false OR active) ORDER BY time_of_day NULLS LAST, title",
        db.owner_id(), active_only,
    )


async def update_routine(routine_id: UUID, changes: dict[str, Any], actor: str = "agent") -> dict:
    allowed = {"title", "kind", "days_of_week", "time_of_day", "anchor", "anchor_offset_min", "active"}
    bad = set(changes) - allowed
    if bad:
        raise ValueError(f"Cannot change {sorted(bad)}")
    before = await db.fetchrow("SELECT * FROM routines WHERE id=%s AND user_id=%s", routine_id, db.owner_id())
    if not before:
        raise ValueError("Routine not found")
    cols = ", ".join(f"{c}=%s" for c in changes)
    row = await db.fetchrow(f"UPDATE routines SET {cols} WHERE id=%s RETURNING *", *changes.values(), routine_id)
    if changes.get("active") is False or "time_of_day" in changes or "anchor" in changes or "days_of_week" in changes:
        await db.execute(
            "UPDATE reminders SET status='cancelled' WHERE routine_id=%s AND status='pending' AND fire_at > now()", routine_id
        )
        await generate_routine_reminders(local_today())
    await audit.record(actor, "update", "routines", routine_id, before, row)
    return row


def routine_fire_time(r: dict, day: date, profile: dict) -> datetime | None:
    tzname = profile.get("timezone") or "Asia/Kolkata"
    wd = datetime.combine(day, time()).isoweekday()
    if wd not in (r["days_of_week"] or []):
        return None
    offset = timedelta(minutes=r.get("anchor_offset_min") or 0)
    if r.get("time_of_day"):
        return at_local(day, r["time_of_day"], tzname)
    works = wd in (profile.get("work_days") or [1, 2, 3, 4, 5])
    anchor = r.get("anchor")
    base: time | None
    if anchor == "wake":
        base = profile.get("wake_time")
    elif anchor == "before_office":
        base = profile.get("office_start")
        if base is not None:
            base = (datetime.combine(day, base) - timedelta(minutes=(profile.get("commute_minutes") or 0))).time()
        if not works:
            return None
    elif anchor == "after_office":
        base = profile.get("office_end")
        if not works:
            return None
    elif anchor == "bedtime":
        base = profile.get("sleep_time")
    else:
        return None
    if base is None:
        return None
    return at_local(day, (datetime.combine(day, base) + offset).time(), tzname)


async def generate_routine_reminders(day: date) -> int:
    """Idempotent: dedupe_key = routine:<id>:<date>. Skips times already long past."""
    profile = await get_profile()
    created = 0
    cutoff = utcnow() - timedelta(hours=1)
    for r in await list_routines():
        fire = routine_fire_time(r, day, profile)
        if fire is None or fire < cutoff:
            continue
        rid = await create_reminder(
            f"{r['title']}", fire, routine_id=r["id"], dedupe_key=f"routine:{r['id']}:{day.isoformat()}"
        )
        created += 1 if rid else 0
    return created


# ----------------------------------------------------------------------------
# Calendar rows (Google sync lives in google_svc; these are the local rows)
# ----------------------------------------------------------------------------
async def save_event(
    title: str,
    start_at: datetime,
    end_at: datetime | None = None,
    description: str | None = None,
    all_day: bool = False,
    google_event_id: str | None = None,
    etag: str | None = None,
    created_by: str = "agent",
    status: str = "confirmed",
) -> dict:
    if google_event_id:
        row = await db.fetchrow(
            "INSERT INTO calendar_events(user_id,google_event_id,title,description,start_at,end_at,all_day,etag,created_by,status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id, google_event_id) DO UPDATE SET title=EXCLUDED.title, description=EXCLUDED.description, "
            "start_at=EXCLUDED.start_at, end_at=EXCLUDED.end_at, all_day=EXCLUDED.all_day, etag=EXCLUDED.etag, status=EXCLUDED.status "
            "RETURNING *",
            db.owner_id(), google_event_id, title, description, start_at, end_at, all_day, etag, created_by, status,
        )
    else:
        row = await db.fetchrow(
            "INSERT INTO calendar_events(user_id,title,description,start_at,end_at,all_day,created_by,status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            db.owner_id(), title, description, start_at, end_at, all_day, created_by, status,
        )
    return row


async def list_events(start: datetime, end: datetime) -> list[dict]:
    return await db.fetch(
        "SELECT id,title,description,start_at,end_at,all_day,status,google_event_id FROM calendar_events "
        "WHERE user_id=%s AND status<>'cancelled' AND start_at >= %s AND start_at < %s ORDER BY start_at",
        db.owner_id(), start, end,
    )


async def find_event(query: str) -> dict | None:
    try:
        row = await db.fetchrow("SELECT * FROM calendar_events WHERE id=%s AND user_id=%s", UUID(query), db.owner_id())
        if row:
            return row
    except ValueError:
        pass
    return await db.fetchrow(
        "SELECT *, similarity(lower(title), lower(%s)) AS sim FROM calendar_events WHERE user_id=%s AND status<>'cancelled' "
        "AND start_at >= now() - interval '1 day' AND (similarity(lower(title), lower(%s)) > 0.2 OR lower(title) LIKE %s) "
        "ORDER BY sim DESC, start_at LIMIT 1",
        query, db.owner_id(), query, f"%{query.lower()}%",
    )


async def update_event(event_id: UUID, changes: dict[str, Any], actor: str = "agent") -> dict:
    allowed = {"title", "description", "start_at", "end_at", "status", "google_event_id", "etag"}
    bad = set(changes) - allowed
    if bad:
        raise ValueError(f"Cannot change {sorted(bad)}")
    before = await db.fetchrow("SELECT * FROM calendar_events WHERE id=%s", event_id)
    cols = ", ".join(f"{c}=%s" for c in changes)
    row = await db.fetchrow(f"UPDATE calendar_events SET {cols} WHERE id=%s RETURNING *", *changes.values(), event_id)
    if row and row["status"] == "cancelled":
        await db.execute("UPDATE reminders SET status='cancelled' WHERE event_id=%s AND status='pending'", event_id)
    await audit.record(actor, "update", "calendar_events", event_id, before, row)
    return row


async def event_reminders(event: dict, title: str | None = None) -> None:
    """Morning-of reminder + one shortly before. Idempotent per event."""
    profile = await get_profile()
    tzname = profile.get("timezone") or "Asia/Kolkata"
    start: datetime = event["start_at"]
    label = title or event["title"]
    ev_id = event["id"]
    day = start.astimezone(tz(tzname)).date()
    wake = profile.get("wake_time") or time(7, 0)
    morning = at_local(day, (datetime.combine(day, wake) + timedelta(minutes=30)).time(), tzname)
    before = start - timedelta(minutes=30) if not event.get("all_day") else None
    stamp = start.isoformat()
    # cancel pending reminders made for an older start time (event moved); keep current ones
    await db.execute(
        "UPDATE reminders SET status='cancelled' WHERE event_id=%s AND status='pending' AND dedupe_key NOT LIKE %s",
        ev_id, f"%:{stamp}",
    )
    if morning > utcnow() and (before is None or morning < before):
        await create_reminder(f"Today: {label}", morning, event_id=ev_id, dedupe_key=f"event:{ev_id}:morning:{stamp}")
    if before and before > utcnow():
        await create_reminder(f"Coming up in 30 min: {label}", before, event_id=ev_id, dedupe_key=f"event:{ev_id}:pre:{stamp}")


async def close_finished_event_tasks(grace_hours: int = 2) -> int:
    """A to-do linked to a calendar event is finished once the event is over, whether or not anyone said 'done'.
    Without this it would sit in 'pending' forever and clutter every briefing."""
    rows = await db.fetch(
        "SELECT t.id FROM tasks t JOIN calendar_events e ON e.id = t.calendar_event_id "
        "WHERE t.user_id=%s AND t.status IN ('open','snoozed') "
        "AND COALESCE(e.end_at, e.start_at) + make_interval(hours => %s) < now()",
        db.owner_id(), grace_hours)
    for r in rows:
        await update_task(r["id"], {"status": "done"}, actor="job")
    return len(rows)
