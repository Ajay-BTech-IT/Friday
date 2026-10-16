"""APScheduler jobs (Architecture section 6). Needs an always-on process; a sleeping PC misses reminders,
so everything here is idempotent and catches up on the next start."""
from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings
from database import conversations as conv_db
from database import db
from database import memory as memory_db
from database import tasks as tasks_db
from jobs import briefing, dispatch
from orchestrator import extractor
from orchestrator.model import chat_model, text_of
from timeutil import local_today, parse_hhmm, tz

log = logging.getLogger("jobs")
_scheduler: AsyncIOScheduler | None = None


def _guarded(fn):
    async def run():
        try:
            await fn()
        except Exception:
            log.exception("job %s failed", fn.__name__)

    run.__name__ = fn.__name__
    return run


async def tick_reminders() -> None:
    await dispatch.dispatch_due()
    await tasks_db.close_finished_event_tasks()
    await briefing.due_deliveries()


async def tick_extract() -> None:
    await extractor.run_batch()


async def tick_calendar() -> None:
    from google_svc import calendar as gcal, oauth

    if settings.google_enabled and (await oauth.status())["connected"]:
        await gcal.sync()


async def summarize_conversations() -> None:
    """Roll idle conversations into a short summary memory (keeps the prompt small, keeps the story)."""
    rows = await db.fetch(
        "SELECT c.id FROM conversations c WHERE c.user_id=%s AND c.summary IS NULL AND EXISTS "
        "(SELECT 1 FROM messages m WHERE m.conversation_id=c.id) AND NOT EXISTS "
        "(SELECT 1 FROM messages m WHERE m.conversation_id=c.id AND m.created_at > now() - interval '6 hours') LIMIT 5",
        db.owner_id(),
    )
    for r in rows:
        msgs = await db.fetch(
            "SELECT role, content FROM messages WHERE conversation_id=%s AND role IN ('user','assistant') AND content IS NOT NULL "
            "ORDER BY created_at LIMIT 80", r["id"])
        if len(msgs) < 4:
            await db.execute("UPDATE conversations SET summary='(short)', ended_at=now() WHERE id=%s", r["id"])
            continue
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in msgs)[:12000]
        res = await chat_model(0.2).ainvoke(
            "Summarise this conversation between a user and their personal assistant in 2-3 sentences. Keep concrete "
            "facts, decisions and anything left open. No fluff.\n\n" + transcript)
        summary = text_of(res.content).strip()
        await db.execute("UPDATE conversations SET summary=%s, ended_at=now() WHERE id=%s", summary, r["id"])
        await memory_db.save_summary(summary, {"conversation_id": str(r["id"])})


async def overnight() -> None:
    """Gmail sync -> routine reminders -> morning briefing, so mornings never wait on an LLM."""
    if settings.google_enabled:
        try:
            from google_svc import oauth
            from jobs import email_sync

            if (await oauth.status())["connected"]:
                log.info("overnight mail: %s", await email_sync.run())
        except Exception:
            log.exception("overnight gmail failed")
    n = await tasks_db.generate_routine_reminders(local_today())
    log.info("routine reminders created: %d", n)
    try:
        await summarize_conversations()
    except Exception:
        log.exception("conversation summaries failed")
    await briefing.generate("morning", local_today(), overwrite=False)


async def evening() -> None:
    await briefing.generate("evening", local_today())
    await briefing.deliver("evening", local_today())


async def catch_up() -> None:
    """On startup: make sure today's reminders and briefing exist even if the PC was off overnight."""
    await tasks_db.generate_routine_reminders(local_today())
    await briefing.generate("morning", local_today(), overwrite=False)


def start() -> AsyncIOScheduler:
    global _scheduler
    sched = AsyncIOScheduler(timezone=tz(), job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
    sched.add_job(_guarded(tick_reminders), "interval", seconds=settings.reminder_poll_seconds, id="reminders")
    sched.add_job(_guarded(tick_extract), "interval", seconds=settings.extract_poll_seconds, id="extract")
    sched.add_job(_guarded(tick_calendar), "interval", minutes=15, id="calendar")
    t = parse_hhmm(settings.overnight_job_time)
    sched.add_job(_guarded(overnight), CronTrigger(hour=t.hour, minute=t.minute, timezone=tz()), id="overnight")
    e = parse_hhmm(settings.evening_briefing_time)
    sched.add_job(_guarded(evening), CronTrigger(hour=e.hour, minute=max(e.minute - 5, 0), timezone=tz()), id="evening")
    sched.add_job(_guarded(catch_up), "date", run_date=None, id="catch_up")
    sched.start()
    _scheduler = sched
    return sched


def stop() -> None:
    if _scheduler:
        _scheduler.shutdown(wait=False)


JOBS = {"overnight": overnight, "evening": evening, "extract": tick_extract, "reminders": tick_reminders,
        "calendar": tick_calendar, "catch_up": catch_up, "summaries": summarize_conversations}
