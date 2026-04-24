"""Pure helpers: parse GPT recipe JSON and build Tandoor API payloads (no I/O)."""

from __future__ import annotations

import json
from fractions import Fraction
from typing import Any, List, Optional, Tuple

from app.constants import (
    ING_NOTE_MAX,
    RECIPE_DESC_MAX,
    RECIPE_NAME_MAX,
    STEP_NAME_MAX,
)


def detect_platform(url: str) -> str:
    u = url.lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u:
        return "instagram"
    return "unknown"


def pick_thumbnail_url(info_meta: dict) -> Optional[str]:
    thumb = info_meta.get("thumbnail")
    if thumb:
        return thumb

    thumbs = info_meta.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        for t in reversed(thumbs):
            u = (t or {}).get("url")
            if u:
                return u
        u = (thumbs[0] or {}).get("url")
        return u

    return None


def format_ingredient_line(amount: str, unit: str, item: str, note: str) -> str:
    qty = " ".join(p for p in [amount, unit] if p).strip()
    line = " ".join(p for p in [qty, item] if p).strip()
    if note:
        line = f"{line}, {note}".strip(", ").strip()
    return line


def parse_amount_to_float(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    if isinstance(val, bool):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().lower()
    if s in ("pinch", "dash", "to taste"):
        return 0.0
    range_separated = "-" in s and "/" not in s.split("-", 1)[0]
    if range_separated:
        s = s.split("-", 1)[0].strip()
    total = 0.0
    for part in s.replace("–", "-").split():
        if not part:
            continue
        if "/" in part:
            try:
                total += float(Fraction(part))
            except (ValueError, ZeroDivisionError):
                pass
        else:
            try:
                total += float(part.replace(",", "."))
            except ValueError:
                pass
    return total


def as_int_minutes(v: Any) -> int:
    if v is None:
        return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return max(0, v)
    if isinstance(v, float):
        return max(0, int(round(v)))
    try:
        s = str(v).strip()
        if not s:
            return 0
        return max(0, int(round(float(s.replace(",", ".")))))
    except (ValueError, TypeError):
        return 0


def parse_servings(recipe: dict) -> Tuple[int, str]:
    raw = recipe.get("servings")
    text = (recipe.get("servings_text") or "").strip()[:32]
    n = 1
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        n = max(1, int(round(float(raw))))
    elif isinstance(raw, str):
        s = raw.strip()
        if "-" in s and s and s[0].isdigit():
            s = s.split("-", 1)[0].strip()
        try:
            n = max(1, int(round(float(s.replace(",", ".")))))
        except ValueError:
            n = 1
    return n, text


def working_and_waiting_minutes(recipe: dict) -> Tuple[int, int]:
    prep = as_int_minutes(recipe.get("prep_time_minutes"))
    cook = as_int_minutes(recipe.get("cook_time_minutes"))
    passive = as_int_minutes(recipe.get("passive_time_minutes"))
    if prep or cook or passive:
        return prep + cook, passive
    legacy = recipe.get("total_time")
    if legacy is not None and str(legacy).strip():
        m = as_int_minutes(legacy)
        return m, 0
    return 0, 0


def build_keywords(recipe: dict, platform: str) -> List[str]:
    seen = set()
    out: List[str] = []

    def add(k: str) -> None:
        k = (k or "").strip()
        if len(k) < 2:
            return
        key = k.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(k[:128])

    add("Social import")
    if platform and platform != "unknown":
        add(platform.capitalize())

    cuisine = recipe.get("cuisine")
    if isinstance(cuisine, str):
        add(cuisine)

    for kw in recipe.get("keywords") or []:
        if isinstance(kw, str):
            add(kw)
        elif isinstance(kw, dict) and kw.get("name"):
            add(str(kw["name"]))

    return out[:16]


def normalize_gpt_steps(recipe: dict) -> List[dict]:
    """Turn GPT `steps` plus optional legacy flat `ingredients` into a list of dicts."""
    raw_steps = recipe.get("steps") or []
    flat_ingredients = recipe.get("ingredients") or []

    out: List[dict] = []
    for s in raw_steps:
        if isinstance(s, str):
            out.append(
                {
                    "name": "",
                    "instruction": s.strip(),
                    "time_minutes": None,
                    "ingredients": [],
                }
            )
        elif isinstance(s, dict):
            inst = s.get("instruction") or s.get("text") or ""
            if isinstance(inst, dict):
                inst = inst.get("text") or ""
            out.append(
                {
                    "name": (s.get("name") or "").strip(),
                    "instruction": str(inst).strip(),
                    "time_minutes": s.get("time_minutes", s.get("time")),
                    "ingredients": list(s.get("ingredients") or []),
                }
            )

    any_step_ing = any(len(st.get("ingredients") or []) > 0 for st in out)
    if flat_ingredients and not any_step_ing:
        converted = []
        for ing in flat_ingredients:
            if isinstance(ing, dict):
                converted.append(
                    {
                        "amount": ing.get("amount"),
                        "unit": ing.get("unit") or "",
                        "item": ing.get("item") or "",
                        "note": ing.get("note") or "",
                        "is_section_header": bool(ing.get("is_section_header")),
                        "no_amount": bool(ing.get("no_amount")),
                    }
                )
        if converted:
            out.insert(
                0,
                {
                    "name": "Ingredients",
                    "instruction": "Gather and measure the following.",
                    "time_minutes": None,
                    "ingredients": converted,
                },
            )

    if not out:
        out.append(
            {
                "name": "Instructions",
                "instruction": "Watch the source video for detailed technique.",
                "time_minutes": None,
                "ingredients": [],
            }
        )
    return out


def tandoor_ingredient_payload(ing: dict, order: int) -> Optional[dict]:
    item = (ing.get("item") or ing.get("food") or "").strip()
    is_header = bool(ing.get("is_section_header") or ing.get("is_header"))
    if is_header:
        title = item or "Section"
        return {
            "food": {"name": title[:128]},
            "unit": {"name": ""},
            "amount": 0,
            "note": "",
            "order": order,
            "is_header": True,
            "no_amount": True,
            "original_text": title[:512],
        }

    if not item:
        return None

    unit = (ing.get("unit") or "").strip()
    note = (ing.get("note") or "").strip()[:ING_NOTE_MAX]
    no_amount = bool(ing.get("no_amount"))
    amount_f = 0.0 if no_amount else parse_amount_to_float(ing.get("amount"))
    if not no_amount and amount_f <= 0 and not unit:
        no_amount = True

    amt_str = ""
    if not no_amount and ing.get("amount") is not None and str(ing.get("amount")).strip() != "":
        amt_str = str(ing.get("amount")).strip()
    orig = format_ingredient_line(amt_str, unit, item, note)[:512]

    return {
        "food": {"name": item[:128]},
        "unit": {"name": (unit or "")[:64]},
        "amount": float(amount_f),
        "note": note,
        "order": order,
        "is_header": False,
        "no_amount": no_amount,
        "original_text": orig,
    }


def build_tandoor_steps(recipe: dict) -> List[dict]:
    normalized = normalize_gpt_steps(recipe)
    steps_out: List[dict] = []
    for idx, st in enumerate(normalized):
        name = (st.get("name") or "").strip()[:STEP_NAME_MAX]
        instruction = (st.get("instruction") or "").strip()

        t = st.get("time_minutes")
        step_time = as_int_minutes(t) if t is not None else 0

        ing_payload: List[dict] = []
        for j, raw in enumerate(st.get("ingredients") or []):
            if not isinstance(raw, dict):
                continue
            row = tandoor_ingredient_payload(raw, j)
            if row:
                ing_payload.append(row)

        if not instruction:
            instruction = (
                "Prepare the ingredients listed for this step."
                if ing_payload
                else "See the source video for details."
            )

        steps_out.append(
            {
                "name": name,
                "instruction": instruction,
                "time": step_time,
                "order": idx,
                "ingredients": ing_payload,
            }
        )
    return steps_out


def build_tandoor_recipe_payload(recipe: dict, page_url: str, platform: str) -> dict:
    title = (recipe.get("title") or "Social Recipe").strip()[:RECIPE_NAME_MAX]
    description = (recipe.get("description") or "").strip()
    if len(description) > RECIPE_DESC_MAX:
        description = description[: RECIPE_DESC_MAX - 3] + "..."

    source_url = (recipe.get("source_url") or page_url or "").strip()[:1024]

    servings, servings_text = parse_servings(recipe)
    working_time, waiting_time = working_and_waiting_minutes(recipe)
    keywords = [{"name": n} for n in build_keywords(recipe, platform)]
    steps = build_tandoor_steps(recipe)

    return {
        "name": title,
        "description": description,
        "source_url": source_url,
        "servings": servings,
        "servings_text": servings_text,
        "working_time": working_time,
        "waiting_time": waiting_time,
        "internal": True,
        "private": False,
        "show_ingredient_overview": True,
        "keywords": keywords,
        "steps": steps,
    }


def gpt_recipe_system_and_user_prompts(
    caption: str, transcript: str, url: str, manual_title: Optional[str]
) -> Tuple[str, str]:
    """Build the fixed extraction prompts (used by OpenAI and unit-tested for stability)."""
    prompt = """
You are a recipe extraction engine for Tandoor Recipes (structured steps, each step may list its own ingredients).

Given:
- The social media CAPTION for a cooking video
- The video TRANSCRIPT
- The SOURCE URL

Extract one complete recipe. Prefer accurate amounts from the transcript when the caption is vague.

Respond ONLY with JSON using this schema:

{
  "title": "string",
  "description": "string (plain text, under 500 characters if possible)",
  "servings": "number or null (integer servings)",
  "servings_text": "string or null (e.g. portions, slices, cookies — max ~30 chars)",
  "prep_time_minutes": "integer or null (active prep/mise en place)",
  "cook_time_minutes": "integer or null (stovetop/oven active cooking)",
  "passive_time_minutes": "integer or null (chill, marinate, rest, unattended bake)",
  "cuisine": "string or null (short, e.g. Italian)",
  "keywords": ["optional extra tags, short phrases, max 8"],
  "steps": [
    {
      "name": "short step title (optional, can be empty string)",
      "instruction": "clear instruction text for this step",
      "time_minutes": "integer minutes for this step or null",
      "ingredients": [
        {
          "amount": "number or string like 1, 0.5, 1/2, 1 1/4",
          "unit": "string (cup, tbsp, tsp, g, kg, ml, l, oz, lb, pinch, clove, …) or empty string if none",
          "item": "food name (ingredient)",
          "note": "string or null (prep notes: diced, melted, …)",
          "is_section_header": "boolean, true only for a non-quantified section divider (e.g. For the sauce)",
          "no_amount": "boolean, true for items like salt to taste when amount is not meaningful"
        }
      ]
    }
  ],
  "source_url": "string (the video URL)"
}

Rules:
- Put ingredients on the step where they are first used when possible. You MAY use a first step named
  "Ingredients" with instruction "Gather and measure." listing all ingredients, then later steps with empty
  "ingredients" arrays if that fits the video better.
- Every non-header ingredient MUST have a meaningful "item" (food name).
- Use "is_section_header": true only for section titles; for those set "item" to the section title text,
  "amount" to 0 or empty, "no_amount": true, "unit": "".
- Include all distinct cooking steps in order; do not merge unrelated actions.
- If time is unknown, use nulls — do not invent precise minutes.
- "source_url" must be the provided SOURCE URL below.
""".strip()

    user_content = f"""
SOURCE URL:
{url}

DEFAULT / MANUAL TITLE (prefer for recipe title if caption title is junk):
{manual_title or ''}

CAPTION:
{caption}

TRANSCRIPT:
{transcript}
"""
    return prompt, user_content


def parse_gpt_recipe_json(raw: str) -> dict:
    """Parse model output; separated for tests."""
    return json.loads(raw)
