"""Turns a stored reminder ("Reminder: drink water") into something a friend would say: "Hey Ajay, drink some water
to stay hydrated." Written in the active persona's voice. Used for the notification, the spoken version and the email.
Falls back to a simple friendly template if the model is unavailable, so a reminder is never delayed or lost."""
from __future__ import annotations

import logging
import re

from orchestrator.model import chat_model, text_of

log = logging.getLogger("reminder_text")

_PREFIX = re.compile(r"^(reminder|today|coming up in \d+ min|coming up)\s*:\s*", re.I)


def clean(message: str) -> str:
    return _PREFIX.sub("", message.strip()).strip()


def fallback(messages: list[str], owner: str) -> str:
    items = [clean(m) for m in messages if m.strip()]
    if len(items) == 1:
        return f"{items[0][:1].upper() + items[0][1:]}, boss."
    return "; ".join(items) + f", {owner}."


async def compose(messages: list[str], persona: dict, owner: str) -> str:
    """One short, natural message for one or several reminders. No prefix, no markup, safe to speak aloud."""
    items = [clean(m) for m in messages if m.strip()]
    if not items:
        return f"Hey {owner}!"
    listing = "\n".join(f"- {i}" for i in items)
    prompt = (
        f"You are {persona['name']}, {owner}'s personal assistant and close friend. Tone: {persona.get('tone')}. "
        f"Style: {persona.get('speaking_style')}.\n"
        f"Tell {owner} the following reminder{'s' if len(items) > 1 else ''} the way a friend would say it in one breath: "
        "short, direct, casual, in second person. Say EXACTLY what the task is, nothing more.\n"
        "Examples: 'charge my airpods' -> 'Charge your AirPods, boss.'; 'call mom' -> 'Call your mom, boss.'; "
        "'drink water' -> 'Drink some water, boss.'\n"
        "Rules: at most 8 words per item. Do NOT add benefits, reasons, jokes or filler (no 'to keep the tunes flowing', "
        "no 'just a heads-up', no 'quick nudge'). Do NOT explain. You may end with 'boss' or their name, nothing fancier. "
        "No 'Reminder:' prefix, no brackets, no quotes, no emojis, no markdown, no lists, no ids. If there are several "
        "items, join them in one short sentence. Output ONLY the message.\n\n"
        f"Reminder{'s' if len(items) > 1 else ''}:\n{listing}"
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
    """One honest sentence about reminders that came due while the assistant was offline."""
    listing = "; ".join(items)
    prompt = (
        f"You are {persona['name']}, {owner}'s personal assistant and friend. Tone: {persona.get('tone')}.\n"
        f"While you were offline (the PC was off or asleep) these reminders came due and were missed: {listing}.\n"
        f"Tell {owner} in one or two short, natural spoken sentences (under 40 words): that you were offline, and what they "
        "missed with when it was due. No emojis, no markdown, no lists. Output ONLY the message."
    )
    try:
        res = await chat_model(0.6).ainvoke(prompt)
        line = re.sub(r"\s+", " ", text_of(res.content).strip().strip('"'))
        if line:
            return line
    except Exception as e:
        log.warning("missed-reminder text failed: %s", e)
    return f"Hey {owner}, I was offline for a while and missed: {listing}."
