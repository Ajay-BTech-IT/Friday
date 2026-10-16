"""Policy-guarded generic table access for the autonomous agent: SELECT, INSERT, UPDATE. Never DELETE.

Why it exists: dedicated tools cover the common flows; this lets the agent still help with anything else
in the data model (contacts, notes in extra columns, corrections) without a new tool for each case.

Guarantees
  * Only tables in POLICY are reachable. Secrets (oauth_credentials), users, schema_migrations are not.
  * user_id is injected and filtered automatically; the agent cannot read or write another scope.
  * Column allow-lists per table; ids, timestamps, embeddings and approval fields are never writable.
  * UPDATE is by primary key only (no mass updates). No DELETE/DDL exists (db._guard also blocks them).
  * Every write is recorded in audit_log.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from database import audit, db

GLOBAL_BLOCKED = {"id", "user_id", "created_at", "embedding", "source_message_id", "source_email_id"}


@dataclass
class Policy:
    select: bool = True
    insert: bool = False
    update: bool = False
    scope: str = "user"                      # user | meal | food | none
    writable: set[str] | None = None         # None = all columns minus blocked
    blocked: set[str] = field(default_factory=set)
    hidden: set[str] = field(default_factory=set)   # never returned
    note: str = ""


POLICY: dict[str, Policy] = {
    "contacts": Policy(insert=True, update=True, note="people the agent can act on; aliases text[]"),
    "tasks": Policy(insert=True, update=True, blocked={"completed_at", "calendar_event_id"},
                    note="prefer create_task/complete_task tools for reminders"),
    "routines": Policy(insert=True, update=True),
    "reminders": Policy(insert=True, update=True, blocked={"sent_at", "attempts", "dedupe_key"}),
    "calendar_events": Policy(insert=True, update=True, blocked={"google_event_id", "etag", "created_by"},
                              note="prefer calendar tools so Google stays in sync"),
    "briefings": Policy(),
    "emails": Policy(update=True, writable={"category", "importance", "needs_reply", "status", "summary"},
                     hidden={"body_text"}),
    "email_drafts": Policy(insert=True, update=True,
                           writable={"to_addrs", "cc_addrs", "subject", "body"},
                           note="status/approval is NEVER writable; only the user approves via the API"),
    "nutrition_goals": Policy(insert=True, update=True),
    "food_catalog": Policy(insert=True, update=True, scope="food", blocked={"verified"}),
    "meals": Policy(insert=True, update=True, scope="user"),
    "meal_items": Policy(insert=True, update=True, scope="meal"),
    "daily_nutrition": Policy(),
    "memories": Policy(note="use remember/update_fact/retire_memory tools to change memories"),
    "messages": Policy(hidden={"tool_calls"}),
    "conversations": Policy(),
    "notifications": Policy(update=True, writable={"seen_at"}),
    "personas": Policy(insert=True, update=True, scope="none", blocked={"is_default"}),
    "audit_log": Policy(),
    "agent_runs": Policy(),
}

_cols: dict[str, dict[str, tuple[str, str]]] = {}


async def _columns(table: str) -> dict[str, tuple[str, str]]:
    if not _cols:
        rows = await db.fetch(
            "SELECT table_name, column_name, data_type, udt_name FROM information_schema.columns "
            "WHERE table_schema='public' ORDER BY table_name, ordinal_position"
        )
        for r in rows:
            _cols.setdefault(r["table_name"], {})[r["column_name"]] = (r["data_type"], r["udt_name"])
    return _cols.get(table, {})


def _cast(col_type: tuple[str, str]) -> str:
    data_type, udt = col_type
    if data_type == "ARRAY":
        return udt.lstrip("_") + "[]"
    if data_type == "USER-DEFINED":
        return udt
    return udt


def _wrap(col_type: tuple[str, str], value: Any) -> Any:
    if col_type[1] in ("jsonb", "json"):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        return db.jsonb(value)
    return value


def _policy(table: str) -> Policy:
    p = POLICY.get(table)
    if p is None:
        raise ValueError(f"Table '{table}' is not available. Available: {', '.join(sorted(POLICY))}")
    return p


def _scope_sql(table: str, p: Policy) -> tuple[str, list]:
    uid = db.owner_id()
    if p.scope == "user":
        return "user_id = %s", [uid]
    if p.scope == "meal":
        return "meal_id IN (SELECT id FROM meals WHERE user_id = %s)", [uid]
    if p.scope == "food":
        return "(user_id IS NULL OR user_id = %s)", [uid]
    return "TRUE", []


def _writable_cols(table: str, p: Policy, cols: dict) -> set[str]:
    base = set(p.writable) if p.writable is not None else set(cols)
    return {c for c in base if c in cols and c not in GLOBAL_BLOCKED and c not in p.blocked}


def _clean(row: dict, p: Policy) -> dict:
    out = {}
    for k, v in row.items():
        if k == "embedding" or k in p.hidden:
            continue
        out[k] = v
    return audit.jsonable(out)


async def describe() -> list[dict]:
    out = []
    for t, p in sorted(POLICY.items()):
        cols = await _columns(t)
        if not cols:
            continue
        w = _writable_cols(t, p, cols) if (p.insert or p.update) else set()
        out.append({
            "table": t,
            "can": [x for x, ok in (("select", p.select), ("insert", p.insert), ("update", p.update)) if ok],
            "columns": {c: cols[c][1] for c in cols if c != "embedding" and c not in p.hidden},
            "writable_columns": sorted(w),
            "note": p.note,
        })
    return out


_OPS = {"=": "=", "!=": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">=", "like": "LIKE", "ilike": "ILIKE"}


async def select(table: str, columns: list[str] | None = None, filters: list[dict] | None = None,
                 order_by: str | None = None, descending: bool = True, limit: int = 25) -> list[dict]:
    p = _policy(table)
    cols = await _columns(table)
    sel_cols = [c for c in (columns or list(cols)) if c in cols and c != "embedding" and c not in p.hidden]
    if not sel_cols:
        raise ValueError("No valid columns requested")
    scope_sql, params = _scope_sql(table, p)
    where = [scope_sql]
    for f in filters or []:
        c, op = f.get("column"), str(f.get("op", "=")).lower()
        if c not in cols:
            raise ValueError(f"Unknown column '{c}' in {table}")
        ct = cols[c]
        if op == "is_null":
            where.append(f'"{c}" IS NULL')
        elif op == "not_null":
            where.append(f'"{c}" IS NOT NULL')
        elif op == "in":
            vals = f["value"] if isinstance(f["value"], list) else str(f["value"]).split(",")
            where.append(f'"{c}" = ANY(%s::{_cast(ct)}[])' if ct[0] != "ARRAY" else f'"{c}" && %s::{_cast(ct)}')
            params.append([str(v).strip() for v in vals])
        elif op == "contains" and ct[0] == "ARRAY":
            where.append(f'%s = ANY("{c}")')
            params.append(str(f["value"]))
        elif op in _OPS:
            cast = "text" if op in ("like", "ilike") else _cast(ct)
            lhs = f'"{c}"::text' if op in ("like", "ilike") else f'"{c}"'
            where.append(f"{lhs} {_OPS[op]} %s::{cast}")
            params.append(f["value"])
        else:
            raise ValueError(f"Unsupported op '{op}'. Use = != < <= > >= like ilike in is_null not_null contains")
    order = ""
    if order_by:
        if order_by not in cols:
            raise ValueError(f"Unknown order_by column '{order_by}'")
        order = f' ORDER BY "{order_by}" {"DESC" if descending else "ASC"}'
    sql = f'SELECT {", ".join(chr(34) + c + chr(34) for c in sel_cols)} FROM "{table}" WHERE {" AND ".join(where)}{order} LIMIT %s'
    rows = await db.fetch(sql, *params, max(1, min(int(limit), 100)))
    return [_clean(r, p) for r in rows]


async def insert(table: str, values: dict[str, Any], actor: str = "agent") -> dict:
    p = _policy(table)
    if not p.insert:
        raise ValueError(f"Inserting into '{table}' is not allowed via generic access")
    cols = await _columns(table)
    writable = _writable_cols(table, p, cols)
    bad = [k for k in values if k not in writable]
    if bad:
        raise ValueError(f"Columns not writable in {table}: {bad}. Writable: {sorted(writable)}")
    names, marks, params = [], [], []
    if "user_id" in cols:
        names.append("user_id"); marks.append("%s"); params.append(db.owner_id())
    if p.scope == "meal":
        meal_id = values.get("meal_id")
        if not meal_id or not await db.fetchval("SELECT 1 FROM meals WHERE id=%s AND user_id=%s", UUID(str(meal_id)), db.owner_id()):
            raise ValueError("meal_id must reference one of your meals")
    for k, v in values.items():
        names.append(k)
        marks.append(f"%s::{_cast(cols[k])}")
        params.append(_wrap(cols[k], v))
    sql = f'INSERT INTO "{table}"({", ".join(chr(34) + n + chr(34) for n in names)}) VALUES ({", ".join(marks)}) RETURNING *'
    row = await db.fetchrow(sql, *params)
    await audit.record(actor, "insert", table, row.get("id"), None, row)
    await _after_write(table, row)
    return _clean(row, p)


async def update(table: str, row_id: str, changes: dict[str, Any], actor: str = "agent") -> dict:
    p = _policy(table)
    if not p.update:
        raise ValueError(f"Updating '{table}' is not allowed via generic access")
    cols = await _columns(table)
    writable = _writable_cols(table, p, cols)
    bad = [k for k in changes if k not in writable]
    if bad:
        raise ValueError(f"Columns not writable in {table}: {bad}. Writable: {sorted(writable)}")
    if not changes:
        raise ValueError("No changes supplied")
    scope_sql, scope_params = _scope_sql(table, p)
    if p.scope == "food":  # global catalog rows are read-only; only your own entries can change
        scope_sql, scope_params = "user_id = %s", [db.owner_id()]
    rid = UUID(str(row_id))
    before = await db.fetchrow(f'SELECT * FROM "{table}" WHERE id=%s AND {scope_sql}', rid, *scope_params)
    if not before:
        raise ValueError(f"No {table} row with id {row_id} in your data")
    if table == "email_drafts" and before["status"] != "pending_approval":
        raise ValueError("Only drafts that are pending approval can be edited")
    sets = ", ".join(f'"{k}"=%s::{_cast(cols[k])}' for k in changes)
    row = await db.fetchrow(
        f'UPDATE "{table}" SET {sets} WHERE id=%s AND {scope_sql} RETURNING *',
        *[_wrap(cols[k], v) for k, v in changes.items()], rid, *scope_params,
    )
    await audit.record(actor, "update", table, rid, before, row)
    await _after_write(table, row)
    return _clean(row, p)


async def _after_write(table: str, row: dict) -> None:
    """Keep derived rows (reminders) consistent when the agent edits the source rows directly."""
    if table == "tasks":
        from database import tasks

        await tasks.resync_task(row["id"])
    elif table == "personas":
        from database import persona

        persona.invalidate()
    elif table == "calendar_events":
        from database import tasks

        if row["status"] != "cancelled":
            await tasks.event_reminders(row)
