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


async def _lang() -> str:
    line = await persona_db.language_line()
    return f"\n\n{line} Keep the same short, direct style." if line else ""


async def compose(messages: list[str], persona: dict, owner: str) -> str:
    """One short, natural message for one or several reminders. No prefix, no markup, safe to speak aloud."""
    items = [clean(m) for m in messages if m.strip()]
    if not items:
        return f"Hey {owner}!"
    listing = "\n".join(f"- {i}" for i in items)
    prompt = (
        f"You are {persona['name']}, {owner}'s personal assistant and close friend. Tone: {persona.get('tone')}. "
        f"Style: {persona.get('speaking_style')}.\n"
        f"Tell {owner} the following reminder{'s' if len(items) > 1 else ''} the way someone who genuinely looks after "
        "them would: caring, warm, natural, one or two short sentences (about 8 to 18 words). It must be about EXACTLY the "
        "task given, nothing else.\n"
        "Shape: the real task, said kindly, plus one short, TRUE reason or gentle care when it fits (water -> your body "
        "needs it; AirPods -> so they're ready when you need them; call mom -> she'll be happy to hear from you; medicine "
        "-> don't skip it). If no real reason fits, just say the task warmly. Never invent facts, never pad.\n"
        "Examples: 'charge my airpods' -> 'Charge your AirPods now, boss, so they're ready when you need them.'; "
        "'drink water' -> 'Time for some water, boss. Your body will thank you.'; 'call mom' -> 'Call your mom, boss. "
        "She'll love hearing from you.'\n"
        "Rules: grammatically correct and the meaning exactly right in whatever language you write; never a bare "
        "command. No cute filler or jokes (no 'keep the tunes flowing'), no 'just a heads-up', no 'quick nudge'. You may "
        "address them as 'boss' or by name. No 'Reminder:' prefix, no brackets, no quotes, no emojis, no markdown, no "
        "lists, no ids. If there are several items, join them in one smooth sentence. Output ONLY the message.\n\n"
        f"Reminder{'s' if len(items) > 1 else ''}:\n{listing}"
        + await _lang()
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
        "missed with when it was due. No emojis, no markdown, no lists. Output ONLY the message." + await _lang()
    )
    try:
        res = await chat_model(0.6).ainvoke(prompt)
        line = re.sub(r"\s+", " ", text_of(res.content).strip().strip('"'))
        if line:
            return line
    except Exception as e:
        log.warning("missed-reminder text failed: %s", e)
    return f"Hey {owner}, I was offline for a while and missed: {listing}."
