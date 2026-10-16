"""After-reply learning: pull commitments, facts and preferences out of what the user said.

"need to buy eggs" -> task (due_hint after_office); "I moved offices" -> new fact, old one retired.
Runs in the background on messages where extracted = false, so chat latency is unaffected.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from database import conversations as conv_db
from database import db
from database import memory as memory_db
from database import tasks as tasks_db
from orchestrator.model import chat_model
from timeutil import local_now, parse_when

log = logging.getLogger("extractor")


class Commitment(BaseModel):
    title: str = Field(description="Short imperative task, e.g. 'Buy eggs'")
    due_hint: Literal["morning", "before_leaving", "before_office", "afternoon", "lunch", "after_office",
                      "evening", "tonight", "bedtime", "tomorrow_morning", "none"] = "none"
    due_at: str | None = Field(None, description="ISO 8601 local datetime ONLY if the user gave an exact day/time")
    priority: int = Field(2, description="1 urgent .. 4 low")


class Fact(BaseModel):
    content: str = Field(description="Self-contained sentence about the user, third person ('User moved to ...')")
    kind: Literal["fact", "preference"] = "fact"
    importance: float = Field(0.5, description="0-1")


class Extraction(BaseModel):
    commitments: list[Commitment] = []
    facts: list[Fact] = []


PROMPT = """You read ONE message from the user to their personal assistant and extract what is worth keeping.
Now: {now}.

Extract:
- commitments: the USER'S OWN real-world to-dos that they state as an intention ("need to buy eggs", "have to pay the
  rent"). NEVER extract a request addressed to the assistant: send/draft/compose/reply to an email, call someone,
  schedule or book something, check mail, log a meal, "remind me ...", questions. The assistant does those itself.
  Not things already finished, not general chat.
- facts/preferences: durable information about the user (where they work/live, people, habits, likes, portion
  sizes, goals). Not one-off moods, not meal logs, not tasks.
If nothing qualifies return empty lists. Be conservative; precision beats recall.
If the assistant already handled it (it used tools), extract NO commitments.

Recent conversation (oldest first):
{context}

USER MESSAGE TO ANALYSE:
{message}
"""


async def extract_message(msg: dict) -> dict:
    ctx_rows = await conv_db.context_around(msg["id"], 4)
    context = "\n".join(f"{r['role']}: {r['content']}" for r in reversed(ctx_rows))
    prompt = PROMPT.format(now=local_now().strftime("%A %d %B %Y %H:%M"), context=context or "(none)", message=msg["content"])
    res: Extraction = await chat_model(0.0).with_structured_output(Extraction).ainvoke(prompt)

    # If the assistant used tools in answering this message it already did the work (drafted, scheduled, called...):
    # a leftover "task" would only nag the user later about something that is done.
    reply = await db.fetchrow(
        "SELECT tool_calls FROM messages WHERE conversation_id=(SELECT conversation_id FROM messages WHERE id=%s) "
        "AND role='assistant' AND created_at >= (SELECT created_at FROM messages WHERE id=%s) "
        "AND created_at < COALESCE((SELECT min(created_at) FROM messages WHERE role='user' AND created_at > "
        "(SELECT created_at FROM messages WHERE id=%s) AND conversation_id=(SELECT conversation_id FROM messages WHERE id=%s)), 'infinity') "
        "ORDER BY created_at LIMIT 1",
        msg["id"], msg["id"], msg["id"], msg["id"])
    if reply and reply["tool_calls"] and reply["tool_calls"].get("tools"):
        res.commitments = []

    made = {"tasks": 0, "memories": 0}
    for c in res.commitments:
        due = None
        if c.due_at:
            try:
                due = parse_when(c.due_at)
            except ValueError:
                due = None
        r = await tasks_db.create_task(
            c.title, due_hint=None if c.due_hint == "none" else c.due_hint, due_at=due, priority=c.priority,
            source="extracted", source_message_id=msg["id"], actor="extractor",
        )
        made["tasks"] += 0 if r["duplicate"] else 1
    for f in res.facts:
        try:
            r = await memory_db.remember_or_update(f.kind, f.content, f.importance, msg["id"], actor="extractor")
            made["memories"] += 1 if r["status"] != "already_known" else 0
        except Exception as e:
            log.warning("memory write skipped: %s", e)
    return made


async def run_batch(limit: int = 5) -> int:
    """Process pending user messages. A message is marked extracted even on partial failure only if the LLM call
    succeeded, so transient network errors are retried on the next tick."""
    rows = await conv_db.unextracted(limit)
    done = 0
    for m in rows:
        # let the assistant reply land first so the extractor sees the whole exchange
        from timeutil import utcnow

        if (utcnow() - m["created_at"]).total_seconds() < 5:
            continue
        try:
            made = await extract_message(m)
            await conv_db.mark_extracted(m["id"])
            done += 1
            if made["tasks"] or made["memories"]:
                log.info("extracted from message: %s", made)
        except Exception as e:
            log.warning("extraction failed for %s: %s", m["id"], e)
    return done
