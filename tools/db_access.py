"""Generic database tools: SELECT, INSERT, UPDATE on policy-approved tables. There is no delete."""
from __future__ import annotations

from langchain_core.tools import tool

from database import generic
from tools._util import out, parse_json, safe


@tool
@safe
async def db_describe() -> str:
    """List the tables you may use, what you can do on each (select/insert/update), their columns and which
    columns are writable. Call this before using db_select/db_insert/db_update on an unfamiliar table."""
    return out(await generic.describe())


@tool
@safe
async def db_select(table: str, columns: str | None = None, filters_json: str | None = None,
                    order_by: str | None = None, descending: bool = True, limit: int = 25) -> str:
    """Read rows from a table (your own data only). columns: comma list (default all).
    filters_json: JSON list like [{"column":"status","op":"=","value":"open"}]. Ops: = != < <= > >= like ilike
    in is_null not_null contains (for array columns). Max 100 rows."""
    cols = [c.strip() for c in columns.split(",")] if columns else None
    filters = parse_json(filters_json, "filters_json") or []
    return out(await generic.select(table, cols, filters, order_by, descending, limit))


@tool
@safe
async def db_insert(table: str, values_json: str) -> str:
    """Add a row. values_json is a JSON object of column -> value, e.g. {"name":"Mom","aliases":["amma"],"phone":"..."}.
    user_id, id and timestamps are filled in for you. Fails (with the allowed columns) if a column is not writable."""
    values = parse_json(values_json, "values_json")
    if not isinstance(values, dict):
        raise ValueError("values_json must be a JSON object")
    return out(await generic.insert(table, values))


@tool
@safe
async def db_update(table: str, row_id: str, changes_json: str) -> str:
    """Change one row by its id. changes_json is a JSON object of column -> new value. Only the listed columns
    change. To 'remove' something, set its status (done/cancelled/ignored) or active=false. Rows are never deleted.
    If the user has not told you the new value, ask them first instead of guessing."""
    changes = parse_json(changes_json, "changes_json")
    if not isinstance(changes, dict):
        raise ValueError("changes_json must be a JSON object")
    return out(await generic.update(table, row_id, changes))
