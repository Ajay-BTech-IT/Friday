"""Morning / evening briefings: precomputed overnight, delivered at wake time, no LLM wait."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta

from database import briefings as briefings_db
from database import db
from database import emails as emails_db
from database import memory as memory_db
from database import nutrition as nut
from database import persona as persona_db
from database import tasks as tasks_db
from database.audit import jsonable
from database.profile import get_profile
from jobs import notify
from orchestrator.model import chat_model, text_of
from timeutil import at_local, in_window, local_now, local_today, tz, utcnow

log = logging.getLogger("briefing")


def _t(dt: datetime | None, tzname: str) -> str:
    return dt.astimezone(tz(tzname)).strftime("%H:%M") if dt else ""


async def _day_bounds(day: date, tzname: str) -> tuple[datetime, datetime]:
    return at_local(day, time(0, 0), tzname), at_local(day + timedelta(days=1), time(0, 0), tzname)


async def collect(kind: str, day: date) -> dict:
    profile = await get_profile()
    tzname = profile.get("timezone") or "Asia/Kolkata"
    start, end = await _day_bounds(day, tzname)
    data: dict = {"kind": kind, "date": day.isoformat(), "weekday": day.strftime("%A")}

    if kind == "morning":
        ystart, _ = await _day_bounds(day - timedelta(days=1), tzname)
        told = await db.fetch(
            "SELECT title FROM tasks WHERE user_id=%s AND created_at >= %s AND created_at < %s AND source IN ('explicit','extracted') "
            "AND status IN ('open','snoozed') AND NOT (due_at IS NOT NULL AND due_at < now() AND EXISTS "
            "(SELECT 1 FROM reminders r WHERE r.task_id=tasks.id AND r.status='sent'))", db.owner_id(), ystart, start)
        data["told_yesterday"] = [t["title"] for t in told]
        open_t = await db.fetch(
            "SELECT title, due_at FROM tasks WHERE user_id=%s AND status IN ('open','snoozed') AND (due_at IS NULL OR due_at < %s) "
            "AND source <> 'email' AND NOT (due_at IS NOT NULL AND due_at < now() AND EXISTS "
            "(SELECT 1 FROM reminders r WHERE r.task_id=tasks.id AND r.status='sent')) "
            "ORDER BY due_at NULLS LAST, priority LIMIT 10", db.owner_id(), end)
        data["tasks_today"] = [{"title": t["title"], "at": _t(t["due_at"], tzname)} for t in open_t]
        events = await tasks_db.list_events(start, end)
        data["events"] = [{"title": e["title"], "at": "all day" if e["all_day"] else _t(e["start_at"], tzname)} for e in events]
        mail = await emails_db.list_emails(utcnow() - timedelta(hours=18), 1, None, 50)
        important = [m for m in mail if (m["importance"] or 0) >= 4]
        data["email"] = {"new": len(mail), "important": [f"{m['from_addr'].split('<')[0].strip()}: {m['summary']}" for m in important[:3]],
                         "needs_reply": sum(1 for m in mail if m["needs_reply"])}
        routines = []
        for r in await tasks_db.list_routines():
            fire = tasks_db.routine_fire_time(r, day, profile)
            if fire:
                routines.append({"title": r["title"], "at": _t(fire, tzname)})
        data["routines"] = sorted(routines, key=lambda x: x["at"])
        goal = await nut.get_goal(day)
        yest = await nut.day_totals(day - timedelta(days=1))
        data["nutrition"] = {
            "goal_kcal": goal["calories"] if goal else None,
            "goal_protein_g": float(goal["protein_g"]) if goal and goal["protein_g"] is not None else None,
            "yesterday_kcal": round(yest["calories"]), "yesterday_protein_g": round(yest["protein_g"]),
        }
    else:  # evening
        open_t = await tasks_db.list_tasks("open", 10)
        data["open_tasks"] = [t["title"] for t in open_t]
        s = await nut.day_summary(day)
        data["nutrition"] = {"eaten_kcal": round(s["totals"]["calories"]), "eaten_protein_g": round(s["totals"]["protein_g"]),
                             "goal_kcal": s["goal"]["calories"] if s["goal"] else None,
                             "goal_protein_g": float(s["goal"]["protein_g"]) if s["goal"] and s["goal"]["protein_g"] is not None else None,
                             "meals_logged": len({i["meal_id"] for i in s["items"]})}
        tstart, tend = await _day_bounds(day + timedelta(days=1), tzname)
        tev = await tasks_db.list_events(tstart, tend)
        data["tomorrow"] = {
            "events": [{"title": e["title"], "at": _t(e["start_at"], tzname)} for e in tev[:4]],
            "due": [t["title"] for t in await tasks_db.list_tasks("open", 6, tend) if t["due_at"] and t["due_at"] >= tstart],
            "routines": [r["title"] for r in await tasks_db.list_routines()
                         if tasks_db.routine_fire_time(r, day + timedelta(days=1), profile)][:5],
        }
    return data


def _fallback(data: dict) -> str:
    if data["kind"] == "morning":
        parts = [f"Good morning! {data['weekday']}."]
        if data["told_yesterday"]:
            parts.append("You told me: " + ", ".join(data["told_yesterday"]) + ".")
        if data["events"]:
            parts.append("Calendar: " + ", ".join(f"{e['title']} ({e['at']})" for e in data["events"]) + ".")
        em = data["email"]
        if em["new"]:
            parts.append(f"{em['new']} new emails, {len(em['important'])} important, {em['needs_reply']} need a reply.")
        if data["routines"] and not (data["told_yesterday"] or data["events"]):
            parts.append("Nothing new from you, so it's a normal day: " + ", ".join(r["title"] for r in data["routines"]) + ".")
        elif data["routines"]:
            parts.append("Routine: " + ", ".join(r["title"] for r in data["routines"]) + ".")
        n = data["nutrition"]
        if n["goal_kcal"]:
            parts.append(f"Target today: {n['goal_kcal']} kcal" + (f", {n['goal_protein_g']:.0f} g protein." if n["goal_protein_g"] else "."))
        return " ".join(parts)
    n = data["nutrition"]
    parts = ["Evening check-in: how did the day go?"]
    if data["open_tasks"]:
        parts.append("Still open: " + ", ".join(data["open_tasks"][:5]) + ".")
    if n["goal_kcal"]:
        parts.append(f"You're at {n['eaten_kcal']} of {n['goal_kcal']} kcal.")
    tm = data["tomorrow"]
    if tm["events"]:
        parts.append("Tomorrow: " + ", ".join(f"{e['title']} ({e['at']})" for e in tm["events"]) + ".")
    return " ".join(parts)


async def compose(data: dict) -> str:
    persona = await persona_db.get_active()
    mem = await memory_db.retrieve("goals habits preferences", 3) if data["kind"] == "evening" else []
    prompt = (
        await persona_db.voice_brief(persona, ((await get_profile()).get("display_name") or "the user").split()[0]) + "\n\n"
        f"Write the {data['kind']} briefing as a spoken message of 60-110 words (about 20 seconds). Priority order: "
        "things the user told you > calendar > emails > routines > coaching. Skip empty sections. If the user told you "
        "nothing special and has no events, say it's a normal day and list the routine items. Include the nutrition "
        "target (morning) or an honest coaching line about today's intake (evening). Evening: ask how the day went, "
        "preview tomorrow. No markdown, no lists, no emojis, do not invent anything not in the data.\n\n"
        f"DATA:\n{json.dumps(jsonable(data), ensure_ascii=False)}\n"
        + ("MEMORIES:\n" + "\n".join(m["content"] for m in mem) if mem else "")
    )
    res = await chat_model(0.5).ainvoke(prompt)
    return text_of(res.content).strip()


async def generate(kind: str, day: date | None = None, overwrite: bool = True) -> dict:
    day = day or local_today()
    data = await collect(kind, day)
    try:
        content = await compose(data) or _fallback(data)
    except Exception as e:
        log.warning("briefing LLM failed, using template: %s", e)
        content = _fallback(data)
    return await briefings_db.save(kind, day, content, data, overwrite=overwrite) or await briefings_db.get(kind, day)


async def deliver(kind: str, day: date | None = None) -> bool:
    day = day or local_today()
    b = await briefings_db.get(kind, day)
    if b and b["delivered_at"]:
        return False
    if b is None:  # normally precomputed overnight; only generate here if the PC was off
        b = await generate(kind, day)
    title = "Good morning" if kind == "morning" else "Evening wrap-up"
    await notify.deliver(title, b["content"], "briefing")
    await briefings_db.mark_delivered(b["id"])
    return True


async def due_deliveries() -> None:
    """Called every tick: send the morning briefing at wake time, the evening one at the configured time."""
    from config import settings
    from timeutil import parse_hhmm

    profile = await get_profile()
    tzname = profile.get("timezone") or "Asia/Kolkata"
    now = local_now(tzname)
    day = now.date()
    wake = profile.get("wake_time") or time(7, 0)
    if now.time() >= wake and now.time() < time(14, 0):
        await deliver("morning", day)
    ev = parse_hhmm(settings.evening_briefing_time)
    if now.time() >= ev:
        await deliver("evening", day)
