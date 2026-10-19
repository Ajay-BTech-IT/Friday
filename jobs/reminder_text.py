"""Turns a stored reminder ("Reminder: drink water") into something a friend would say: "Hey Ajay, drink some water
to stay hydrated." Written in the active persona's voice. Used for the notification, the spoken version and the email.
Falls back to a simple friendly template if the model is unavailable, so a reminder is never delayed or lost."""
from __future__ import annotations

import logging
import re

from database import persona as persona_db
from orchestrator.model import chat_model, text_of

log = logging.getLogger("reminder_text")

_PREFIX = re.compile(r"^(reminder|today|coming up in \d+ min|coming up)\s*:\s*", re.I)


def clean(message: str) -> str:
    return _PREFIX.sub("", message.strip()).strip()


def fallback(messages: list[str], owner: str) -> str:
    items = [clean(m) for m in messages if m.strip()]
    if len(items) == 1:
        return f"{items[0][:1].upper() + items[0][1:]}, {owner}. Don't forget."
    return "; ".join(items) + f", {owner}."


async def compose(messages: list[str], persona: dict, owner: str) -> str:
    """One short message for one or several reminders that are due NOW, spoken in the persona's own voice.
    This only states the facts; tone, language and manner come from the persona (persona_db.voice_brief)."""
    items = [clean(m) for m in messages if m.strip()]
    if not items:
        return f"Hey {owner}!"
    listing = "\n".join(f"- {i}" for i in items)
    prompt = (
        await persona_db.voice_brief(persona, owner) + "\n\n"
        f"TASK: remind {owner} of the following, which {'are' if len(items) > 1 else 'is'} due right now. Say exactly what "
        "needs doing, in your own voice, in one or two short sentences (under 30 words). Add something only if it is "
        "plainly true and useful. Never invent details, and never claim what other people expect, feel or are doing. Plain spoken text: no 'Reminder:' prefix, no brackets, no "
        "quotes, no emojis, no markdown, no lists. Output ONLY the message.\n\n"
        f"Due now:\n{listing}"
    )
    try:
        res = await chat_model(0.8).ainvoke(prompt)
        line = text_of(res.content).strip().strip('"').strip()
        line = re.sub(r"\s+", " ", line)
        if not line or len(line) > 300:
            raise ValueError("empty or too long")
        return line
    except Exception as e:
        log.warning("friendly reminder text failed, using template: %s", e)
        return fallback(messages, owner)


async def compose_missed(items: list[str], persona: dict, owner: str) -> str:
    """One honest note about reminders that came due while the assistant was offline, in the persona's voice."""
    listing = "; ".join(items)
    prompt = (
        await persona_db.voice_brief(persona, owner) + "\n\n"
        f"TASK: while you were offline (the PC was off or asleep) these reminders came due and were missed: {listing}. "
        f"Tell {owner} in one or two short spoken sentences (under 40 words) that you were offline and what was missed, "
        "with when each was due. Plain spoken text: no emojis, no markdown, no lists. Output ONLY the message."
    )
    try:
        res = await chat_model(0.6).ainvoke(prompt)
        line = re.sub(r"\s+", " ", text_of(res.content).strip().strip('"'))
        if line:
            return line
    except Exception as e:
        log.warning("missed-reminder text failed: %s", e)
    return f"Hey {owner}, I was offline for a while and missed: {listing}."
