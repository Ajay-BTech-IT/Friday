"""Read-mostly endpoints for the PC client: notifications, tasks, briefings, nutrition, audit, jobs."""
from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import require_auth
from database import briefings as briefings_db
from database import db
from database import notifications
from database import nutrition as nut
from database import persona as persona_db
from database import profile as profile_db
from database import tasks as tasks_db
from database.audit import jsonable
from timeutil import local_today

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/notifications", tags=["notifications"])
async def get_notifications(unseen: bool = True, limit: int = 30) -> list[dict]:
    return jsonable(await (notifications.unseen(limit) if unseen else notifications.recent(limit)))


class SeenIn(BaseModel):
    ids: list[UUID] | None = None  # empty = mark all


@router.post("/notifications/seen", tags=["notifications"])
async def mark_seen(body: SeenIn) -> dict:
    return {"marked": await notifications.mark_seen(body.ids)}


@router.get("/profile", tags=["profile"])
async def profile() -> dict:
    return jsonable(await profile_db.get_profile())


@router.get("/tasks", tags=["tasks"])
async def tasks(status: str | None = "open", limit: int = 50) -> list[dict]:
    return jsonable(await tasks_db.list_tasks(None if status in (None, "all") else status, limit))


@router.get("/briefing/{kind}", tags=["briefing"])
async def get_briefing(kind: str, day: date | None = None) -> dict:
    if kind not in ("morning", "evening"):
        raise HTTPException(400, "kind must be morning or evening")
    b = await briefings_db.get(kind, day or local_today())
    if not b:
        raise HTTPException(404, "No briefing yet. POST /briefing/{kind}/generate")
    return jsonable(b)


@router.post("/briefing/{kind}/generate", tags=["briefing"])
async def generate_briefing(kind: str) -> dict:
    if kind not in ("morning", "evening"):
        raise HTTPException(400, "kind must be morning or evening")
    from jobs import briefing

    return jsonable(await briefing.generate(kind, local_today()))


@router.get("/nutrition/today", tags=["nutrition"])
async def nutrition_today(day: date | None = None) -> dict:
    return jsonable(await nut.day_summary(day or local_today()))


@router.get("/personas", tags=["persona"])
async def personas() -> dict:
    active = await persona_db.get_active()
    return jsonable({"active": active["name"], "available": [p["name"] for p in await persona_db.list_personas()]})


@router.get("/audit", tags=["audit"])
async def audit(limit: int = 50) -> list[dict]:
    return jsonable(await db.fetch(
        "SELECT actor, action, table_name, row_id, before, after, created_at FROM audit_log WHERE user_id=%s "
        "ORDER BY created_at DESC LIMIT %s", db.owner_id(), min(limit, 200)))


@router.post("/jobs/{name}", tags=["jobs"])
async def run_job(name: str) -> dict:
    from jobs.runner import JOBS

    fn = JOBS.get(name)
    if not fn:
        raise HTTPException(404, f"Unknown job. Available: {sorted(JOBS)}")
    await fn()
    return {"ran": name}
