"""Gemini chat model + embeddings (local Ollama by default, Gemini optional), loaded once and reused."""
from __future__ import annotations

import asyncio
import logging
import time
from functools import lru_cache

import httpx

from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

from config import settings

log = logging.getLogger("model")


@lru_cache(maxsize=4)
def chat_model(temperature: float = 0.6) -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.google_api_key,
        temperature=temperature,
        max_retries=3,
        timeout=60,
    )


@lru_cache(maxsize=1)
def embedder() -> GoogleGenerativeAIEmbeddings:
    """Gemini embeddings, only used when EMBEDDING_PROVIDER=gemini."""
    model = settings.embedding_model
    if not model.startswith("models/"):
        model = f"models/{model}"
    return GoogleGenerativeAIEmbeddings(
        model=model,
        google_api_key=settings.google_api_key,
        output_dimensionality=settings.embedding_dim,
    )


_http: httpx.AsyncClient | None = None
_ollama_down_until = 0.0  # after a refused connection, do not retry for a while (keeps every chat fast)


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(90, connect=2))
    return _http


async def close_embedder() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


def _prefix(kind: str) -> str:
    """nomic-embed-text was trained with task prefixes; using them noticeably improves retrieval."""
    if settings.embedding_model.lower().startswith("nomic"):
        return "search_query: " if kind == "query" else "search_document: "
    return ""


async def _ollama(texts: list[str], kind: str) -> list[list[float]]:
    global _ollama_down_until
    if time.monotonic() < _ollama_down_until:
        raise RuntimeError(f"Ollama is not reachable at {settings.ollama_url} (will retry shortly).")
    body = {"model": settings.embedding_model, "input": [_prefix(kind) + t[:8000] for t in texts], "truncate": True}
    try:
        r = await _client().post(f"{settings.ollama_url}/api/embed", json=body)
        if r.status_code == 404 and "not found" in r.text.lower():
            raise RuntimeError(f"Model '{settings.embedding_model}' is not installed. Run:  ollama pull {settings.embedding_model}")
        if r.status_code == 404:  # very old Ollama: single-prompt endpoint
            out = []
            for t in body["input"]:
                rr = await _client().post(f"{settings.ollama_url}/api/embeddings", json={"model": settings.embedding_model, "prompt": t})
                rr.raise_for_status()
                out.append(rr.json()["embedding"])
            vecs = out
        else:
            r.raise_for_status()
            vecs = r.json()["embeddings"]
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        _ollama_down_until = time.monotonic() + 20
        raise RuntimeError(f"Cannot reach Ollama at {settings.ollama_url}. Install it from ollama.com and start it.") from e
    for v in vecs:
        if len(v) != settings.embedding_dim:
            raise RuntimeError(
                f"'{settings.embedding_model}' returns {len(v)} dimensions but the database expects {settings.embedding_dim}. "
                "Use a 768-dimension model (nomic-embed-text) or change the schema."
            )
    return vecs


async def embed(text: str, kind: str = "document") -> list[float]:
    """Embed one string. kind = 'document' (what gets stored) or 'query' (what is searched with). Raises on failure."""
    if settings.embedding_provider == "ollama":
        return (await _ollama([text], kind))[0]
    return await embedder().aembed_query(text[:8000])


async def embed_many(texts: list[str], kind: str = "document") -> list[list[float]]:
    if not texts:
        return []
    if settings.embedding_provider == "ollama":
        return await _ollama(texts, kind)
    return await embedder().aembed_documents([t[:8000] for t in texts])


async def safe_embed(text: str) -> list[float] | None:
    """Embedding for something to be STORED. None when the embedder is unavailable (memory degrades to text search)."""
    try:
        return await embed(text, "document")
    except Exception as e:
        log.warning("embedding failed: %s", e)
        return None


async def safe_embed_query(text: str) -> list[float] | None:
    """Embedding for something to be SEARCHED with."""
    try:
        return await embed(text, "query")
    except Exception as e:
        log.warning("query embedding failed: %s", e)
        return None


def text_of(content) -> str:
    """Gemini may return a str or a list of content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
        return "".join(parts)
    return str(content or "")


async def warm() -> None:
    """Instantiate models at startup so the first request is not slow."""
    chat_model()
    if settings.embedding_provider == "gemini" and settings.google_api_key:
        embedder()
    await asyncio.sleep(0)
