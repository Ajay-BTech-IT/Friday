"""call_contact: the agent never dials. It returns a CLIENT ACTION; the phone app resolves the number and calls."""
from __future__ import annotations

import re

from langchain_core.tools import tool

from database import db
from tools._util import out, safe


@tool
@safe
async def call_contact(name_or_number: str) -> str:
    """Place a phone call from the user's phone ("call Mom", "call 98765 43210"). Pass the person's name as the user
    said it, or a phone number. The phone app looks the name up in its own contacts and dials it; you only trigger it.
    After calling this tool just say you are calling them (e.g. "Calling Mom."). Only works when the user is on the
    phone app."""
    q = name_or_number.strip()
    digits = re.sub(r"[^\d+]", "", q)
    if len(digits) >= 7 and not re.search(r"[a-zA-Z]", q):
        return out({"client_action": {"type": "call", "number": digits, "label": q}})
    row = await db.fetchrow(
        "SELECT name, aliases, phone FROM contacts WHERE user_id=%s AND (lower(name)=lower(%s) OR %s = ANY(SELECT lower(a) FROM unnest(aliases) a)) LIMIT 1",
        db.owner_id(), q, q.lower(),
    )
    names = [q]
    number = None
    if row:
        names = list(dict.fromkeys([row["name"], q, *(row["aliases"] or [])]))
        number = re.sub(r"[^\d+]", "", row["phone"] or "") or None
    return out({"client_action": {"type": "call", "name": q, "names": names, "number": number, "label": row["name"] if row else q}})
