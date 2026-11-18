from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from app.constants import (
    DEFAULT_EXTRACT_EFFORT,
    DEFAULT_EXTRACT_MODEL,
    DEFAULT_VISION_MODEL,
    DEFAULT_WHISPER_MODEL,
    PLACEHOLDER_SECRETS,
)

TRANSCRIBERS = ("local", "openai", "none")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def normalize_tandoor_api_token(raw: str) -> str:
    """Strip whitespace, line endings, BOM, quotes, and accidental duplicate ``Bearer `` prefix."""
    t = (raw or "").strip()
    t = t.replace("﻿", "").strip()
    t = t.strip("\r\n")
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        t = t[1:-1].strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    tandoor_url: str
    tandoor_api_token: str
    ingest_api_key: str
    openai_api_key: Optional[str] = None
    ytdlp_cookie_file: Optional[str] = None
    ytdlp_ffmpeg_location: Optional[str] = None
    tandoor_oauth_token_via_query: bool = False
    extract_model: str = DEFAULT_EXTRACT_MODEL
    vision_model: str = DEFAULT_VISION_MODEL
    extract_effort: str = DEFAULT_EXTRACT_EFFORT
    frame_ocr: bool = True
    fetch_comments: bool = True
    transcriber: str = "local"
    whisper_model: str = DEFAULT_WHISPER_MODEL
    whisper_threads: int = 4
    whisper_model_dir: Optional[str] = None
    notify_url: Optional[str] = None

    def validate(self) -> "Settings":
        missing = [
            name
            for name, val in (
                ("ANTHROPIC_API_KEY", self.anthropic_api_key),
                ("TANDOOR_URL", self.tandoor_url),
                ("TANDOOR_API_TOKEN", self.tandoor_api_token),
                ("INGEST_API_KEY", self.ingest_api_key),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
        if self.ingest_api_key.lower() in PLACEHOLDER_SECRETS or len(self.ingest_api_key) < 16:
            raise RuntimeError(
                "INGEST_API_KEY looks like a placeholder or is shorter than 16 characters. "
                "Generate one with: openssl rand -hex 32"
            )
        if self.transcriber not in TRANSCRIBERS:
            raise RuntimeError(f"TRANSCRIBER must be one of {TRANSCRIBERS}, got {self.transcriber!r}")
        if self.transcriber == "openai" and not self.openai_api_key:
            raise RuntimeError("TRANSCRIBER=openai requires OPENAI_API_KEY")
        if self.extract_effort not in EFFORTS:
            raise RuntimeError(f"CLAUDE_EXTRACT_EFFORT must be one of {EFFORTS}")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            threads = int(_env("WHISPER_THREADS", "4"))
        except ValueError:
            threads = 4
        return cls(
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            tandoor_url=_env("TANDOOR_URL").rstrip("/"),
            tandoor_api_token=normalize_tandoor_api_token(os.environ.get("TANDOOR_API_TOKEN") or ""),
            ingest_api_key=_env("INGEST_API_KEY"),
            openai_api_key=_env("OPENAI_API_KEY") or None,
            ytdlp_cookie_file=_env("YTDLP_COOKIE_FILE") or None,
            ytdlp_ffmpeg_location=_env("YTDLP_FFMPEG_LOCATION") or None,
            tandoor_oauth_token_via_query=_env_truthy("TANDOOR_OAUTH_TOKEN_VIA_QUERY"),
            extract_model=_env("CLAUDE_EXTRACT_MODEL", DEFAULT_EXTRACT_MODEL),
            vision_model=_env("CLAUDE_VISION_MODEL", DEFAULT_VISION_MODEL),
            extract_effort=_env("CLAUDE_EXTRACT_EFFORT", DEFAULT_EXTRACT_EFFORT).lower(),
            frame_ocr=_env_truthy("FRAME_OCR", default=True),
            fetch_comments=_env_truthy("FETCH_COMMENTS", default=True),
            transcriber=_env("TRANSCRIBER", "local").lower(),
            whisper_model=_env("WHISPER_MODEL", DEFAULT_WHISPER_MODEL),
            whisper_threads=max(1, threads),
            whisper_model_dir=_env("WHISPER_MODEL_DIR") or None,
            notify_url=_env("NOTIFY_URL") or None,
        ).validate()
