"""Semantic memory (pgvector): facts, preferences, commitments, summaries.

Facts that change are never overwritten: the old row gets valid_to + superseded_by
so history stays and retrieval only sees what is currently true.
"""
from __future__ import annotations

import math
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from database import audit, db
from config import settings
from orchestrator.model import safe_embed, safe_embed_query

log = logging.getLogger("memory")

KINDS = ("fact", "preference", "episode", "commitment", "summary")
DUP_DISTANCE = 0.08  # cosine distance under which two memories are "the same"


async def find_similar(embedding: list[float], kind: str | None = None, max_distance: float = 0.25, limit: int = 3) -> list[dict]:
    v = db.vec(embedding)
    rows = await db.fetch(
        "SELECT id, kind, content, importance, (embedding <=> %s::vector) AS distance FROM memories "
        "WHERE user_id=%s AND valid_to IS NULL AND (%s::text IS NULL OR kind=%s) AND metadata->>'embedder' = %s "
        "ORDER BY embedding <=> %s::vector LIMIT %s",
        v, db.owner_id(), kind, kind, settings.embedder_tag, v, limit,
    )
    return [r for r in rows if r["distance"] <= max_distance]


async def store(
    kind: str,
    content: str,
    importance: float = 0.5,
    source_message_id: UUID | None = None,
    metadata: dict | None = None,
    supersedes: UUID | None = None,
    actor: str = "agent",
) -> dict:
    """Store a memory. Near-duplicates are merged (importance bumped), not repeated."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    content = content.strip()
    emb = await safe_embed(content)
    if emb is None:
        raise RuntimeError("Embedding unavailable right now; memory not stored")

    if supersedes is None:
        dup = await find_similar(emb, kind, DUP_DISTANCE, 1)
        if dup:
            await db.execute(
                "UPDATE memories SET importance=LEAST(1, GREATEST(importance, %s) + 0.05), access_count=access_count+1 WHERE id=%s",
                importance, dup[0]["id"],
            )
            return {"id": dup[0]["id"], "status": "already_known", "content": dup[0]["content"]}

    async with db.transaction() as tx:
        row = await tx.fetchrow(
            "INSERT INTO memories(user_id,kind,content,embedding,importance,source_message_id,metadata) "
            "VALUES (%s,%s,%s,%s::vector,%s,%s,%s) RETURNING id",
            db.owner_id(), kind, content, db.vec(emb), importance, source_message_id,
            db.jsonb({**(metadata or {}), "embedder": settings.embedder_tag}),
        )
        if supersedes:
            await tx.execute(
                "UPDATE memories SET valid_to=now(), superseded_by=%s WHERE id=%s AND valid_to IS NULL",
                row["id"], supersedes,
            )
    await audit.record(actor, "insert", "memories", row["id"], None, {"kind": kind, "content": content, "supersedes": supersedes})
    return {"id": row["id"], "status": "stored" if not supersedes else "superseded_old"}


async def remember_or_update(kind: str, content: str, importance: float = 0.6, source_message_id: UUID | None = None, actor: str = "agent") -> dict:
    """Store a fact; if it contradicts a very similar current fact, retire the old one."""
    emb = await safe_embed(content)
    if emb is None:
        raise RuntimeError("Embedding unavailable right now; memory not stored")
    near = await find_similar(emb, kind, 0.35, 1)
    if near and near[0]["distance"] > DUP_DISTANCE:
        return await store(kind, content, importance, source_message_id, supersedes=near[0]["id"], actor=actor)
    return await store(kind, content, importance, source_message_id, actor=actor)


async def retrieve(query: str, k: int = 6, query_embedding: list[float] | None = None) -> list[dict]:
    """Top-k current memories ranked by similarity + importance + recency."""
    emb = query_embedding or await safe_embed_query(query)
    if emb is None:
        return await _text_fallback(query, k)
    v = db.vec(emb)
    rows = await db.fetch(
        "SELECT id, kind, content, importance, created_at, 1 - (embedding <=> %s::vector) AS sim "
        "FROM memories WHERE user_id=%s AND valid_to IS NULL AND metadata->>'embedder' = %s "
        "ORDER BY embedding <=> %s::vector LIMIT %s",
        v, db.owner_id(), settings.embedder_tag, v, max(k * 3, 12),
    )
    now = datetime.now(timezone.utc)
    for r in rows:
        age_days = max((now - r["created_at"]).total_seconds() / 86400, 0)
        recency = math.exp(-age_days / 60)
        r["score"] = 0.6 * float(r["sim"]) + 0.25 * float(r["importance"]) + 0.15 * recency
    rows = [r for r in rows if r["sim"] >= 0.45]
    rows.sort(key=lambda r: r["score"], reverse=True)
    top = rows[:k]
    if top:
        await db.execute(
            "UPDATE memories SET access_count=access_count+1, last_accessed_at=now() WHERE id = ANY(%s)",
            [r["id"] for r in top],
        )
    return top


async def _text_fallback(query: str, k: int) -> list[dict]:
    return await db.fetch(
        "SELECT id, kind, content, importance, created_at, 0.5 AS sim FROM memories "
        "WHERE user_id=%s AND valid_to IS NULL AND to_tsvector('english', content) @@ plainto_tsquery('english', %s) "
        "ORDER BY importance DESC LIMIT %s",
        db.owner_id(), query, k,
    )


async def recent_commitments(since: datetime) -> list[dict]:
    return await db.fetch(
        "SELECT id, content, created_at FROM memories WHERE user_id=%s AND kind='commitment' AND created_at>=%s ORDER BY created_at",
        db.owner_id(), since,
    )


async def save_summary(content: str, metadata: dict[str, Any] | None = None) -> None:
    await store("summary", content, 0.4, metadata=metadata, actor="job")


async def reembed_all(batch: int = 16) -> dict:
    """Re-embed every memory whose vector came from a different model (or none), so switching embedders never leaves
    unusable vectors behind. Safe to run repeatedly; only touches rows that need it. Returns counts."""
    from orchestrator.model import embed_many

    done = failed = 0
    while True:
        rows = await db.fetch(
            "SELECT id, content FROM memories WHERE user_id=%s AND COALESCE(metadata->>'embedder','') <> %s "
            "ORDER BY created_at LIMIT %s",
            db.owner_id(), settings.embedder_tag, batch,
        )
        if not rows:
            break
        try:
            vecs = await embed_many([r["content"] for r in rows], "document")
        except Exception as e:
            log.warning("re-embedding stopped: %s", e)
            failed += len(rows)
            break
        for r, v in zip(rows, vecs):
            await db.execute(
                "UPDATE memories SET embedding=%s::vector, metadata = metadata || %s WHERE id=%s",
                db.vec(v), db.jsonb({"embedder": settings.embedder_tag}), r["id"],
            )
            done += 1
    return {"reembedded": done, "failed": failed, "model": settings.embedder_tag}
