"""Shared helpers for LangChain tools: JSON results and errors the model can recover from."""
from __future__ import annotations

import functools
import json
import logging
from typing import Any

from database.audit import jsonable

log = logging.getLogger("tools")


def out(obj: Any) -> str:
    return json.dumps(jsonable(obj), ensure_ascii=False, default=str)


def safe(fn):
    """Return {"error": ...} instead of raising, so the agent can explain or retry."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - tool boundary
            log.warning("tool %s failed: %s", fn.__name__, e)
            return out({"error": f"{type(e).__name__}: {e}"})

    return wrapper


def parse_json(text: str | None, what: str) -> Any:
    if text is None or text == "":
        return None
    try:
        return json.loads(text)
    except ValueError as e:
        raise ValueError(f"{what} must be valid JSON: {e}")
