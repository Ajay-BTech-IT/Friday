"""Meal tools: log by text, daily totals vs goal, goals, corrections that teach the agent."""
from __future__ import annotations

import logging
from datetime import date

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from database import memory as memory_db
from database import nutrition as nut
from orchestrator.model import chat_model
from timeutil import local_today, parse_when
from tools._util import out, safe

log = logging.getLogger("tools.meals")


class MealItemIn(BaseModel):
    name: str = Field(description="Food name, e.g. 'idli', 'sambar', 'boiled egg'")
    quantity: float = Field(1, description="How many servings/pieces (2 idlis -> 2). If unit is g/ml, the amount.")
    unit: str | None = Field(None, description="piece, katori, cup, g, ml, plate ... optional")


class _Est(BaseModel):
    name: str
    calories: float
    protein_g: float = 0
    carbs_g: float = 0
    fat_g: float = 0
    fiber_g: float = 0
    confidence: float = Field(0.6, description="0-1 how sure you are")


class _EstList(BaseModel):
    items: list[_Est]


async def _estimate(missing: list[MealItemIn]) -> list[_Est]:
    """One Gemini call for items not found in the catalog. Totals are for the stated portion."""
    lines = "\n".join(f"- {m.quantity:g} {m.unit or 'serving(s)'} of {m.name}" for m in missing)
    prompt = (
        "Estimate nutrition for the TOTAL stated portion of each food (not per 100g). Typical Indian home-cooked "
        "portions unless stated. Be realistic, not optimistic. Return one entry per line, same order, using the "
        f"food name as given.\n{lines}"
    )
    res = await chat_model(0.1).with_structured_output(_EstList).ainvoke(prompt)
    return res.items


VAGUE = {"lunch", "breakfast", "dinner", "snack", "snacks", "meal", "food", "brunch", "something", "stuff", "tiffin",
         "eating", "khana", "anything", "usual", "my usual", "normal", "regular meal", "a meal", "some food"}


def _check_specific(items: list[MealItemIn]) -> None:
    """Never invent a meal: the user must have said WHAT they ate."""
    for it in items:
        if it.name.strip().lower() in VAGUE or len(it.name.strip()) < 2:
            raise ValueError("The user has not said what they ate (only a meal name like 'lunch'). Do NOT log anything. "
                             "Ask them which foods they had and roughly how much, then call log_meal.")


async def _resolve(items: list[MealItemIn]) -> list[dict]:
    _check_specific(items)
    resolved: list[dict | None] = []
    missing: list[tuple[int, MealItemIn]] = []
    for i, it in enumerate(items):
        food = await nut.find_food(it.name)
        if food:
            if it.unit in ("g", "ml") and food["serving_grams"]:
                scale = it.quantity / float(food["serving_grams"])
            else:
                scale = it.quantity
            resolved.append({
                "food_id": food["id"], "name": it.name, "quantity": it.quantity, "unit": it.unit or food["serving_desc"],
                "calories": float(food["calories"]) * scale, "protein_g": float(food["protein_g"]) * scale,
                "carbs_g": float(food["carbs_g"]) * scale, "fat_g": float(food["fat_g"]) * scale,
                "fiber_g": float(food["fiber_g"]) * scale, "confidence": 0.95 if food["user_id"] else 0.8,
            })
        else:
            resolved.append(None)
            missing.append((i, it))
    if missing:
        ests = await _estimate([m for _, m in missing])
        for (i, it), e in zip(missing, ests):
            resolved[i] = {"name": it.name, "quantity": it.quantity, "unit": it.unit, "calories": e.calories,
                           "protein_g": e.protein_g, "carbs_g": e.carbs_g, "fat_g": e.fat_g, "fiber_g": e.fiber_g,
                           "confidence": max(0.0, min(1.0, e.confidence))}
        if any(r is None for r in resolved):
            raise RuntimeError("Could not estimate every item; ask the user for portions")
    return resolved  # type: ignore[return-value]


def _day_view(s: dict) -> dict:
    t, g = s["totals"], s["goal"]
    view = {"eaten": {k: round(v) for k, v in t.items()}}
    if g:
        view["goal"] = {"calories": g["calories"], "protein_g": float(g["protein_g"]) if g["protein_g"] is not None else None}
        view["remaining"] = {k: (round(v) if v is not None else None) for k, v in s["remaining"].items()}
    else:
        view["goal"] = "not set - ask the user for calorie and protein targets"
    return view


@tool
@safe
async def log_meal(items: list[MealItemIn], meal_type: str | None = None, eaten_at: str | None = None,
                   raw_text: str | None = None) -> str:
    """Log what the user ate ("had 2 idlis and sambar for breakfast"). List each food as its own item with a
    quantity. NEVER guess foods: if the user only says they had 'lunch' (or breakfast/dinner/a meal) without naming
    what they ate, do not call this; ask what they ate first. Nutrition comes from the food catalog first, otherwise an estimate; every entry stays correctable.
    meal_type: breakfast | lunch | dinner | snack. eaten_at is optional ISO 8601 local time (default now).
    Returns this meal's total and what is left for the day against the goal.
    """
    items = [MealItemIn.model_validate(i) if isinstance(i, dict) else i for i in items]
    resolved = await _resolve(items)
    when = parse_when(eaten_at) if eaten_at else None
    res = await nut.log_meal(resolved, meal_type, when, raw_text)
    summary = await nut.day_summary(local_today() if when is None else when.astimezone().date())
    meal_total = {k: round(sum(float(i[k]) for i in res["items"])) for k in ("calories", "protein_g")}
    return out({
        "logged": [{"item_id": i["id"], "name": i["name"], "qty": f"{i['quantity']} {i['unit'] or ''}".strip(),
                    "kcal": i["calories"], "protein_g": i["protein_g"], "confidence": i["confidence"]} for i in res["items"]],
        "meal_total": meal_total, "today": _day_view(summary),
        "tip": "Estimates for home-cooked food can be off; the user can correct any item.",
    })


@tool
@safe
async def get_day_summary(day: str | None = None) -> str:
    """Totals vs goal for a day (YYYY-MM-DD, default today) with every logged item."""
    d = date.fromisoformat(day) if day else local_today()
    s = await nut.day_summary(d)
    return out({"day": s["day"], **_day_view(s), "items": [
        {"item_id": i["item_id"], "meal": i["meal_type"], "name": i["name"], "kcal": i["calories"], "protein_g": i["protein_g"]}
        for i in s["items"]]})


@tool
@safe
async def set_nutrition_goal(calories: int, protein_g: float | None = None, carbs_g: float | None = None,
                             fat_g: float | None = None, fiber_g: float | None = None,
                             water_ml: int | None = None, note: str | None = None) -> str:
    """Set or change the daily nutrition goal from today on (past days keep their old goal)."""
    row = await nut.set_goal(calories, protein_g, carbs_g, fat_g, fiber_g, water_ml, note)
    return out({"goal_set": {"from": row["effective_from"], "calories": row["calories"], "protein_g": row["protein_g"]}})


@tool
@safe
async def correct_meal(item: str, quantity: float | None = None, calories: float | None = None,
                       protein_g: float | None = None, carbs_g: float | None = None,
                       fat_g: float | None = None, unit: str | None = None) -> str:
    """Fix a logged item ("that was a bigger portion", "rice was 250g"). `item` is the item id or its name
    (most recent match). If only quantity is given, the nutrition is scaled proportionally. The correction is
    remembered so future estimates for this food improve.
    """
    it = await nut.find_item(item)
    if not it:
        return out({"error": f"No recent logged item matching '{item}'"})
    changes: dict = {}
    old_q = float(it["quantity"]) or 1.0
    if quantity is not None:
        changes["quantity"] = quantity
    if unit is not None:
        changes["unit"] = unit
    explicit = {k: v for k, v in dict(calories=calories, protein_g=protein_g, carbs_g=carbs_g, fat_g=fat_g).items() if v is not None}
    if explicit:
        changes.update(explicit)
    elif quantity is not None:
        f = quantity / old_q
        for k in ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g"):
            changes[k] = round(float(it[k]) * f, 1)
    if not changes:
        return out({"error": "Nothing to correct. Ask what the right quantity or numbers are."})
    row = await nut.correct_item(it["id"], changes)

    per = max(float(row["quantity"]) or 1.0, 0.0001)
    try:
        await nut.save_food(row["name"], f"1 {row['unit'] or 'serving'} (your corrected value)",
                            {k: float(row[k]) / per for k in ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g")},
                            verified=True)
        await memory_db.remember_or_update(
            "preference",
            f"User's {row['name']}: {row['quantity']} {row['unit'] or 'serving(s)'} is about {row['calories']:.0f} kcal, "
            f"{row['protein_g']:.0f} g protein (corrected by the user).", 0.6,
        )
    except Exception as e:
        log.warning("correction memory skipped: %s", e)
    s = await nut.day_summary(local_today())
    return out({"corrected": row["name"], "now": {"qty": row["quantity"], "kcal": row["calories"], "protein_g": row["protein_g"]},
                "today": _day_view(s)})


@tool
@safe
async def food_nutrition(food: str, amount_g: float = 100) -> str:
    """Answer "how many calories / how much protein is in X" for any food, without logging anything.
    amount_g is the weight in grams (default 100). Uses the user's food catalog when it knows the food, otherwise an
    estimate. Use this for nutrition QUESTIONS; use log_meal only when the user says they ate something."""
    f = await nut.find_food(food)
    if f and f["serving_grams"]:
        k = amount_g / float(f["serving_grams"])
        vals = {m: round(float(f[m]) * k, 1) for m in ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g")}
        return out({"food": f["name"], "per": f"{amount_g:g} g", "source": "catalog", **vals})
    est = (await _estimate([MealItemIn(name=food, quantity=amount_g, unit="g")]))[0]
    return out({"food": food, "per": f"{amount_g:g} g", "source": "estimate (approximate)", "calories": est.calories,
                "protein_g": est.protein_g, "carbs_g": est.carbs_g, "fat_g": est.fat_g, "fiber_g": est.fiber_g})
