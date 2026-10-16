"""Cheap pre-checks: no LLM call for persona switching or trivial messages."""
from __future__ import annotations

import re

from database import persona as persona_db

_SWITCH = re.compile(
    r"^\s*(?:hey\s+|hi\s+|ok\s+|okay\s+|please\s+)*"
    r"(?:switch(?:\s+(?:over\s+)?to)?|change(?:\s+(?:the\s+)?persona)?\s+to|be|become|talk\s+to|put\s+on|bring\s+(?:me\s+)?(?:back\s+)?)\s+"
    r"(?:me\s+)?(?:to\s+)?(?:the\s+)?(?P<name>[a-zA-Z][a-zA-Z ]{1,20}?)\s*[.!?]*\s*$",
    re.I,
)

_TRIVIAL = re.compile(r"^\s*(hi|hello|hey|yo|thanks|thank you|ok|okay|cool|good\s*(morning|night|evening)|bye)\W*$", re.I)
_MEAL = re.compile(r"\b(had|ate|eaten|drank|having|breakfast|lunch|dinner|snack)\b", re.I)


async def try_switch(text: str) -> dict | None:
    """Return the new persona if the message is a switch command, else None."""
    m = _SWITCH.match(text)
    if not m:
        return None
    return await persona_db.switch(m.group("name").strip())


def skip_retrieval(text: str) -> bool:
    """Memory retrieval is wasted on greetings, acknowledgements and plain meal logging."""
    if _TRIVIAL.match(text):
        return True
    return bool(_MEAL.search(text)) and len(text) < 120


# ---------------------------------------------------------------------------
# Sending email by chat: "send it" -> agent shows the draft -> "yes" -> sent.
# Decided by this code on the user's own typed words, never by the LLM.
# ---------------------------------------------------------------------------
_SEND = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|yes|yeah|please|pls|now)[,\s]+)*(?:send|dispatch)\s*(?:it|him|her|them|that|this|mail|email|"
    r"the\s+(?:mail|email|draft|message)|(?:it\s+)?now)?(?:\s+(?:out|now|please|pls))*\W*$",
    re.I,
)
_YES = re.compile(r"^\s*(?:yes|yeah|yep|yup|y|confirm(?:ed)?|sure|ok(?:ay)?|send(?:\s+it)?|do\s+it|go\s+ahead)\W*$", re.I)
_NO = re.compile(r"^\s*(?:no|nope|n|cancel|stop|wait|don'?t|do\s+not|not\s+yet)\b.*$", re.I)
_CONFIRM_TTL = 300  # seconds

_awaiting: dict = {"draft_id": None, "until": 0.0}


async def try_send_flow(text: str) -> str | None:
    """Return a reply if this message belongs to the send-confirmation flow, else None (LLM handles it)."""
    import time

    from database import emails as emails_db
    from google_svc.send import SendError, send_draft

    if _awaiting["draft_id"] and time.time() < _awaiting["until"]:
        draft_id = _awaiting["draft_id"]
        if _YES.match(text):
            _awaiting.update(draft_id=None, until=0.0)
            try:
                row = await send_draft(draft_id)
            except SendError as e:
                return f"I couldn't send it: {e}. The draft is still saved, you can try again."
            return f"Sent to {', '.join(row['to_addrs'])}."
        _awaiting.update(draft_id=None, until=0.0)
        if _NO.match(text):
            return "Okay, I won't send it. The draft stays in your drafts."
        return None  # something else entirely: forget the pending send and handle normally

    if _SEND.match(text):
        d = await emails_db.find_draft(None)
        if not d:
            return None
        _awaiting.update(draft_id=d["id"], until=time.time() + _CONFIRM_TTL)
        body = d["body"] if len(d["body"]) <= 400 else d["body"][:400] + "..."
        return (f"Ready to send this to {', '.join(d['to_addrs'])}, subject \"{d['subject']}\":\n\n{body}\n\n"
                "Say yes to send it, or tell me what to change.")
    return None


def arm_send(draft_id) -> None:
    """Called by the request_send_draft tool. Nothing is sent: the user's own typed 'yes' still has to arrive."""
    import time

    _awaiting.update(draft_id=draft_id, until=time.time() + _CONFIRM_TTL)
