"""Briefing + notification tools."""
from __future__ import annotations

from langchain_core.tools import tool

from database import briefings as briefings_db
from timeutil import local_today
from tools._util import out, safe


@tool
@safe
async def get_briefing(kind: str = "morning") -> str:
    """Today's precomputed briefing. kind: morning | evening. Generated on the spot if it does not exist yet."""
    day = local_today()
    b = await briefings_db.get(kind, day)
    if not b:
        from jobs import briefing

        b = await briefing.generate(kind, day)
    return out({"kind": kind, "for": day, "content": b["content"]})
