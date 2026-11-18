"""Download social video + metadata once, then derive audio, frames and comments (yt-dlp + ffmpeg)."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

from yt_dlp import YoutubeDL

from app.constants import (
    COMMENTS_TIMEOUT_S,
    FRAME_DEDUPE_HAMMING,
    FRAME_WIDTH,
    MAX_SAMPLED_FRAMES,
    MAX_TOP_COMMENTS,
    YTDLP_FORMAT,
)
from app.recipe_payload import pick_thumbnail_url

if TYPE_CHECKING:
    from app.settings import Settings


@dataclass
class Frame:
    path: str
    t: float  # seconds from start


@dataclass
class DownloadedMedia:
    media_path: str
    caption: str
    title: str
    uploader: str
    duration: float
    thumbnail_url: Optional[str]
    tags: List[str] = field(default_factory=list)
    video_id: str = ""


def ensure_ffmpeg(ffmpeg_location: str | None) -> Tuple[str, str]:
    """Returns (ffmpeg_path, ffprobe_path) or raises. Tries ``ffmpeg_location`` first."""
    if ffmpeg_location:
        ffmpeg = os.path.join(ffmpeg_location, "ffmpeg")
        ffprobe = os.path.join(ffmpeg_location, "ffprobe")
        if os.path.exists(ffmpeg) and os.path.exists(ffprobe):
            return ffmpeg, ffprobe

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "ffmpeg/ffprobe not found in PATH. Install ffmpeg in the ingest container "
            "or set YTDLP_FFMPEG_LOCATION to the folder containing ffmpeg+ffprobe."
        )
    return ffmpeg, ffprobe


def yt_dlp_base_opts(settings: "Settings") -> dict:
    opts: dict = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if settings.ytdlp_cookie_file:
        opts["cookiefile"] = settings.ytdlp_cookie_file
    if settings.ytdlp_ffmpeg_location:
        opts["ffmpeg_location"] = settings.ytdlp_ffmpeg_location
    return opts


def download_media(
    url: str,
    settings: "Settings",
    workdir: str,
    ydl_factory: Callable[[dict], YoutubeDL] = YoutubeDL,
) -> DownloadedMedia:
    """One yt-dlp call: metadata + a <=720p file into ``workdir`` (previously two network round-trips)."""
    opts = {
        **yt_dlp_base_opts(settings),
        "format": YTDLP_FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
    }
    with ydl_factory(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    if not os.path.exists(filename):
        # Merged outputs can change extension (e.g. .webm -> .mp4).
        base = os.path.splitext(filename)[0]
        candidates = [p for p in glob.glob(base + ".*") if not p.endswith(".part")]
        if not candidates:
            raise RuntimeError("yt-dlp reported success but downloaded file not found.")
        filename = candidates[0]

    return DownloadedMedia(
        media_path=filename,
        caption=info.get("description") or "",
        title=info.get("title") or "",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=float(info.get("duration") or 0.0),
        thumbnail_url=pick_thumbnail_url(info),
        tags=[t for t in (info.get("tags") or []) if isinstance(t, str)],
        video_id=str(info.get("id") or ""),
    )


def select_comments(comments: list, max_top: int = MAX_TOP_COMMENTS) -> List[dict]:
    """Every creator comment/reply (where amounts often hide) plus the most-liked others."""
    creator, others = [], []
    for c in comments or []:
        if not isinstance(c, dict) or not (c.get("text") or "").strip():
            continue
        row = {
            "author": c.get("author") or "",
            "text": c["text"].strip()[:1000],
            "likes": int(c.get("like_count") or 0),
            "is_creator": bool(c.get("author_is_uploader")),
        }
        (creator if row["is_creator"] else others).append(row)
    others.sort(key=lambda r: r["likes"], reverse=True)
    return creator + others[:max_top]


def fetch_comments(
    url: str,
    settings: "Settings",
    timeout_s: int = COMMENTS_TIMEOUT_S,
    popen: Callable = subprocess.Popen,
) -> List[dict]:
    """Run yt-dlp in a child process so it can be killed if pagination runs long."""
    cmd = [sys.executable, "-m", "yt_dlp", "--skip-download", "--write-comments", "--dump-single-json",
           "--no-warnings", "--quiet", "--no-playlist"]
    if settings.ytdlp_cookie_file:
        cmd += ["--cookies", settings.ytdlp_cookie_file]
    cmd.append(url)
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        print(f"Comments fetch exceeded {timeout_s}s; continuing without comments.")
        return []
    if proc.returncode != 0 or not out:
        return []
    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        return []
    return select_comments(info.get("comments") or [])


def extract_audio(
    input_path: str,
    ffmpeg_location: str | None,
    fmt: str = "wav",
    run: Callable = subprocess.run,
) -> Optional[str]:
    """16 kHz mono audio. WAV for local Whisper (no lossy re-encode), small MP3 for the OpenAI API.
    Returns None when the media has no audio stream."""
    ffmpeg, _ = ensure_ffmpeg(ffmpeg_location)
    base, _ext = os.path.splitext(input_path)
    if fmt == "mp3":
        out_path = f"{base}.audio.mp3"
        codec = ["-b:a", "32k"]
    else:
        out_path = f"{base}.audio.wav"
        codec = ["-c:a", "pcm_s16le"]
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", input_path, "-vn", "-ac", "1", "-ar", "16000",
           *codec, out_path]
    proc = run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(out_path):
        stderr = proc.stderr or ""
        if "does not contain any stream" in stderr or "Output file is empty" in stderr:
            return None
        raise RuntimeError(f"ffmpeg audio extract failed. stderr: {stderr[-500:]}")
    return out_path


def frame_sample_fps(duration: float, max_frames: int = MAX_SAMPLED_FRAMES) -> float:
    """1 frame/sec for short clips; spread ``max_frames`` across longer ones."""
    if duration <= 0:
        return 1.0
    return min(1.0, max_frames / duration)


def dhash(path: str, size: int = 8) -> int:
    from PIL import Image

    with Image.open(path) as im:
        g = im.convert("L").resize((size + 1, size), Image.Resampling.BILINEAR)
        px = list(g.tobytes())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def dedupe_frames(frames: List[Frame], threshold: int = FRAME_DEDUPE_HAMMING,
                  hasher: Callable[[str], int] = dhash) -> List[Frame]:
    """Drop frames nearly identical to the last kept one (talking-head stretches)."""
    kept: List[Frame] = []
    last: Optional[int] = None
    for f in frames:
        try:
            h = hasher(f.path)
        except Exception:
            kept.append(f)
            continue
        if last is not None and bin(h ^ last).count("1") <= threshold:
            continue
        kept.append(f)
        last = h
    return kept


def extract_frames(
    input_path: str,
    duration: float,
    workdir: str,
    ffmpeg_location: str | None,
    run: Callable = subprocess.run,
    hasher: Callable[[str], int] = dhash,
) -> List[Frame]:
    """Sample JPEG frames at an adaptive rate, then drop near-duplicates."""
    ffmpeg, _ = ensure_ffmpeg(ffmpeg_location)
    fps = frame_sample_fps(duration)
    frame_dir = os.path.join(workdir, "frames")
    os.makedirs(frame_dir, exist_ok=True)
    pattern = os.path.join(frame_dir, "f_%04d.jpg")
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", input_path,
           "-vf", f"fps={fps:.4f},scale={FRAME_WIDTH}:-2", "-q:v", "4",
           "-frames:v", str(MAX_SAMPLED_FRAMES), pattern]
    proc = run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("Frame extraction failed:", (proc.stderr or "")[-300:])
        return []
    paths = sorted(glob.glob(os.path.join(frame_dir, "f_*.jpg")))
    # fps filter emits frame i at t ≈ (i + 0.5) / fps
    frames = [Frame(path=p, t=round((i + 0.5) / fps, 1)) for i, p in enumerate(paths)]
    return dedupe_frames(frames, hasher=hasher)
