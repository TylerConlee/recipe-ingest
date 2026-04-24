from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def normalize_tandoor_api_token(raw: str) -> str:
    """Strip whitespace and accidental duplicate ``Bearer `` prefix from env/copied values."""
    t = (raw or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    tandoor_url: str
    tandoor_api_token: str
    ingest_api_key: str
    ytdlp_cookie_file: Optional[str] = None
    ytdlp_ffmpeg_location: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Settings":
        openai_api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        tandoor_url = (os.environ.get("TANDOOR_URL") or "").strip().rstrip("/")
        tandoor_api_token = normalize_tandoor_api_token(os.environ.get("TANDOOR_API_TOKEN") or "")
        ingest_api_key = (os.environ.get("INGEST_API_KEY") or "").strip()
        cookie = os.environ.get("YTDLP_COOKIE_FILE")
        ffmpeg_loc = os.environ.get("YTDLP_FFMPEG_LOCATION")
        if not all([openai_api_key, tandoor_url, tandoor_api_token, ingest_api_key]):
            raise RuntimeError("Missing required env vars")
        return cls(
            openai_api_key=openai_api_key,
            tandoor_url=tandoor_url,
            tandoor_api_token=tandoor_api_token,
            ingest_api_key=ingest_api_key,
            ytdlp_cookie_file=(cookie or None),
            ytdlp_ffmpeg_location=(ffmpeg_loc or None),
        )
