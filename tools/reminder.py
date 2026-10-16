"""Reminder tools: tasks, routines, snooze, upcoming reminders."""
from __future__ import annotations

from datetime import timedelta

from langchain_core.tools import tool

from database import db
from database import tasks as tasks_db
from timeutil import local_today, parse_hhmm, parse_when, utcnow
from tools._util import out, parse_json, safe


@tool
@safe
async def create_task(
    title: str,
    due_hint: str | None = None,
    due_at: str | None = None,
    notes: str | None = None,
    priority: int = 2,
    via_email: bool | None = None,
) -> str:
    """Create a to-do and its reminder in one step.

    Use for loose to-dos ("need to buy eggs", "pay the bill"). If the user wants something SCHEDULED at a specific
    day/time (book, appointment, meeting, "Thursday at 8"), use create_calendar_event instead. Do NOT wait for them
    to say "remind me". Give either due_at (ISO 8601 local time, e.g. 2026-10-16T18:30:00) when they gave a
    time/date, or due_hint when they gave a vague moment. due_hint is one of: morning, before_leaving,
    before_office, afternoon, lunch, after_office, evening, tonight, bedtime, tomorrow_morning.
    The hint is converted to a real time using the user's office hours. priority 1(urgent)-4(low).
    Reminders are emailed to the user's own address when they fire (and also shown in the app) by default. Leave
    via_email unset normally; pass false ONLY if the user says "no mail" / "in-app only"; true if they ask for mail
    explicitly.
    For a bare clock time like "5 o'clock" use the NEXT upcoming occurrence of that time today/tomorrow.
    Returns the task; if a similar open task already exists it is returned with duplicate=true.
    """
    due = parse_when(due_at) if due_at else None
    if due_hint and due_hint not in tasks_db.DUE_HINTS:
        due_hint = None
    res = await tasks_db.create_task(title, notes, due_hint, due, priority, via_email=via_email)
    t = res["task"]
    return out({"duplicate": res["duplicate"], "task_id": t["id"], "title": t["title"], "due_at": t["due_at"],
                "status": t["status"], "by_email": (via_email is not False) and not res["duplicate"] and bool(t["due_at"])})


@tool
@safe
async def list_tasks(status: str = "open", limit: int = 20) -> str:
    """List tasks. status: open | done | snoozed | cancelled | all."""
    rows = await tasks_db.list_tasks(None if status == "all" else status, limit)
    return out(rows)


@tool
@safe
async def complete_task(task: str) -> str:
    """Mark a task done. `task` is its id or part of its title ("eggs"). Cancels its pending reminder."""
    t = await tasks_db.find_task(task)
    if not t:
        return out({"error": f"No open task matching '{task}'"})
    row = await tasks_db.update_task(t["id"], {"status": "done"})
    return out({"done": row["title"], "task_id": row["id"]})


@tool
@safe
async def snooze_task(task: str, minutes: int | None = None, until: str | None = None) -> str:
    """Snooze a task. Give minutes from now OR `until` (ISO 8601 local time)."""
    t = await tasks_db.find_task(task)
    if not t:
        return out({"error": f"No open task matching '{task}'"})
    when = parse_when(until) if until else utcnow() + timedelta(minutes=minutes or 30)
    row = await tasks_db.snooze_task(t["id"], when)
    return out({"snoozed": row["title"], "until": row["due_at"]})


@tool
@safe
async def update_task(
    task: str,
    title: str | None = None,
    notes: str | None = None,
    priority: int | None = None,
    due_at: str | None = None,
    due_hint: str | None = None,
    status: str | None = None,
) -> str:
    """Change a task (title, notes, priority, new due time/hint, or status open|done|snoozed|cancelled).
    Use status='cancelled' instead of deleting. Nothing in this system is ever deleted."""
    t = await tasks_db.find_task(task, ("open", "snoozed", "done", "cancelled"))
    if not t:
        return out({"error": f"No task matching '{task}'"})
    changes = {k: v for k, v in dict(title=title, notes=notes, priority=priority, due_hint=due_hint, status=status).items() if v is not None}
    if due_at:
        changes["due_at"] = parse_when(due_at)
    if not changes:
        return out({"error": "No changes supplied"})
    row = await tasks_db.update_task(t["id"], changes)
    return out({"updated": row["title"], "status": row["status"], "due_at": row["due_at"]})


@tool
@safe
async def create_routine(
    title: str,
    kind: str = "habit",
    days_of_week: str | None = None,
    time_of_day: str | None = None,
    anchor: str | None = None,
    anchor_offset_min: int = 0,
) -> str:
    """Create a recurring daily habit that reminds the user at the right time (workout, pack lunch, take charger).

    kind: workout | meal_prep | carry_item | habit | other. days_of_week: comma list of ISO numbers (Mon=1..Sun=7),
    default every day. Give time_of_day ("HH:MM") OR an anchor: wake | before_office | after_office | bedtime,
    plus anchor_offset_min (negative = earlier, e.g. -30 for 30 minutes before).
    """
    days = [int(d) for d in days_of_week.split(",")] if days_of_week else None
    tod = parse_hhmm(time_of_day) if time_of_day else None
    res = await tasks_db.create_routine(title, kind, days, tod, anchor, anchor_offset_min)
    await tasks_db.generate_routine_reminders(local_today())
    r = res["routine"]
    return out({"duplicate": res["duplicate"], "routine_id": r["id"], "title": r["title"]})


@tool
@safe
async def list_routines() -> str:
    """List the user's active routines."""
    return out(await tasks_db.list_routines())


@tool
@safe
async def update_routine(
    routine: str,
    title: str | None = None,
    time_of_day: str | None = None,
    anchor: str | None = None,
    anchor_offset_min: int | None = None,
    days_of_week: str | None = None,
    active: bool | None = None,
) -> str:
    """Change a routine (match by title or id). Set active=false to stop it instead of deleting."""
    rows = await tasks_db.list_routines(active_only=False)
    q = routine.lower()
    match = next((r for r in rows if str(r["id"]) == routine or q in r["title"].lower()), None)
    if not match:
        return out({"error": f"No routine matching '{routine}'"})
    changes: dict = {}
    if title is not None: changes["title"] = title
    if time_of_day is not None: changes["time_of_day"] = parse_hhmm(time_of_day)
    if anchor is not None: changes["anchor"] = anchor
    if anchor_offset_min is not None: changes["anchor_offset_min"] = anchor_offset_min
    if days_of_week is not None: changes["days_of_week"] = [int(d) for d in days_of_week.split(",")]
    if active is not None: changes["active"] = active
    if not changes:
        return out({"error": "No changes supplied"})
    row = await tasks_db.update_routine(match["id"], changes)
    return out({"updated": row["title"], "active": row["active"]})


@tool
@safe
async def upcoming_reminders(hours: int = 24) -> str:
    """Pending reminders firing within the next N hours."""
    rows = await db.fetch(
        "SELECT message, fire_at, status FROM reminders WHERE user_id=%s AND status='pending' AND fire_at <= now() + make_interval(hours => %s) "
        "ORDER BY fire_at LIMIT 40",
        db.owner_id(), hours,
    )
    return out(rows)
