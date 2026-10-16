"""Emails + drafts. Sending is NOT here: only api/drafts.py (after approval) may send."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from database import audit, db


async def upsert_email(msg: dict) -> tuple[dict, bool]:
    """Insert a fetched Gmail message. Returns (row, is_new)."""
    row = await db.fetchrow(
        "INSERT INTO emails(user_id,gmail_message_id,thread_id,from_addr,to_addrs,subject,snippet,body_text,received_at,labels) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (user_id,gmail_message_id) DO NOTHING RETURNING *",
        db.owner_id(), msg["id"], msg["thread_id"], msg["from"], msg.get("to") or [], msg.get("subject"),
        msg.get("snippet"), msg.get("body"), msg["received_at"], msg.get("labels") or [],
    )
    if row:
        return row, True
    return await db.fetchrow("SELECT * FROM emails WHERE user_id=%s AND gmail_message_id=%s", db.owner_id(), msg["id"]), False


async def pending_classification(limit: int = 20) -> list[dict]:
    return await db.fetch(
        "SELECT * FROM emails WHERE user_id=%s AND status='new' ORDER BY received_at LIMIT %s", db.owner_id(), limit
    )


async def set_classification(email_id: UUID, category: str, importance: int, summary: str, needs_reply: bool) -> None:
    # raw body is dropped after summarising (privacy); Gmail still holds the original
    await db.execute(
        "UPDATE emails SET category=%s, importance=%s, summary=%s, needs_reply=%s, status='summarized', body_text=NULL WHERE id=%s",
        category, max(1, min(5, importance)), summary, needs_reply, email_id,
    )


async def list_emails(since: datetime | None = None, min_importance: int = 1, needs_reply: bool | None = None,
                      limit: int = 20, query: str | None = None) -> list[dict]:
    return await db.fetch(
        "SELECT id,from_addr,subject,summary,snippet,category,importance,needs_reply,status,received_at,thread_id,gmail_message_id "
        "FROM emails WHERE user_id=%s AND received_at >= COALESCE(%s::timestamptz, '-infinity') AND COALESCE(importance,3) >= %s "
        "AND (%s::boolean IS NULL OR needs_reply=%s) AND status<>'ignored' "
        "AND (%s::text IS NULL OR from_addr ILIKE %s OR subject ILIKE %s OR summary ILIKE %s) ORDER BY received_at DESC LIMIT %s",
        db.owner_id(), since, min_importance, needs_reply, needs_reply, query, f"%{query}%", f"%{query}%", f"%{query}%", limit,
    )


async def get_email(email_id: UUID) -> dict | None:
    return await db.fetchrow("SELECT * FROM emails WHERE id=%s AND user_id=%s", email_id, db.owner_id())


_STOP = {"the", "reply", "mail", "email", "emails", "from", "about", "for", "and", "with", "message", "his", "her", "him",
         "that", "this", "regarding", "back", "send", "thanks", "thank", "you"}


async def find_email(query: str) -> dict | None:
    """Find a stored email by uuid, or by ANY of the words in the query (sender, subject, summary), best match first."""
    import re

    try:
        row = await get_email(UUID(query))
        if row:
            return row
    except ValueError:
        pass
    words = [w for w in dict.fromkeys(re.findall(r"[\w.@+-]+", query.lower())) if len(w) >= 2 and w not in _STOP][:8]
    if not words:
        return None
    score = " + ".join(
        "(lower(from_addr) LIKE %s)::int + (lower(coalesce(subject,'')) LIKE %s)::int + (lower(coalesce(summary,'')) LIKE %s)::int"
        for _ in words)
    params: list = []
    for w in words:
        params += [f"%{w}%"] * 3
    return await db.fetchrow(
        f"SELECT * FROM (SELECT *, ({score}) AS score FROM emails WHERE user_id=%s) t WHERE score > 0 "
        "ORDER BY score DESC, received_at DESC LIMIT 1",
        *params, db.owner_id(),
    )


# ---- drafts -----------------------------------------------------------------
async def create_draft(to_addrs: list[str], subject: str, body: str, in_reply_to: UUID | None = None,
                       thread_id: str | None = None, cc_addrs: list[str] | None = None,
                       gmail_draft_id: str | None = None) -> dict:
    row = await db.fetchrow(
        "INSERT INTO email_drafts(user_id,in_reply_to,thread_id,gmail_draft_id,to_addrs,cc_addrs,subject,body) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        db.owner_id(), in_reply_to, thread_id, gmail_draft_id, to_addrs, cc_addrs or [], subject, body,
    )
    await audit.record("agent", "insert", "email_drafts", row["id"], None, {k: row[k] for k in ("to_addrs", "subject", "status")})
    return row


async def list_drafts(status: str | None = "pending_approval") -> list[dict]:
    return await db.fetch(
        "SELECT * FROM email_drafts WHERE user_id=%s AND (%s::text IS NULL OR status=%s) ORDER BY created_at DESC LIMIT 50",
        db.owner_id(), status, status,
    )


async def get_draft(draft_id: UUID) -> dict | None:
    return await db.fetchrow("SELECT * FROM email_drafts WHERE id=%s AND user_id=%s", draft_id, db.owner_id())


async def edit_draft(draft_id: UUID, changes: dict[str, Any]) -> dict:
    """Only pending drafts can be edited. Status is never changed here."""
    allowed = {"to_addrs", "cc_addrs", "subject", "body"}
    bad = set(changes) - allowed
    if bad:
        raise ValueError(f"Cannot edit {sorted(bad)}")
    cols = ", ".join(f"{c}=%s" for c in changes)
    row = await db.fetchrow(
        f"UPDATE email_drafts SET {cols} WHERE id=%s AND user_id=%s AND status='pending_approval' RETURNING *",
        *changes.values(), draft_id, db.owner_id(),
    )
    if not row:
        raise ValueError("Draft not found or no longer pending approval")
    return row


async def set_draft_status(draft_id: UUID, status: str, error: str | None = None) -> dict:
    row = await db.fetchrow(
        "UPDATE email_drafts SET status=%s, error=%s, "
        "approved_at=CASE WHEN %s IN ('approved','sent') AND approved_at IS NULL THEN now() ELSE approved_at END, "
        "sent_at=CASE WHEN %s='sent' THEN now() ELSE sent_at END WHERE id=%s RETURNING *",
        status, error, status, status, draft_id,
    )
    await audit.record("api", "status_change", "email_drafts", draft_id, None, {"status": status, "error": error})
    return row


async def find_draft(query: str | None) -> dict | None:
    """Resolve a draft by id, by subject words, or (blank / 'latest') the newest one still pending."""
    q = (query or "").strip()
    if q and q.lower() not in ("latest", "last", "it", "that", "this"):
        try:
            row = await get_draft(UUID(q))
            if row:
                return row
        except ValueError:
            pass
        row = await db.fetchrow(
            "SELECT * FROM email_drafts WHERE user_id=%s AND status='pending_approval' AND (subject ILIKE %s OR %s = ANY(to_addrs)) "
            "ORDER BY created_at DESC LIMIT 1", db.owner_id(), f"%{q}%", q)
        if row:
            return row
    return await db.fetchrow(
        "SELECT * FROM email_drafts WHERE user_id=%s AND status='pending_approval' ORDER BY created_at DESC LIMIT 1", db.owner_id())


EMAIL_RE = __import__("re").compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


async def lookup_candidates(name: str) -> list[dict]:
    """Possible email addresses for a person's name, best first. Each: {address, seen_as, source, trusted}.
    Only an exact saved contact is `trusted`; everything inferred from mail history is a guess to confirm."""
    from email.utils import parseaddr

    n = name.strip().lower()
    if not n:
        return []
    out: list[dict] = []
    seen: set[str] = set()

    def add(addr: str, seen_as: str, source: str, trusted: bool = False) -> None:
        addr = (addr or "").strip()
        if EMAIL_RE.match(addr) and addr.lower() not in seen:
            seen.add(addr.lower())
            out.append({"address": addr, "seen_as": seen_as, "source": source, "trusted": trusted})

    for r in await db.fetch(
        "SELECT name, email, (lower(name)=%s OR %s = ANY(SELECT lower(a) FROM unnest(aliases) a)) AS exact "
        "FROM contacts WHERE user_id=%s AND email IS NOT NULL AND "
        "(lower(name)=%s OR %s = ANY(SELECT lower(a) FROM unnest(aliases) a) OR lower(name) LIKE %s) ORDER BY 3 DESC LIMIT 5",
        n, n, db.owner_id(), n, n, f"%{n}%"):
        add(str(r["email"]), r["name"], "saved contact", bool(r["exact"]))
    for r in await db.fetch(
        "SELECT from_addr FROM emails WHERE user_id=%s AND lower(from_addr) LIKE %s ORDER BY received_at DESC LIMIT 8",
        db.owner_id(), f"%{n}%"):
        display, addr = parseaddr(r["from_addr"])
        add(addr, display or addr, "an email they sent you")
    for r in await db.fetch(
        "SELECT to_addrs FROM email_drafts WHERE user_id=%s AND status='sent' AND "
        "(lower(body) LIKE %s OR lower(array_to_string(to_addrs, ',')) LIKE %s) ORDER BY sent_at DESC LIMIT 5",
        db.owner_id(), f"%dear {n}%", f"%{n}%"):
        for a in r["to_addrs"]:
            add(a, n, "an email you sent before")
    return out
