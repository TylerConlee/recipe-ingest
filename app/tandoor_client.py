"""HTTP client for Tandoor recipe create + cover image upload."""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Callable, Optional, Tuple

import httpx

from app.constants import BROWSER_UA
from app.recipe_payload import build_tandoor_recipe_payload

if TYPE_CHECKING:
    from app.settings import Settings


def tandoor_json_headers(settings: "Settings") -> dict:
    return {
        "Authorization": f"Bearer {settings.tandoor_api_token}",
        "Content-Type": "application/json",
    }


def download_cover_bytes(
    url: str,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> Tuple[Optional[bytes], str, str]:
    try:
        with client_factory(timeout=60.0, follow_redirects=True) as http:
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
) -> dict:
    body = build_tandoor_recipe_payload(recipe, page_url, platform)

    def do(http: httpx.Client) -> dict:
        print("=== TANDOOR CREATE RECIPE ===")
        create_res = http.post(
            f"{settings.tandoor_url}/api/recipe/",
            json=body,
            headers=tandoor_json_headers(settings),
        )
        if not create_res.is_success:
            print("Tandoor create failed:", create_res.status_code, create_res.text)
        create_res.raise_for_status()
        created = create_res.json()
        recipe_id = created.get("id")
        print("Created recipe id:", recipe_id, "name:", created.get("name"))

        if image_url and recipe_id:
            file_bytes, filename, mime = download_cover_bytes(image_url, client_factory=client_factory)
            if file_bytes:
                try:
                    print("=== TANDOOR RECIPE IMAGE (multipart) ===", filename, len(file_bytes), "bytes")
                    img_res = http.put(
                        f"{settings.tandoor_url}/api/recipe/{recipe_id}/image/",
                        headers={"Authorization": f"Bearer {settings.tandoor_api_token}"},
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
    with client_factory(timeout=120.0) as http:
        return do(http)
