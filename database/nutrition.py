"""Nutrition goals, food catalog, meals, daily totals."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from database import audit, db
from timeutil import local_today, tz, utcnow

MACROS = ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g")


async def get_goal(day: date | None = None) -> dict | None:
    return await db.fetchrow(
        "SELECT * FROM nutrition_goals WHERE user_id=%s AND effective_from <= %s ORDER BY effective_from DESC LIMIT 1",
        db.owner_id(), day or local_today(),
    )


async def set_goal(calories: int, protein_g=None, carbs_g=None, fat_g=None, fiber_g=None, water_ml=None,
                   note: str | None = None, effective_from: date | None = None, actor: str = "agent") -> dict:
    """Versioned: a new goal applies from a date, old days keep their old goal."""
    eff = effective_from or local_today()
    prev = await get_goal(eff)
    row = await db.fetchrow(
        "INSERT INTO nutrition_goals(user_id,effective_from,calories,protein_g,carbs_g,fat_g,fiber_g,water_ml,note) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (user_id, effective_from) DO UPDATE SET calories=EXCLUDED.calories, "
        "protein_g=COALESCE(EXCLUDED.protein_g, nutrition_goals.protein_g), carbs_g=COALESCE(EXCLUDED.carbs_g, nutrition_goals.carbs_g), "
        "fat_g=COALESCE(EXCLUDED.fat_g, nutrition_goals.fat_g), fiber_g=COALESCE(EXCLUDED.fiber_g, nutrition_goals.fiber_g), "
        "water_ml=COALESCE(EXCLUDED.water_ml, nutrition_goals.water_ml), note=COALESCE(EXCLUDED.note, nutrition_goals.note) RETURNING *",
        db.owner_id(), eff, calories, protein_g, carbs_g, fat_g, fiber_g, water_ml, note,
    )
    await audit.record(actor, "update" if prev and prev["effective_from"] == eff else "insert", "nutrition_goals", row["id"], prev, row)
    return row


async def find_food(name: str) -> dict | None:
    """User's own corrected foods win over the global catalog."""
    n = name.strip().lower()
    return await db.fetchrow(
        "SELECT *, GREATEST(similarity(lower(name), %s), "
        "COALESCE((SELECT max(similarity(lower(a), %s)) FROM unnest(aliases) a), 0)) AS sim "
        "FROM food_catalog WHERE (user_id IS NULL OR user_id=%s) AND "
        "(similarity(lower(name), %s) > 0.5 OR %s = ANY(SELECT lower(a) FROM unnest(aliases) a) OR lower(name) = %s) "
        "ORDER BY (user_id IS NULL), sim DESC LIMIT 1",
        n, n, db.owner_id(), n, n, n,
    )


async def save_food(name: str, serving_desc: str, macros: dict, aliases: list[str] | None = None, verified: bool = False) -> dict:
    """Add (or refresh) a user-specific food entry, e.g. from a correction."""
    existing = await db.fetchrow(
        "SELECT id FROM food_catalog WHERE user_id=%s AND lower(name)=lower(%s)", db.owner_id(), name
    )
    if existing:
        return await db.fetchrow(
            "UPDATE food_catalog SET serving_desc=%s, calories=%s, protein_g=%s, carbs_g=%s, fat_g=%s, fiber_g=%s, verified=%s "
            "WHERE id=%s RETURNING *",
            serving_desc, macros["calories"], macros.get("protein_g", 0), macros.get("carbs_g", 0),
            macros.get("fat_g", 0), macros.get("fiber_g", 0), verified, existing["id"],
        )
    return await db.fetchrow(
        "INSERT INTO food_catalog(user_id,name,aliases,serving_desc,calories,protein_g,carbs_g,fat_g,fiber_g,verified) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        db.owner_id(), name, aliases or [], serving_desc, macros["calories"], macros.get("protein_g", 0),
        macros.get("carbs_g", 0), macros.get("fat_g", 0), macros.get("fiber_g", 0), verified,
    )


async def log_meal(
    items: list[dict],
    meal_type: str | None = None,
    eaten_at: datetime | None = None,
    raw_text: str | None = None,
    source: str = "text",
    source_message_id: UUID | None = None,
    actor: str = "agent",
) -> dict:
    if not items:
        raise ValueError("A meal needs at least one item")
    if meal_type not in (None, "breakfast", "lunch", "dinner", "snack"):
        meal_type = None
    async with db.transaction() as tx:
        meal = await tx.fetchrow(
            "INSERT INTO meals(user_id,eaten_at,meal_type,raw_text,source,source_message_id) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            db.owner_id(), eaten_at or utcnow(), meal_type, raw_text, source, source_message_id,
        )
        saved = []
        for it in items:
            saved.append(await tx.fetchrow(
                "INSERT INTO meal_items(meal_id,food_id,name,quantity,unit,calories,protein_g,carbs_g,fat_g,fiber_g,confidence) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                meal["id"], it.get("food_id"), it["name"], it.get("quantity", 1), it.get("unit"),
                round(float(it["calories"]), 1), round(float(it.get("protein_g", 0)), 1), round(float(it.get("carbs_g", 0)), 1),
                round(float(it.get("fat_g", 0)), 1), round(float(it.get("fiber_g", 0)), 1), it.get("confidence"),
            ))
    await audit.record(actor, "insert", "meals", meal["id"], None, {"meal": meal, "items": saved})
    return {"meal": meal, "items": saved}


async def day_totals(day: date | None = None) -> dict:
    day = day or local_today()
    row = await db.fetchrow("SELECT * FROM daily_nutrition WHERE user_id=%s AND day=%s", db.owner_id(), day)
    zero = {k: 0.0 for k in MACROS}
    if not row:
        return zero
    return {k: float(row[k] or 0) for k in MACROS}


async def day_meals(day: date | None = None) -> list[dict]:
    day = day or local_today()
    tzname = (await db.fetchval("SELECT timezone FROM users WHERE id=%s", db.owner_id())) or "Asia/Kolkata"
    return await db.fetch(
        "SELECT m.id AS meal_id, m.meal_type, m.eaten_at, i.id AS item_id, i.name, i.quantity, i.unit, i.calories, "
        "i.protein_g, i.carbs_g, i.fat_g, i.confidence FROM meals m JOIN meal_items i ON i.meal_id=m.id "
        "WHERE m.user_id=%s AND (m.eaten_at AT TIME ZONE %s)::date = %s ORDER BY m.eaten_at, i.name",
        db.owner_id(), tzname, day,
    )


async def day_summary(day: date | None = None) -> dict:
    day = day or local_today()
    totals = await day_totals(day)
    goal = await get_goal(day)
    remaining = None
    if goal:
        remaining = {
            "calories": float(goal["calories"]) - totals["calories"],
            "protein_g": (float(goal["protein_g"]) - totals["protein_g"]) if goal["protein_g"] is not None else None,
        }
    return {"day": day.isoformat(), "totals": totals, "goal": goal, "remaining": remaining, "items": await day_meals(day)}


async def get_item(item_id: UUID) -> dict | None:
    return await db.fetchrow(
        "SELECT i.*, m.eaten_at FROM meal_items i JOIN meals m ON m.id=i.meal_id WHERE i.id=%s AND m.user_id=%s",
        item_id, db.owner_id(),
    )


async def find_item(query: str, day: date | None = None) -> dict | None:
    """Most recent meal item that matches a name (default: last 3 days)."""
    try:
        return await get_item(UUID(query))
    except ValueError:
        pass
    return await db.fetchrow(
        "SELECT i.*, m.eaten_at FROM meal_items i JOIN meals m ON m.id=i.meal_id "
        "WHERE m.user_id=%s AND m.eaten_at > now() - interval '3 days' AND "
        "(lower(i.name) LIKE %s OR similarity(lower(i.name), lower(%s)) > 0.3) ORDER BY m.eaten_at DESC LIMIT 1",
        db.owner_id(), f"%{query.lower()}%", query,
    )


async def correct_item(item_id: UUID, changes: dict[str, Any], actor: str = "agent") -> dict:
    allowed = {"name", "quantity", "unit", "calories", "protein_g", "carbs_g", "fat_g", "fiber_g"}
    bad = set(changes) - allowed
    if bad:
        raise ValueError(f"Cannot change {sorted(bad)}")
    before = await get_item(item_id)
    if not before:
        raise ValueError("Meal item not found")
    cols = ", ".join(f"{c}=%s" for c in changes)
    row = await db.fetchrow(f"UPDATE meal_items SET {cols}, confidence=1.0 WHERE id=%s RETURNING *", *changes.values(), item_id)
    await db.execute("UPDATE meals SET user_corrected=true WHERE id=%s", before["meal_id"])
    await audit.record(actor, "update", "meal_items", item_id, before, row)
    return row
