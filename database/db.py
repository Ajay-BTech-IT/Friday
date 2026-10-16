"""Postgres pool, auto-migration, seeding and query helpers.

Safety rule (Architecture principle): the application can ADD, SELECT and UPDATE
but never DELETE. Every statement sent through the helpers below is checked by
`_guard`, so a bug or a hallucinated tool call cannot remove data. Removal is
modelled as a status change (done / cancelled / ignored) or `valid_to` on memory.
"""
from __future__ import annotations

import hashlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Sequence
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from config import settings

log = logging.getLogger("db")

_pool: AsyncConnectionPool | None = None
OWNER_ID: UUID | None = None

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")

_FORBIDDEN = re.compile(
    r"\b(delete\s+from|drop\s+(table|view|index|schema|database|extension)|truncate|"
    r"alter\s+table|create\s+table|grant|revoke)\b",
    re.I,
)
_LITERAL = re.compile(r"'(?:[^']|'')*'")


class WriteNotAllowed(Exception):
    pass


def _guard(sql: str) -> None:
    stripped = _LITERAL.sub("''", sql)
    m = _FORBIDDEN.search(stripped)
    if m:
        raise WriteNotAllowed(f"Statement blocked (no delete/DDL at runtime): {m.group(0)!r}")


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


def vec(values: Sequence[float]) -> str:
    """pgvector text literal; cast with ::vector in SQL."""
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised. Call init_db() first.")
    return _pool


async def fetch(sql: str, *args: Any) -> list[dict]:
    _guard(sql)
    async with pool().connection() as conn:
        cur = await conn.execute(sql, args or None)
        return await cur.fetchall() if cur.description else []


async def fetchrow(sql: str, *args: Any) -> dict | None:
    rows = await fetch(sql, *args)
    return rows[0] if rows else None


async def fetchval(sql: str, *args: Any) -> Any:
    row = await fetchrow(sql, *args)
    return next(iter(row.values())) if row else None


async def execute(sql: str, *args: Any) -> int:
    _guard(sql)
    async with pool().connection() as conn:
        cur = await conn.execute(sql, args or None)
        return cur.rowcount


@asynccontextmanager
async def transaction() -> AsyncIterator["Tx"]:
    """Multi-statement atomic unit. Same guard applies."""
    async with pool().connection() as conn:
        async with conn.transaction():
            yield Tx(conn)


class Tx:
    def __init__(self, conn: AsyncConnection):
        self._c = conn

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        _guard(sql)
        cur = await self._c.execute(sql, args or None)
        return await cur.fetchall() if cur.description else []

    async def fetchrow(self, sql: str, *args: Any) -> dict | None:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def execute(self, sql: str, *args: Any) -> int:
        _guard(sql)
        cur = await self._c.execute(sql, args or None)
        return cur.rowcount


def owner_id() -> UUID:
    if OWNER_ID is None:
        raise RuntimeError("Owner not initialised")
    return OWNER_ID


# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------
async def init_db() -> None:
    global _pool, OWNER_ID
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is empty. Fill it in .env")

    _pool = AsyncConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=10,
        open=False,
        kwargs={"row_factory": dict_row, "autocommit": True},
    )
    await _pool.open(wait=True, timeout=30)

    await _migrate()

    from database import persona  # local import: avoids cycle

    OWNER_ID = await _seed_owner()
    await persona.seed_personas()
    await _seed_foods()
    log.info("Database ready. Owner=%s", OWNER_ID)


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def _migrate() -> None:
    """Apply schema.sql (idempotent) when its checksum changed, then numbered migrations."""
    async with pool().connection() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        files: list[Path] = [SCHEMA_FILE]
        if MIGRATIONS_DIR.exists():
            files += sorted(MIGRATIONS_DIR.glob("*.sql"))
        for f in files:
            text = f.read_text(encoding="utf-8")
            checksum = hashlib.sha256(text.encode()).hexdigest()
            cur = await conn.execute("SELECT checksum FROM schema_migrations WHERE name=%s", (f.name,))
            row = await cur.fetchone()
            if row and row["checksum"] == checksum:
                continue
            log.info("Applying %s", f.name)
            async with conn.transaction():
                await conn.execute(text)
                await conn.execute(
                    "INSERT INTO schema_migrations(name, checksum) VALUES (%s,%s) "
                    "ON CONFLICT (name) DO UPDATE SET checksum=EXCLUDED.checksum, applied_at=now()",
                    (f.name, checksum),
                )


async def _seed_owner() -> UUID:
    row = await fetchrow(
        "INSERT INTO users(email, display_name, timezone) VALUES (%s,%s,%s) "
        "ON CONFLICT (email) DO UPDATE SET email=EXCLUDED.email RETURNING id",
        settings.owner_email, settings.owner_name, settings.owner_timezone,
    )
    uid = row["id"]
    await execute("INSERT INTO user_profile(user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", uid)
    return uid


# (name, aliases, serving, grams, kcal, protein, carbs, fat, fiber) per serving
_FOODS = [
    ("Idli", ["idly"], "1 idli (40g)", 40, 58, 2.0, 12.0, 0.4, 0.6),
    ("Plain dosa", ["dosa"], "1 dosa", 90, 133, 3.0, 20.0, 4.0, 1.0),
    ("Masala dosa", [], "1 dosa", 180, 250, 5.0, 38.0, 9.0, 2.5),
    ("Sambar", [], "1 katori (150ml)", 150, 75, 3.5, 10.0, 2.5, 3.0),
    ("Coconut chutney", ["chutney"], "2 tbsp", 30, 55, 0.8, 2.5, 4.8, 1.0),
    ("Chapati", ["roti", "phulka"], "1 medium", 40, 104, 3.0, 18.0, 2.5, 2.7),
    ("Paratha plain", ["paratha"], "1 medium", 80, 260, 5.0, 36.0, 10.0, 2.5),
    ("Cooked white rice", ["rice"], "1 katori (150g)", 150, 195, 4.0, 43.0, 0.4, 0.6),
    ("Dal tadka", ["dal", "toor dal"], "1 katori (150g)", 150, 150, 8.0, 20.0, 4.0, 5.0),
    ("Rajma curry", ["rajma"], "1 katori (150g)", 150, 190, 9.0, 27.0, 5.0, 7.0),
    ("Chole", ["chana masala"], "1 katori (150g)", 150, 210, 9.0, 28.0, 7.0, 8.0),
    ("Paneer curry", ["paneer"], "1 katori (150g)", 150, 320, 14.0, 8.0, 26.0, 1.5),
    ("Chicken curry", [], "1 katori (150g)", 150, 240, 22.0, 6.0, 14.0, 1.0),
    ("Boiled egg", ["egg", "eggs"], "1 medium egg", 50, 78, 6.3, 0.6, 5.3, 0.0),
    ("Omelette", [], "2-egg omelette", 110, 190, 13.0, 1.5, 14.0, 0.0),
    ("Curd", ["dahi", "yogurt"], "1 katori (100g)", 100, 60, 3.5, 4.7, 3.3, 0.0),
    ("Milk (whole)", ["milk"], "1 glass (200ml)", 200, 125, 6.4, 9.6, 6.6, 0.0),
    ("Banana", [], "1 medium", 118, 105, 1.3, 27.0, 0.4, 3.1),
    ("Apple", [], "1 medium", 182, 95, 0.5, 25.0, 0.3, 4.4),
    ("Poha", [], "1 plate (150g)", 150, 250, 5.0, 45.0, 6.0, 2.5),
    ("Upma", [], "1 plate (150g)", 150, 230, 5.0, 33.0, 8.0, 3.0),
    ("Samosa", [], "1 piece", 100, 262, 4.0, 24.0, 17.0, 2.0),
    ("Whey protein scoop", ["protein shake", "whey"], "1 scoop (30g)", 30, 120, 24.0, 3.0, 1.5, 0.0),
    ("Peanut butter", [], "1 tbsp", 16, 94, 4.0, 3.0, 8.0, 1.0),
    ("Oats cooked", ["oats"], "1 bowl (200g)", 200, 150, 5.0, 27.0, 3.0, 4.0),
    ("Biryani chicken", ["biryani"], "1 plate (300g)", 300, 490, 24.0, 58.0, 17.0, 2.5),
    ("Tea with milk and sugar", ["chai", "tea"], "1 cup (150ml)", 150, 70, 2.0, 11.0, 2.2, 0.0),
]


async def _seed_foods() -> None:
    if await fetchval("SELECT count(*) FROM food_catalog WHERE user_id IS NULL") > 0:
        return
    for n, al, serv, g, kcal, p, c, f, fi in _FOODS:
        await execute(
            "INSERT INTO food_catalog(user_id,name,aliases,serving_desc,serving_grams,calories,"
            "protein_g,carbs_g,fat_g,fiber_g,verified) VALUES (NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)",
            n, al, serv, g, kcal, p, c, f, fi,
        )
