from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.settings import Settings
from app.tandoor_client import (
    download_cover_bytes,
    send_recipe_to_tandoor,
    tandoor_json_headers,
    tandoor_request_url,
)


@pytest.fixture
def tandoor_settings() -> Settings:
    return Settings(
        anthropic_api_key="sk-ant-test",
        tandoor_url="http://tandoor.test",
        tandoor_api_token="secret-token",
        ingest_api_key="i",
    )


def test_tandoor_json_headers(tandoor_settings):
    h = tandoor_json_headers(tandoor_settings)
    assert h["Content-Type"] == "application/json"
    assert h["Authorization"] == "Bearer secret-token"


def test_tandoor_request_url_without_query_flag(tandoor_settings):
    assert tandoor_request_url(tandoor_settings, "/api/recipe/") == "http://tandoor.test/api/recipe/"


def test_tandoor_request_url_with_query_flag():
    s = Settings(
        anthropic_api_key="sk-ant-test",
        tandoor_url="https://host",
        tandoor_api_token="tda_abc",
        ingest_api_key="i",
        tandoor_oauth_token_via_query=True,
    )
    u = tandoor_request_url(s, "/api/recipe/")
    assert u.startswith("https://host/api/recipe/?")
    assert "access_token=tda_abc" in u


def test_send_recipe_appends_access_token_when_query_flag_enabled():
    s = Settings(
        anthropic_api_key="sk-ant-test",
        tandoor_url="http://tandoor.test",
        tandoor_api_token="tda_secret",
        ingest_api_key="i",
        tandoor_oauth_token_via_query=True,
    )
    recipe = {"title": "R", "steps": [{"instruction": "Do", "ingredients": []}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/recipe/":
            assert request.url.query.decode() == "access_token=tda_secret"
            assert request.headers.get("authorization") == "Bearer tda_secret"
            return httpx.Response(201, json={"id": 1, "name": "R", "source_url": ""})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="http://tandoor.test") as client:
        send_recipe_to_tandoor(
            s,
            recipe,
            image_url=None,
            page_url="https://src",
            platform="instagram",
            http_client=client,
        )


def test_send_recipe_create_only_no_image(tandoor_settings):
    recipe = {"title": "R", "steps": [{"instruction": "Do", "ingredients": []}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/recipe/":
            assert b'"name"' in request.content
            assert request.headers.get("authorization") == "Bearer secret-token"
            return httpx.Response(201, json={"id": 99, "name": "R", "source_url": "https://src"})
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="http://tandoor.test") as client:
        out = send_recipe_to_tandoor(
            tandoor_settings,
            recipe,
            image_url=None,
            page_url="https://src",
            platform="instagram",
            http_client=client,
        )
    assert out["id"] == 99
    assert out["name"] == "R"


def test_send_recipe_with_image_upload(tandoor_settings):
    recipe = {"title": "R", "steps": [{"instruction": "Do", "ingredients": []}]}
    calls = {"put": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/recipe/":
            return httpx.Response(201, json={"id": 5, "name": "R", "source_url": None})
        if request.method == "PUT" and request.url.path == "/api/recipe/5/image/":
            calls["put"] += 1
            return httpx.Response(200, json={"id": 5, "image": "http://img"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="http://tandoor.test") as client:
        with patch(
            "app.tandoor_client.download_cover_bytes",
            return_value=(b"\xff\xd8\xff\xd9", "c.jpg", "image/jpeg"),
        ):
            send_recipe_to_tandoor(
                tandoor_settings,
                recipe,
                image_url="https://cdn.example/t.jpg",
                page_url="https://src",
                platform="tiktok",
                http_client=client,
            )
    assert calls["put"] == 1


def test_send_recipe_create_http_error(tandoor_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="http://tandoor.test") as client:
        with pytest.raises(httpx.HTTPStatusError):
            send_recipe_to_tandoor(
                tandoor_settings,
                {"title": "x", "steps": [{"instruction": "y", "ingredients": []}]},
                None,
                "https://u",
                "unknown",
                http_client=client,
            )


def test_download_cover_bytes_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"content-type": "image/png"})

    transport = httpx.MockTransport(handler)
    factory = lambda **kw: httpx.Client(transport=transport, base_url="https://img.test")
    data, name, mime = download_cover_bytes("https://img.test/p.png", client_factory=factory)
    assert data == b"x"
    assert name.endswith(".png")
    assert mime == "image/png"


def test_download_cover_bytes_webp_and_jpeg_branches():
    def handler(request: httpx.Request) -> httpx.Response:
        if "w" in str(request.url):
            return httpx.Response(200, content=b"w", headers={"content-type": "image/webp"})
        return httpx.Response(200, content=b"j", headers={"content-type": "image/jpeg"})

    transport = httpx.MockTransport(handler)
    factory = lambda **kw: httpx.Client(transport=transport, base_url="https://img.test")

    d1, n1, m1 = download_cover_bytes("https://img.test/w", client_factory=factory)
    assert m1 == "image/webp" and n1.endswith(".webp")

    d2, n2, m2 = download_cover_bytes("https://img.test/j", client_factory=factory)
    assert m2 == "image/jpeg" and n2.endswith(".jpg")


def test_download_cover_bytes_unknown_content_type_uses_jpeg_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"content-type": "application/octet-stream"})

    transport = httpx.MockTransport(handler)
    factory = lambda **kw: httpx.Client(transport=transport, base_url="https://bin.test")
    data, name, mime = download_cover_bytes("https://bin.test/file", client_factory=factory)
    assert data == b"x"
    assert name.endswith(".jpg")
    assert mime == "image/jpeg"


def test_download_cover_bytes_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    factory = lambda **kw: httpx.Client(transport=transport, base_url="https://bad.test")
    data, name, mime = download_cover_bytes("https://bad.test/x", client_factory=factory)
    assert data is None
    assert name == "cover.jpg"


def test_send_recipe_image_put_failure_is_swallowed(tandoor_settings):
    recipe = {"title": "R", "steps": [{"instruction": "Do", "ingredients": []}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"id": 3, "name": "R", "source_url": ""})
        if request.method == "PUT":
            return httpx.Response(500, text="image failed")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="http://tandoor.test") as client:
        with patch(
            "app.tandoor_client.download_cover_bytes",
            return_value=(b"\xff\xd8", "c.jpg", "image/jpeg"),
        ):
            out = send_recipe_to_tandoor(
                tandoor_settings,
                recipe,
                image_url="https://thumb/x.jpg",
                page_url="https://page",
                platform="tiktok",
                http_client=client,
            )
    assert out["id"] == 3


def test_send_recipe_uses_context_manager_client(tandoor_settings):
    inner = MagicMock()
    req = httpx.Request("POST", "http://tandoor.test/api/recipe/")
    inner.post.return_value = httpx.Response(201, json={"id": 8, "name": "N", "source_url": "https://s"}, request=req)
    cm = MagicMock()
    cm.__enter__.return_value = inner
    cm.__exit__.return_value = None

    mock_cls = MagicMock(return_value=cm)
    out = send_recipe_to_tandoor(
        tandoor_settings,
        {"title": "T", "steps": [{"instruction": "i", "ingredients": []}]},
        None,
        "https://page",
        "unknown",
        http_client=None,
        client_factory=mock_cls,
    )
    assert out["id"] == 8
    mock_cls.assert_called_once()
    assert isinstance(mock_cls.call_args.kwargs["transport"], httpx.HTTPTransport)
    inner.post.assert_called_once()


def test_send_recipe_skips_image_when_download_returns_none(tandoor_settings):
    recipe = {"title": "R", "steps": [{"instruction": "Do", "ingredients": []}]}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if request.method == "POST":
            return httpx.Response(201, json={"id": 2, "name": "R", "source_url": ""}, request=request)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="http://tandoor.test") as client:
        with patch("app.tandoor_client.download_cover_bytes", return_value=(None, "cover.jpg", "image/jpeg")):
            out = send_recipe_to_tandoor(
                tandoor_settings,
                recipe,
                image_url="https://thumb/x",
                page_url="https://page",
                platform="tiktok",
                http_client=client,
            )
    assert seen == ["POST"]
    assert out["id"] == 2
