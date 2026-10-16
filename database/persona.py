"""Personas are data: name, tone, prompt, voice. Switching changes style only."""
from __future__ import annotations

import re

from database import db

_cache: dict = {"active": None, "all": None, "lang": None}

_BASE_RULES = (
    "You are a personal companion and mentor for one person. Be concise (2-4 short sentences unless asked for "
    "detail), honest, and warm. Speak like a friend, never like a command-line robot. Never lecture. "
    "Use Indian context naturally (rupees, katori, chai) when relevant."
)

# No character names live in code. The only built-in row is a neutral first-run default; every real persona (and its
# name, tone, voice) is created and renamed by the user through the agent's persona tools and stored in `personas`.
_DEFAULT = {
    "name": "Assistant",
    "aliases": ["assistant"],
    "gender": "male",
    "tone": "calm, friendly, steady",
    "speaking_style": "short clear sentences, warm and direct",
    "greeting": "Hi! What are we tackling?",
    "system_prompt": "A calm, friendly personal assistant who gives straight answers and honest coaching without drama.",
}


async def seed_personas() -> None:
    """First start only: make sure at least one persona exists, then activate the default one."""
    if not await db.fetchval("SELECT count(*) FROM personas"):
        p = _DEFAULT
        await db.execute(
            "INSERT INTO personas(name,aliases,gender,tone,system_prompt,speaking_style,greeting,is_default) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,true)",
            p["name"], p["aliases"], p["gender"], p["tone"], p["system_prompt"], p["speaking_style"], p["greeting"],
        )
    await db.execute(
        "UPDATE user_profile SET active_persona_id = (SELECT id FROM personas WHERE is_default LIMIT 1) "
        "WHERE user_id=%s AND active_persona_id IS NULL",
        db.owner_id(),
    )
    _cache.update(active=None, all=None)


def _resolve(row: dict | None, lang: dict | None = None) -> dict | None:
    """Voice a persona speaks with: in a non-English language, a voice for that language (the persona's own voice if it
    already fits); otherwise the persona's own voice, or the default for its gender."""
    if row is None:
        return None
    from voice.tts import default_voice

    male = (row.get("gender") or "").lower().startswith("m") or not row.get("gender")
    voice = row.get("tts_voice_id")
    if lang and lang.get("locale") and lang.get("voices") and not (voice or "").lower().startswith(lang["locale"].lower()):
        voices = lang["voices"]
        voice = voices.get("male" if male else "female") or voices.get("male") or voices.get("female") or voice
    return {**row, "tts_voice_id": voice or default_voice(row.get("gender"))}


async def get_language() -> dict | None:
    """The language the user asked for ({'name','locale','script','style','voices'}), or None for plain English."""
    if _cache["lang"] is None:
        from database import profile as profile_db

        extra = (await profile_db.get_profile()).get("extra") or {}
        lang = extra.get("language")
        _cache["lang"] = lang if lang and lang.get("name") and lang["name"].lower() != "english" else {}
    return _cache["lang"] or None


async def set_language(name: str, locale: str | None, script: str, style: str | None, voices: dict | None) -> dict:
    from database import profile as profile_db

    rec = {"name": name.strip(), "locale": locale, "script": script, "style": style, "voices": voices or {}}
    await profile_db.update_profile({"extra": {"language": rec}})
    invalidate()
    return rec


async def language_line() -> str:
    """One instruction for any text the assistant writes (reminders, briefings); empty for English."""
    lang = await get_language()
    if not lang:
        return ""
    where = (f"{lang['name']} words in their native script and English words in Latin letters"
             if lang.get("script") == "native" else "all in Latin letters")
    return f"LANGUAGE: write it in {lang['name']}" + (f" ({lang['style']})" if lang.get("style") else "") + f", {where}."


def base_rules() -> str:
    return _BASE_RULES


async def list_personas() -> list[dict]:
    if _cache["all"] is None:
        lang = await get_language()
        _cache["all"] = [_resolve(r, lang) for r in await db.fetch("SELECT * FROM personas WHERE active ORDER BY is_default DESC, name")]
    return _cache["all"]


async def get_active() -> dict:
    if _cache["active"] is None:
        row = await db.fetchrow(
            "SELECT p.* FROM user_profile up JOIN personas p ON p.id = up.active_persona_id WHERE up.user_id=%s",
            db.owner_id(),
        )
        if row is None:
            row = await db.fetchrow("SELECT * FROM personas WHERE is_default LIMIT 1")
        _cache["active"] = _resolve(row, await get_language())
    return _cache["active"]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


async def find(name_or_alias: str) -> dict | None:
    key = _norm(name_or_alias)
    for p in await list_personas():
        if key == _norm(p["name"]) or key in [_norm(a) for a in (p["aliases"] or [])]:
            return p
    return None


async def switch(name_or_alias: str) -> dict | None:
    p = await find(name_or_alias)
    if p is None:
        return None
    await db.execute("UPDATE user_profile SET active_persona_id=%s, updated_at=now() WHERE user_id=%s", p["id"], db.owner_id())
    _cache["active"] = p
    return p


def invalidate() -> None:
    _cache.update(active=None, all=None, lang=None)


def _aliases(name: str, extra: list[str] | None = None) -> list[str]:
    out = [_norm(name)]
    for a in extra or []:
        if _norm(a) and _norm(a) not in out:
            out.append(_norm(a))
    return out


def _rename_in(text: str | None, old: str, new: str) -> str | None:
    return re.sub(re.escape(old), new, text, flags=re.I) if text else text


async def create(name: str, *, gender: str | None, tone: str | None, speaking_style: str | None,
                 personality: str | None, greeting: str | None, voice_id: str | None = None,
                 activate: bool = True) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("name is empty")
    if await find(name):
        raise ValueError(f"a persona named '{name}' already exists")
    row = await db.fetchrow(
        "INSERT INTO personas(name,aliases,gender,tone,system_prompt,speaking_style,greeting,tts_voice_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        name, _aliases(name), gender, tone, personality or f"{name} is a personal assistant.", speaking_style,
        greeting or f"{name} here. What's up?", voice_id,
    )
    invalidate()
    if activate:
        await switch(name)
    return await find(name) or row


async def update(name_or_alias: str | None, **fields) -> tuple[dict, dict]:
    """Change a persona (the active one when name is None). Renaming also rewrites the old name inside its prompt and
    greeting. Returns (before, after)."""
    before = await find(name_or_alias) if name_or_alias else await get_active()
    if before is None:
        raise ValueError(f"no persona named '{name_or_alias}'")
    cols = {k: v for k, v in fields.items() if v is not None and k in
            ("name", "gender", "tone", "speaking_style", "system_prompt", "greeting", "tts_voice_id", "tts_style",
             "tts_rate", "tts_pitch")}
    extra_alias = fields.get("add_alias")
    new_name = (cols.get("name") or "").strip()
    if new_name and _norm(new_name) != _norm(before["name"]):
        other = await find(new_name)
        if other and other["id"] != before["id"]:
            raise ValueError(f"a persona named '{new_name}' already exists")
        cols["name"] = new_name
        cols.setdefault("system_prompt", _rename_in(before["system_prompt"], before["name"], new_name))
        cols.setdefault("greeting", _rename_in(before["greeting"], before["name"], new_name))
        keep = [a for a in (before["aliases"] or []) if _norm(a) != _norm(before["name"])]
        cols["aliases"] = _aliases(new_name, keep + ([extra_alias] if extra_alias else []))
    else:
        cols.pop("name", None)
        if extra_alias:
            cols["aliases"] = _aliases(before["name"], list(before["aliases"] or []) + [extra_alias])
    if cols:
        sets = ", ".join(f"{k}=%s" for k in cols)
        await db.execute(f"UPDATE personas SET {sets} WHERE id=%s", *cols.values(), before["id"])
    invalidate()
    after = next(p for p in await list_personas() if p["id"] == before["id"])
    if (await get_active())["id"] == after["id"]:
        _cache["active"] = after
    return before, after
