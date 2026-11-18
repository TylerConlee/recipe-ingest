from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import JobStore, create_app, notify
from app.pipeline import IngestResult, NotARecipe
from app.settings import Settings

KEY = "correct-key-0123456789"


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="sk-ant", tandoor_url="http://t", tandoor_api_token="t",
                    ingest_api_key=KEY, notify_url="https://ntfy.test/topic")


@pytest.fixture
def sent(monkeypatch):
    msgs = []
    monkeypatch.setattr(main.httpx, "post", lambda url, **kw: msgs.append((url, kw)))
    return msgs


def _ok_ingest(calls=None):
    def ingest(url, settings, client, manual_title=None):
        if calls is not None:
            calls.append((url, manual_title))
        return IngestResult(platform="tiktok", tandoor_id=42, tandoor_name="Pasta", source_url=url,
                            uncertainties=["check salt"])
    return ingest


def _client(settings, ingest):
    return TestClient(create_app(settings, client=object(), ingest=ingest), raise_server_exceptions=False)


def test_401_wrong_or_missing_key(settings):
    c = _client(settings, _ok_ingest())
    assert c.post("/ingest/social", json={"url": "https://www.tiktok.com/x"}, headers={"X-Api-Key": "nope"}).status_code == 401
    assert c.post("/ingest/social", json={"url": "https://www.tiktok.com/x"}).status_code == 401
    assert c.get("/jobs/abc").status_code == 401


def test_sync_ingest_200_and_notify(settings, sent):
    calls = []
    c = _client(settings, _ok_ingest(calls))
    r = c.post("/ingest/social", json={"url": "https://www.tiktok.com/@c/video/1", "manual_title": "M"},
               headers={"X-Api-Key": KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["tandoor_id"] == 42 and body["tandoor_name"] == "Pasta"
    assert body["tandoor_link"] == "http://t/view/recipe/42"
    assert calls == [("https://www.tiktok.com/@c/video/1", "M")]
    assert sent[0][1]["headers"]["Title"] == "Recipe imported"
    assert b"1 things to double-check" in sent[0][1]["content"]


def test_duplicate_url_is_deduplicated(settings, sent):
    calls = []
    c = _client(settings, _ok_ingest(calls))
    h = {"X-Api-Key": KEY}
    c.post("/ingest/social", json={"url": "https://www.tiktok.com/x"}, headers=h)
    r = c.post("/ingest/tiktok", json={"url": "https://www.tiktok.com/x"}, headers=h)
    assert r.status_code == 200 and r.json()["deduplicated"] is True
    assert len(calls) == 1


def test_background_job_flow(settings, sent):
    c = _client(settings, _ok_ingest())
    h = {"X-Api-Key": KEY}
    r = c.post("/ingest/social", json={"url": "https://www.tiktok.com/bg", "background": True}, headers=h)
    assert r.status_code == 202
    jid = r.json()["job_id"]
    for _ in range(50):
        job = c.get(f"/jobs/{jid}", headers=h).json()
        if job["status"] == "done":
            break
        time.sleep(0.02)
    assert job["status"] == "done" and job["result"]["tandoor_id"] == 42
    assert c.get("/jobs/missing", headers=h).status_code == 404


def test_inflight_duplicate_returns_202(settings):
    c = _client(settings, _ok_ingest())
    store: JobStore = c.app.state.jobs
    job = store.create("https://www.tiktok.com/busy")
    store.update(job["id"], status="running")
    r = c.post("/ingest/social", json={"url": "https://www.tiktok.com/busy"}, headers={"X-Api-Key": KEY})
    assert r.status_code == 202 and r.json()["job_id"] == job["id"]


def test_not_a_recipe_422(settings, sent):
    def ingest(*a, **k):
        raise NotARecipe("No recipe found in this video.")
    c = _client(settings, ingest)
    r = c.post("/ingest/social", json={"url": "https://www.tiktok.com/dance"}, headers={"X-Api-Key": KEY})
    assert r.status_code == 422
    assert sent[0][1]["headers"]["Title"] == "Recipe import skipped"


def test_error_500_then_retry_allowed(settings, sent):
    attempts = []

    def ingest(url, *a, **k):
        attempts.append(url)
        if len(attempts) == 1:
            raise RuntimeError("yt-dlp blocked")
        return _ok_ingest()(url, *a, **k)

    c = _client(settings, ingest)
    h = {"X-Api-Key": KEY}
    assert c.post("/ingest/social", json={"url": "https://www.tiktok.com/e"}, headers=h).status_code == 500
    assert sent[0][1]["headers"]["Title"] == "Recipe import failed"
    assert c.post("/ingest/social", json={"url": "https://www.tiktok.com/e"}, headers=h).status_code == 200


def test_background_error_recorded(settings, sent):
    def ingest(*a, **k):
        raise RuntimeError("boom")
    c = _client(settings, ingest)
    h = {"X-Api-Key": KEY}
    jid = c.post("/ingest/social", json={"url": "https://www.tiktok.com/b", "background": True}, headers=h).json()["job_id"]
    for _ in range(50):
        job = c.get(f"/jobs/{jid}", headers=h).json()
        if job["status"] == "error":
            break
        time.sleep(0.02)
    assert job["status"] == "error" and "boom" in job["error"]


def test_healthz(settings):
    r = _client(settings, _ok_ingest()).get("/healthz")
    assert r.json()["extract_model"] == "claude-opus-5-5"


def test_job_prune(monkeypatch):
    store = JobStore()
    job = store.create("u")
    monkeypatch.setattr(main.time, "time", lambda: job["created"] + main.JOB_TTL_S + 1)
    store.create("v")
    assert store.get(job["id"]) is None


def test_notify_noop_and_failure(settings):
    s_none = Settings(anthropic_api_key="a", tandoor_url="t", tandoor_api_token="t", ingest_api_key=KEY)
    notify(s_none, "x", "y", post=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not post")))

    def boom(*a, **k):
        raise OSError("offline")
    notify(settings, "x", "y", post=boom)  # swallowed
