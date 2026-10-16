"""Reminder dispatcher. Respects quiet hours and the daily cap, groups low-importance items."""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from config import settings
from database import db, notifications
from database import tasks as tasks_db
from database.profile import get_profile
from jobs import notify
from timeutil import at_local, in_window, local_now, tz, utcnow

log = logging.getLogger("dispatch")


def _next_wake(profile: dict) -> datetime:
    tzname = profile.get("timezone") or "Asia/Kolkata"
    wake = profile.get("wake_time") or time(7, 0)
    local = local_now(tzname)
    cand = at_local(local.date(), wake, tzname)
    return cand if cand > utcnow() else at_local(local.date() + timedelta(days=1), wake, tzname)


async def dispatch_due() -> int:
    due = await tasks_db.due_reminders()
    if not due:
        return 0
    profile = await get_profile()
    tzname = profile.get("timezone") or "Asia/Kolkata"
    now_local = local_now(tzname)
    sleep, wake = profile.get("sleep_time"), profile.get("wake_time")
    quiet = bool(sleep and wake) and in_window(now_local.time(), sleep, wake)

    ready: list[dict] = []
    urgent: list[dict] = []
    for r in due:
        # task already finished/cancelled meanwhile -> drop quietly
        if r["task_id"] and r["task_status"] in ("done", "cancelled"):
            await tasks_db.mark_reminder(r["id"], "cancelled")
            continue
        # missed by a long time (app was off): routine items are stale, drop them
        if r["routine_id"] and r["fire_at"] < utcnow() - timedelta(hours=6):
            await tasks_db.mark_reminder(r["id"], "cancelled")
            continue
        is_urgent = r.get("task_priority") == 1
        if quiet and not is_urgent:
            await tasks_db.defer_reminder(r["id"], _next_wake(profile))
            continue
        (urgent if is_urgent else ready).append(r)

    sent = 0
    # reminders that came due while the PC/server was off: one honest digest, no individual pings, no late emails
    late_cut = utcnow() - timedelta(hours=3)
    stale = [r for r in urgent + ready if r["fire_at"] < late_cut]
    if stale:
        urgent = [r for r in urgent if r not in stale]
        ready = [r for r in ready if r not in stale]
        await _deliver_missed(stale, tzname)
        sent += len(stale)
    # reminders the user asked to get by email: in-app notice + a mail to their own address, never grouped or capped
    mailed = [r for r in urgent + ready if r.get("channel") == "email"]
    urgent = [r for r in urgent if r.get("channel") != "email"]
    ready = [r for r in ready if r.get("channel") != "email"]
    for r in mailed:
        await _deliver_email(r)
        sent += 1
    for r in urgent:
        from jobs import reminder_text

        persona, owner = await _voice()
        line = await reminder_text.compose([r["message"]], persona, owner)
        await notify.deliver(line, None, "reminder", True, r["id"])
        await _after_send(r)
        sent += 1

    if ready:
        used = await notifications.count_today(tzname)
        if used >= settings.daily_notify_cap:
            # cap reached: low-importance items wait for tomorrow's briefing (routines just lapse)
            for r in ready:
                if r["routine_id"]:
                    await tasks_db.mark_reminder(r["id"], "cancelled")
                else:
                    await tasks_db.defer_reminder(r["id"], _next_wake(profile))
            log.info("daily notification cap reached; deferred %d reminders", len(ready))
        else:
            if len(ready) == 1:
                await notify.deliver("Reminder", ready[0]["message"], "reminder", False, ready[0]["id"])
            else:
                body = "\n".join(f"- {r['message']}" for r in ready)
                await notify.deliver(f"{len(ready)} reminders", body, "reminder", False, ready[0]["id"])
            for r in ready:
                await _after_send(r)
                sent += 1
    return sent


async def _voice() -> tuple[dict, str]:
    """Active persona and the owner's first name, for phrasing reminders like a friend."""
    from database import persona as persona_db
    from database.profile import get_profile

    persona = await persona_db.get_active()
    name = ((await get_profile()).get("display_name") or "there").split()[0]
    return persona, name


async def _after_send(r: dict) -> None:
    await tasks_db.mark_reminder(r["id"], "sent")
    # a snoozed task wakes up as open again once its snooze reminder fires
    if r["task_id"] and r["task_status"] == "snoozed":
        from database import db

        await db.execute("UPDATE tasks SET status='open' WHERE id=%s AND status='snoozed'", r["task_id"])
    # "remind me at 5 to play badminton": once the reminder has been delivered the job is done, so it must not come back
    # in tomorrow's briefing. Urgent tasks and tasks without their own timed reminder stay open.
    if r["task_id"] and (r.get("dedupe_key") or "").endswith(":due") and r.get("task_priority") != 1:
        from database import db

        task = await db.fetchrow("SELECT id, source, status FROM tasks WHERE id=%s", r["task_id"])
        if task and task["status"] == "open" and task["source"] in ("explicit", "extracted"):
            await tasks_db.update_task(task["id"], {"status": "done"}, actor="job")


async def _deliver_missed(stale: list[dict], tzname: str) -> None:
    from jobs import reminder_text

    persona, owner = await _voice()
    items = [f"{reminder_text.clean(r['message'])} (was due {r['fire_at'].astimezone(tz(tzname)).strftime('%a %I:%M %p')})" for r in stale]
    line = await reminder_text.compose_missed(items, persona, owner)
    await notify.deliver(line, None, "reminder", False, stale[0]["id"])
    for r in stale:
        await _after_send(r)


MAX_EMAIL_ATTEMPTS = 3


async def _deliver_email(r: dict) -> None:
    from database.profile import get_profile
    from google_svc import oauth
    from google_svc import send as gsend
    from jobs import reminder_mail, reminder_text

    persona, owner = await _voice()
    profile = await get_profile()
    line = await reminder_text.compose([r["message"]], persona, owner)
    problem = ""
    try:
        subject, plain, html = await reminder_mail.build(r["message"], persona, r["fire_at"], profile.get("display_name") or "there",
                                                         profile.get("timezone") or "Asia/Kolkata", line=line)
        await gsend.send_reminder_email(subject, plain, html)
    except oauth.GoogleTransient as e:
        # network not up yet (e.g. right after the PC woke): try again in 2 minutes instead of giving up
        if (r.get("attempts") or 0) + 1 < MAX_EMAIL_ATTEMPTS:
            log.warning("reminder email delayed (network): %s", e)
            await db.execute("UPDATE reminders SET attempts=attempts+1, fire_at=%s WHERE id=%s", utcnow() + timedelta(minutes=2), r["id"])
            return
        problem = " (I couldn't email this one.)"
    except Exception as e:
        log.warning("reminder email failed: %s", e)
        problem = " (I couldn't email this one.)"
    # title = the friendly sentence: it is what is shown AND what is spoken
    await notify.deliver(line + problem, None, "reminder", False, r["id"])
    await _after_send(r)
