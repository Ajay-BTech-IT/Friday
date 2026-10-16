"""Reminder emails: subject and body are the same short friendly line the notification shows (no persona label)."""
from __future__ import annotations

import html as htmllib
import logging
import re
from datetime import datetime

from orchestrator.model import chat_model, text_of
from timeutil import tz

log = logging.getLogger("reminder_mail")

_FENCE = re.compile(r"^```(?:html)?\s*|\s*```$", re.I)
_BLOCK = re.compile(r"<(script|style\s+[^>]*src|iframe|object|embed|form|link|meta|base)\b[\s\S]*?(?:</\1>|/?>)", re.I)
_EVENT = re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_JS = re.compile(r"javascript:", re.I)


def sanitize(doc: str) -> str:
    """Keep the markup Gemini wrote but drop anything active or remote (scripts, iframes, images, handlers)."""
    doc = _FENCE.sub("", doc.strip())
    doc = _BLOCK.sub("", doc)
    doc = _IMG.sub("", doc)
    doc = _EVENT.sub("", doc)
    return _JS.sub("", doc)


def to_plain(doc: str) -> str:
    t = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", doc)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|li|tr)>", "\n", t)
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", t)).strip()


def simple_html(line: str, when: str) -> str:
    return (
        '<div style="font-family:Segoe UI,Arial,sans-serif;background:#f4f6f8;padding:24px">'
        '<div style="max-width:520px;margin:auto;background:#ffffff;border-radius:14px;padding:28px;'
        'border:1px solid #e3e8ee">'
        f'<h2 style="margin:0 0 10px;color:#14213d;font-size:24px;line-height:1.3">{htmllib.escape(line)}</h2>'
        f'<div style="color:#6b7785;font-size:14px">{htmllib.escape(when)}</div></div></div>'
    )


async def build(message: str, persona: dict, fire_at: datetime, owner_name: str, tzname: str,
                line: str | None = None) -> tuple[str, str, str]:
    """Returns (subject, plain_text, html). The mail says exactly what the notification says: no persona label,
    no sign-off, no generated extras."""
    clean = message.removeprefix("Reminder: ").strip()
    text = (line or clean).strip()
    when = fire_at.astimezone(tz(tzname)).strftime("%I:%M %p, %A %d %B").lstrip("0")
    subject = text if len(text) <= 80 else text[:77] + "..."
    return subject, f"{text}\n\n{when}", simple_html(text, when)
