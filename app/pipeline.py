"""End-to-end ingest: download → (transcribe ‖ frames→Haiku OCR ‖ comments ‖ cover) → Opus → Tandoor."""

from __future__ import annotations

import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional

from app import media_pipeline as media
from app import recipe_ai
from app import transcribe as stt
from app.recipe_payload import detect_platform
from app.tandoor_client import download_cover_bytes, send_recipe_to_tandoor

if TYPE_CHECKING:
    from anthropic import Anthropic

    from app.settings import Settings


class NotARecipe(Exception):
    """The video was processed fine but contains no recipe."""


@dataclass
class IngestResult:
    platform: str
    tandoor_id: Any
    tandoor_name: str
    source_url: str
    uncertainties: List[str] = field(default_factory=list)
    timings: Dict[str, float] = field(default_factory=dict)
    stats: Dict[str, int] = field(default_factory=dict)

    def as_response(self) -> dict:
        return {
            "status": "ok",
            "platform": self.platform,
            "tandoor_id": self.tandoor_id,
            "tandoor_name": self.tandoor_name,
            "source_url": self.source_url,
            "uncertainties": self.uncertainties,
            "timings": self.timings,
            "stats": self.stats,
        }


class _Timer:
    def __init__(self) -> None:
        self.timings: Dict[str, float] = {}

    @contextmanager
    def __call__(self, name: str) -> Iterator[None]:
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.timings[name] = round(time.monotonic() - t0, 2)


def run_ingest(
    url: str,
    settings: "Settings",
    client: "Anthropic",
    manual_title: Optional[str] = None,
    workdir_root: Optional[str] = None,
) -> IngestResult:
    timer = _Timer()
    platform = detect_platform(url)
    start = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="ingest-", dir=workdir_root) as workdir, \
            ThreadPoolExecutor(max_workers=4) as pool:
        # Comments run in their own killable process alongside the download.
        comments_f = pool.submit(media.fetch_comments, url, settings) if settings.fetch_comments else None

        with timer("download"):
            dl = media.download_media(url, settings, workdir)
        print(f"=== {platform}: {dl.title!r} by {dl.uploader} ({dl.duration:.0f}s) ===")

        cover_f = pool.submit(download_cover_bytes, dl.thumbnail_url) if dl.thumbnail_url else None

        def audio_branch() -> stt.Transcript:
            with timer("transcribe"):
                if settings.transcriber == "none":
                    return stt.EMPTY
                fmt = "mp3" if settings.transcriber == "openai" else "wav"
                audio = media.extract_audio(dl.media_path, settings.ytdlp_ffmpeg_location, fmt=fmt)
                return stt.transcribe(audio, settings)

        def visual_branch():
            with timer("frames"):
                frames = media.extract_frames(dl.media_path, dl.duration, workdir, settings.ytdlp_ffmpeg_location)
            with timer("frame_ocr"):
                notes = recipe_ai.describe_frames(client, settings, frames) if settings.frame_ocr else []
            return frames, notes

        audio_f = pool.submit(audio_branch)
        visual_f = pool.submit(visual_branch)

        transcript = audio_f.result()
        frames, notes = visual_f.result()
        with timer("comments_wait"):
            comments = comments_f.result() if comments_f else []

        key = recipe_ai.select_key_frames(frames, notes)
        text = recipe_ai.build_extraction_text(
            url=url,
            platform=platform,
            title_hint=manual_title or dl.title,
            uploader=dl.uploader,
            caption=dl.caption,
            tags=dl.tags,
            comments=comments,
            transcript=transcript.timestamped(),
            frame_timeline=recipe_ai.format_frame_timeline(frames, notes),
            key_frame_times=[frames[i].t for i in key],
        )
        with timer("extract"):
            recipe = recipe_ai.extract_recipe(client, settings, frames=frames, key_frames=key, text=text)

        if not recipe.is_recipe:
            raise NotARecipe("No recipe found in this video.")

        recipe_dict = recipe.model_dump()
        if manual_title:
            recipe_dict["title"] = manual_title

        cover = cover_f.result() if cover_f else None
        with timer("tandoor"):
            created = send_recipe_to_tandoor(
                settings,
                recipe=recipe_dict,
                image_url=dl.thumbnail_url,
                page_url=url,
                platform=platform,
                image=cover,
            )

    timer.timings["total"] = round(time.monotonic() - start, 2)
    stats = {
        "duration_s": int(dl.duration),
        "frames_sampled": len(frames),
        "frames_to_extractor": len(key),
        "transcript_segments": len(transcript.segments),
        "comments": len(comments),
        "ingredients": sum(len(s.ingredients) for s in recipe.steps),
        "steps": len(recipe.steps),
    }
    print("=== TIMINGS ===", timer.timings, stats)
    return IngestResult(
        platform=platform,
        tandoor_id=created.get("id"),
        tandoor_name=created.get("name") or recipe.title,
        source_url=created.get("source_url") or url,
        uncertainties=recipe.uncertainties,
        timings=timer.timings,
        stats=stats,
    )
