"""Email draft approval. This is the ONLY place an email can be sent, and only after the user approves."""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import require_auth
from database import emails as emails_db
from database.audit import jsonable

log = logging.getLogger("drafts")
router = APIRouter(prefix="/drafts", tags=["drafts"], dependencies=[Depends(require_auth)])


class DraftEdit(BaseModel):
    to_addrs: list[str] | None = None
    cc_addrs: list[str] | None = None
    subject: str | None = None
    body: str | None = None


@router.get("")
async def list_drafts(status: str | None = "pending_approval") -> list[dict]:
    return jsonable(await emails_db.list_drafts(None if status in (None, "all") else status))


@router.patch("/{draft_id}")
async def edit_draft(draft_id: UUID, edit: DraftEdit) -> dict:
    changes = {k: v for k, v in edit.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "Nothing to change")
    try:
        return jsonable(await emails_db.edit_draft(draft_id, changes))
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/{draft_id}/reject")
async def reject(draft_id: UUID) -> dict:
    d = await emails_db.get_draft(draft_id)
    if not d:
        raise HTTPException(404, "Draft not found")
    if d["status"] not in ("pending_approval", "failed"):
        raise HTTPException(409, f"Draft is already {d['status']}")
    return jsonable(await emails_db.set_draft_status(draft_id, "rejected"))


@router.post("/{draft_id}/approve")
async def approve(draft_id: UUID) -> dict:
    from google_svc.send import SendError, send_draft

    try:
        return jsonable(await send_draft(draft_id))
    except SendError as e:
        raise HTTPException(e.status, str(e))
