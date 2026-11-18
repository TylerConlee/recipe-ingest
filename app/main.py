# app/main.py
from __future__ import annotations

import hmac
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import AnyHttpUrl, BaseModel

from app.pipeline import NotARecipe, run_ingest
from app.recipe_ai import make_client
from app.settings import Settings

JOB_TTL_S = 24 * 3600
DEDUPE_WINDOW_S = 10 * 60


class SocialRequest(BaseModel):
    url: AnyHttpUrl
    manual_title: Optional[str] = None
    # Return 202 immediately and process in the background (pair with NOTIFY_URL).
    background: bool = False


class JobStore:
    """In-memory job registry with URL de-duplication (iOS Shortcuts love to double-fire)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def _prune(self) -> None:
        cutoff = time.time() - JOB_TTL_S
        for jid in [j for j, v in self._jobs.items() if v["created"] < cutoff]:
            del self._jobs[jid]

    def find_recent(self, url: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            for job in self._jobs.values():
                if job["url"] == url and job["status"] in ("queued", "running", "done") \
                        and now - job["created"] < DEDUPE_WINDOW_S:
                    return dict(job)
        return None

    def create(self, url: str) -> Dict[str, Any]:
        with self._lock:
            self._prune()
            job = {"id": uuid.uuid4().hex[:12], "url": url, "status": "queued",
                   "created": time.time(), "result": None, "error": None}
            self._jobs[job["id"]] = job
            return dict(job)

    def update(self, jid: str, **kw: Any) -> None:
        with self._lock:
            if jid in self._jobs:
                self._jobs[jid].update(kw)

    def get(self, jid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(jid)
            return dict(job) if job else None


def notify(settings: Settings, title: str, message: str, click: Optional[str] = None,
           post: Optional[Callable[..., Any]] = None) -> None:
    """POST a plain-text notification (ntfy-compatible) if NOTIFY_URL is configured."""
    if not settings.notify_url:
        return
    post = post or httpx.post
    headers = {"Title": title}
    if click:
        headers["Click"] = click
    try:
        post(settings.notify_url, content=message.encode("utf-8"), headers=headers, timeout=10.0)
    except Exception as e:
        print("Notify failed:", repr(e))


def create_app(settings: Optional[Settings] = None, client: Any = None,
               ingest: Callable[..., Any] = run_ingest) -> FastAPI:
    settings = settings or Settings.from_env()
    client = client or make_client(settings)

    app = FastAPI(title="recipe-ingest")
    app.state.settings = settings
    app.state.client = client
    app.state.jobs = JobStore()
    # Bounded: the box powers off under sustained full-CPU load, so never run many Whisper jobs at once.
    app.state.executor = ThreadPoolExecutor(max_workers=2)

    def check_key(x_api_key: Optional[str]) -> None:
        if not x_api_key or not hmac.compare_digest(x_api_key.encode(), settings.ingest_api_key.encode()):
            raise HTTPException(status_code=401, detail="Invalid API key")

    def recipe_link(tandoor_id: Any) -> Optional[str]:
        return f"{settings.tandoor_url}/view/recipe/{tandoor_id}" if tandoor_id else None

    def process(job_id: str, url: str, manual_title: Optional[str]) -> Dict[str, Any]:
        jobs: JobStore = app.state.jobs
        jobs.update(job_id, status="running")
        try:
            result = ingest(url, settings, app.state.client, manual_title=manual_title).as_response()
        except NotARecipe as e:
            jobs.update(job_id, status="not_a_recipe", error=str(e))
            notify(settings, "Recipe import skipped", f"No recipe found in {url}")
            raise
        except Exception as e:
            jobs.update(job_id, status="error", error=repr(e))
            notify(settings, "Recipe import failed", f"{url}\n{e!r}"[:500])
            raise
        result["job_id"] = job_id
        result["tandoor_link"] = recipe_link(result.get("tandoor_id"))
        jobs.update(job_id, status="done", result=result)
        caveat = f" ({len(result['uncertainties'])} things to double-check)" if result.get("uncertainties") else ""
        notify(settings, "Recipe imported", f"{result['tandoor_name']}{caveat}", click=result["tandoor_link"])
        return result

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "extract_model": settings.extract_model,
                "vision_model": settings.vision_model, "transcriber": settings.transcriber}

    @app.post("/ingest/social")
    def ingest_social(body: SocialRequest, request: Request, x_api_key: str = Header(None)):
        check_key(x_api_key)
        url = str(body.url)
        jobs: JobStore = request.app.state.jobs

        existing = jobs.find_recent(url)
        if existing:
            if existing["status"] == "done":
                return {**existing["result"], "deduplicated": True}
            return JSONResponse(status_code=202, content={"status": existing["status"], "job_id": existing["id"],
                                                          "deduplicated": True})

        job = jobs.create(url)
        if body.background:
            request.app.state.executor.submit(_swallow(process), job["id"], url, body.manual_title)
            return JSONResponse(status_code=202, content={"status": "queued", "job_id": job["id"]})

        try:
            return process(job["id"], url, body.manual_title)
        except NotARecipe as e:
            raise HTTPException(status_code=422, detail=str(e))

    @app.post("/ingest/tiktok")
    def ingest_tiktok(body: SocialRequest, request: Request, x_api_key: str = Header(None)):
        return ingest_social(body, request, x_api_key)

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, request: Request, x_api_key: str = Header(None)):
        check_key(x_api_key)
        job = request.app.state.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job")
        return job

    return app


def _swallow(fn: Callable[..., Any]) -> Callable[..., None]:
    def wrapper(*a: Any, **kw: Any) -> None:
        try:
            fn(*a, **kw)
        except Exception:
            pass  # recorded on the job + notified already

    return wrapper


try:
    app = create_app()
except RuntimeError as _e:  # pragma: no cover - surfaces config errors clearly in container logs
    print(f"FATAL config error: {_e}")
    raise
