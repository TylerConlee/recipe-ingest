# app/main.py
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from openai import OpenAI
from pydantic import AnyHttpUrl, BaseModel

from app.constants import MAX_AUDIO_BYTES
from app.media_pipeline import fetch_social_metadata_and_audio
from app.recipe_ai import extract_recipe_with_gpt, transcribe_audio
from app.settings import Settings
from app.tandoor_client import send_recipe_to_tandoor


class SocialRequest(BaseModel):
    url: AnyHttpUrl
    manual_title: Optional[str] = None


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    openai_client = OpenAI(api_key=settings.openai_api_key)

    app = FastAPI()
    app.state.settings = settings
    app.state.openai_client = openai_client

    @app.post("/ingest/social")
    def ingest_social(body: SocialRequest, request: Request, x_api_key: str = Header(None)):
        st: Settings = request.app.state.settings
        oc: OpenAI = request.app.state.openai_client

        if x_api_key != st.ingest_api_key:
            raise HTTPException(status_code=401, detail="Invalid API key")

        meta = fetch_social_metadata_and_audio(str(body.url), st)
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
            transcript = transcribe_audio(oc, audio_path, MAX_AUDIO_BYTES)
            print("=== TRANSCRIPT (first 500 chars) ===")
            print(transcript[:500], "..." if len(transcript) > 500 else "")

            recipe = extract_recipe_with_gpt(
                oc,
                caption=caption,
                transcript=transcript,
                url=page_url,
                manual_title=body.manual_title or default_title,
            )

            tandoor_recipe = send_recipe_to_tandoor(
                st,
                recipe=recipe,
                image_url=thumbnail_url,
                page_url=page_url,
                platform=platform,
            )

            return {
                "status": "ok",
                "platform": platform,
                "tandoor_id": tandoor_recipe.get("id"),
                "tandoor_name": tandoor_recipe.get("name"),
                "source_url": tandoor_recipe.get("source_url"),
            }
        finally:
            try:
                os.remove(audio_path)
            except OSError:
                pass

    @app.post("/ingest/tiktok")
    def ingest_tiktok(body: SocialRequest, request: Request, x_api_key: str = Header(None)):
        return ingest_social(body, request, x_api_key)

    return app


app = create_app()
