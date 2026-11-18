import pytest

from app.settings import Settings, normalize_tandoor_api_token

GOOD_KEY = "k" * 32


def _base_env(monkeypatch, **extra):
    for k in ("OPENAI_API_KEY", "TRANSCRIBER", "CLAUDE_EXTRACT_EFFORT", "FRAME_OCR", "FETCH_COMMENTS",
              "WHISPER_THREADS", "NOTIFY_URL", "YTDLP_COOKIE_FILE", "YTDLP_FFMPEG_LOCATION",
              "TANDOOR_OAUTH_TOKEN_VIA_QUERY", "CLAUDE_EXTRACT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("TANDOOR_URL", "https://example.com/")
    monkeypatch.setenv("TANDOOR_API_TOKEN", "b")
    monkeypatch.setenv("INGEST_API_KEY", GOOD_KEY)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_normalize_tandoor_api_token_strips_bearer_prefix():
    assert normalize_tandoor_api_token("Bearer abc123") == "abc123"
    assert normalize_tandoor_api_token("  bearer  xyz  ") == "xyz"


def test_normalize_tandoor_api_token_strips_bom_quotes_and_newlines():
    assert normalize_tandoor_api_token("﻿tda_abc") == "tda_abc"
    assert normalize_tandoor_api_token('"tda_xyz"') == "tda_xyz"
    assert normalize_tandoor_api_token("tda_one\r\n") == "tda_one"
    assert normalize_tandoor_api_token("'q'") == "q"


def test_from_env_defaults(monkeypatch):
    _base_env(monkeypatch, TANDOOR_API_TOKEN="Bearer eyJtoken")
    s = Settings.from_env()
    assert s.tandoor_url == "https://example.com"
    assert s.tandoor_api_token == "eyJtoken"
    assert s.extract_model == "claude-opus-5-5"
    assert s.vision_model == "claude-haiku-5-5"
    assert s.extract_effort == "high"
    assert s.transcriber == "local"
    assert s.frame_ocr is True and s.fetch_comments is True
    assert s.openai_api_key is None and s.notify_url is None


def test_from_env_overrides(monkeypatch):
    _base_env(monkeypatch, TRANSCRIBER="openai", OPENAI_API_KEY="sk-o", CLAUDE_EXTRACT_EFFORT="XHIGH",
              FRAME_OCR="0", FETCH_COMMENTS="false", WHISPER_THREADS="nope", NOTIFY_URL="https://ntfy.sh/x",
              YTDLP_COOKIE_FILE="/c.txt", YTDLP_FFMPEG_LOCATION="/ff", TANDOOR_OAUTH_TOKEN_VIA_QUERY="yes")
    s = Settings.from_env()
    assert s.transcriber == "openai" and s.openai_api_key == "sk-o"
    assert s.extract_effort == "xhigh"
    assert s.frame_ocr is False and s.fetch_comments is False
    assert s.whisper_threads == 4
    assert s.notify_url == "https://ntfy.sh/x"
    assert s.ytdlp_cookie_file == "/c.txt" and s.ytdlp_ffmpeg_location == "/ff"
    assert s.tandoor_oauth_token_via_query is True


def test_from_env_missing_lists_names(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        Settings.from_env()


@pytest.mark.parametrize("key", ["some-long-random-string", "short"])
def test_rejects_placeholder_or_short_ingest_key(monkeypatch, key):
    _base_env(monkeypatch, INGEST_API_KEY=key)
    with pytest.raises(RuntimeError, match="INGEST_API_KEY"):
        Settings.from_env()


@pytest.mark.parametrize("env,match", [
    ({"TRANSCRIBER": "bogus"}, "TRANSCRIBER"),
    ({"TRANSCRIBER": "openai"}, "OPENAI_API_KEY"),
    ({"CLAUDE_EXTRACT_EFFORT": "ultra"}, "EFFORT"),
])
def test_invalid_combos(monkeypatch, env, match):
    _base_env(monkeypatch, **env)
    with pytest.raises(RuntimeError, match=match):
        Settings.from_env()
