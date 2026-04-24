"""Download social video metadata/media and extract Whisper-sized audio (yt-dlp + ffmpeg)."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING, Callable, Tuple

from yt_dlp import YoutubeDL

from app.constants import AUDIO_BITRATE_TIERS_KBPS, MAX_AUDIO_BYTES
from app.recipe_payload import detect_platform, pick_thumbnail_url

if TYPE_CHECKING:
    from app.settings import Settings


def ensure_ffmpeg(ffmpeg_location: str | None) -> Tuple[str, str]:
    """
    Returns (ffmpeg_path, ffprobe_path) or raises.
    If ffmpeg_location is set, try that directory first.
    """
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
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
    }
    if settings.ytdlp_cookie_file:
        opts["cookiefile"] = settings.ytdlp_cookie_file
    if settings.ytdlp_ffmpeg_location:
        opts["ffmpeg_location"] = settings.ytdlp_ffmpeg_location
    return opts


def download_best_media(
    url: str,
    settings: "Settings",
    ydl_factory: Callable[[dict], YoutubeDL] = YoutubeDL,
) -> Tuple[str, dict]:
    opts = {
        **yt_dlp_base_opts(settings),
        "format": "best",
        "outtmpl": "%(id)s.%(ext)s",
    }

    with ydl_factory(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    if not os.path.exists(filename):
        raise RuntimeError("yt-dlp reported success but downloaded file not found.")

    return filename, info


def extract_audio_with_ffmpeg(
    input_path: str,
    target_kbps: int,
    ffmpeg_location: str | None,
    run: Callable = subprocess.run,
) -> str:
    ffmpeg, _ = ensure_ffmpeg(ffmpeg_location)

    base, _ext = os.path.splitext(input_path)
    out_path = f"{base}.audio_{target_kbps}k.mp3"

    cmd = [
        ffmpeg,
        "-y",
        "-i",
        input_path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        f"{target_kbps}k",
        out_path,
    ]

    proc = run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(
            f"ffmpeg audio extract failed (kbps={target_kbps}). "
            f"stderr: {proc.stderr[-500:]}"
        )

    return out_path


def get_metadata_only(
    url: str,
    settings: "Settings",
    ydl_factory: Callable[[dict], YoutubeDL] = YoutubeDL,
) -> dict:
    with ydl_factory(yt_dlp_base_opts(settings)) as ydl:
        info_meta = ydl.extract_info(url, download=False)

    description = info_meta.get("description") or ""
    title = info_meta.get("title") or ""
    thumbnail_url = pick_thumbnail_url(info_meta)

    return {
        "caption": description,
        "title": title,
        "thumbnail_url": thumbnail_url,
        "info_meta": info_meta,
    }


def fetch_social_metadata_and_audio(
    url: str,
    settings: "Settings",
    ydl_factory: Callable[[dict], YoutubeDL] = YoutubeDL,
    run: Callable = subprocess.run,
) -> dict:
    platform = detect_platform(url)

    meta = get_metadata_only(url, settings, ydl_factory=ydl_factory)
    caption = meta["caption"]
    title = meta["title"]
    thumbnail_url = meta["thumbnail_url"]

    print(f"=== PLATFORM: {platform} ===")
    print("=== DOWNLOADING BEST MEDIA (for audio extraction) ===")
    media_path, _info = download_best_media(url, settings, ydl_factory=ydl_factory)
    media_size = os.path.getsize(media_path)
    print(f"Downloaded media: {media_path} ({media_size} bytes)")

    print("=== EXTRACTING AUDIO FOR WHISPER ===")
    last_audio_path = None
    ffmpeg_loc = settings.ytdlp_ffmpeg_location
    for kbps in AUDIO_BITRATE_TIERS_KBPS:
        audio_path = extract_audio_with_ffmpeg(media_path, kbps, ffmpeg_loc, run=run)
        last_audio_path = audio_path
        size = os.path.getsize(audio_path)
        print(f"Audio @ {kbps}kbps: {audio_path} ({size} bytes)")
        if size <= MAX_AUDIO_BYTES:
            try:
                os.remove(media_path)
            except OSError:
                pass
            return {
                "caption": caption,
                "title": title,
                "audio_path": audio_path,
                "thumbnail_url": thumbnail_url,
                "page_url": url,
                "platform": platform,
            }

    raise RuntimeError(
        f"Failed to produce an audio file under {MAX_AUDIO_BYTES} bytes. "
        f"Last attempt: {last_audio_path}"
    )
