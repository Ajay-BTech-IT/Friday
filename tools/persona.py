"""Persona tools: the agent's character (name, tone, voice) is data in the `personas` table and is changed by talking to it."""
from __future__ import annotations

from langchain_core.tools import tool

from database import persona as persona_db
from tools._util import out, safe
from voice import tts


def _view(p: dict, active_id=None) -> dict:
    return {"name": p["name"], "gender": p["gender"], "tone": p["tone"], "speaking_style": p["speaking_style"],
            "voice_id": p["tts_voice_id"], "active": p["id"] == active_id}


@tool
@safe
async def list_personas() -> str:
    """List every saved persona (name, gender, tone, style, voice) and which one is active right now."""
    active = await persona_db.get_active()
    return out([_view(p, active["id"]) for p in await persona_db.list_personas()])


@tool
@safe
async def switch_persona(name: str) -> str:
    """Switch to an EXISTING persona by name. Changes tone and voice only; memory is shared. If the name is not saved
    yet, call create_persona instead (the user wants it to just work)."""
    p = await persona_db.switch(name)
    if not p:
        names = [x["name"] for x in await persona_db.list_personas()]
        return out({"error": f"No persona named '{name}'. Saved: {names}. Use create_persona to make it."})
    return out({"switched_to": p["name"], "greeting": p["greeting"], "voice_id": p["tts_voice_id"]})


@tool
@safe
async def create_persona(name: str, gender: str, tone: str, speaking_style: str, personality: str,
                         greeting: str | None = None, voice_id: str | None = None) -> str:
    """Create a brand-new persona and switch to it. Use when the user says 'switch to <name>' and no such persona is
    saved. YOU design it from the name and what you know of that character (e.g. a calm British butler AI vs a sharp
    upbeat assistant). gender is 'male' or 'female' (picks the default voice; if you truly cannot tell, ask once).
    tone = few words ('sharp, upbeat, loyal'); speaking_style = how it talks ('short punchy lines, light sarcasm');
    personality = 1-3 sentences describing who it is WITHOUT repeating its name; greeting = one line it says on arrival.
    voice_id optional: pick one with list_voices, otherwise the gender default is used."""
    p = await persona_db.create(name, gender=gender, tone=tone, speaking_style=speaking_style,
                                personality=personality, greeting=greeting, voice_id=voice_id)
    return out({"created_and_switched_to": p["name"], "greeting": p["greeting"], "voice_id": p["tts_voice_id"]})


@tool
@safe
async def update_persona(name: str | None = None, new_name: str | None = None, gender: str | None = None,
                         tone: str | None = None, speaking_style: str | None = None, personality: str | None = None,
                         greeting: str | None = None, voice_id: str | None = None, voice_style: str | None = None,
                         voice_rate: int | None = None, voice_pitch: int | None = None,
                         add_alias: str | None = None) -> str:
    """Edit a persona. `name` = which persona (omit = the ACTIVE one). Pass only what changes.
    "your name is PRIME" -> new_name='PRIME' (renames the active persona and its saved prompt/greeting).
    "talk more casually / be sassier" -> tone / speaking_style. "use a deeper / British / female voice" -> pick a
    voice_id from list_voices (voice_rate and voice_pitch are -50..50, voice_style one of the voice's styles).
    add_alias = another word that should also switch to this persona. Returns before/after."""
    before, after = await persona_db.update(
        name, name=new_name, gender=gender, tone=tone, speaking_style=speaking_style, system_prompt=personality,
        greeting=greeting, tts_voice_id=voice_id, tts_style=voice_style, tts_rate=voice_rate, tts_pitch=voice_pitch,
        add_alias=add_alias)
    keys = ("name", "gender", "tone", "speaking_style", "tts_voice_id", "tts_style", "tts_rate", "tts_pitch")
    return out({"updated": True, "changed": {k: {"from": before[k], "to": after[k]} for k in keys if before[k] != after[k]}})


@tool
@safe
async def list_voices(locale: str | None = None, gender: str | None = None) -> str:
    """List available speaking voices. locale is a prefix like 'en-IN', 'en-US', 'en-UK'; gender 'male' or 'female'.
    Use before changing a persona's voice, and tell the user which voice you picked."""
    return out(await tts.list_voices(locale, gender))


@tool
@safe
async def set_language(language: str, locale: str | None = None, script: str = "native", style: str | None = None) -> str:
    """Change the language you speak AND write in, until the user changes it again. "switch to Tamil" ->
    language='Tamil', locale='ta-IN', style='polished, professional Tamil with common English terms; no slang or colloquial fillers'.
    "back to English" -> language='English'. locale is the voice locale: ta-IN, hi-IN, te-IN, kn-IN, ml-IN, bn-IN,
    mr-IN, gu-IN, pa-IN, en-IN, en-US, en-UK, fr-FR, de-DE, es-ES, ja-JP ... script='native' (default: the language's own
    script, best for the voice) or 'roman' (Latin letters, only if the user asks to read it in English letters).
    style = one short phrase on how mixed/casual the language should be. A matching voice is picked automatically."""
    name = language.strip()
    if name.lower() in ("english", "en", ""):
        await persona_db.set_language("English", None, "native", None, None)
        return out({"language": "English", "voice": "persona's own"})
    voices: dict = {}
    if locale and script == "native" and not locale.lower().startswith("en"):
        for g in ("male", "female"):
            found = await tts.list_voices(locale, g, limit=1)
            if found:
                voices[g] = found[0]["voice_id"]
    rec = await persona_db.set_language(name, locale, script, style, voices)
    return out({"language": rec["name"], "script": script, "voices": voices or "none found, keeping the current voice"})
