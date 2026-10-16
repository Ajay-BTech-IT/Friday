"""Gmail tools. Read mail and create DRAFTS only. There is no send tool: sending is the approve endpoint."""
from __future__ import annotations

import logging
from datetime import timedelta
from email.utils import parseaddr
from uuid import UUID

from langchain_core.tools import tool

from config import settings
from database import emails as emails_db
from google_svc import oauth
from timeutil import utcnow
from tools._util import out, safe

log = logging.getLogger("tools.gmail")


async def _need_google() -> str | None:
    if not settings.google_enabled:
        return "Gmail is not configured. Fill GOOGLE_CLIENT_ID/SECRET and TOKEN_ENCRYPTION_KEY in .env."
    if not (await oauth.status())["connected"]:
        return "Gmail is not connected yet. Open http://localhost:8000/auth/google/start in a browser once."
    return None


async def _resolve_email(query: str) -> dict | None:
    """Stored mail first (any word of the query); if not stored, find it live in Gmail and import it."""
    e = await emails_db.find_email(query)
    if e or await _need_google():
        return e
    import re

    words = [w for w in re.findall(r"[\w.@+-]+", query) if len(w) >= 2 and w.lower() not in emails_db._STOP][:6]
    if not words:
        return None
    from google_svc import gmail

    hits = await gmail.search(" OR ".join(words), 5)
    hits = [h for h in hits if "SENT" not in h.get("labels", [])]
    if not hits:
        return None
    row, _ = await emails_db.upsert_email(await gmail.get_message(hits[0]["id"]))
    return row


async def _addresses(raw: str) -> tuple[list[str], list[dict]]:
    """Turn "Shahid, bob@x.com" into addresses the user can rely on.

    Returns (addresses, pending). A literal email address, or a name that exactly matches a SAVED contact, is used
    directly. Anything inferred (from old mail, sent drafts, a Gmail search) is only a guess: it goes into `pending`
    with its candidates so the agent must ask the user to confirm. Unknown names go into `pending` with no candidates.
    """
    import re
    from email.utils import getaddresses, parseaddr

    found: list[str] = []
    pending: list[dict] = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        addr = parseaddr(part)[1]
        if emails_db.EMAIL_RE.match(addr):
            found.append(addr)
            continue
        name = re.sub(r"[^\w .'-]", "", part).strip()
        cands = await emails_db.lookup_candidates(name)
        trusted = [c for c in cands if c["trusted"]]
        if len(trusted) == 1:
            found.append(trusted[0]["address"])
            continue
        if not cands and not await _need_google():
            try:
                from google_svc import gmail

                first = name.lower().split()[0] if name else ""
                own = (settings.owner_email or "").lower()
                seen: set[str] = set()
                for h in await gmail.search(f"from:{name} OR to:{name}", 8):
                    for display, cand in getaddresses([h.get("from", ""), h.get("to", "")]):
                        if (first and cand.lower() != own and cand.lower() not in seen and emails_db.EMAIL_RE.match(cand)
                                and (first in display.lower() or first in cand.lower())):
                            seen.add(cand.lower())
                            cands.append({"address": cand, "seen_as": display or cand,
                                          "source": "your Gmail", "trusted": False})
            except Exception as e:
                log.warning("address search failed: %s", e)
        pending.append({"name": part, "candidates": cands[:4]})
    return found, pending


def _ask_user(pending: list[dict]) -> str:
    """Tool result telling the agent to ask the user instead of guessing."""
    p = pending[0]
    if not p["candidates"]:
        return out({"error": f"I don't have an email address for {p['name']}.",
                    "instruction": "Ask the user for their email address. Once they give it, create the draft with that exact "
                                   "address and save it as a contact with db_insert (table contacts: name, email) so you "
                                   "do not have to ask again."})
    return out({"needs_confirmation": True, "name": p["name"], "candidates": p["candidates"],
                "instruction": "Do NOT create or change the draft yet. Ask the user to confirm which address to use, naming "
                               "each candidate's address and where you saw it (e.g. 'I found Shahid at m.shahidh360@gmail.com "
                               "from an email you sent him earlier. Is that the right one?'). After they answer, call the "
                               "tool again with that exact address, and save it as a contact with db_insert (contacts: name, "
                               "email) so you remember it next time."})


@tool
@safe
async def sync_mail_now() -> str:
    """Fetch new Gmail now, classify and summarise it. Normally this runs overnight automatically."""
    if (msg := await _need_google()):
        return out({"error": msg})
    from jobs import email_sync

    return out(await email_sync.run())


_last_sync = None


async def _fresh_mail() -> str | None:
    """Pull new Gmail before answering mail questions (at most every 10 minutes). Returns a warning if it failed."""
    global _last_sync
    if await _need_google():
        return None
    if _last_sync and utcnow() - _last_sync < timedelta(minutes=10):
        return None
    try:
        from jobs import email_sync

        await email_sync.run()
        _last_sync = utcnow()
        return None
    except Exception as e:
        log.warning("auto sync failed: %s", e)
        return f"Could not refresh from Gmail just now ({e}); showing what is already stored."


@tool
@safe
async def list_mail(min_importance: int = 1, needs_reply: bool | None = None, since_hours: int = 48, limit: int = 15,
                    query: str | None = None) -> str:
    """Summaries of stored mail (newest first), refreshed from Gmail first. importance is 1-5 (5 = act now). Use
    needs_reply=true for mail that expects an answer. `query` filters by sender, subject or summary text (e.g. "lensa").
    For older mail or exact Gmail searches use search_mail instead."""
    warning = await _fresh_mail()
    rows = await emails_db.list_emails(utcnow() - timedelta(hours=since_hours), min_importance, needs_reply, limit, query)
    return out({"warning": warning, "count": len(rows), "mail": [
        {"id": r["id"], "from": r["from_addr"], "subject": r["subject"], "importance": r["importance"],
         "needs_reply": r["needs_reply"], "summary": r["summary"] or r["snippet"], "received": r["received_at"]}
        for r in rows]})


@tool
@safe
async def search_mail(query: str, limit: int = 8) -> str:
    """Search ALL of Gmail live, using Gmail search syntax: "from:lensa", "subject:invoice newer_than:7d",
    "is:unread from:hr". Returns sender, subject, date and a snippet. Use this when asked about mail from a
    person/company or older mail."""
    if (msg := await _need_google()):
        return out({"error": msg})
    from google_svc import gmail

    rows = await gmail.search(query, max(1, min(limit, 20)))
    return out({"query": query, "count": len(rows), "mail": rows})


@tool
@safe
async def read_email(email: str) -> str:
    """Read the live content of one email (id, or words from subject/sender) straight from Gmail. Use before
    drafting a reply so the reply is accurate."""
    if (msg := await _need_google()):
        return out({"error": msg})
    e = await _resolve_email(email)
    if not e:
        return out({"error": f"No email found for '{email}', even searching Gmail."})
    from google_svc import gmail

    ctx = await gmail.get_message_context(e["thread_id"])
    return out({"id": e["id"], "from": e["from_addr"], "subject": e["subject"], "thread": ctx["text"]})


@tool
@safe
async def draft_reply(email: str, body: str) -> str:
    """Create a reply DRAFT to an email (id, or ANY words from its sender/subject, e.g. "andreson" or "bangalore trip"). `body` is the reply text you wrote.
    The draft waits for the user's approval; it is never sent from here. Tell the user to review it."""
    e = await _resolve_email(email)
    if not e:
        return out({"error": f"No email found for '{email}', even searching Gmail. Use compose_draft with the person's address instead."})
    to = parseaddr(e["from_addr"])[1] or e["from_addr"]
    subject = e["subject"] or ""
    subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    gmail_id = None
    note = None
    if not await _need_google():
        try:
            from google_svc import gmail

            ctx = await gmail.get_message_context(e["thread_id"], 100)
            gmail_id = await gmail.create_draft([to], subject, body, thread_id=e["thread_id"],
                                                reply_to_msgid=ctx["last"].get("message_id_header"))
        except Exception as ex:
            note = f"Draft stored locally; Gmail draft not created ({ex})"
    else:
        note = "Gmail not connected: draft stored locally and cannot be sent until Gmail is connected."
    d = await emails_db.create_draft([to], subject, body, in_reply_to=e["id"], thread_id=e["thread_id"], gmail_draft_id=gmail_id)
    return out({"draft_id": d["id"], "to": to, "subject": subject, "status": d["status"], "note": note,
                "next": "Waiting for the user's approval in the app (GET /drafts)."})


@tool
@safe
async def compose_draft(to: str, subject: str, body: str, cc: str | None = None) -> str:
    """Create a NEW email draft. `to`/`cc` are comma-separated addresses. Waits for approval; never sent here.
    Write `body` yourself as a complete, natural email (greeting, the message in first person, sign-off with the
    user's name). Never paste the user's instruction as the body. To CHANGE an existing draft use edit_draft."""
    tos, pending = await _addresses(to)
    if pending:
        return _ask_user(pending)
    if not tos:
        return out({"error": "No recipient given. Ask who the email is for."})
    ccs = None
    if cc:
        ccs, cc_pending = await _addresses(cc)
        if cc_pending:
            return _ask_user(cc_pending)
    same = await emails_db.find_draft(subject)
    if same and same["subject"].strip().lower() == subject.strip().lower() and utcnow() - same["created_at"] < timedelta(minutes=30):
        return out({"error": f"A pending draft with this subject already exists (id {same['id']}). Use edit_draft to change it."})
    gmail_id = None
    note = None
    if not await _need_google():
        try:
            from google_svc import gmail

            gmail_id = await gmail.create_draft(tos, subject, body, ccs)
        except Exception as ex:
            note = f"WARNING: the draft is saved here but NOT in the Gmail Drafts folder ({ex}). Tell the user."
    else:
        note = "Gmail not connected: draft stored locally and cannot be sent until Gmail is connected."
    d = await emails_db.create_draft(tos, subject, body, cc_addrs=ccs, gmail_draft_id=gmail_id)
    return out({"draft_id": d["id"], "to": tos, "subject": subject, "status": d["status"], "note": note,
                "next": "Read it back in one line, naming the recipient's exact email address, then ask: 'Say yes to send it, or tell me what to change.'"})


@tool
@safe
async def edit_draft(draft: str | None = None, to: str | None = None, cc: str | None = None,
                     subject: str | None = None, body: str | None = None) -> str:
    """Change a draft that is still waiting for approval: recipients (`to`/`cc`, comma-separated), subject or body.
    `draft` is its id or words from the subject; leave it empty for the most recent pending draft. Only pass the
    fields that change. This never sends anything; the user still approves it."""
    d = await emails_db.find_draft(draft)
    if not d:
        return out({"error": "There is no draft waiting for approval."})
    changes: dict = {}
    if to:
        tos, pending = await _addresses(to)
        if pending:
            return _ask_user(pending)
        changes["to_addrs"] = tos
    if cc is not None:
        if cc.strip():
            ccs, cc_pending = await _addresses(cc)
            if cc_pending:
                return _ask_user(cc_pending)
            changes["cc_addrs"] = ccs
        else:
            changes["cc_addrs"] = []
    if subject:
        changes["subject"] = subject
    if body:
        changes["body"] = body
    if not changes:
        return out({"error": "Nothing to change. Ask what should change."})
    row = await emails_db.edit_draft(d["id"], changes)
    note = None
    if row["gmail_draft_id"] and not await _need_google():
        try:
            from google_svc import gmail

            await gmail.update_draft(row["gmail_draft_id"], row["to_addrs"], row["subject"], row["body"], row["cc_addrs"], row["thread_id"])
        except Exception as ex:
            note = f"Updated here; the Gmail copy will be refreshed when you approve ({ex})"
    return out({"draft_id": row["id"], "to": row["to_addrs"], "cc": row["cc_addrs"], "subject": row["subject"],
                "status": row["status"], "note": note})


@tool
@safe
async def reject_draft(draft: str | None = None) -> str:
    """Discard a draft that is waiting for approval (marked rejected, never sent). `draft` is its id or words from
    the subject; empty = the most recent pending draft."""
    d = await emails_db.find_draft(draft)
    if not d:
        return out({"error": "There is no draft waiting for approval."})
    row = await emails_db.set_draft_status(d["id"], "rejected")
    return out({"rejected": row["subject"], "draft_id": row["id"]})


@tool
@safe
async def request_send_draft(draft: str | None = None) -> str:
    """Use when the user wants an email draft SENT ("send it", "send him", "just send", "go ahead and send", "I
    reviewed it, send"). This does NOT send anything: it shows the user the draft and asks them to confirm, and
    only their own reply "yes" sends it. `draft` is its id or subject words; empty = the newest pending draft.
    After calling it, tell the user the recipient and subject and say: "Say yes to send it."."""
    from orchestrator import router

    d = await emails_db.find_draft(draft)
    if not d:
        return out({"error": "There is no draft waiting to be sent. Offer to write one."})
    bad = [a for a in d["to_addrs"] if not emails_db.EMAIL_RE.match(a)]
    if bad:
        return out({"error": f"The recipient {bad} is not an email address. Use edit_draft to set a real address first."})
    router.arm_send(d["id"])
    return out({"awaiting_confirmation": True, "to": d["to_addrs"], "subject": d["subject"], "body": d["body"][:500],
                "instruction": "Nothing was sent yet. Ask the user to reply yes to send it."})


@tool
@safe
async def list_drafts(status: str = "pending_approval") -> str:
    """Show email drafts. status: pending_approval | approved | rejected | sent | failed | all."""
    rows = await emails_db.list_drafts(None if status == "all" else status)
    return out([{"id": r["id"], "to": r["to_addrs"], "subject": r["subject"], "status": r["status"], "body": r["body"][:300]} for r in rows])
