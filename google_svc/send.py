"""The single code path that sends an approved email draft. Used by POST /drafts/{id}/approve and by the
chat confirmation flow ("send it" -> "yes"). The LLM never calls this."""
from __future__ import annotations

import logging
from uuid import UUID

from database import emails as emails_db

log = logging.getLogger("send")


class SendError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


async def send_draft(draft_id: UUID) -> dict:
    d = await emails_db.get_draft(draft_id)
    if not d:
        raise SendError("Draft not found", 404)
    if d["status"] not in ("pending_approval", "failed"):
        raise SendError(f"Draft is already {d['status']}", 409)

    await emails_db.set_draft_status(draft_id, "approved")  # nothing is sent unless status = approved
    try:
        from google_svc import gmail

        reply_id = None
        if d["thread_id"]:
            try:
                reply_id = (await gmail.get_message_context(d["thread_id"], 100))["last"].get("message_id_header")
            except Exception:
                reply_id = None
        gid = d["gmail_draft_id"]
        if gid:  # make sure Gmail holds exactly what was approved (it may have been edited)
            gid = await gmail.update_draft(gid, d["to_addrs"], d["subject"], d["body"], d["cc_addrs"], d["thread_id"], reply_id)
        else:
            gid = await gmail.create_draft(d["to_addrs"], d["subject"], d["body"], d["cc_addrs"], d["thread_id"], reply_id)
        await gmail.send_draft(gid)
    except Exception as e:
        log.warning("send failed: %s", e)
        await emails_db.set_draft_status(draft_id, "failed", str(e))
        raise SendError(f"Could not send: {e}") from e
    return await emails_db.set_draft_status(draft_id, "sent")


async def send_reminder_email(subject: str, plain: str, html: str) -> str:
    """Email a fired reminder to the owner's OWN address (REMINDER_EMAIL). The recipient is fixed in .env and cannot
    be chosen by the model or by a tool argument, so this can never mail anyone else."""
    from config import settings
    from google_svc import gmail

    to = settings.reminder_email
    if not to:
        raise SendError("REMINDER_EMAIL is not set in .env", 400)
    return await gmail.send_message([to], subject, plain, html)
