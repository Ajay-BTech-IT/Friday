"""LangGraph tool-calling agent: retrieval -> LLM -> tools -> reply, then background learning."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator
from uuid import UUID

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from database import conversations as conv_db
from database import memory as memory_db
from database import persona as persona_db
from database import profile as profile_db
from orchestrator import character, router
from orchestrator.model import chat_model, safe_embed_query, text_of
from tools import TOOLS

log = logging.getLogger("agent")

_graph = None
FALLBACK = "I hit a snag reaching my brain just now. Nothing was lost, please try that again in a moment."
BUSY = "Gemini is busy right now (it's overloaded on Google's side). Nothing was lost; try again in a few seconds."
OFFLINE = ("I can't reach Gemini right now. It looks like your internet connection on the PC is dropping out. "
           "Nothing was lost; try again in a few seconds.")


PERSONA_TOOLS = {"switch_persona", "create_persona", "update_persona", "set_language"}


def _start_event(persona: dict) -> dict:
    """Tells the voice layer which persona/voice this reply uses (first event of every stream)."""
    return {"type": "start", "persona": persona["name"], "voice_id": persona.get("tts_voice_id"),
            "style": persona.get("tts_style"), "rate": persona.get("tts_rate"), "pitch": persona.get("tts_pitch")}


def graph():
    global _graph
    if _graph is None:
        _graph = create_react_agent(chat_model(), TOOLS)
    return _graph


async def _prepare(text: str, device_id: UUID | None, channel: str, client: str = "pc") -> dict:
    t0 = time.perf_counter()
    conv_id = await conv_db.current_conversation(device_id)
    history = await conv_db.recent(conv_id, 14)           # before inserting the new message
    user_msg = await conv_db.add_message(conv_id, "user", text, channel=channel)

    skip = router.skip_retrieval(text)

    async def _memories() -> list[dict]:
        if skip:
            return []
        emb = await safe_embed_query(text)
        return await memory_db.retrieve(text, 6, query_embedding=emb)

    persona, profile, memories = await asyncio.gather(persona_db.get_active(), profile_db.get_profile(), _memories())
    retrieval_ms = int((time.perf_counter() - t0) * 1000)
    system = await character.build_system_prompt(persona, profile, memories, client)
    messages = [SystemMessage(content=system)]
    for h in history:
        messages.append(HumanMessage(content=h["content"]) if h["role"] == "user" else AIMessage(content=h["content"]))
    messages.append(HumanMessage(content=text))
    return {"conv_id": conv_id, "user_msg": user_msg, "messages": messages, "persona": persona, "retrieval_ms": retrieval_ms}


async def _save_reply(ctx: dict, reply: str, tools_called: list[str]) -> None:
    """Saved BEFORE returning so the user's next message always sees this turn in history."""
    try:
        await conv_db.add_message(ctx["conv_id"], "assistant", reply, tool_calls={"tools": tools_called} if tools_called else None)
    except Exception as e:
        log.warning("could not save assistant message: %s", e)


async def _log_run(ctx: dict, tools_called: list[str], llm_ms: int, t_start: float,
                   tokens_in: int | None, tokens_out: int | None, error: str | None) -> None:
    """Background: latency/observability row."""
    try:
        await conv_db.log_run(ctx["user_msg"]["id"], "chat", tools_called, ctx["retrieval_ms"], llm_ms,
                              int((time.perf_counter() - t_start) * 1000), tokens_in, tokens_out, error)
    except Exception as e:
        log.warning("log_run failed: %s", e)


async def _answer_from_tools(user_text: str, tool_outputs: list[str]) -> str:
    """Gemini sometimes returns an empty final message after tool calls. Ask once more, plainly, using the tool results."""
    results = "\n".join(o[:1800] for o in tool_outputs[-4:])
    prompt = (f"The user said: {user_text!r}\nYou looked things up or did actions. Results:\n{results}\n\n"
              "Reply to the user now in 1-3 plain, friendly sentences based ONLY on these results. No JSON, no ids.")
    try:
        res = await chat_model(0.3).ainvoke(prompt)
        return text_of(res.content).strip()
    except Exception as e:
        log.warning("answer fallback failed: %s", e)
        return ""


def _client_actions(tool_outputs: list[str]) -> list[dict]:
    """Tool results may carry {"client_action": {...}} for the phone app to execute (e.g. a call)."""
    import json

    acts = []
    for o in tool_outputs:
        try:
            d = json.loads(o)
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("client_action"), dict):
            acts.append(d["client_action"])
    return acts


def _tool_outputs(messages: list) -> list[str]:
    return [text_of(m.content) for m in messages if isinstance(m, ToolMessage)]


def _collect(messages: list) -> tuple[list[str], int | None, int | None]:
    tools, tin, tout = [], 0, 0
    for m in messages:
        if isinstance(m, ToolMessage) and m.name:
            tools.append(m.name)
        um = getattr(m, "usage_metadata", None)
        if um:
            tin += um.get("input_tokens", 0)
            tout += um.get("output_tokens", 0)
    return tools, tin or None, tout or None


async def _canned_reply(persona: dict, reply: str, tools: list[str], text: str, device_id: UUID | None, channel: str) -> dict:
    """Replies decided by code (persona switch, email send confirmation): no LLM call."""
    conv_id = await conv_db.current_conversation(device_id)
    um = await conv_db.add_message(conv_id, "user", text, channel=channel)
    await conv_db.mark_extracted(um["id"])  # nothing to learn from a command
    await conv_db.add_message(conv_id, "assistant", reply)
    return {"reply": reply, "persona": persona["name"], "tools": tools, "conversation_id": conv_id,
            "voice_id": persona.get("tts_voice_id")}


async def _switch_reply(new_persona: dict, text: str, device_id: UUID | None, channel: str) -> dict:
    reply = new_persona["greeting"] or f"{new_persona['name']} here."
    return await _canned_reply(new_persona, reply, ["switch_persona"], text, device_id, channel)


async def _send_flow_reply(text: str, device_id: UUID | None, channel: str) -> dict | None:
    reply = await router.try_send_flow(text)
    if reply is None:
        return None
    return await _canned_reply(await persona_db.get_active(), reply, ["send_confirmation"], text, device_id, channel)


_TRANSIENT = ("RemoteProtocolError", "ReadError", "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteError",
              "PoolTimeout", "ServiceUnavailable", "DeadlineExceeded", "TimeoutError", "ServerError", "getaddrinfo",
              "timed out", "UNAVAILABLE", "overloaded", "Connection reset", "Connection aborted", "503")


def _failure_text(e: Exception) -> str:
    """What to tell the user: overload vs. their internet vs. something else."""
    if not _is_transient(e):
        return FALLBACK
    m = f"{type(e).__name__} {str(e)[:200]}"
    return BUSY if any(k in m for k in ("503", "overloaded", "UNAVAILABLE", "ServiceUnavailable", "ServerError")) else OFFLINE


def _is_transient(e: Exception) -> bool:
    return any(n in type(e).__name__ or n in str(e)[:200] for n in _TRANSIENT)


async def _invoke(ctx: dict) -> tuple[list, str, list[str], int | None, int | None]:
    """Run the agent. A flaky internet connection (dropped socket, DNS hiccup, timeout) is retried up to 3 times."""
    last: Exception | None = None
    for attempt, wait in enumerate((0, 1.5, 4.0)):
        if wait:
            await asyncio.sleep(wait)
        try:
            result = await graph().ainvoke({"messages": ctx["messages"]}, config={"recursion_limit": 16})
            break
        except Exception as e:
            if not _is_transient(e):
                raise
            last = e
            log.warning("connection to the model failed (%s), attempt %d/3", type(e).__name__, attempt + 1)
    else:
        raise last  # type: ignore[misc]
    new_msgs = result["messages"][len(ctx["messages"]):]
    final = next((m for m in reversed(new_msgs) if isinstance(m, AIMessage) and not m.tool_calls), None)
    reply = text_of(final.content).strip() if final else ""
    tools_called, tin, tout = _collect(new_msgs)
    return new_msgs, reply, tools_called, tin, tout


async def chat(text: str, device_id: UUID | None = None, channel: str = "text", client: str = "pc") -> dict:
    t_start = time.perf_counter()

    # 1. router: persona switch needs no LLM
    new_persona = await router.try_switch(text)
    if new_persona:
        return await _switch_reply(new_persona, text, device_id, channel)
    canned = await _send_flow_reply(text, device_id, channel)
    if canned:
        return canned

    ctx = await _prepare(text, device_id, channel, client)
    t_llm = time.perf_counter()
    error = None
    new_msgs: list = []
    try:
        new_msgs, reply, tools_called, tin, tout = await _invoke(ctx)
        if not reply and not tools_called:  # Gemini sometimes returns an empty message: simply ask again once
            log.warning("empty model response, retrying once")
            new_msgs, reply, tools_called, tin, tout = await _invoke(ctx)
        if not reply and tools_called:
            reply = await _answer_from_tools(text, _tool_outputs(new_msgs))
        if not reply:
            reply = "Done." if tools_called else "Sorry, I didn't catch that. Could you say it again?"
        if PERSONA_TOOLS.intersection(tools_called):
            ctx["persona"] = await persona_db.get_active()
    except Exception as e:  # LLM/network failure must never crash the API
        log.exception("agent failed")
        reply = _failure_text(e)
        tools_called, tin, tout, error = [], None, None, f"{type(e).__name__}: {e}"
    llm_ms = int((time.perf_counter() - t_llm) * 1000)
    await _save_reply(ctx, reply, tools_called)
    asyncio.create_task(_log_run(ctx, tools_called, llm_ms, t_start, tin, tout, error))
    return {"reply": reply, "persona": ctx["persona"]["name"], "tools": tools_called, "conversation_id": ctx["conv_id"],
            "voice_id": ctx["persona"].get("tts_voice_id"), "error": error,
            "actions": _client_actions(_tool_outputs(new_msgs)) if not error else []}


async def chat_stream(text: str, device_id: UUID | None = None, channel: str = "text", client: str = "pc") -> AsyncIterator[dict]:
    """Yields {"type":"token","text":..} then {"type":"done", ...}. Streaming is what makes it feel fast."""
    t_start = time.perf_counter()
    new_persona = await router.try_switch(text)
    if new_persona:
        res = await _switch_reply(new_persona, text, device_id, channel)
        yield _start_event(new_persona)
        yield {"type": "token", "text": res["reply"]}
        yield {"type": "done", **res}
        return
    canned = await _send_flow_reply(text, device_id, channel)
    if canned:
        persona = await persona_db.get_active()
        yield _start_event(persona)
        yield {"type": "token", "text": canned["reply"]}
        yield {"type": "done", **canned}
        return

    ctx = await _prepare(text, device_id, channel, client)
    yield _start_event(ctx["persona"])
    t_llm = time.perf_counter()
    parts: list[str] = []
    tools_called: list[str] = []
    tool_outs: list[str] = []
    error = None
    try:
        final_text = ""
        async for mode, data in graph().astream({"messages": ctx["messages"]}, config={"recursion_limit": 16},
                                                stream_mode=["messages", "values"]):
            if mode == "values":
                last = data["messages"][-1] if data.get("messages") else None
                if isinstance(last, AIMessage) and not last.tool_calls:
                    final_text = text_of(last.content)
                continue
            chunk, meta = data
            if isinstance(chunk, ToolMessage):
                if chunk.name:
                    tools_called.append(chunk.name)
                    tool_outs.append(text_of(chunk.content))
                    yield {"type": "tool", "name": chunk.name}
                    if chunk.name in PERSONA_TOOLS:  # the character changed mid-turn: the reply is spoken in the new voice
                        ctx["persona"] = await persona_db.get_active()
                        yield _start_event(ctx["persona"])
                    for act in _client_actions([text_of(chunk.content)]):
                        yield {"type": "action", "action": act}
            elif (isinstance(chunk, AIMessageChunk) and not chunk.tool_call_chunks
                  and meta.get("langgraph_node") == "agent"):  # ignore LLM calls made INSIDE tools (classifiers etc.)
                piece = text_of(chunk.content)
                if piece:
                    parts.append(piece)
                    yield {"type": "token", "text": piece}
        if not parts and final_text.strip():  # model did not stream: send the whole reply at once
            parts.append(final_text)
            yield {"type": "token", "text": final_text}
        if not parts and tools_called:  # empty final message after tools: ask once more using the results
            fallback = await _answer_from_tools(text, tool_outs)
            if fallback:
                parts.append(fallback)
                yield {"type": "token", "text": fallback}
        if not parts and not tools_called:  # the model said nothing at all: ask again once (not a connection problem)
            log.warning("empty model response, retrying once")
            _, retry_reply, retry_tools, _, _ = await _invoke(ctx)
            tools_called.extend(retry_tools)
            if retry_reply:
                parts.append(retry_reply)
                yield {"type": "token", "text": retry_reply}
    except Exception as e:
        log.warning("stream failed: %s: %s", type(e).__name__, e)
        error = f"{type(e).__name__}: {e}"
        if not parts and _is_transient(e):
            try:  # same request again through the patient (3-try) path
                _, retry_reply, retry_tools, _, _ = await _invoke(ctx)
                tools_called.extend(retry_tools)
                if retry_reply:
                    error = None
                    parts.append(retry_reply)
                    yield {"type": "token", "text": retry_reply}
            except Exception as e2:
                error = f"{type(e2).__name__}: {e2}"
        if not parts:
            msg = _failure_text(e)
            parts.append(msg)
            yield {"type": "token", "text": msg}
    reply = "".join(parts).strip() or ("Done." if tools_called else "Sorry, I didn't catch that. Could you say it again?")
    llm_ms = int((time.perf_counter() - t_llm) * 1000)
    await _save_reply(ctx, reply, tools_called)
    asyncio.create_task(_log_run(ctx, tools_called, llm_ms, t_start, None, None, error))
    yield {"type": "done", "reply": reply, "persona": ctx["persona"]["name"], "tools": tools_called, "error": error}
