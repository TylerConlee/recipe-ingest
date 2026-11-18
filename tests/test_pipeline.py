from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import pipeline
from app.media_pipeline import DownloadedMedia, Frame
from app.recipe_ai import FrameNote, Ingredient, RecipeExtraction, Step
from app.settings import Settings
from app.transcribe import Segment, Transcript


def _s(**kw):
    return Settings(anthropic_api_key="k", tandoor_url="http://t", tandoor_api_token="t", ingest_api_key="i", **kw)


def _recipe(is_recipe=True):
    ing = Ingredient(amount="2", unit="tbsp", item="butter", note="", is_section_header=False,
                     no_amount=False, source="on_screen_text")
    return RecipeExtraction(is_recipe=is_recipe, title="Butter Noodles", description="", servings=2,
                            servings_text="", prep_time_minutes=5, cook_time_minutes=10, passive_time_minutes=0,
                            cuisine="", keywords=[], equipment=[], tips=[], uncertainties=["salt amount unclear"],
                            steps=[Step(name="", instruction="Melt butter", time_minutes=0, ingredients=[ing])])


@pytest.fixture
def patched(monkeypatch, tmp_path):
    calls = {}
    dl = DownloadedMedia(media_path=str(tmp_path / "v.mp4"), caption="cap", title="TT", uploader="chef",
                         duration=30.0, thumbnail_url="https://img", tags=["noodles"])
    frames = [Frame(str(tmp_path / f"f{i}.jpg"), float(i)) for i in range(3)]
    monkeypatch.setattr(pipeline.media, "download_media", lambda url, s, wd: dl)
    monkeypatch.setattr(pipeline.media, "fetch_comments", lambda url, s: [{"text": "2 tbsp", "likes": 1, "is_creator": True}])
    monkeypatch.setattr(pipeline.media, "extract_audio", lambda p, loc, fmt: calls.setdefault("fmt", fmt) and "a.wav")
    monkeypatch.setattr(pipeline.media, "extract_frames", lambda *a: frames)
    monkeypatch.setattr(pipeline.stt, "transcribe", lambda a, s: Transcript([Segment(0, 1, "melt the butter")]))
    monkeypatch.setattr(pipeline.recipe_ai, "describe_frames",
                        lambda c, s, f: [FrameNote(frame_index=0, on_screen_text="2 tbsp butter",
                                                   visible_ingredients=[], action="", has_recipe_info=True)])

    def extract(client, s, *, frames, key_frames, text):
        calls["text"] = text
        return calls.get("recipe") or _recipe()

    monkeypatch.setattr(pipeline.recipe_ai, "extract_recipe", extract)
    monkeypatch.setattr(pipeline, "download_cover_bytes", lambda url: (b"img", "cover.jpg", "image/jpeg"))

    def send(settings, recipe, image_url, page_url, platform, image):
        calls["sent"] = recipe
        calls["image"] = image
        return {"id": 7, "name": recipe["title"], "source_url": page_url}

    monkeypatch.setattr(pipeline, "send_recipe_to_tandoor", send)
    return calls


def test_run_ingest_happy_path(patched):
    res = pipeline.run_ingest("https://www.tiktok.com/@chef/video/1", _s(), client=object())
    out = res.as_response()
    assert out["tandoor_id"] == 7 and out["platform"] == "tiktok"
    assert out["uncertainties"] == ["salt amount unclear"]
    assert out["stats"]["frames_sampled"] == 3 and out["stats"]["ingredients"] == 1
    assert {"download", "transcribe", "frames", "frame_ocr", "extract", "tandoor", "total"} <= set(out["timings"])
    assert patched["fmt"] == "wav"
    assert patched["image"] == (b"img", "cover.jpg", "image/jpeg")
    t = patched["text"]
    assert "melt the butter" in t and '"2 tbsp butter"' in t and "- (1 likes) 2 tbsp" in t


def test_run_ingest_manual_title_and_toggles(patched):
    s = _s(frame_ocr=False, fetch_comments=False, transcriber="none")
    pipeline.run_ingest("https://instagram.com/reel/1", s, client=object(), manual_title="Mine")
    assert patched["sent"]["title"] == "Mine"
    assert "fmt" not in patched
    assert "<creator_comments>\n(none)" in patched["text"]


def test_run_ingest_openai_uses_mp3(patched):
    pipeline.run_ingest("https://t/x", _s(transcriber="openai", openai_api_key="o"), client=object())
    assert patched["fmt"] == "mp3"


def test_run_ingest_not_a_recipe(patched):
    patched["recipe"] = _recipe(is_recipe=False)
    with pytest.raises(pipeline.NotARecipe):
        pipeline.run_ingest("https://t/x", _s(), client=object())
    assert "sent" not in patched
