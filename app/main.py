# app/main.py
import os
import re
import shutil
import subprocess
from typing import Optional, Tuple

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, AnyHttpUrl
import httpx
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from openai import OpenAI

app = FastAPI()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MEALIE_URL = os.getenv("MEALIE_URL")
MEALIE_API_TOKEN = os.getenv("MEALIE_API_TOKEN")
INGEST_API_KEY = os.getenv("INGEST_API_KEY")

# Optional:
YTDLP_COOKIE_FILE = os.getenv("YTDLP_COOKIE_FILE")  # e.g. /cookies/cookies.txt
YTDLP_FFMPEG_LOCATION = os.getenv("YTDLP_FFMPEG_LOCATION")  # optional path if ffmpeg not in PATH

if not all([OPENAI_API_KEY, MEALIE_URL, MEALIE_API_TOKEN, INGEST_API_KEY]):
    raise RuntimeError("Missing required env vars")

client = OpenAI(api_key=OPENAI_API_KEY)

# Whisper hard limit is ~25 MB; stay below it.
MAX_AUDIO_BYTES = 24 * 1024 * 1024  # 24 MB

# Bitrate tiers for re-encoding (kbps). Start decent, then drop until < MAX_AUDIO_BYTES.
AUDIO_BITRATE_TIERS_KBPS = [48, 32, 24, 16, 12, 8]


class SocialRequest(BaseModel):
    url: AnyHttpUrl
    manual_title: Optional[str] = None


def detect_platform(url: str) -> str:
    u = url.lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u:
        # reels often look like /reel/<id>/ or /reels/<id>/
        return "instagram"
    return "unknown"


def ensure_ffmpeg() -> Tuple[str, str]:
    """
    Returns (ffmpeg_path, ffprobe_path) or raises.
    If YTDLP_FFMPEG_LOCATION is set, we try that first.
    """
    if YTDLP_FFMPEG_LOCATION:
        ffmpeg = os.path.join(YTDLP_FFMPEG_LOCATION, "ffmpeg")
        ffprobe = os.path.join(YTDLP_FFMPEG_LOCATION, "ffprobe")
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


def pick_thumbnail_url(info_meta: dict) -> Optional[str]:
    thumb = info_meta.get("thumbnail")
    if thumb:
        return thumb

    thumbs = info_meta.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        # Prefer the last one (often highest-res)
        for t in reversed(thumbs):
            u = (t or {}).get("url")
            if u:
                return u
        # fallback
        u = (thumbs[0] or {}).get("url")
        return u

    return None


def yt_dlp_base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
    }
    if YTDLP_COOKIE_FILE:
        opts["cookiefile"] = YTDLP_COOKIE_FILE
    if YTDLP_FFMPEG_LOCATION:
        opts["ffmpeg_location"] = YTDLP_FFMPEG_LOCATION
    return opts


def download_best_media(url: str) -> Tuple[str, dict]:
    """
    Downloads best media (video+audio) to a local file.
    Returns (downloaded_filepath, info_dict).
    """
    opts = {
        **yt_dlp_base_opts(),
        "format": "best",                 # works across many sites including TikTok/IG (when accessible)
        "outtmpl": "%(id)s.%(ext)s",
    }

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    if not os.path.exists(filename):
        raise RuntimeError("yt-dlp reported success but downloaded file not found.")

    return filename, info


def extract_audio_with_ffmpeg(input_path: str, target_kbps: int) -> str:
    """
    Extract + encode to MP3 @ target_kbps, mono, 16kHz to keep tiny.
    Returns output_path.
    """
    ffmpeg, _ = ensure_ffmpeg()

    base, _ext = os.path.splitext(input_path)
    out_path = f"{base}.audio_{target_kbps}k.mp3"

    cmd = [
        ffmpeg,
        "-y",
        "-i", input_path,
        "-vn",
        "-ac", "1",            # mono
        "-ar", "16000",        # 16kHz sample rate
        "-b:a", f"{target_kbps}k",
        out_path,
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(
            f"ffmpeg audio extract failed (kbps={target_kbps}). "
            f"stderr: {proc.stderr[-500:]}"
        )

    return out_path


def get_metadata_only(url: str) -> dict:
    """
    Extract metadata without downloading media.
    """
    with YoutubeDL(yt_dlp_base_opts()) as ydl:
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


def fetch_social_metadata_and_audio(url: str) -> dict:
    """
    Unified fetch for TikTok + Instagram Reels.

    Steps:
    1) metadata-only extract (caption/title/thumbnail)
    2) download best media
    3) extract mp3 audio using ffmpeg at descending bitrates until under MAX_AUDIO_BYTES
    """
    platform = detect_platform(url)

    # 1) metadata
    meta = get_metadata_only(url)
    caption = meta["caption"]
    title = meta["title"]
    thumbnail_url = meta["thumbnail_url"]

    # 2) download media
    print(f"=== PLATFORM: {platform} ===")
    print("=== DOWNLOADING BEST MEDIA (for audio extraction) ===")
    media_path, _info = download_best_media(url)
    media_size = os.path.getsize(media_path)
    print(f"Downloaded media: {media_path} ({media_size} bytes)")

    # 3) extract audio small enough for Whisper
    print("=== EXTRACTING AUDIO FOR WHISPER ===")
    last_audio_path = None
    for kbps in AUDIO_BITRATE_TIERS_KBPS:
        audio_path = extract_audio_with_ffmpeg(media_path, target_kbps=kbps)
        last_audio_path = audio_path
        size = os.path.getsize(audio_path)
        print(f"Audio @ {kbps}kbps: {audio_path} ({size} bytes)")
        if size <= MAX_AUDIO_BYTES:
            # cleanup the big media file; keep only audio for transcription
            try:
                os.remove(media_path)
            except Exception:
                pass
            return {
                "caption": caption,
                "title": title,
                "audio_path": audio_path,
                "thumbnail_url": thumbnail_url,
                "page_url": url,
                "platform": platform,
            }

    # If we got here, even the lowest tier exceeded MAX_AUDIO_BYTES (very unlikely for reels/tiktoks)
    raise RuntimeError(
        f"Failed to produce an audio file under {MAX_AUDIO_BYTES} bytes. "
        f"Last attempt: {last_audio_path}"
    )


def transcribe_audio(audio_path: str) -> str:
    size = os.path.getsize(audio_path)
    print(f"=== TRANSCRIBE: file size {size} bytes ===")

    if size > MAX_AUDIO_BYTES:
        print("Audio still too large; skipping transcription and relying on caption.")
        return ""

    with open(audio_path, "rb") as f:
        transcription = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
        )

    return getattr(transcription, "text", None) or getattr(transcription, "output_text", "")


def extract_recipe_with_gpt(caption: str, transcript: str, url: str, manual_title: Optional[str]) -> dict:
    prompt = """
You are a recipe extraction engine.

Given:
- The social media CAPTION text for a cooking video
- The video TRANSCRIPT
- The SOURCE URL

Extract a single recipe if possible.

Respond ONLY with JSON using this schema:

{
  "title": "string",
  "description": "string",
  "servings": "string or null",
  "total_time": "string or null",
  "ingredients": [
    {
      "amount": "string (e.g. '1', '1/2')",
      "unit": "string (e.g. 'cup', 'tsp', or '')",
      "item": "string (e.g. 'all-purpose flour')",
      "note": "string (optional extra info)"
    }
  ],
  "steps": [
    "string instruction step 1",
    "string instruction step 2"
  ],
  "source_url": "string"
}

Use the transcript to fill in missing measurements or steps if the caption is incomplete.
If you are not sure about something, leave it empty or null instead of guessing wildly.
Use the provided source URL.
""".strip()

    user_content = f"""
SOURCE URL:
{url}

MANUAL TITLE (optional):
{manual_title or ''}

CAPTION:
{caption}

TRANSCRIPT:
{transcript}
"""

    completion = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You are a precise recipe parser."},
            {"role": "user", "content": prompt},
            {"role": "user", "content": user_content},
        ],
    )

    raw = completion.choices[0].message.content
    print("=== RAW GPT RECIPE JSON ===")
    print(raw)

    import json
    return json.loads(raw)


def format_ingredient(i: dict) -> str:
    amount = i.get("amount") or ""
    unit = i.get("unit") or ""
    item = i.get("item") or ""
    note = i.get("note") or ""

    qty = " ".join(p for p in [amount, unit] if p).strip()
    line = " ".join(p for p in [qty, item] if p).strip()
    if note:
        line = f"{line}, {note}".strip(", ").strip()
    return line


def send_recipe_to_mealie(recipe: dict, image_url: Optional[str], page_url: str) -> dict:
    """
    Create -> fetch skeleton -> update many -> ask Mealie to scrape image from URL (thumbnail URL).
    """
    title = recipe.get("title") or "Social Recipe"
    description = recipe.get("description") or ""
    source_url = recipe.get("source_url") or page_url or ""
    servings = recipe.get("servings") or ""
    total_time = recipe.get("total_time") or ""

    ingredients = recipe.get("ingredients") or []
    steps = recipe.get("steps") or []

    headers = {
        "Authorization": f"Bearer {MEALIE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=60.0) as http:
        print("=== MEALIE CREATE ===")
        create_res = http.post(
            f"{MEALIE_URL}/api/recipes",
            json={"name": title},
            headers=headers,
        )
        create_res.raise_for_status()
        created_identifier = create_res.json()
        print("Create response JSON:", created_identifier)

        get_res = http.get(f"{MEALIE_URL}/api/recipes/{created_identifier}", headers=headers)
        get_res.raise_for_status()
        existing = get_res.json()

        ingredient_objs = []
        for ing in ingredients:
            line = format_ingredient(ing)
            ingredient_objs.append({"note": line, "display": line, "originalText": line})

        instruction_objs = []
        for s in steps:
            text = s if isinstance(s, str) else (s.get("text", "") if isinstance(s, dict) else "")
            instruction_objs.append({"text": text})

        update_payload = existing.copy()
        update_payload["name"] = title
        update_payload["description"] = description
        update_payload["orgURL"] = source_url or existing.get("orgURL")
        update_payload["recipeYield"] = servings or existing.get("recipeYield")
        update_payload["totalTime"] = total_time or existing.get("totalTime")
        update_payload["recipeIngredient"] = ingredient_objs
        update_payload["recipeInstructions"] = instruction_objs

        print("=== MEALIE UPDATE PAYLOAD ===")
        print(update_payload)

        update_res = http.put(
            f"{MEALIE_URL}/api/recipes",
            json=[update_payload],
            headers=headers,
        )
        print("=== MEALIE UPDATE RESPONSE ===")
        print("Status:", update_res.status_code)
        print("Body:", update_res.text)
        update_res.raise_for_status()

        # Ask Mealie to scrape the image from *thumbnail URL* (not the page URL)
        if image_url:
            try:
                recipe_slug = existing.get("slug") or created_identifier
                print("=== ASKING MEALIE TO SCRAPE IMAGE FROM THUMBNAIL URL ===", image_url)
                scrape_res = http.post(
                    f"{MEALIE_URL}/api/recipes/{recipe_slug}/image",
                    headers=headers,
                    json={"includeTags": False, "url": image_url},
                )
                print("Image scrape status:", scrape_res.status_code, scrape_res.text)
                scrape_res.raise_for_status()
            except Exception as e:
                print("Image scrape failed:", repr(e))

        return {
            "id": existing.get("id"),
            "slug": existing.get("slug") or created_identifier,
            "name": title,
        }


@app.post("/ingest/social")
def ingest_social(body: SocialRequest, x_api_key: str = Header(None)):
    if x_api_key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    meta = fetch_social_metadata_and_audio(str(body.url))
    caption = meta["caption"]
    default_title = meta["title"]
    audio_path = meta["audio_path"]
    thumbnail_url = meta.get("thumbnail_url")
    page_url = meta["page_url"]
    platform = meta["platform"]

    print("=== SOCIAL META ===")
    print("Platform:", platform)
    print("URL:", page_url)
    print("Title:", default_title)
    print("Thumbnail URL:", thumbnail_url)
    print("Caption:", caption[:500], "..." if len(caption) > 500 else "")

    try:
        transcript = transcribe_audio(audio_path)
        print("=== TRANSCRIPT (first 500 chars) ===")
        print(transcript[:500], "..." if len(transcript) > 500 else "")

        recipe = extract_recipe_with_gpt(
            caption=caption,
            transcript=transcript,
            url=page_url,
            manual_title=body.manual_title or default_title,
        )

        mealie_recipe = send_recipe_to_mealie(
            recipe=recipe,
            image_url=thumbnail_url,
            page_url=page_url,
        )

        return {
            "status": "ok",
            "platform": platform,
            "mealie_id": mealie_recipe.get("id"),
            "mealie_slug": mealie_recipe.get("slug"),
            "mealie_name": mealie_recipe.get("name"),
        }
    finally:
        try:
            os.remove(audio_path)
        except Exception:
            pass


# Backward-compatible endpoint (calls the unified one)
@app.post("/ingest/tiktok")
def ingest_tiktok(body: SocialRequest, x_api_key: str = Header(None)):
    return ingest_social(body, x_api_key)

