"""Profile tools: office timings, wake/sleep, work days, commute."""
from __future__ import annotations

from langchain_core.tools import tool

from database import profile as profile_db
from tools._util import out, parse_json, safe


@tool
@safe
async def get_profile() -> str:
    """Read the user's core profile (wake/sleep time, office timings, work days, commute, extras)."""
    return out(profile_db.summarize(await profile_db.get_profile()))


@tool
@safe
async def update_profile(
    wake_time: str | None = None,
    sleep_time: str | None = None,
    office_start: str | None = None,
    office_end: str | None = None,
    work_days: str | None = None,
    commute_minutes: int | None = None,
    timezone: str | None = None,
    display_name: str | None = None,
    extra_json: str | None = None,
) -> str:
    """Update the user's core profile. Pass ONLY the fields that change.

    IMPORTANT: if the user says "update my office timing" but has not given the new time, DO NOT call this.
    Ask them for the missing value first (e.g. "What time does office start and end now?"). Call this tool
    once they have answered. Times are 24h "HH:MM" in the user's local time. work_days is a comma list of
    day names or ISO numbers (Mon=1..Sun=7), e.g. "Mon,Tue,Wed,Thu,Fri". extra_json is a JSON object merged
    into free-form profile data (goals, routines notes, anything that is not a column), e.g. {"gym":"6am"}.
    Returns old and new values so you can confirm exactly what changed.
    """
    changes = {
        k: v
        for k, v in dict(wake_time=wake_time, sleep_time=sleep_time, office_start=office_start, office_end=office_end,
                         work_days=work_days, commute_minutes=commute_minutes, timezone=timezone,
                         display_name=display_name).items()
        if v is not None
    }
    extra = parse_json(extra_json, "extra_json")
    if extra:
        changes["extra"] = extra
    if not changes:
        return out({"error": "Nothing to update. Ask the user which value should change and to what."})
    res = await profile_db.update_profile(changes)
    return out({"updated": True, **res})
