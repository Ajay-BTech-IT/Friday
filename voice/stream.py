"""Turn the agent's text stream into a text + audio stream.

Text events go out the moment they exist. Finished sentences are synthesised in parallel (max 3) and emitted
as audio events in the original order, so the client can start speaking after the first sentence while the
model is still writing the rest. Any TTS failure degrades to text only; it never breaks the reply.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import AsyncIterator

from config import settings
from voice import tts

log = logging.getLogger("voice.stream")

_END = re.compile(r"(?<=[.!?।])[\"')\]]*\s+|\n+")
MIN_CHARS = 14
FIRST_CLAUSE_CHARS = 60


class SentenceBuffer:
    """Accumulates streamed tokens and releases complete sentences."""

    def __init__(self) -> None:
        self.buf = ""
        self.pending = ""      # a too-short sentence ("Ok.") waiting to be joined with the next one
        self.sent_any = False

    def _emit(self, sentence: str, out: list[str]) -> None:
        cand = f"{self.pending} {sentence}".strip()
        if len(cand) < MIN_CHARS:
            self.pending = cand
        else:
            self.pending = ""
            self.sent_any = True
            out.append(cand)

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out: list[str] = []
        while True:
            m = _END.search(self.buf)
            if m:
                sentence, self.buf = self.buf[:m.start()], self.buf[m.end():]
                if sentence.strip():
                    self._emit(sentence.strip(), out)
                continue
            # first chunk only: speak a long clause early to cut time-to-first-audio
            if not self.sent_any and len(self.buf) >= FIRST_CLAUSE_CHARS:
                c = max(self.buf.rfind(", "), self.buf.rfind("; "))
                if c >= MIN_CHARS:
                    head, self.buf = self.buf[:c + 1].strip(), self.buf[c + 1:]
                    self._emit(head, out)
                    continue
            return out

    def flush(self) -> list[str]:
        rest = f"{self.pending} {self.buf}".strip()
        self.buf = self.pending = ""
        return [rest] if rest else []


def split_for_tts(text: str, limit: int = 2800) -> list[str]:
    """Murf rejects requests over 3000 characters. Split at sentence ends; hard-split anything still too long."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    pieces: list[str] = []
    for sent in re.split(r"(?<=[.!?।])\s+", text):
        while len(sent) > limit:  # no sentence end in sight: cut at the last space before the limit
            cut = sent.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(sent[:cut])
            sent = sent[cut:].strip()
        if sent:
            pieces.append(sent)
    out, cur = [], ""
    for piece in pieces:
        if cur and len(cur) + len(piece) + 1 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur} {piece}".strip()
    return out + ([cur] if cur else [])


async def with_voice(events: AsyncIterator[dict], voice: bool = True) -> AsyncIterator[dict]:
    """Wrap agent.chat_stream. Adds {"type":"audio","seq":n,"format":"mp3","data":<base64>} events."""
    use_voice = voice and settings.voice_enabled
    full_mode = settings.tts_mode != "sentence"
    acc: list[str] = []
    out: asyncio.Queue = asyncio.Queue()
    jobs: asyncio.Queue = asyncio.Queue()
    sem = asyncio.Semaphore(3)
    state: dict = {"voice_id": None, "style": None, "rate": None, "pitch": None, "done": None, "errors": 0}

    async def synth(sentence: str) -> bytes | None:
        if state["errors"] >= 2:
            return None  # voice is failing this turn: stay text-only
        async with sem:
            try:
                return await tts.synthesize(sentence, state["voice_id"], state["style"], state["rate"], state["pitch"])
            except Exception as e:
                state["errors"] += 1
                log.warning("tts failed: %s", e)
                if state["errors"] == 1:
                    await out.put({"type": "voice_error", "message": (f"{type(e).__name__}: {e}")[:200]})
                return None

    async def pump() -> None:
        buf = SentenceBuffer()
        try:
            async for ev in events:
                t = ev.get("type")
                if t == "start":
                    state.update({k: ev.get(k) for k in ("voice_id", "style", "rate", "pitch")})
                if t == "done":
                    state["done"] = ev
                    continue
                if t == "token" and use_voice:
                    if full_mode:
                        acc.append(ev["text"])
                    else:
                        for s in buf.feed(ev["text"]):
                            await jobs.put(asyncio.create_task(synth(s)))
                await out.put(ev)
            if use_voice:
                rest = split_for_tts("".join(acc)) if full_mode else buf.flush()
                for s in rest:
                    await jobs.put(asyncio.create_task(synth(s)))
        finally:
            await jobs.put(None)

    async def speaker() -> None:
        seq = 0
        while (task := await jobs.get()) is not None:
            audio = await task
            if audio:
                await out.put({"type": "audio", "seq": seq, "format": "mp3", "data": base64.b64encode(audio).decode()})
                seq += 1
        await out.put(None)

    p = asyncio.create_task(pump())
    sp = asyncio.create_task(speaker())
    try:
        while (ev := await out.get()) is not None:
            yield ev
        if state["done"]:
            yield state["done"]
    finally:
        for t in (p, sp):
            if not t.done():
                t.cancel()
