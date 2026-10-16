"""Timezone helpers. DB stores UTC; the owner's timezone is applied at the edges."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from config import settings

_tz_cache: dict[str, ZoneInfo] = {}


def tz(name: str | None = None) -> ZoneInfo:
    name = name or settings.owner_timezone
    if name not in _tz_cache:
        try:
            _tz_cache[name] = ZoneInfo(name)
        except Exception:
            _tz_cache[name] = ZoneInfo("UTC")
    return _tz_cache[name]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def local_now(tzname: str | None = None) -> datetime:
    return utcnow().astimezone(tz(tzname))


def local_today(tzname: str | None = None) -> date:
    return local_now(tzname).date()


def at_local(d: date, t: time, tzname: str | None = None) -> datetime:
    """Combine a local date + time into an aware UTC datetime."""
    return datetime.combine(d, t.replace(tzinfo=None), tzinfo=tz(tzname)).astimezone(timezone.utc)


def parse_when(value: str, tzname: str | None = None) -> datetime:
    """Parse ISO-8601 from the LLM. Naive values are treated as owner-local time."""
    v = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz(tzname))
    return dt.astimezone(timezone.utc)


def parse_hhmm(value: str) -> time:
    value = value.strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I %p", "%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(value.upper(), fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse time '{value}'. Use HH:MM (24h).")


def fmt_local(dt: datetime | None, tzname: str | None = None) -> str:
    if dt is None:
        return "-"
    return dt.astimezone(tz(tzname)).strftime("%a %d %b, %I:%M %p")


def in_window(now_t: time, start: time, end: time) -> bool:
    """True if now_t falls in [start, end), supporting windows that cross midnight."""
    if start <= end:
        return start <= now_t < end
    return now_t >= start or now_t < end
