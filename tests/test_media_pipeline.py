from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import media_pipeline as mp
from app.media_pipeline import Frame
from app.settings import Settings

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _stub_ffmpeg_bin_dir(parent: Path) -> Path:
    """GitHub Actions runners often have no ffmpeg in PATH; tests use tiny shims."""
    d = parent / "stub-ffmpeg-bin"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        p = d / name
        p.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(p, 0o755)
    return d


def _settings(**kw) -> Settings:
    base = dict(anthropic_api_key="x", tandoor_url="http://t", tandoor_api_token="x", ingest_api_key="x")
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def stub_settings(tmp_path) -> Settings:
    return _settings(ytdlp_ffmpeg_location=str(_stub_ffmpeg_bin_dir(tmp_path)))


# --- ffmpeg discovery / yt-dlp opts --------------------------------------

def test_yt_dlp_base_opts_with_cookie():
    o = mp.yt_dlp_base_opts(_settings(ytdlp_cookie_file="/cookies.txt", ytdlp_ffmpeg_location="/ff"))
    assert o["cookiefile"] == "/cookies.txt"
    assert o["ffmpeg_location"] == "/ff"
    assert o["noplaylist"] is True


def test_ensure_ffmpeg_custom_dir(tmp_path):
    d = _stub_ffmpeg_bin_dir(tmp_path)
    ff, fp = mp.ensure_ffmpeg(str(d))
    assert ff.endswith("ffmpeg") and fp.endswith("ffprobe")


def test_ensure_ffmpeg_from_path(monkeypatch, tmp_path):
    monkeypatch.setattr("app.media_pipeline.shutil.which", lambda name: str(tmp_path / name))
    ff, _ = mp.ensure_ffmpeg("/does/not/exist")
    assert Path(ff).name == "ffmpeg"


def test_ensure_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr("app.media_pipeline.shutil.which", lambda _n: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        mp.ensure_ffmpeg(None)


# --- download -------------------------------------------------------------

class FakeYDL:
    def __init__(self, info, write_ext="mp4", actual_ext=None):
        self.info, self.write_ext, self.actual_ext = info, write_ext, actual_ext
        self.opts = None
        self.calls = 0

    def __call__(self, opts):
        self.opts = opts
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download):
        self.calls += 1
        assert download is True
        tmpl = self.opts["outtmpl"]
        path = tmpl.replace("%(id)s", self.info["id"]).replace("%(ext)s", self.actual_ext or self.write_ext)
        Path(path).write_bytes(b"video")
        return self.info

    def prepare_filename(self, info):
        return self.opts["outtmpl"].replace("%(id)s", info["id"]).replace("%(ext)s", self.write_ext)


INFO = {"id": "123", "description": "cap #pasta", "title": "Pasta", "uploader": "chef",
        "duration": 42.5, "thumbnail": "https://img/t.jpg", "tags": ["pasta", 5]}


def test_download_media_single_call(stub_settings, tmp_path):
    ydl = FakeYDL(INFO)
    dl = mp.download_media("https://tiktok.com/x", stub_settings, str(tmp_path), ydl_factory=ydl)
    assert ydl.calls == 1
    assert ydl.opts["format"].startswith("b[height<=720]")
    assert dl.media_path.endswith("123.mp4")
    assert (dl.caption, dl.title, dl.uploader, dl.duration) == ("cap #pasta", "Pasta", "chef", 42.5)
    assert dl.thumbnail_url == "https://img/t.jpg"
    assert dl.tags == ["pasta"]


def test_download_media_finds_merged_extension(stub_settings, tmp_path):
    ydl = FakeYDL(INFO, write_ext="webm", actual_ext="mp4")
    dl = mp.download_media("u", stub_settings, str(tmp_path), ydl_factory=ydl)
    assert dl.media_path.endswith("123.mp4")


def test_download_media_missing_file_raises(stub_settings, tmp_path):
    class NoWrite(FakeYDL):
        def extract_info(self, url, download):
            return self.info

    with pytest.raises(RuntimeError, match="not found"):
        mp.download_media("u", stub_settings, str(tmp_path), ydl_factory=NoWrite(INFO))


# --- comments -------------------------------------------------------------

def test_select_comments_creator_first_then_top_liked():
    comments = [
        {"author": "a", "text": "meh", "like_count": 1},
        {"author": "chef", "text": "2 tbsp gochujang!", "like_count": 0, "author_is_uploader": True},
        {"author": "b", "text": "how much rice?", "like_count": 900},
        {"author": "c", "text": "  ", "like_count": 5000},
        "junk",
    ]
    out = mp.select_comments(comments, max_top=1)
    assert [c["text"] for c in out] == ["2 tbsp gochujang!", "how much rice?"]
    assert out[0]["is_creator"] is True


class FakeProc:
    def __init__(self, out="", rc=0, timeout=False):
        self.out, self.returncode, self.timeout, self.killed = out, rc, timeout, False

    def communicate(self, timeout=None):
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("yt-dlp", timeout)
        return self.out, ""

    def kill(self):
        self.killed = True


def test_fetch_comments_parses_json(stub_settings):
    payload = json.dumps({"comments": [{"text": "hi", "like_count": 3}]})
    seen = {}

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc(out=payload)

    s = _settings(ytdlp_cookie_file="/c.txt")
    out = mp.fetch_comments("https://t/x", s, popen=popen)
    assert out == [{"author": "", "text": "hi", "likes": 3, "is_creator": False}]
    assert "--cookies" in seen["cmd"] and seen["cmd"][-1] == "https://t/x"


@pytest.mark.parametrize("proc", [FakeProc(timeout=True), FakeProc(rc=1, out="x"), FakeProc(out="not json")])
def test_fetch_comments_failures_return_empty(stub_settings, proc):
    assert mp.fetch_comments("u", stub_settings, popen=lambda *a, **k: proc) == []


# --- audio ----------------------------------------------------------------

def test_extract_audio_wav_command(tmp_path, stub_settings):
    media = tmp_path / "v.mp4"
    media.write_bytes(b"x")
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"RIFF")
        return SimpleNamespace(returncode=0, stderr="")

    out = mp.extract_audio(str(media), stub_settings.ytdlp_ffmpeg_location, run=run)
    assert out.endswith(".audio.wav")
    assert "pcm_s16le" in seen["cmd"] and "16000" in seen["cmd"]


def test_extract_audio_mp3(tmp_path, stub_settings):
    media = tmp_path / "v.mp4"

    def run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"ID3")
        return SimpleNamespace(returncode=0, stderr="")

    assert mp.extract_audio(str(media), stub_settings.ytdlp_ffmpeg_location, fmt="mp3", run=run).endswith(".mp3")


def test_extract_audio_no_stream_returns_none(tmp_path, stub_settings):
    run = lambda cmd, **kw: SimpleNamespace(returncode=1, stderr="Output file does not contain any stream")
    assert mp.extract_audio(str(tmp_path / "v.mp4"), stub_settings.ytdlp_ffmpeg_location, run=run) is None


def test_extract_audio_failure_raises(tmp_path, stub_settings):
    run = lambda cmd, **kw: SimpleNamespace(returncode=1, stderr="boom")
    with pytest.raises(RuntimeError, match="boom"):
        mp.extract_audio(str(tmp_path / "v.mp4"), stub_settings.ytdlp_ffmpeg_location, run=run)


# --- frames ---------------------------------------------------------------

@pytest.mark.parametrize("dur,expected", [(0, 1.0), (30, 1.0), (60, 1.0), (120, 0.5)])
def test_frame_sample_fps(dur, expected):
    assert mp.frame_sample_fps(dur) == pytest.approx(expected)


def test_dedupe_frames_drops_near_duplicates():
    frames = [Frame(f"p{i}", float(i)) for i in range(5)]
    hashes = {"p0": 0b0, "p1": 0b1, "p2": 0b11111111, "p3": 0b11111110, "p4": "err"}

    def hasher(p):
        h = hashes[p]
        if h == "err":
            raise ValueError
        return h

    kept = mp.dedupe_frames(frames, threshold=2, hasher=hasher)
    assert [f.path for f in kept] == ["p0", "p2", "p4"]


def test_dhash_distinguishes_images(tmp_path):
    from PIL import Image

    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    img = Image.new("L", (90, 80))
    img.putdata([x % 90 * 2 for x in range(90 * 80)])
    img.save(a)
    img.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(b)
    assert mp.dhash(str(a)) != mp.dhash(str(b))


def test_extract_frames_mocked(tmp_path, stub_settings):
    def run(cmd, **kw):
        pattern = cmd[-1]
        for i in range(1, 4):
            Path(pattern.replace("%04d", f"{i:04d}")).write_bytes(b"jpg")
        return SimpleNamespace(returncode=0, stderr="")

    frames = mp.extract_frames("v.mp4", 3.0, str(tmp_path), stub_settings.ytdlp_ffmpeg_location,
                               run=run, hasher=lambda p: [0, 2**20 - 1, 2**40 - 1][int(p[-5]) - 1])
    assert [f.t for f in frames] == [0.5, 1.5, 2.5]


def test_extract_frames_failure_returns_empty(tmp_path, stub_settings):
    run = lambda cmd, **kw: SimpleNamespace(returncode=1, stderr="no video")
    assert mp.extract_frames("v.mp4", 3.0, str(tmp_path), stub_settings.ytdlp_ffmpeg_location, run=run) == []


@pytest.mark.skipif(not HAVE_FFMPEG, reason="real ffmpeg not installed")
def test_real_ffmpeg_audio_and_frames(tmp_path):
    video = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=24:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-shortest", "-pix_fmt", "yuv420p",
                    str(video)], check=True)
    audio = mp.extract_audio(str(video), None)
    assert audio and os.path.getsize(audio) > 1000
    frames = mp.extract_frames(str(video), 6.0, str(tmp_path), None)
    assert 1 <= len(frames) <= 6
    from PIL import Image

    with Image.open(frames[0].path) as im:
        assert im.size[0] == 540


@pytest.mark.skipif(not HAVE_FFMPEG, reason="real ffmpeg not installed")
def test_real_ffmpeg_video_without_audio(tmp_path):
    video = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    assert mp.extract_audio(str(video), None) is None
