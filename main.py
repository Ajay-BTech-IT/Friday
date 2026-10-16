"""FastAPI app. Start with:  python run.py   (see README)."""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

# Windows dev note (Architecture section 3): psycopg async needs the selector loop. run.py guarantees it; this
# policy line also covers `python -c`/pytest style entry points.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI

from config import settings
from database import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    for p in settings.validate():
        log.warning("config: %s", p)
    if sys.platform == "win32" and "Proactor" in type(asyncio.get_running_loop()).__name__:
        raise RuntimeError("Wrong event loop for psycopg on Windows. Start the server with:  python run.py")

    # 1. database: migrations, owner, personas, foods
    await db.init_db()
    # 2. models loaded once
    from orchestrator import model

    await model.warm()
    asyncio.create_task(_embedder_ready())
    # 3. background jobs
    from jobs import runner

    runner.start()
    log.info("Agent ready at http://%s:%s  (docs: /docs)", settings.host, settings.port)
    try:
        yield
    finally:
        runner.stop()
        from voice import tts

        await tts.close()
        from orchestrator import model as _m

        await _m.close_embedder()
        await db.close_db()


async def _embedder_ready() -> None:
    """Background: confirm the embedder works and bring stored vectors into its space (never blocks start-up)."""
    from database import memory

    try:
        await model_embed_probe()
        res = await memory.reembed_all()
        if res["reembedded"]:
            log.info("Re-embedded %d memories with %s", res["reembedded"], res["model"])
    except Exception as e:
        log.warning("Embeddings not ready (%s). Memory search is paused, chat still works.", e)


async def model_embed_probe() -> None:
    from orchestrator.model import embed

    await embed("ping", "query")


app = FastAPI(title="Personal Agent", version="1.0", lifespan=lifespan)

from api import auth, chat, drafts, misc  # noqa: E402

app.include_router(chat.router)
app.include_router(drafts.router)
app.include_router(misc.router)
app.include_router(auth.router)


@app.get("/health", tags=["health"])
async def health() -> dict:
    ok_db = False
    try:
        ok_db = (await db.fetchval("SELECT 1")) == 1
    except Exception:
        pass
    return {"status": "ok" if ok_db else "degraded", "db": ok_db, "llm_key": bool(settings.google_api_key),
            "embeddings": settings.embedder_tag,
            "google_configured": settings.google_enabled}
