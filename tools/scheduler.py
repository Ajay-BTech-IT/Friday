"""Scheduler tools: one request creates the calendar event AND its reminders (and a linked task)."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from langchain_core.tools import tool

from config import settings
from database import db
from database import tasks as tasks_db
from database.profile import get_profile
from timeutil import at_local, local_today, parse_when, tz
from tools._util import out, safe

log = logging.getLogger("tools.scheduler")


async def _google_ready() -> bool:
    if not settings.google_enabled:
        return False
    from google_svc import oauth

    return (await oauth.status())["connected"]


@tool
@safe
async def create_calendar_event(
    title: str,
    start_at: str,
    end_at: str | None = None,
    description: str | None = None,
    all_day: bool = False,
) -> str:
    """Schedule something ("book Tatkal on Friday", "dentist at 5pm"). Creates the calendar event, a morning-of
    reminder, a 30-minute-before reminder and a linked to-do, all at once. Also writes to Google Calendar when
    connected. start_at/end_at are ISO 8601 in the user's local time (e.g. 2026-10-16T09:00:00). For all-day
    items pass all_day=true with start_at as a date or the morning time.
    """
    tzname = (await get_profile()).get("timezone")
    start = parse_when(start_at, tzname)
    end = parse_when(end_at, tzname) if end_at else None
    same = await db.fetchrow(
        "SELECT * FROM calendar_events WHERE user_id=%s AND status<>'cancelled' AND start_at=%s AND lower(title)=lower(%s)",
        db.owner_id(), start, title,
    )
    if same:  # asking twice must not create twin events
        return out({"event_id": same["id"], "title": title, "start_at": start, "duplicate": True,
                    "note": "This event already exists, nothing new was created."})
    row = await tasks_db.save_event(title, start, end, description, all_day)
    note = None
    if await _google_ready():
        try:
            from google_svc import calendar as gcal

            g = await gcal.insert(title, start, end, description, all_day, tzname or settings.owner_timezone)
            row = await tasks_db.update_event(row["id"], {"google_event_id": g["google_event_id"], "etag": g["etag"]})
        except Exception as e:  # keep the local event even if Google fails
            note = f"Saved locally; Google Calendar sync failed ({e})"
    await tasks_db.event_reminders(row)
    await tasks_db.create_task(title, notes=description, due_at=None, source="explicit", calendar_event_id=row["id"])
    return out({"event_id": row["id"], "title": title, "start_at": start, "google_synced": bool(row.get("google_event_id")),
                "reminders": "morning-of + 30 min before", "note": note})


@tool
@safe
async def list_events(date_from: str | None = None, date_to: str | None = None) -> str:
    """List calendar events between two local dates (YYYY-MM-DD). Default: today through the next 7 days."""
    tzname = (await get_profile()).get("timezone")
    d0 = date.fromisoformat(date_from) if date_from else local_today(tzname)
    d1 = date.fromisoformat(date_to) if date_to else d0 + timedelta(days=7)
    start = at_local(d0, datetime.min.time(), tzname)
    end = at_local(d1 + timedelta(days=1), datetime.min.time(), tzname)
    return out(await tasks_db.list_events(start, end))


@tool
@safe
async def move_event(event: str, new_start_at: str, new_end_at: str | None = None) -> str:
    """Reschedule an event (match by id or title). Reminders move with it."""
    ev = await tasks_db.find_event(event)
    if not ev:
        return out({"error": f"No upcoming event matching '{event}'"})
    tzname = (await get_profile()).get("timezone")
    start = parse_when(new_start_at, tzname)
    end = parse_when(new_end_at, tzname) if new_end_at else (start + (ev["end_at"] - ev["start_at"]) if ev["end_at"] else None)
    row = await tasks_db.update_event(ev["id"], {"start_at": start, "end_at": end})
    note = None
    if ev["google_event_id"] and await _google_ready():
        try:
            from google_svc import calendar as gcal

            await gcal.move(ev["google_event_id"], start, end, tzname or settings.owner_timezone)
        except Exception as e:
            note = f"Moved locally; Google sync failed ({e})"
    await tasks_db.event_reminders(row)
    return out({"moved": row["title"], "start_at": row["start_at"], "note": note})


@tool
@safe
async def cancel_event(event: str) -> str:
    """Cancel an event (match by id or title). It is marked cancelled, never deleted; reminders stop."""
    ev = await tasks_db.find_event(event)
    if not ev:
        return out({"error": f"No upcoming event matching '{event}'"})
    row = await tasks_db.update_event(ev["id"], {"status": "cancelled"})
    await db.execute(
        "UPDATE tasks SET status='cancelled' WHERE user_id=%s AND calendar_event_id=%s AND status IN ('open','snoozed')",
        db.owner_id(), ev["id"],
    )
    note = None
    if ev["google_event_id"] and await _google_ready():
        try:
            from google_svc import calendar as gcal

            await gcal.cancel(ev["google_event_id"])
        except Exception as e:
            note = f"Cancelled locally; Google sync failed ({e})"
    return out({"cancelled": row["title"], "note": note})
