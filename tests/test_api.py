from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings


@pytest.fixture
def api_settings() -> Settings:
    return Settings(
        openai_api_key="sk",
        tandoor_url="http://t",
        tandoor_api_token="t",
        ingest_api_key="correct-key",
    )


def test_ingest_social_401_wrong_key(api_settings):
    app = create_app(api_settings)
    client = TestClient(app)
    r = client.post(
        "/ingest/social",
        json={"url": "https://www.tiktok.com/@chef/video/123"},
        headers={"X-Api-Key": "wrong"},
    )
    assert r.status_code == 401


def test_ingest_social_cleanup_ignores_oserror_on_remove(api_settings, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00" * 200)
    app = create_app(api_settings)
    with patch("app.main.fetch_social_metadata_and_audio") as fsm, patch(
        "app.main.transcribe_audio", return_value=""
    ), patch(
        "app.main.extract_recipe_with_gpt",
        return_value={"title": "T", "steps": [{"instruction": "x", "ingredients": []}]},
    ), patch(
        "app.main.send_recipe_to_tandoor",
        return_value={"id": 1, "name": "T", "source_url": ""},
    ), patch("app.main.os.remove", side_effect=OSError("already gone")):
        fsm.return_value = {
            "caption": "",
            "title": "",
            "audio_path": str(audio),
            "thumbnail_url": None,
            "page_url": "https://www.tiktok.com/x",
            "platform": "tiktok",
        }
        client = TestClient(app)
        r = client.post(
            "/ingest/social",
            json={"url": "https://www.tiktok.com/@chef/video/1"},
            headers={"X-Api-Key": "correct-key"},
        )
    assert r.status_code == 200


def test_ingest_social_200(api_settings, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"\x00" * 200)

    app = create_app(api_settings)
    with patch("app.main.fetch_social_metadata_and_audio") as fsm, patch(
        "app.main.transcribe_audio", return_value="transcript here"
    ), patch(
        "app.main.extract_recipe_with_gpt",
        return_value={"title": "Pasta", "steps": [{"instruction": "Boil", "ingredients": []}]},
    ), patch(
        "app.main.send_recipe_to_tandoor",
        return_value={"id": 42, "name": "Pasta", "source_url": "https://tiktok.com/x"},
    ):
        fsm.return_value = {
            "caption": "yum",
            "title": "TT",
            "audio_path": str(audio),
            "thumbnail_url": None,
            "page_url": "https://www.tiktok.com/x",
            "platform": "tiktok",
        }
        client = TestClient(app)
        r = client.post(
            "/ingest/social",
            json={"url": "https://www.tiktok.com/@chef/video/123"},
            headers={"X-Api-Key": "correct-key"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["tandoor_id"] == 42
    assert body["tandoor_name"] == "Pasta"
    assert not audio.exists()


def test_ingest_tiktok_alias(api_settings, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00" * 50)
    app = create_app(api_settings)
    with patch("app.main.fetch_social_metadata_and_audio") as fsm, patch(
        "app.main.transcribe_audio", return_value=""
    ), patch(
        "app.main.extract_recipe_with_gpt",
        return_value={"title": "X", "steps": [{"instruction": "y", "ingredients": []}]},
    ), patch(
        "app.main.send_recipe_to_tandoor",
        return_value={"id": 1, "name": "X", "source_url": None},
    ):
        fsm.return_value = {
            "caption": "",
            "title": "",
            "audio_path": str(audio),
            "thumbnail_url": None,
            "page_url": "https://instagram.com/reel/1",
            "platform": "instagram",
        }
        client = TestClient(app)
        r = client.post(
            "/ingest/tiktok",
            json={"url": "https://www.instagram.com/reel/abc/"},
            headers={"X-Api-Key": "correct-key"},
        )
    assert r.status_code == 200
    assert r.json()["platform"] == "instagram"


def test_ingest_social_removes_audio_after_exception(api_settings, tmp_path):
    audio = tmp_path / "fail.mp3"
    audio.write_bytes(b"\x00" * 80)
    app = create_app(api_settings)
    with patch("app.main.fetch_social_metadata_and_audio") as fsm, patch(
        "app.main.transcribe_audio", side_effect=RuntimeError("boom")
    ):
        fsm.return_value = {
            "caption": "",
            "title": "",
            "audio_path": str(audio),
            "thumbnail_url": None,
            "page_url": "https://www.tiktok.com/x",
            "platform": "tiktok",
        }
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            "/ingest/social",
            json={"url": "https://www.tiktok.com/@u/video/9"},
            headers={"X-Api-Key": "correct-key"},
        )
    assert r.status_code == 500
    assert not audio.exists()
