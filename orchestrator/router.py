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


# ---------------------------------------------------------------------------
# "close the player": decided by code, because a model that merely SAYS "closed" closes nothing.
# A bare "close"/"stop" only counts while a video was started in the last 30 minutes.
# ---------------------------------------------------------------------------
_PLAYER_WORD = r"(?:player|video|song|music|trailer|youtube|window|playback|playing)"
_CLOSE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|please|pls|hey|jarvis|now)[,\s]+)*(?:close|stop|shut|kill|turn\s+off|exit|quit|end)\s*"
    r"(?:(?:the|this|that|my|it)\s*)*(?P<w>" + _PLAYER_WORD + r")?(?:\s+(?:now|please|pls|off|down))*\W*$", re.I)
_player_until = 0.0


def note_player() -> None:
    """A play_video action went out: for the next 30 minutes a bare 'close' means the player."""
    import time

    global _player_until
    _player_until = time.time() + 1800


def is_player_close(text: str) -> bool:
    import time

    m = _CLOSE.match(text)
    return bool(m and (m.group("w") or time.time() < _player_until))


_PLAY = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|please|pls|hey|jarvis|could\s+you|can\s+you|would\s+you|just|now|and)[,\s]+)*"
    r"play\s+(?:me\s+)?(?P<q>.+?)\s*[.!?]*\s*$", re.I)
_NEXT = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|please|pls|hey|jarvis|now|and)[,\s]+)*(?P<w>next|another|skip|previous|prev|back)"
    r"(?:\s+(?:song|one|video|track|please|pls))*\W*$", re.I)
_PLAY_NOT = re.compile(r"\b(?:not|instead|remind|reminder|schedule|at\s+\d|tomorrow|later|badminton|game|cricket|football)\b", re.I)


_retry_until = 0.0
_GENERIC = set("video videos music song songs something anything stuff random tune tunes track tracks one nice good new".split())


def note_play_failed() -> None:
    """A play request found nothing: for 3 minutes a short follow-up ("bindaas from jailer 2") is the corrected title."""
    import time

    global _retry_until
    _retry_until = time.time() + 180


def media_command(text: str) -> tuple[str, object] | None:
    """('play', query) for "play <something>", ('step', +1/-1) for "next song" / "previous" (only within 30 minutes of
    a video starting). Decided by code, because a model that merely SAYS "playing" plays nothing. Corrections such as
    "play jailer one, not 2" are left to the model, which has to read the search results."""
    import time

    m = _NEXT.match(text)
    if m and time.time() < _player_until:
        return ("step", -1 if m.group("w").lower() in ("previous", "prev", "back") else 1)
    m = _PLAY.match(text)
    if not m:
        words = text.split()
        if time.time() < _retry_until and 1 <= len(words) <= 9 and not text.rstrip().endswith("?"):
            return ("play", text.strip())       # follow-up to a search that found nothing: the user is fixing the title
        return None
    q = re.sub(r"^(?:the|some|a|an)\s+", "", m.group("q").strip(), flags=re.I)
    if not q or _PLAY_NOT.search(q) or re.fullmatch(r"(?:it|that|this|again|more)", q, re.I):
        return None
    if all(w in _GENERIC or w in ("please", "pls", "now", "me", "for", "us") for w in re.findall(r"[\w']+", q.lower())):
        return ("ask", None)                    # "play some video": nothing to search for, so ask instead of guessing
    nxt = re.fullmatch(r"(next|another|previous|prev)(?:\s+(?:song|one|video|track))?", q, re.I)
    if nxt:  # "play the next song": a step, never a search for those words
        return ("step", -1 if nxt.group(1).lower() in ("previous", "prev") else 1) if time.time() < _player_until else None
    return ("play", q)


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
