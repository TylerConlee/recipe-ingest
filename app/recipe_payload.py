"""Pure helpers: turn the extracted recipe dict into Tandoor API payloads (no I/O)."""

from __future__ import annotations

from fractions import Fraction
from typing import Any, List, Optional, Tuple

from app.constants import (
    ING_NOTE_MAX,
    RECIPE_DESC_MAX,
    RECIPE_NAME_MAX,
    STEP_NAME_MAX,
    TANDOOR_DEFAULT_UNIT_NAME,
)


def detect_platform(url: str) -> str:
    u = url.lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u:
        return "instagram"
    if "youtube.com/shorts" in u or "youtu.be" in u:
        return "youtube"
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
        if s is None:
            continue
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
            inst_str = str(inst).strip()
            name = (s.get("name") or "").strip()
            ingredients = list(s.get("ingredients") or [])
            if not inst_str and not name and not ingredients:
                continue
            out.append(
                {
                    "name": name,
                    "instruction": inst_str,
                    "time_minutes": s.get("time_minutes", s.get("time")),
                    "ingredients": ingredients,
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


def _tandoor_unit_name(raw: Any) -> str:
    u = (raw or "").strip() if isinstance(raw, str) else str(raw or "").strip()
    if not u:
        return TANDOOR_DEFAULT_UNIT_NAME
    return u[:64]


def tandoor_ingredient_payload(ing: dict, order: int) -> Optional[dict]:
    item = (ing.get("item") or ing.get("food") or "").strip()
    is_header = bool(ing.get("is_section_header") or ing.get("is_header"))
    if is_header:
        title = item or "Section"
        return {
            "food": {"name": title[:128]},
            "unit": {"name": _tandoor_unit_name(ing.get("unit"))},
            "amount": 0,
            "note": "",
            "order": order,
            "is_header": True,
            "no_amount": True,
            "original_text": title[:512],
        }

    if not item:
        return None

    u_raw = ing.get("unit")
    if isinstance(u_raw, str):
        raw_unit_display = u_raw.strip()
    else:
        raw_unit_display = str(u_raw or "").strip()
    unit = _tandoor_unit_name(ing.get("unit"))
    note = (ing.get("note") or "").strip()
    if ing.get("source") == "visual_estimate" and not ing.get("no_amount"):
        note = f"{note}; amount estimated from video" if note else "amount estimated from video"
    note = note[:ING_NOTE_MAX]
    no_amount = bool(ing.get("no_amount"))
    amount_f = 0.0 if no_amount else parse_amount_to_float(ing.get("amount"))
    if not no_amount and amount_f <= 0 and not raw_unit_display:
        no_amount = True

    amt_str = ""
    if not no_amount and ing.get("amount") is not None and str(ing.get("amount")).strip() != "":
        amt_str = str(ing.get("amount")).strip()
    orig = format_ingredient_line(amt_str, raw_unit_display, item, note)[:512]

    return {
        "food": {"name": item[:128]},
        "unit": {"name": unit},
        "amount": float(amount_f),
        "note": note,
        "order": order,
        "is_header": False,
        "no_amount": no_amount,
        "original_text": orig,
    }


def notes_step_text(recipe: dict) -> str:
    """Equipment, creator tips and import caveats rendered as a final markdown step."""
    sections = []
    equipment = [e for e in (recipe.get("equipment") or []) if isinstance(e, str) and e.strip()]
    tips = [t for t in (recipe.get("tips") or []) if isinstance(t, str) and t.strip()]
    unsure = [u for u in (recipe.get("uncertainties") or []) if isinstance(u, str) and u.strip()]
    if equipment:
        sections.append("**Equipment:** " + ", ".join(e.strip() for e in equipment))
    if tips:
        sections.append("**Tips**\n" + "\n".join(f"- {t.strip()}" for t in tips))
    if unsure:
        sections.append("**Import notes** (double-check against the video)\n"
                        + "\n".join(f"- {u.strip()}" for u in unsure))
    return "\n\n".join(sections)


def build_tandoor_steps(recipe: dict) -> List[dict]:
    normalized = normalize_gpt_steps(recipe)
    notes = notes_step_text(recipe)
    if notes:
        normalized.append({"name": "Notes", "instruction": notes, "time_minutes": None, "ingredients": []})
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
