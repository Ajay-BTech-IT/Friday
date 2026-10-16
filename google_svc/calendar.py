"""Google Calendar wrapper. Cancel = status 'cancelled' (never a hard delete)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from database import tasks as tasks_db
from google_svc import oauth
from timeutil import utcnow

log = logging.getLogger("google.calendar")


async def _svc():
    creds = await oauth.get_credentials()
    return await asyncio.to_thread(lambda: build("calendar", "v3", credentials=creds, cache_discovery=False))


def _body(title: str, start: datetime, end: datetime | None, description: str | None, all_day: bool, tzname: str) -> dict:
    if all_day:
        d = start.astimezone().date()
        return {"summary": title, "description": description or "",
                "start": {"date": d.isoformat()}, "end": {"date": (d + timedelta(days=1)).isoformat()}}
    end = end or start + timedelta(hours=1)
    return {
        "summary": title, "description": description or "",
        "start": {"dateTime": start.isoformat(), "timeZone": tzname},
        "end": {"dateTime": end.isoformat(), "timeZone": tzname},
    }


async def insert(title: str, start: datetime, end: datetime | None, description: str | None, all_day: bool, tzname: str) -> dict:
    svc = await _svc()
    ev = await asyncio.to_thread(
        lambda: svc.events().insert(calendarId="primary", body=_body(title, start, end, description, all_day, tzname)).execute()
    )
    return {"google_event_id": ev["id"], "etag": ev.get("etag")}


async def patch(google_event_id: str, **fields) -> dict:
    svc = await _svc()
    ev = await asyncio.to_thread(
        lambda: svc.events().patch(calendarId="primary", eventId=google_event_id, body=fields).execute()
    )
    return {"etag": ev.get("etag")}


async def move(google_event_id: str, start: datetime, end: datetime | None, tzname: str) -> dict:
    end = end or start + timedelta(hours=1)
    return await patch(
        google_event_id,
        start={"dateTime": start.isoformat(), "timeZone": tzname},
        end={"dateTime": end.isoformat(), "timeZone": tzname},
    )


async def cancel(google_event_id: str) -> None:
    await patch(google_event_id, status="cancelled")


def _event_time(t: dict) -> tuple[datetime, bool]:
    if "dateTime" in t:
        return datetime.fromisoformat(t["dateTime"].replace("Z", "+00:00")).astimezone(timezone.utc), False
    return datetime.fromisoformat(t["date"]).replace(tzinfo=timezone.utc), True


async def sync() -> int:
    """Two-way sync (inbound half) using the sync token. Returns rows touched."""
    svc = await _svc()
    token = await oauth.get_cursor("calendar_sync_token")

    def _run() -> tuple[list[dict], str | None]:
        items: list[dict] = []
        kwargs = {"calendarId": "primary", "singleEvents": True, "showDeleted": True, "maxResults": 250}
        if token:
            kwargs["syncToken"] = token
        else:
            kwargs["timeMin"] = (utcnow() - timedelta(days=1)).isoformat()
            kwargs["timeMax"] = (utcnow() + timedelta(days=90)).isoformat()
        page = None
        while True:
            try:
                resp = svc.events().list(pageToken=page, **kwargs).execute()
            except HttpError as e:
                if e.resp.status == 410:  # sync token invalidated
                    return [], "RESET"
                raise
            items += resp.get("items", [])
            page = resp.get("nextPageToken")
            if not page:
                return items, resp.get("nextSyncToken")

    items, next_token = await asyncio.to_thread(_run)
    if next_token == "RESET":
        await oauth.set_cursor("calendar_sync_token", None)
        return 0
    touched = 0
    for it in items:
        status = "cancelled" if it.get("status") == "cancelled" else ("tentative" if it.get("status") == "tentative" else "confirmed")
        if "start" not in it:
            # cancelled stubs carry no times: just flip an existing row
            from database import db

            await db.execute("UPDATE calendar_events SET status='cancelled' WHERE user_id=%s AND google_event_id=%s", db.owner_id(), it["id"])
            continue
        start, all_day = _event_time(it["start"])
        end = _event_time(it["end"])[0] if "end" in it else None
        row = await tasks_db.save_event(
            it.get("summary") or "(no title)", start, end, it.get("description"), all_day,
            google_event_id=it["id"], etag=it.get("etag"), created_by="sync", status=status,
        )
        if status != "cancelled":
            await tasks_db.event_reminders(row)
        touched += 1
    if next_token:
        await oauth.set_cursor("calendar_sync_token", next_token)
    return touched
