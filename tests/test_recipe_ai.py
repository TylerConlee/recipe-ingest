from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import recipe_ai as ra
from app.media_pipeline import Frame
from app.settings import Settings

S = Settings(anthropic_api_key="k", tandoor_url="http://t", tandoor_api_token="t", ingest_api_key="i")


def _frames(tmp_path, n):
    out = []
    for i in range(n):
        p = tmp_path / f"f{i}.jpg"
        p.write_bytes(b"\xff\xd8jpg")
        out.append(Frame(str(p), float(i)))
    return out


def _note(i, text="", ings=(), info=False):
    return ra.FrameNote(frame_index=i, on_screen_text=text, visible_ingredients=list(ings),
                        action="stir", has_recipe_info=info)


def _recipe(**kw):
    base = dict(is_recipe=True, title="Soup", description="d", servings=2, servings_text="", prep_time_minutes=0,
                cook_time_minutes=10, passive_time_minutes=0, cuisine="", keywords=[], equipment=[],
                steps=[], tips=[], uncertainties=[])
    base.update(kw)
    return ra.RecipeExtraction(**base)


def test_schema_is_structured_output_friendly():
    schema = ra.RecipeExtraction.model_json_schema()
    # every top-level field required → no optional params to blow the structured-output limits
    assert set(schema["required"]) == set(schema["properties"])
    ing = schema["$defs"]["Ingredient"]
    assert set(ing["required"]) == set(ing["properties"])


def test_describe_frames_batches_and_filters(tmp_path):
    frames = _frames(tmp_path, 10)
    client = MagicMock()

    def parse(**kw):
        imgs = [c for c in kw["messages"][0]["content"] if c["type"] == "image"]
        first = int(kw["messages"][0]["content"][0]["text"].split()[1])
        notes = [_note(first + j, text="1 cup") for j in range(len(imgs))] + [_note(999)]
        return SimpleNamespace(parsed_output=ra.FrameNotes(frames=notes), usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    client.messages.parse.side_effect = parse
    notes = ra.describe_frames(client, S, frames)
    assert [n.frame_index for n in notes] == list(range(10))
    assert client.messages.parse.call_count == 2  # batches of 8
    assert client.messages.parse.call_args.kwargs["model"] == "claude-haiku-5-5"


def test_describe_frames_survives_batch_error(tmp_path):
    client = MagicMock()
    client.messages.parse.side_effect = RuntimeError("overloaded")
    assert ra.describe_frames(client, S, _frames(tmp_path, 3)) == []
    assert ra.describe_frames(client, S, []) == []


def test_select_key_frames_prefers_informative():
    frames = [Frame(f"p{i}", float(i)) for i in range(40)]
    notes = [_note(i) for i in range(40)]
    notes[30] = _note(30, text="2 tbsp soy sauce", info=True)
    notes[5] = _note(5, ings=["egg"], info=True)
    picked = ra.select_key_frames(frames, notes, limit=6)
    assert len(picked) == 6 and picked == sorted(picked)
    assert 30 in picked and 5 in picked


def test_select_key_frames_short_video_keeps_all():
    frames = [Frame("p", 0.0)] * 3
    assert ra.select_key_frames(frames, [], limit=24) == [0, 1, 2]


def test_select_key_frames_fills_exactly_to_limit():
    frames = [Frame(f"p{i}", float(i)) for i in range(25)]
    notes = [_note(i, text="x" * i, info=True) for i in range(25)]
    assert len(ra.select_key_frames(frames, notes, limit=24)) == 24


def test_format_frame_timeline():
    frames = [Frame("a", 1.0), Frame("b", 2.5)]
    out = ra.format_frame_timeline(frames, [_note(0, text="350°F"), _note(1, ings=["butter"]), _note(7)])
    assert '[   1.0s] stir | text: "350°F"' in out
    assert "visible: butter" in out
    assert out.count("\n") == 1


def test_build_extraction_text_sections():
    text = ra.build_extraction_text(
        url="https://t/x", platform="tiktok", title_hint="", uploader="chef", caption="",
        tags=["pasta"], comments=[{"text": "use 2 eggs", "likes": 3, "is_creator": True},
                                  {"text": "yum", "likes": 9, "is_creator": False}],
        transcript="", frame_timeline="", key_frame_times=[1.0, 2.5],
    )
    assert "<creator_comments>\n- (3 likes) use 2 eggs" in text
    assert "<viewer_comments>\n- (9 likes) yum" in text
    assert "music-only" in text
    assert "1.0s, 2.5s" in text
    assert "hashtags: pasta" in text


def test_extract_recipe_request_shape(tmp_path):
    frames = _frames(tmp_path, 3)
    client = MagicMock()
    client.messages.parse.return_value = SimpleNamespace(parsed_output=_recipe(), usage=None)
    out = ra.extract_recipe(client, S, frames=frames, key_frames=[0, 2], text="hello")
    assert out.title == "Soup"
    kw = client.messages.parse.call_args.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "high"}
    assert kw["output_format"] is ra.RecipeExtraction
    content = kw["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image", "image", "text"]  # images before text


def test_parsed_fallbacks():
    good = _recipe().model_dump_json()
    res = SimpleNamespace(parsed_output=None, stop_reason="end_turn",
                          content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=good)])
    assert ra._parsed(res, ra.RecipeExtraction).title == "Soup"
    with pytest.raises(RuntimeError, match="declined"):
        ra._parsed(SimpleNamespace(parsed_output=None, stop_reason="refusal", content=[]), ra.RecipeExtraction)
    with pytest.raises(RuntimeError, match="output tokens"):
        ra._parsed(SimpleNamespace(parsed_output=None, stop_reason="max_tokens", content=[]), ra.RecipeExtraction)


def test_make_client_uses_key():
    seen = {}
    ra.make_client(S, factory=lambda **kw: seen.update(kw))
    assert seen["api_key"] == "k" and seen["timeout"] >= 120
