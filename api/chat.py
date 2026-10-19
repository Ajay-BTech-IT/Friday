"""POST /chat (text) and POST /chat/stream (server-sent events)."""
from __future__ import annotations

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.deps import require_auth
from database.audit import jsonable
from database import persona as persona_db
from orchestrator import agent
from voice import stream as voice_stream, tts

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(require_auth)])
_lock = asyncio.Lock()  # one conversation turn at a time keeps history and writes consistent


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    device_id: UUID | None = None
    channel: str = "text"
    voice: bool = True  # replies stream as text AND audio by default
    client: str = "pc"  # "phone" lets the agent place calls through the app


@router.post("")
async def chat(body: ChatIn) -> dict:
    async with _lock:
        return jsonable(await agent.chat(body.text, body.device_id, body.channel, body.client))


@router.post("/stream")
async def chat_stream(body: ChatIn):
    async def gen():
        async with _lock:
            async for ev in voice_stream.with_voice(agent.chat_stream(body.text, body.device_id, body.channel, body.client), body.voice):
                yield f"data: {json.dumps(jsonable(ev), ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


class ExpressionIn(BaseModel):
    user_text: str = Field(max_length=4000)
    reply: str = Field(min_length=1, max_length=4000)
    previous: dict | None = None


@router.post("/expression")
async def expression(body: ExpressionIn) -> dict:
    """How the active persona's face looks while saying `reply` (the web UI draws it). {} if the model is unavailable.
    Not behind the conversation lock: it must never wait for, or delay, a chat turn."""
    from orchestrator import expression as expr

    face = await expr.express(await persona_db.get_active(), body.user_text, body.reply, body.previous)
    return face or {}


class TTSIn(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    persona: str | None = None


@router.post("/tts")
async def speak(body: TTSIn):
    """Speak any text (reminders, briefings) in the active persona's voice. Returns audio/mpeg."""
    from fastapi import HTTPException
    from fastapi.responses import Response

    p = (await persona_db.find(body.persona) if body.persona else None) or await persona_db.get_active()
    try:
        audio = await tts.synthesize(body.text, p.get("tts_voice_id"), p.get("tts_style"), p.get("tts_rate"), p.get("tts_pitch"))
    except tts.TTSError as e:
        raise HTTPException(503, str(e))
    return Response(audio, media_type="audio/mpeg")


@router.get("/history")
async def history(limit: int = 30) -> list[dict]:
    """Recent turns across conversations (shared with the PC), oldest first, for the phone app's chat screen."""
    from database import db

    rows = await db.fetch(
        "SELECT role, content, created_at FROM messages WHERE user_id=%s AND role IN ('user','assistant') AND content IS NOT NULL "
        "ORDER BY created_at DESC LIMIT %s", db.owner_id(), max(1, min(limit, 100)))
    return jsonable(list(reversed(rows)))


@router.post("/stt")
async def stt(request: Request):
    """Raw audio in the request body (Content-Type audio/aac, audio/mpeg, ...) -> {"text": "..."}."""
    from fastapi import HTTPException
    from voice import stt as stt_mod

    data = await request.body()
    if len(data) > 12_000_000:
        raise HTTPException(413, "Audio too large")
    try:
        text = await stt_mod.transcribe(data, request.headers.get("content-type", "audio/aac"))
    except stt_mod.STTError as e:
        raise HTTPException(422, str(e))
    return {"text": text}
