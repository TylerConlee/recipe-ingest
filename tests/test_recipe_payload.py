import json

import pytest

from app.constants import RECIPE_DESC_MAX
from app.recipe_payload import (
    as_int_minutes,
    build_keywords,
    build_tandoor_recipe_payload,
    build_tandoor_steps,
    detect_platform,
    format_ingredient_line,
    normalize_gpt_steps,
    parse_amount_to_float,
    parse_servings,
    pick_thumbnail_url,
    tandoor_ingredient_payload,
    working_and_waiting_minutes,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.tiktok.com/@x/video/1", "tiktok"),
        ("HTTPS://TIKTOK.COM/foo", "tiktok"),
        ("https://instagram.com/reel/abc", "instagram"),
        ("https://www.instagram.com/p/xyz", "instagram"),
        ("https://youtube.com/watch?v=1", "unknown"),
    ],
)
def test_detect_platform(url, expected):
    assert detect_platform(url) == expected


def test_pick_thumbnail_direct():
    assert pick_thumbnail_url({"thumbnail": "https://a/thumb.jpg"}) == "https://a/thumb.jpg"


def test_pick_thumbnail_from_list_last_with_url():
    meta = {
        "thumbnails": [
            {"url": "https://low.jpg"},
            {"url": "https://high.jpg"},
        ]
    }
    assert pick_thumbnail_url(meta) == "https://high.jpg"


def test_pick_thumbnail_from_list_first_when_last_empty():
    meta = {"thumbnails": [{"url": None}, {"url": "https://only.jpg"}]}
    assert pick_thumbnail_url(meta) == "https://only.jpg"


def test_pick_thumbnail_none():
    assert pick_thumbnail_url({}) is None


def test_pick_thumbnail_list_only_second_has_url():
    meta = {"thumbnails": [{"url": None}, {"url": "https://second.jpg"}]}
    assert pick_thumbnail_url(meta) == "https://second.jpg"


def test_pick_thumbnail_list_all_urls_empty_returns_none():
    meta = {"thumbnails": [{"url": None}, {"url": ""}]}
    assert pick_thumbnail_url(meta) is None


def test_normalize_gpt_steps_nested_instruction_dict_text():
    recipe = {"steps": [{"instruction": {"text": "nested"}, "ingredients": []}]}
    out = normalize_gpt_steps(recipe)
    assert out[0]["instruction"] == "nested"


def test_normalize_gpt_steps_skips_non_dict_ingredient():
    recipe = {
        "steps": [
            {
                "instruction": "Mix",
                "ingredients": ["skip-me", {"item": "salt", "amount": 1, "unit": "tsp"}],
            }
        ]
    }
    steps = build_tandoor_steps(recipe)
    assert len(steps[0]["ingredients"]) == 1


@pytest.mark.parametrize(
    "val,expected",
    [
        (None, 0.0),
        ("", 0.0),
        (2, 2.0),
        (1.5, 1.5),
        ("1/2", 0.5),
        ("1 1/2", 1.5),
        ("2-3", 2.0),
        ("pinch", 0.0),
        ("1,5", 1.5),
        (True, 0.0),
    ],
)
def test_parse_amount_to_float(val, expected):
    assert parse_amount_to_float(val) == pytest.approx(expected)


def test_parse_amount_invalid_fraction():
    assert parse_amount_to_float("1/0") == 0.0


def test_as_int_minutes():
    assert as_int_minutes(None) == 0
    assert as_int_minutes(True) == 0
    assert as_int_minutes(5) == 5
    assert as_int_minutes(5.7) == 6
    assert as_int_minutes("12") == 12
    assert as_int_minutes("bad") == 0


def test_parse_servings_number():
    assert parse_servings({"servings": 4, "servings_text": " bowls "}) == (4, "bowls")


def test_parse_servings_range_string():
    assert parse_servings({"servings": "4-6"}) == (4, "")


def test_parse_servings_invalid_string():
    assert parse_servings({"servings": "many"}) == (1, "")


def test_working_waiting_from_prep_cook_passive():
    assert working_and_waiting_minutes(
        {"prep_time_minutes": 10, "cook_time_minutes": 20, "passive_time_minutes": 30}
    ) == (30, 30)


def test_working_waiting_legacy_total_time():
    assert working_and_waiting_minutes({"total_time": 45}) == (45, 0)


def test_build_keywords_dedupe_and_cap():
    recipe = {
        "cuisine": "Italian",
        "keywords": ["Quick", "quick", {"name": "Pasta"}, "x"],
    }
    kws = build_keywords(recipe, "tiktok")
    assert "Social import" in kws
    assert "Tiktok" in kws
    assert "Italian" in kws
    assert "Quick" in kws
    assert "Pasta" in kws
    assert "x" not in kws  # too short


def test_normalize_gpt_steps_string_steps():
    recipe = {"steps": ["Mix.", "Bake."], "ingredients": []}
    out = normalize_gpt_steps(recipe)
    assert len(out) == 2
    assert out[0]["instruction"] == "Mix."


def test_normalize_gpt_steps_dict_with_text_key():
    recipe = {"steps": [{"name": "A", "text": "Do thing", "ingredients": []}]}
    out = normalize_gpt_steps(recipe)
    assert out[0]["instruction"] == "Do thing"


def test_normalize_gpt_steps_flat_ingredients_merged():
    recipe = {
        "steps": ["Cook until done."],
        "ingredients": [{"amount": "1", "unit": "cup", "item": "rice", "note": ""}],
    }
    out = normalize_gpt_steps(recipe)
    assert out[0]["name"] == "Ingredients"
    assert len(out[0]["ingredients"]) == 1
    assert out[1]["instruction"] == "Cook until done."


def test_normalize_gpt_steps_empty_defaults():
    recipe: dict = {}
    out = normalize_gpt_steps(recipe)
    assert len(out) == 1
    assert "Watch the source video" in out[0]["instruction"]


def test_tandoor_ingredient_header():
    row = tandoor_ingredient_payload(
        {"is_section_header": True, "item": "For the sauce", "amount": 0},
        0,
    )
    assert row is not None
    assert row["is_header"] is True
    assert row["food"]["name"] == "For the sauce"
    assert row["unit"]["name"] == "each"


def test_tandoor_ingredient_blank_unit_becomes_each():
    row = tandoor_ingredient_payload({"item": "eggs", "amount": 2, "unit": "", "note": ""}, 0)
    assert row is not None
    assert row["unit"]["name"] == "each"


def test_normalize_gpt_steps_skips_empty_dict_and_none():
    recipe = {"steps": [None, {}, {"instruction": "Go", "ingredients": []}]}
    out = normalize_gpt_steps(recipe)
    assert len(out) == 1
    assert out[0]["instruction"] == "Go"


def test_tandoor_ingredient_skips_empty_item():
    assert tandoor_ingredient_payload({"item": "  ", "amount": 1, "unit": "cup"}, 0) is None


def test_tandoor_ingredient_no_amount_salt():
    row = tandoor_ingredient_payload(
        {"item": "salt", "amount": 0, "unit": "", "no_amount": True, "note": "to taste"},
        0,
    )
    assert row["no_amount"] is True
    assert row["amount"] == 0.0
    assert row["unit"]["name"] == "each"


def test_build_tandoor_steps_fills_empty_instruction():
    recipe = {
        "steps": [
            {
                "name": "",
                "instruction": "",
                "ingredients": [{"item": "flour", "amount": 1, "unit": "cup"}],
            }
        ]
    }
    steps = build_tandoor_steps(recipe)
    assert "Prepare the ingredients" in steps[0]["instruction"]


def test_build_tandoor_recipe_payload_truncates_description():
    long_d = "x" * (RECIPE_DESC_MAX + 50)
    payload = build_tandoor_recipe_payload(
        {"title": "T", "description": long_d, "steps": [{"instruction": "Go", "ingredients": []}]},
        "https://src",
        "instagram",
    )
    assert len(payload["description"]) <= RECIPE_DESC_MAX
    assert payload["description"].endswith("...")


def test_build_tandoor_recipe_payload_source_fallback():
    payload = build_tandoor_recipe_payload(
        {"title": "T", "steps": [{"instruction": "x", "ingredients": []}]},
        "https://fallback",
        "unknown",
    )
    assert payload["source_url"] == "https://fallback"


def test_detect_platform_youtube():
    assert detect_platform("https://youtube.com/shorts/abc") == "youtube"


def test_visual_estimate_marked_in_note():
    row = tandoor_ingredient_payload(
        {"amount": "1", "unit": "cup", "item": "rice", "note": "rinsed", "source": "visual_estimate"}, 0)
    assert row["note"] == "rinsed; amount estimated from video"
    row = tandoor_ingredient_payload({"amount": "1", "unit": "cup", "item": "rice", "source": "visual_estimate"}, 0)
    assert row["note"] == "amount estimated from video"
    row = tandoor_ingredient_payload(
        {"item": "salt", "no_amount": True, "source": "visual_estimate"}, 0)
    assert row["note"] == ""


def test_notes_step_appended_with_tips_equipment_uncertainties():
    steps = build_tandoor_steps({
        "steps": [{"instruction": "Cook", "ingredients": []}],
        "equipment": ["wok", " "],
        "tips": ["Use day-old rice"],
        "uncertainties": ["oil amount not stated"],
    })
    assert steps[-1]["name"] == "Notes"
    body = steps[-1]["instruction"]
    assert "**Equipment:** wok" in body and "- Use day-old rice" in body and "- oil amount not stated" in body
    assert steps[-1]["order"] == 1


def test_no_notes_step_when_empty():
    steps = build_tandoor_steps({"steps": [{"instruction": "Cook", "ingredients": []}], "tips": [], "equipment": []})
    assert [s["name"] for s in steps] == [""]
