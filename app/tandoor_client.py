"""HTTP client for Tandoor recipe create + cover image upload."""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Callable, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from app.constants import BROWSER_UA
from app.recipe_payload import build_tandoor_recipe_payload

if TYPE_CHECKING:
    from app.settings import Settings


class TandoorBearerAuth(httpx.Auth):
    """
    Send ``Authorization: Bearer <token>`` on every request, including after redirects.

    Passing credentials via ``auth=`` (instead of only a static ``headers=`` dict) lets httpx
    re-apply them after redirects. Some reverse proxies also drop ``Authorization`` unless
    configured to forward it—see the README troubleshooting section.
    """

    def __init__(self, token: str) -> None:
        self._token = (token or "").strip()

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


def tandoor_json_headers(settings: "Settings") -> dict:
    """Headers for JSON Tandoor API calls (used by tests and any code that needs a plain dict)."""
    token = (settings.tandoor_api_token or "").strip()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def tandoor_request_url(settings: "Settings", path: str) -> str:
    """
    Build absolute URL for a Tandoor API path (path must start with ``/``).

    When ``settings.tandoor_oauth_token_via_query`` is true, append ``access_token`` to the
    query string (RFC 6750) so auth still works if a reverse proxy strips ``Authorization``.
    """
    base = settings.tandoor_url.rstrip("/")
    url = f"{base}{path if path.startswith('/') else '/' + path}"
    if not settings.tandoor_oauth_token_via_query:
        return url
    token = (settings.tandoor_api_token or "").strip()
    parts = urlparse(url)
    q = list(parse_qsl(parts.query, keep_blank_values=True))
    q = [(k, v) for k, v in q if k != "access_token"]
    q.append(("access_token", token))
    return urlunparse(parts._replace(query=urlencode(q)))


def download_cover_bytes(
    url: str,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> Tuple[Optional[bytes], str, str]:
    try:
        with client_factory(timeout=60.0, follow_redirects=True,
                            transport=httpx.HTTPTransport(retries=3)) as http:
            r = http.get(url, headers={"User-Agent": BROWSER_UA})
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if "png" in ctype:
                ext, mime = ".png", "image/png"
            elif "webp" in ctype:
                ext, mime = ".webp", "image/webp"
            elif "jpeg" in ctype or "jpg" in ctype or "image" in ctype:
                ext, mime = ".jpg", "image/jpeg"
            else:
                ext, mime = ".jpg", "image/jpeg"
            return r.content, f"cover{ext}", mime
    except Exception as e:
        print("Thumbnail download failed:", repr(e))
        return None, "cover.jpg", "image/jpeg"


def send_recipe_to_tandoor(
    settings: "Settings",
    recipe: dict,
    image_url: Optional[str],
    page_url: str,
    platform: str,
    http_client: Optional[httpx.Client] = None,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
    image: Optional[Tuple[Optional[bytes], str, str]] = None,
) -> dict:
    """Create the recipe, then attach a cover. ``image`` lets the caller pass bytes it already
    downloaded in parallel with the AI work; otherwise ``image_url`` is fetched here."""
    body = build_tandoor_recipe_payload(recipe, page_url, platform)
    tandoor_auth = TandoorBearerAuth(settings.tandoor_api_token)

    def do(http: httpx.Client) -> dict:
        print("=== TANDOOR CREATE RECIPE ===")
        create_res = http.post(
            tandoor_request_url(settings, "/api/recipe/"),
            json=body,
            auth=tandoor_auth,
        )
        if not create_res.is_success:
            print("Tandoor create failed:", create_res.status_code, create_res.text)
        create_res.raise_for_status()
        created = create_res.json()
        recipe_id = created.get("id")
        print("Created recipe id:", recipe_id, "name:", created.get("name"))

        if recipe_id and (image is not None or image_url):
            if image is not None:
                file_bytes, filename, mime = image
            else:
                file_bytes, filename, mime = download_cover_bytes(image_url, client_factory=client_factory)
            if file_bytes:
                try:
                    print("=== TANDOOR RECIPE IMAGE (multipart) ===", filename, len(file_bytes), "bytes")
                    img_res = http.put(
                        tandoor_request_url(settings, f"/api/recipe/{recipe_id}/image/"),
                        auth=tandoor_auth,
                        files={"image": (filename, BytesIO(file_bytes), mime)},
                    )
                    print("Image PUT status:", img_res.status_code, img_res.text[:500])
                    img_res.raise_for_status()
                except Exception as e:
                    print("Tandoor image upload failed:", repr(e))

        return {
            "id": recipe_id,
            "name": created.get("name") or body["name"],
            "source_url": created.get("source_url") or body.get("source_url"),
        }

    if http_client is not None:
        return do(http_client)
    # Transport-level retries only re-send when the connection never opened, so no duplicate recipes.
    with client_factory(timeout=120.0, transport=httpx.HTTPTransport(retries=3)) as http:
        return do(http)
