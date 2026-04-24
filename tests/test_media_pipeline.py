from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from app.media_pipeline import (
    download_best_media,
    ensure_ffmpeg,
    extract_audio_with_ffmpeg,
    fetch_social_metadata_and_audio,
    get_metadata_only,
    yt_dlp_base_opts,
)
from app.settings import Settings


@pytest.fixture
def settings_no_cookie() -> Settings:
    return Settings(
        openai_api_key="x",
        tandoor_url="http://t",
        tandoor_api_token="x",
        ingest_api_key="x",
        ytdlp_cookie_file=None,
        ytdlp_ffmpeg_location=None,
    )


def test_yt_dlp_base_opts_with_cookie(monkeypatch):
    s = Settings(
        openai_api_key="x",
        tandoor_url="http://t",
        tandoor_api_token="x",
        ingest_api_key="x",
        ytdlp_cookie_file="/cookies.txt",
        ytdlp_ffmpeg_location="/ff",
    )
    o = yt_dlp_base_opts(s)
    assert o["cookiefile"] == "/cookies.txt"
    assert o["ffmpeg_location"] == "/ff"


def test_ensure_ffmpeg_custom_dir(tmp_path, monkeypatch):
    (tmp_path / "ffmpeg").write_text("#!/bin/sh\ntrue\n")
    (tmp_path / "ffprobe").write_text("#!/bin/sh\ntrue\n")
    os.chmod(tmp_path / "ffmpeg", 0o755)
    os.chmod(tmp_path / "ffprobe", 0o755)
    ff, fp = ensure_ffmpeg(str(tmp_path))
    assert ff.endswith("ffmpeg")
    assert fp.endswith("ffprobe")


def test_ensure_ffmpeg_from_path(monkeypatch, tmp_path):
    monkeypatch.setattr("app.media_pipeline.shutil.which", lambda name: str(tmp_path / name))
    for name in ("ffmpeg", "ffprobe"):
        p = tmp_path / name
        p.write_text("#!/bin/sh\ntrue\n")
        os.chmod(p, 0o755)
    ff, fp = ensure_ffmpeg(None)
    assert Path(ff).name == "ffmpeg"


def test_ensure_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr("app.media_pipeline.shutil.which", lambda _n: None)
    with pytest.raises(RuntimeError, match="ffmpeg/ffprobe not found"):
        ensure_ffmpeg(None)


class _FakeYDL:
    def __init__(self, opts: Dict[str, Any], media_file: Path):
        self.opts = opts
        self.media_file = media_file

    def __enter__(self) -> "_FakeYDL":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict:
        if download:
            self.media_file.write_bytes(b"video-bytes-here" * 50)
            return {"id": "vid1", "ext": "mp4"}
        return {
            "description": "caption text",
            "title": "Video title",
            "thumbnail": "https://cdn.example/thumb.jpg",
        }

    def prepare_filename(self, info: dict) -> str:
        return str(self.media_file)


def test_get_metadata_only(settings_no_cookie, tmp_path):
    media = tmp_path / "vid1.mp4"

    def factory(opts: dict) -> _FakeYDL:
        return _FakeYDL(opts, media)

    meta = get_metadata_only("https://tiktok.com/x", settings_no_cookie, ydl_factory=factory)
    assert meta["caption"] == "caption text"
    assert meta["title"] == "Video title"
    assert "thumb" in (meta["thumbnail_url"] or "")


def test_download_best_media(settings_no_cookie, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    media = tmp_path / "vid1.mp4"

    def factory(opts: dict) -> _FakeYDL:
        return _FakeYDL(opts, media)

    path, info = download_best_media("https://tiktok.com/x", settings_no_cookie, ydl_factory=factory)
    assert Path(path).exists()
    assert info["id"] == "vid1"


def test_download_best_media_missing_file_raises(settings_no_cookie, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class BadYDL:
        def __init__(self, opts: dict):
            pass

        def __enter__(self) -> "BadYDL":
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = False) -> dict:
            return {"id": "x"}

        def prepare_filename(self, info: dict) -> str:
            return str(tmp_path / "nope.mp4")

    with pytest.raises(RuntimeError, match="downloaded file not found"):
        download_best_media("https://tiktok.com/x", settings_no_cookie, ydl_factory=BadYDL)


def test_extract_audio_with_ffmpeg_mock_run(tmp_path):
    media = tmp_path / "in.mp4"
    media.write_bytes(b"x")

    def fake_run(cmd, capture_output=True, text=True):
        out_path = cmd[-1]
        Path(out_path).write_bytes(b"\x00" * 500)
        return SimpleNamespace(returncode=0, stderr="")

    out = extract_audio_with_ffmpeg(str(media), 48, str(tmp_path), run=fake_run)
    assert Path(out).exists()
    assert Path(out).suffix == ".mp3"


def test_extract_audio_ffmpeg_failure(tmp_path):
    media = tmp_path / "in.mp4"
    media.write_bytes(b"x")

    def bad_run(cmd, capture_output=True, text=True):
        return SimpleNamespace(returncode=1, stderr="ffmpeg failed")

    with pytest.raises(RuntimeError, match="ffmpeg audio extract failed"):
        extract_audio_with_ffmpeg(str(media), 48, str(tmp_path), run=bad_run)


def test_fetch_social_media_remove_oserror_is_ignored(settings_no_cookie, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    media = tmp_path / "vid1.mp4"

    def factory(opts: dict) -> _FakeYDL:
        return _FakeYDL(opts, media)

    def fake_run(cmd, capture_output=True, text=True):
        Path(cmd[-1]).write_bytes(b"\x00" * 500)
        return SimpleNamespace(returncode=0, stderr="")

    real_remove = os.remove

    def noisy_remove(p):
        if str(p) == str(media):
            raise OSError("simulated busy media file")
        return real_remove(p)

    monkeypatch.setattr("app.media_pipeline.os.remove", noisy_remove)

    result = fetch_social_metadata_and_audio(
        "https://www.tiktok.com/x",
        settings_no_cookie,
        ydl_factory=factory,
        run=fake_run,
    )
    assert Path(result["audio_path"]).exists()
    assert media.exists()


def test_fetch_social_metadata_and_audio_end_to_end(settings_no_cookie, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    media = tmp_path / "vid1.mp4"

    def factory(opts: dict) -> _FakeYDL:
        return _FakeYDL(opts, media)

    def fake_run(cmd, capture_output=True, text=True):
        Path(cmd[-1]).write_bytes(b"\x00" * 500)
        return SimpleNamespace(returncode=0, stderr="")

    result = fetch_social_metadata_and_audio(
        "https://www.tiktok.com/@u/video/1",
        settings_no_cookie,
        ydl_factory=factory,
        run=fake_run,
    )
    assert result["platform"] == "tiktok"
    assert Path(result["audio_path"]).exists()
    assert not media.exists()  # media removed after success


def test_fetch_social_raises_when_audio_never_small_enough(settings_no_cookie, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    media = tmp_path / "vid1.mp4"

    def factory(opts: dict) -> _FakeYDL:
        return _FakeYDL(opts, media)

    def huge_run(cmd, capture_output=True, text=True):
        Path(cmd[-1]).write_bytes(b"\x00" * (25 * 1024 * 1024))
        return SimpleNamespace(returncode=0, stderr="")

    with pytest.raises(RuntimeError, match="Failed to produce an audio file"):
        fetch_social_metadata_and_audio(
            "https://www.tiktok.com/x",
            settings_no_cookie,
            ydl_factory=factory,
            run=huge_run,
        )
