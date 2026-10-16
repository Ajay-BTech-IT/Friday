"""Gmail wrapper. Blocking Google client calls run in a thread.

Deliberately NO delete/trash functions exist. Sending is exposed only as `send_draft`,
which api/drafts.py calls after the user approves.
"""
from __future__ import annotations

import asyncio
import base64
import html
import logging
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from google_svc import oauth

log = logging.getLogger("google.gmail")


async def _svc():
    creds = await oauth.get_credentials()
    return await asyncio.to_thread(lambda: build("gmail", "v1", credentials=creds, cache_discovery=False))


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _body(payload: dict) -> str:
    plain, htmls = [], []

    def walk(p: dict):
        mime = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            htmls.append(_decode(data))
        for sub in p.get("parts") or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain)
    if htmls:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(htmls))
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", html.unescape(text)).strip()
    return ""


def _parse(m: dict) -> dict:
    h = m["payload"].get("headers", [])
    try:
        received = parsedate_to_datetime(_header(h, "Date")).astimezone(timezone.utc)
    except Exception:
        received = datetime.fromtimestamp(int(m["internalDate"]) / 1000, tz=timezone.utc)
    return {
        "id": m["id"],
        "thread_id": m["threadId"],
        "from": _header(h, "From") or "unknown",
        "to": [a for _, a in getaddresses([_header(h, "To")]) if a],
        "subject": _header(h, "Subject"),
        "snippet": m.get("snippet", ""),
        "body": _body(m["payload"])[:8000],
        "received_at": received,
        "labels": m.get("labelIds", []),
        "message_id_header": _header(h, "Message-ID"),
    }


async def fetch_new(max_messages: int = 40) -> list[dict]:
    """New inbox mail since the stored history cursor (first run: last 2 days)."""
    svc = await _svc()
    cursor = await oauth.get_cursor("gmail_history_id")

    def _run() -> tuple[list[dict], str | None]:
        ids: list[str] = []
        profile = svc.users().getProfile(userId="me").execute()
        new_cursor = profile.get("historyId")
        use_list = cursor is None
        if not use_list:
            try:
                req = svc.users().history().list(userId="me", startHistoryId=cursor, historyTypes=["messageAdded"], labelId="INBOX")
                while req is not None:
                    resp = req.execute()
                    for h in resp.get("history", []):
                        for added in h.get("messagesAdded", []):
                            ids.append(added["message"]["id"])
                    req = svc.users().history().list_next(req, resp)
            except HttpError as e:
                if e.resp.status == 404:  # cursor too old
                    use_list = True
                else:
                    raise
        if use_list:
            resp = svc.users().messages().list(userId="me", q="in:inbox newer_than:2d", maxResults=max_messages).execute()
            ids = [m["id"] for m in resp.get("messages", [])]
        out = []
        for mid in list(dict.fromkeys(ids))[:max_messages]:
            try:
                m = svc.users().messages().get(userId="me", id=mid, format="full").execute()
            except HttpError:
                continue
            labels = m.get("labelIds", [])
            if "DRAFT" in labels or "SENT" in labels:
                continue
            out.append(_parse(m))
        return out, new_cursor

    messages, new_cursor = await asyncio.to_thread(_run)
    if new_cursor:
        await oauth.set_cursor("gmail_history_id", new_cursor)
    return messages


async def get_message_context(thread_id: str, max_chars: int = 6000) -> dict:
    """Fetch the latest message in a thread for drafting a reply (bodies are not kept in the DB)."""
    svc = await _svc()

    def _run() -> dict:
        t = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        msgs = [_parse(m) for m in t.get("messages", [])]
        last = msgs[-1]
        text = "\n\n---\n\n".join(f"From: {m['from']}\n{m['body']}" for m in msgs[-3:])
        return {"last": last, "text": text[:max_chars]}

    return await asyncio.to_thread(_run)


def _mime(to: list[str], subject: str, body: str, cc: list[str] | None, reply_to_msgid: str | None,
          html: str | None = None) -> str:
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if reply_to_msgid:
        msg["In-Reply-To"] = reply_to_msgid
        msg["References"] = reply_to_msgid
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


async def create_draft(to: list[str], subject: str, body: str, cc: list[str] | None = None,
                       thread_id: str | None = None, reply_to_msgid: str | None = None) -> str:
    svc = await _svc()
    message = {"raw": _mime(to, subject, body, cc, reply_to_msgid)}
    if thread_id:
        message["threadId"] = thread_id
    res = await asyncio.to_thread(lambda: svc.users().drafts().create(userId="me", body={"message": message}).execute())
    return res["id"]


async def update_draft(draft_id: str, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                       thread_id: str | None = None, reply_to_msgid: str | None = None) -> str:
    svc = await _svc()
    message = {"raw": _mime(to, subject, body, cc, reply_to_msgid)}
    if thread_id:
        message["threadId"] = thread_id
    res = await asyncio.to_thread(
        lambda: svc.users().drafts().update(userId="me", id=draft_id, body={"message": message}).execute()
    )
    return res["id"]


async def send_draft(draft_id: str) -> str:
    """Called ONLY from the approval endpoint."""
    svc = await _svc()
    res = await asyncio.to_thread(lambda: svc.users().drafts().send(userId="me", body={"id": draft_id}).execute())
    return res["id"]


async def search(q: str, max_results: int = 10) -> list[dict]:
    """Live Gmail search (same syntax as the Gmail search box, e.g. 'from:lensa newer_than:7d')."""
    svc = await _svc()

    def _run() -> list[dict]:
        resp = svc.users().messages().list(userId="me", q=q, maxResults=max_results).execute()
        out = []
        for m in resp.get("messages", []):
            full = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                              metadataHeaders=["From", "To", "Subject", "Date"]).execute()
            h = full["payload"].get("headers", [])
            out.append({"id": m["id"], "thread_id": m["threadId"], "from": _header(h, "From"),
                        "to": _header(h, "To"), "subject": _header(h, "Subject"), "date": _header(h, "Date"),
                        "snippet": full.get("snippet", "")})
        return out

    return await asyncio.to_thread(_run)


async def get_message(message_id: str) -> dict:
    """One full message, parsed like fetch_new() output (for importing a mail found by live search)."""
    svc = await _svc()
    m = await asyncio.to_thread(lambda: svc.users().messages().get(userId="me", id=message_id, format="full").execute())
    return _parse(m)


async def send_message(to: list[str], subject: str, body: str, html: str | None = None) -> str:
    """Raw send. Do not call directly: google_svc/send.py wraps it (draft approval, or self-addressed reminders)."""
    svc = await _svc()
    raw = _mime(to, subject, body, None, None, html)
    res = await asyncio.to_thread(lambda: svc.users().messages().send(userId="me", body={"raw": raw}).execute())
    return res["id"]
