"""Audit trail: every autonomous write is recorded so nothing is silent."""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from database import db

log = logging.getLogger("audit")


def jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items() if k != "embedding"}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, bytes):
        return "<bytes>"
    return v


async def record(actor: str, action: str, table: str, row_id: Any, before: Any = None, after: Any = None) -> None:
    try:
        await db.execute(
            "INSERT INTO audit_log(user_id, actor, action, table_name, row_id, before, after) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            db.owner_id(), actor, action, table, str(row_id) if row_id is not None else None,
            db.jsonb(jsonable(before)) if before is not None else None,
            db.jsonb(jsonable(after)) if after is not None else None,
        )
    except Exception as e:  # auditing must never break the main flow
        log.warning("audit failed: %s", e)
