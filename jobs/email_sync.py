"""Overnight Gmail job: fetch -> classify + summarise -> action items become tasks."""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from database import emails as emails_db
from database import tasks as tasks_db
from google_svc import gmail
from jobs import notify
from orchestrator.model import chat_model

log = logging.getLogger("email_sync")


class Judgement(BaseModel):
    n: int = Field(description="The number of the email as listed")
    category: Literal["work", "finance", "travel", "promo", "personal", "other"]
    importance: int = Field(description="1-5. 5 = needs action today; newsletters/promos = 1-2")
    summary: str = Field(description="One short sentence: what it is and what is asked")
    needs_reply: bool
    action_items: list[str] = Field(default=[], description="Concrete to-dos for the user, if any")


class Judgements(BaseModel):
    items: list[Judgement]


PROMPT = """Classify these emails for a busy professional. Be strict about importance: promos, newsletters,
notifications and receipts are 1-2. Real people asking something, deadlines, bills, travel, HR are 3-5.
needs_reply only if a person expects an answer. action_items ONLY when the sender demands something with a
deadline from the user (a bill due, a document requested, a reply by a date). NEVER marketing, newsletters,
notifications, social-media or job-alert emails, and never how-to steps or suggestions.

{emails}"""


async def classify_pending() -> dict:
    rows = await emails_db.pending_classification(40)
    stats = {"classified": 0, "important": 0, "tasks": 0}
    for i in range(0, len(rows), 8):
        batch = rows[i:i + 8]
        listing = "\n\n".join(
            f"[{n}] From: {e['from_addr']}\nSubject: {e['subject']}\n{(e['body_text'] or e['snippet'] or '')[:1500]}"
            for n, e in enumerate(batch, 1)
        )
        try:
            res = await chat_model(0.0).with_structured_output(Judgements).ainvoke(PROMPT.format(emails=listing))
        except Exception as e:
            log.warning("classification failed, will retry later: %s", e)
            break
        by_n = {j.n: j for j in res.items}
        for n, e in enumerate(batch, 1):
            j = by_n.get(n)
            if not j:
                continue
            await emails_db.set_classification(e["id"], j.category, j.importance, j.summary, j.needs_reply)
            stats["classified"] += 1
            if j.importance >= 4:
                stats["important"] += 1
            # at most ONE task, only for genuinely important non-marketing mail, and no pushed reminder (no due_hint)
            if j.importance >= 4 and j.category in ("work", "finance", "travel") and j.action_items:
                r = await tasks_db.create_task(j.action_items[0], notes=f"From email: {e['subject']}", priority=3,
                                               source="email", source_email_id=e["id"], actor="job")
                stats["tasks"] += 0 if r["duplicate"] else 1
            if j.importance >= 5:
                await notify.deliver("Important email", f"{e['from_addr']}: {j.summary}", "email", True)
    return stats


async def run() -> dict:
    fetched = await gmail.fetch_new()
    new = 0
    for m in fetched:
        _, is_new = await emails_db.upsert_email(m)
        new += 1 if is_new else 0
    stats = await classify_pending()
    return {"fetched": len(fetched), "new": new, **stats}
