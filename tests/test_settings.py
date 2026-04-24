import os

import pytest

from app.settings import Settings, normalize_tandoor_api_token


def test_normalize_tandoor_api_token_strips_bearer_prefix():
    assert normalize_tandoor_api_token("Bearer abc123") == "abc123"
    assert normalize_tandoor_api_token("  bearer  xyz  ") == "xyz"


def test_from_env_normalizes_tandoor_token(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "a")
    monkeypatch.setenv("TANDOOR_URL", "http://x")
    monkeypatch.setenv("TANDOOR_API_TOKEN", "Bearer eyJtoken")
    monkeypatch.setenv("INGEST_API_KEY", "c")
    s = Settings.from_env()
    assert s.tandoor_api_token == "eyJtoken"


def test_from_env_success(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "a")
    monkeypatch.setenv("TANDOOR_URL", "https://example.com/")
    monkeypatch.setenv("TANDOOR_API_TOKEN", "b")
    monkeypatch.setenv("INGEST_API_KEY", "c")
    monkeypatch.setenv("YTDLP_COOKIE_FILE", "/c.txt")
    monkeypatch.setenv("YTDLP_FFMPEG_LOCATION", "/opt/ffmpeg")
    s = Settings.from_env()
    assert s.openai_api_key == "a"
    assert s.tandoor_url == "https://example.com"
    assert s.tandoor_api_token == "b"
    assert s.ingest_api_key == "c"
    assert s.ytdlp_cookie_file == "/c.txt"
    assert s.ytdlp_ffmpeg_location == "/opt/ffmpeg"


@pytest.mark.parametrize(
    "unset",
    ["OPENAI_API_KEY", "TANDOOR_URL", "TANDOOR_API_TOKEN", "INGEST_API_KEY"],
)
def test_from_env_missing_required(monkeypatch, unset):
    for k in ("OPENAI_API_KEY", "TANDOOR_URL", "TANDOOR_API_TOKEN", "INGEST_API_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv(unset, raising=False)
    with pytest.raises(RuntimeError, match="Missing required env vars"):
        Settings.from_env()


def test_optional_cookie_none_when_empty(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "a")
    monkeypatch.setenv("TANDOOR_URL", "http://x")
    monkeypatch.setenv("TANDOOR_API_TOKEN", "b")
    monkeypatch.setenv("INGEST_API_KEY", "c")
    monkeypatch.delenv("YTDLP_COOKIE_FILE", raising=False)
    monkeypatch.delenv("YTDLP_FFMPEG_LOCATION", raising=False)
    s = Settings.from_env()
    assert s.ytdlp_cookie_file is None
    assert s.ytdlp_ffmpeg_location is None
