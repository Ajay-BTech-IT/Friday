"""The face of the assistant. After a reply, the persona itself decides how its face looks while saying it: the model
returns continuous face values (eyes, brows, mouth, head, gaze, glow colour) and an emoji, as a few "beats" so the
face changes while it speaks, like a person's does. Nothing here maps an emotion to a face or to a colour: no emotion
list, no keyword table, no colour table. The web page only draws whatever numbers come back."""
from __future__ import annotations

import asyncio
import json
import logging
import re

from orchestrator.model import chat_model, text_of

log = logging.getLogger("expression")

# name -> (min, max, what it controls). The ranges only keep the drawing sane; they say nothing about feelings.
FIELDS: dict[str, tuple[float, float, str]] = {
    "eye_open": (0.0, 1.0, "how wide the eyes are open: 0 closed, 0.8 relaxed, 1 wide"),
    "lid_droop": (0.0, 1.0, "upper lids lowered while the eyes stay open, as when tired, bored or doubtful: 0 none, 1 heavy"),
    "eye_smile": (0.0, 1.0, "lower lids pushed up (a squint or the crinkle of a smile): 0 none, 1 full"),
    "pupil_size": (0.6, 1.5, "pupil size: 1 normal, smaller is narrower, larger is dilated"),
    "brow_raise": (-1.0, 1.0, "both brows: -1 pulled low, 0 resting, 1 lifted high"),
    "brow_asym": (-1.0, 1.0, "one brow higher than the other: -1 left brow higher, 0 level, 1 right brow higher"),
    "brow_tilt": (-1.0, 1.0, "slant of the brows: -1 inner ends up (worried, pleading), 0 flat, 1 inner ends down (stern)"),
    "mouth_curve": (-1.0, 1.0, "mouth corners: -1 turned down, 0 flat, 1 turned up"),
    "mouth_open": (0.0, 1.0, "how far the mouth is open while resting between words"),
    "mouth_width": (0.0, 1.0, "narrow to wide mouth"),
    "mouth_skew": (-1.0, 1.0, "one corner higher than the other: -1 left higher, 1 right higher (a smirk)"),
    "lip_press": (0.0, 1.0, "lips pressed thin and tight: 0 relaxed, 1 tightly pressed"),
    "mouth_round": (0.0, 1.0, "lips pursed into an O: 0 none, 1 fully rounded"),
    "head_tilt": (-15.0, 15.0, "head roll in degrees"),
    "head_nod": (-1.0, 1.0, "head lowered (-1) or lifted (1)"),
    "head_turn": (-1.0, 1.0, "head turned to the left (-1) or the right (1)"),
    "gaze_x": (-1.0, 1.0, "where the eyes look: -1 left, 1 right"),
    "gaze_y": (-1.0, 1.0, "where the eyes look: -1 up, 1 down"),
    "hue": (0.0, 360.0, "colour of your face glow as a hue angle in degrees (0 red, 30 orange, 60 yellow, 120 green, 180 cyan, 240 blue, 300 magenta)"),
    "glow": (0.0, 1.0, "how bright the glow is"),
    "blink_rate": (0.3, 3.0, "how often you blink: 1 normal, lower is slow and steady, higher is quick and nervous"),
}
HOLD = (2.0, 20.0)  # seconds the last beat stays before the face relaxes


def _prompt(persona: dict, user_text: str, reply: str, previous: dict | None) -> str:
    fields = "\n".join(f'    "{k}": number {lo} to {hi}, {desc}' for k, (lo, hi, desc) in FIELDS.items())
    prev = f"\nYour face a moment ago (for continuity): {json.dumps(previous)}" if previous else ""
    words = len(reply.split())
    return (
        f"You are {persona['name']}. {persona.get('system_prompt', '')}\n"
        f"Tone: {persona.get('tone')}. Style: {persona.get('speaking_style')}.\n\n"
        "You have a face on screen, and it should behave like a real person's face. You just replied to the user and your face "
        "must show how YOU feel while saying it, in your own character: a restrained character is smaller than a playful one, "
        "but the feeling must always be readable at a glance, so restraint means smaller, never invisible.\n"
        "THE GLOW COLOUR IS YOUR MOOD RING. It is the strongest signal on your face: every different feeling gets a clearly "
        "different, saturated hue chosen by you (you decide what each colour means to you). Do not stay on your usual cyan "
        "unless you really feel calm and neutral. Make the colour and brightness change whenever the feeling changes.\n"
        "People's faces change as they speak: react first, then settle, or build up. So give 1 to 3 beats across the reply "
        f"(it is about {words} words long, spoken over roughly {max(1.5, words / 2.7):.0f} seconds). 'at' is how far through "
        "the speech each beat starts (0 to 1; the first is always 0). The first beat lists every key; later beats list only "
        "the keys that change.\n"
        f"The user said: {user_text[:600]}\nYou replied: {reply[:700]}{prev}\n\n"
        "Answer with ONE JSON object and nothing else:\n"
        '{\n  "mood": one or two lowercase words naming the feeling, in your own words,\n'
        '  "emoji": one emoji that expresses your reaction, or "" if you would not use one,\n'
        '  "hold": seconds (2 to 20) the final expression stays,\n'
        '  "beats": [ { "at": 0, ...keys... }, { "at": 0.5, ...changed keys... } ]\n}\n'
        f"Keys available in each beat:\n{fields}"
    )


def _num(raw: dict) -> dict:
    out: dict = {}
    for k, (lo, hi, _d) in FIELDS.items():
        try:
            out[k] = max(lo, min(hi, float(raw[k])))
        except (KeyError, TypeError, ValueError):
            pass  # a missing value simply leaves that part of the face where it is
    return out


def _clean(raw: dict) -> dict:
    beats = []
    for b in (raw.get("beats") or [])[:4]:
        if isinstance(b, dict):
            f = _num(b)
            try:
                f["at"] = max(0.0, min(1.0, float(b.get("at", 0))))
            except (TypeError, ValueError):
                f["at"] = 0.0
            beats.append(f)
    if not beats:                                   # a model that answered with one flat face is fine too
        flat = _num(raw)
        if flat:
            beats = [{**flat, "at": 0.0}]
    beats.sort(key=lambda b: b["at"])
    if beats:
        beats[0]["at"] = 0.0
    emoji = str(raw.get("emoji") or "").encode("utf-8", "ignore").decode("utf-8").strip()  # drop broken surrogates
    try:
        hold = max(HOLD[0], min(HOLD[1], float(raw.get("hold", 5))))
    except (TypeError, ValueError):
        hold = 5.0
    out = {k: v for k, v in (beats[0] if beats else {}).items() if k != "at"}   # first beat also flat, for simple clients
    out.update(beats=beats, hold=hold, emoji=emoji[:8] if emoji and len(emoji) <= 8 else "",
               mood=re.sub(r"[^\w\s'-]", "", str(raw.get("mood") or "")).strip().lower()[:30])
    return out


async def express(persona: dict, user_text: str, reply: str, previous: dict | None = None) -> dict | None:
    """Ask the persona how its face looks. None when the model is unreachable (the face then just stays as it is)."""
    try:
        res = await asyncio.wait_for(chat_model(0.9).ainvoke(_prompt(persona, user_text, reply, previous)), timeout=14)
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text_of(res.content).strip(), flags=re.I)
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
        face = _clean(data)
        return face if face.get("beats") else None
    except Exception as e:
        log.warning("expression unavailable: %s: %s", type(e).__name__, e)
        return None
