"""Core profile: small, structured, ALWAYS injected into the prompt."""
from __future__ import annotations

from datetime import time
from typing import Any

from database import audit, db
from timeutil import parse_hhmm

TIME_FIELDS = ("wake_time", "sleep_time", "office_start", "office_end")
_PROFILE_COLS = TIME_FIELDS + ("work_days", "commute_minutes", "persona", "extra")
_USER_COLS = ("display_name", "timezone")


async def get_profile() -> dict:
    row = await db.fetchrow(
        "SELECT u.display_name, u.timezone, u.email, up.wake_time, up.sleep_time, up.office_start, up.office_end, "
        "up.work_days, up.commute_minutes, up.persona, up.extra "
        "FROM users u LEFT JOIN user_profile up ON up.user_id = u.id WHERE u.id=%s",
        db.owner_id(),
    )
    return row or {}


def _coerce(field: str, value: Any) -> Any:
    if field in TIME_FIELDS:
        if value in (None, ""):
            return None
        return value if isinstance(value, time) else parse_hhmm(str(value))
    if field == "work_days":
        names = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}
        days = []
        for d in value if isinstance(value, (list, tuple)) else str(value).split(","):
            d = str(d).strip().lower()
            days.append(names.get(d[:3]) if not d.isdigit() else int(d))
        if not days or any(x is None or not 1 <= x <= 7 for x in days):
            raise ValueError("work_days must be ISO weekday numbers 1(Mon)-7(Sun) or day names")
        return sorted(set(days))
    if field == "commute_minutes":
        n = int(value)
        if not 0 <= n <= 300:
            raise ValueError("commute_minutes must be 0-300")
        return n
    return value


async def update_profile(changes: dict[str, Any], actor: str = "agent") -> dict:
    """Update only the supplied fields. Returns {changed: {field: [old, new]}}."""
    if not changes:
        raise ValueError("No fields supplied")
    before = await get_profile()
    prof_set: dict[str, Any] = {}
    user_set: dict[str, Any] = {}
    for k, v in changes.items():
        if k in _PROFILE_COLS and k != "extra":
            prof_set[k] = _coerce(k, v)
        elif k in _USER_COLS:
            user_set[k] = v
        elif k == "extra":
            if not isinstance(v, dict):
                raise ValueError("extra must be an object")
        else:
            raise ValueError(f"Unknown profile field '{k}'. Allowed: {', '.join(_PROFILE_COLS + _USER_COLS)}")

    if "timezone" in user_set:
        from zoneinfo import ZoneInfo

        ZoneInfo(str(user_set["timezone"]))  # raises for an invalid IANA name

    async with db.transaction() as tx:
        if prof_set:
            cols = ", ".join(f"{c}=%s" for c in prof_set)
            await tx.execute(
                f"UPDATE user_profile SET {cols}, updated_at=now() WHERE user_id=%s",
                *prof_set.values(), db.owner_id(),
            )
        if "extra" in changes:
            await tx.execute(
                "UPDATE user_profile SET extra = extra || %s, updated_at=now() WHERE user_id=%s",
                db.jsonb(changes["extra"]), db.owner_id(),
            )
        if user_set:
            cols = ", ".join(f"{c}=%s" for c in user_set)
            await tx.execute(f"UPDATE users SET {cols} WHERE id=%s", *user_set.values(), db.owner_id())

    after = await get_profile()
    changed = {k: [_s(before.get(k)), _s(after.get(k))] for k in changes if _s(before.get(k)) != _s(after.get(k))}
    if changed:
        await audit.record(actor, "update", "user_profile", db.owner_id(), {k: v[0] for k, v in changed.items()}, {k: v[1] for k, v in changed.items()})
    return {"changed": changed, "profile": {k: _s(v) for k, v in after.items()}}


def _s(v: Any) -> Any:
    if isinstance(v, time):
        return v.strftime("%H:%M")
    return v


def summarize(p: dict) -> str:
    """Compact text injected into every prompt."""
    def t(x):
        return x.strftime("%H:%M") if x else "unknown"

    days = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
    wd = ",".join(days[d] for d in (p.get("work_days") or [])) or "unknown"
    lines = [
        f"Name: {p.get('display_name')}  Timezone: {p.get('timezone')}",
        f"Wake {t(p.get('wake_time'))} | Sleep {t(p.get('sleep_time'))} | Office {t(p.get('office_start'))}-{t(p.get('office_end'))} "
        f"on {wd} | Commute {p.get('commute_minutes') if p.get('commute_minutes') is not None else 'unknown'} min",
    ]
    extra = p.get("extra") or {}
    if extra:
        lines.append("Other: " + "; ".join(f"{k}={v}" for k, v in extra.items()))
    return "\n".join(lines)
