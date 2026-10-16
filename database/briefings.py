"""Precomputed briefings (read fast, no LLM at request time)."""
from __future__ import annotations

from datetime import date
from typing import Any

from database import db


async def save(kind: str, for_date: date, content: str, structured: dict | None = None, overwrite: bool = True) -> dict:
    from database.audit import jsonable

    if overwrite:
        return await db.fetchrow(
            "INSERT INTO briefings(user_id,kind,for_date,content,structured) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id,kind,for_date) DO UPDATE SET content=EXCLUDED.content, structured=EXCLUDED.structured, "
            "generated_at=now() RETURNING *",
            db.owner_id(), kind, for_date, content, db.jsonb(jsonable(structured or {})),
        )
    return await db.fetchrow(
        "INSERT INTO briefings(user_id,kind,for_date,content,structured) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (user_id,kind,for_date) DO NOTHING RETURNING *",
        db.owner_id(), kind, for_date, content, db.jsonb(jsonable(structured or {})),
    )


async def get(kind: str, for_date: date) -> dict | None:
    return await db.fetchrow(
        "SELECT * FROM briefings WHERE user_id=%s AND kind=%s AND for_date=%s", db.owner_id(), kind, for_date
    )


async def mark_delivered(briefing_id: Any) -> None:
    await db.execute("UPDATE briefings SET delivered_at=now() WHERE id=%s", briefing_id)
