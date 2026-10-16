"""Murf Falcon text-to-speech (HTTP streaming). One call per sentence; each result is a complete MP3."""
from __future__ import annotations

import logging
import re

import httpx

from config import settings

log = logging.getLogger("tts")

URL = "https://api.murf.ai/v1/speech/stream"
_client: httpx.AsyncClient | None = None

_URL_RE = re.compile(r"https?://\S+")
_MD_RE = re.compile(r"[*_`#>~|]+")
_EMOJI_RE = re.compile("[\U0001F000-\U0001FFFF☀-➿️]")


def speakable(text: str) -> str:
    """Strip what should not be read aloud: markdown, links, emojis, list bullets."""
    t = _URL_RE.sub("link", text)
    t = _EMOJI_RE.sub("", t)
    t = _MD_RE.sub("", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    return re.sub(r"\s+", " ", t).strip()


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class TTSError(Exception):
    pass


async def synthesize(text: str, voice_id: str | None, style: str | None = None,
                     rate: int | None = None, pitch: int | None = None) -> bytes:
    """Return one MP3 (24 kHz mono) for `text`."""
    text = speakable(text)
    if not text:
        return b""
    voice_id = voice_id or settings.murf_voice_male
    if not settings.voice_enabled or not voice_id:
        raise TTSError("voice not configured (MURF_API_KEY / voice id)")
    body: dict = {"text": text, "voiceId": voice_id, "model": settings.murf_model,
                  "format": "MP3", "sampleRate": 24000, "channelType": "MONO"}
    if style:
        body["style"] = style
    if rate is not None:
        body["rate"] = max(-50, min(50, int(rate)))
    if pitch is not None:
        body["pitch"] = max(-50, min(50, int(pitch)))
    r = await client().post(URL, headers={"api-key": settings.murf_api_key, "Content-Type": "application/json"}, json=body)
    if r.status_code != 200:
        raise TTSError(f"Murf {r.status_code}: {r.text[:200]}")
    return r.content


def default_voice(gender: str | None) -> str:
    return settings.murf_voice_female if (gender or "").lower().startswith("f") else settings.murf_voice_male


async def list_voices(locale: str | None = None, gender: str | None = None, limit: int = 40) -> list[dict]:
    """Voices Murf offers for the Falcon model (compact), optionally filtered by locale prefix ('en-IN') and gender."""
    if not settings.murf_api_key:
        raise TTSError("MURF_API_KEY missing")
    r = await client().get("https://api.murf.ai/v1/speech/voices", params={"model": "FALCON"},
                           headers={"api-key": settings.murf_api_key})
    if r.status_code != 200:
        raise TTSError(f"Murf {r.status_code}: {r.text[:200]}")
    rows = []
    for v in r.json():
        if locale and not v.get("locale", "").lower().startswith(locale.lower()):
            continue
        if gender and v.get("gender", "").lower()[:1] != gender.lower()[:1]:
            continue
        rows.append({"voice_id": v["voiceId"], "gender": v.get("gender"), "accent": v.get("accent"),
                     "age": v.get("description"), "styles": v.get("availableStyles")})
    return rows[:limit]
