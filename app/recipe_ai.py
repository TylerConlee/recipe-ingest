"""OpenAI Whisper + GPT recipe extraction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from app.recipe_payload import (
    gpt_recipe_system_and_user_prompts,
    parse_gpt_recipe_json,
)

if TYPE_CHECKING:
    from openai import OpenAI


def transcribe_audio(openai_client: "OpenAI", audio_path: str, max_audio_bytes: int) -> str:
    import os

    size = os.path.getsize(audio_path)
    print(f"=== TRANSCRIBE: file size {size} bytes ===")

    if size > max_audio_bytes:
        print("Audio still too large; skipping transcription and relying on caption.")
        return ""

    with open(audio_path, "rb") as f:
        transcription = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
        )

    return getattr(transcription, "text", None) or getattr(transcription, "output_text", "")


def extract_recipe_with_gpt(
    openai_client: "OpenAI",
    caption: str,
    transcript: str,
    url: str,
    manual_title: Optional[str],
) -> dict:
    prompt, user_content = gpt_recipe_system_and_user_prompts(caption, transcript, url, manual_title)

    completion = openai_client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You are a precise recipe parser. Output only valid JSON."},
            {"role": "user", "content": prompt},
            {"role": "user", "content": user_content},
        ],
    )

    raw = completion.choices[0].message.content
    print("=== RAW GPT RECIPE JSON ===")
    print(raw)

    if not raw or not raw.strip():
        return parse_gpt_recipe_json("{}")
    return parse_gpt_recipe_json(raw)
