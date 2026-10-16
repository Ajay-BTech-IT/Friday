"""Speech-to-text for the phone: the app records a short clip, Gemini transcribes it (English, Hindi or a mix)."""
from __future__ import annotations

import base64
import logging

from langchain_core.messages import HumanMessage

from orchestrator.model import chat_model, text_of

log = logging.getLogger("stt")

SUPPORTED = {"audio/aac", "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/ogg", "audio/flac",
             "audio/mp4", "audio/x-m4a", "audio/aiff", "audio/webm", "audio/3gpp", "audio/amr"}
PROMPT = (
    "Transcribe this short voice message exactly as spoken. The speaker is an Indian English speaker and may mix in "
    "Hindi or Tamil words; write Hindi words in Latin letters (Hinglish). Output ONLY the transcript, no quotes, "
    "no commentary. If there is no intelligible speech, output exactly: [no speech]"
)


class STTError(Exception):
    pass


async def transcribe(data: bytes, mime: str = "audio/aac") -> str:
    mime = (mime or "audio/aac").split(";")[0].strip().lower()
    if mime == "audio/m4a":
        mime = "audio/mp4"
    if mime not in SUPPORTED:
        raise STTError(f"Unsupported audio type {mime}")
    if len(data) < 800:
        raise STTError("Audio too short")
    from database import persona as persona_db

    lang = await persona_db.get_language()
    prompt = PROMPT + (f" The speaker currently speaks {lang['name']} mixed with English: write {lang['name']} words in "
                       f"{'their native script' if lang.get('script') == 'native' else 'Latin letters'} and English words in Latin letters."
                       if lang else "")
    msg = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {"type": "media", "mime_type": mime, "data": base64.b64encode(data).decode()},
    ])
    try:
        res = await chat_model(0.0).ainvoke([msg])
    except Exception as e:
        raise STTError(f"Transcription failed: {e}") from e
    text = text_of(res.content).strip().strip('"')
    return "" if text.lower().startswith("[no speech") else text
