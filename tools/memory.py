"""Semantic memory tools. Nothing is deleted: outdated facts are retired (valid_to) and keep history."""
from __future__ import annotations

from langchain_core.tools import tool

from database import audit, db
from database import memory as memory_db
from tools._util import out, safe


@tool
@safe
async def remember(content: str, kind: str = "fact", importance: float = 0.6) -> str:
    """Save something worth remembering long-term: a fact ("I moved to a new office"), a preference
    ("rice portion is 250g"), a commitment or an episode. kind: fact | preference | commitment | episode.
    If it contradicts an older similar fact, the old one is retired automatically and history is kept.
    importance 0-1."""
    res = await memory_db.remember_or_update(kind if kind in memory_db.KINDS else "fact", content, importance)
    return out(res)


@tool
@safe
async def recall(query: str, limit: int = 6) -> str:
    """Search long-term memory for facts, preferences and past commitments relevant to a topic."""
    rows = await memory_db.retrieve(query, limit)
    return out([{"id": r["id"], "kind": r["kind"], "content": r["content"], "when": r["created_at"]} for r in rows])


@tool
@safe
async def retire_memory(query: str) -> str:
    """Mark a memory as no longer true (soft: valid_to is set, the row is kept). Use when the user says a stored
    fact is wrong or outdated and there is no replacement."""
    rows = await memory_db.retrieve(query, 1)
    if not rows:
        return out({"error": "No matching memory"})
    await db.execute("UPDATE memories SET valid_to=now() WHERE id=%s AND user_id=%s", rows[0]["id"], db.owner_id())
    await audit.record("agent", "status_change", "memories", rows[0]["id"], {"valid_to": None}, {"valid_to": "now"})
    return out({"retired": rows[0]["content"]})
