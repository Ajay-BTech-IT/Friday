"""Conversations + messages (short-term memory and audit)."""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from database import db

IDLE_GAP = timedelta(hours=6)


async def current_conversation(device_id: UUID | None = None) -> UUID:
    """Reuse the latest conversation unless idle for 6h (then start a fresh one)."""
    row = await db.fetchrow(
        "SELECT c.id, COALESCE(max(m.created_at), c.started_at) AS last_at FROM conversations c "
        "LEFT JOIN messages m ON m.conversation_id=c.id WHERE c.user_id=%s GROUP BY c.id "
        "ORDER BY last_at DESC LIMIT 1",
        db.owner_id(),
    )
    from timeutil import utcnow

    if row and utcnow() - row["last_at"] < IDLE_GAP:
        return row["id"]
    new = await db.fetchrow(
        "INSERT INTO conversations(user_id, device_id) VALUES (%s,%s) RETURNING id", db.owner_id(), device_id
    )
    return new["id"]


async def add_message(conversation_id: UUID, role: str, content: str | None, tool_calls=None, channel: str = "text") -> dict:
    return await db.fetchrow(
        "INSERT INTO messages(conversation_id,user_id,role,content,tool_calls,channel,extracted) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        conversation_id, db.owner_id(), role, content,
        db.jsonb(tool_calls) if tool_calls is not None else None, channel,
        role != "user",  # only user messages need extraction
    )


async def recent(conversation_id: UUID, limit: int = 12) -> list[dict]:
    rows = await db.fetch(
        "SELECT role, content, created_at FROM messages WHERE conversation_id=%s AND role IN ('user','assistant') "
        "AND content IS NOT NULL ORDER BY created_at DESC LIMIT %s",
        conversation_id, limit,
    )
    return list(reversed(rows))


async def unextracted(limit: int = 10) -> list[dict]:
    return await db.fetch(
        "SELECT id, conversation_id, content, created_at FROM messages WHERE user_id=%s AND NOT extracted AND role='user' "
        "ORDER BY created_at LIMIT %s",
        db.owner_id(), limit,
    )


async def mark_extracted(message_id: UUID) -> None:
    await db.execute("UPDATE messages SET extracted=true WHERE id=%s", message_id)


async def context_around(message_id: UUID, before: int = 4) -> list[dict]:
    """The few messages leading up to (and including the reply to) a user message."""
    return await db.fetch(
        "SELECT role, content FROM messages WHERE conversation_id=(SELECT conversation_id FROM messages WHERE id=%s) "
        "AND role IN ('user','assistant') AND content IS NOT NULL AND created_at <= "
        "(SELECT created_at FROM messages WHERE id=%s) + interval '2 minutes' "
        "ORDER BY created_at DESC LIMIT %s",
        message_id, message_id, before + 2,
    )


async def log_run(message_id: UUID | None, intent: str | None, tools_called: list[str], retrieval_ms: int,
                  llm_ms: int, total_ms: int, tokens_in: int | None = None, tokens_out: int | None = None,
                  error: str | None = None) -> None:
    await db.execute(
        "INSERT INTO agent_runs(user_id,message_id,intent,tools_called,retrieval_ms,llm_ms,total_ms,tokens_in,tokens_out,error) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        db.owner_id(), message_id, intent, tools_called, retrieval_ms, llm_ms, total_ms, tokens_in, tokens_out, error,
    )
