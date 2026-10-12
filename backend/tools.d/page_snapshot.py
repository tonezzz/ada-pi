"""page_snapshot — screenshot a web page via playlived headless Chrome.

Navigate a playwright-headless session to any URL, take a PNG, and
publish it through the shared input-bridge /frame asset hop
(traffic_camera.publish_relay, prefix=snap) — returns an image_url
the LINE/Telegram relays can send verbatim.

Use to turn CMS pages (travel plans, reports, diagrams) into sendable
images: publish the page (cms_publish returns public_url), snapshot
that URL, then chat_send image_url=<result>. Deterministic rendering —
real fonts, exact text — unlike generative image tools, which garble
dates and Thai text on document-like images.

playlived is the fleet headless-Chrome daemon (tony-dell :9230,
tailnet-reachable). Sessions are closed after each shot.

Env:
    PLAYLIVED_URL  default http://100.68.142.13:9230
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from typing import Any

import httpx

from backend import traffic_camera

PLAYLIVED = os.environ.get(
    "PLAYLIVED_URL", "http://100.68.142.13:9230").rstrip("/")

DECLARATION = {
    "name": "page_snapshot",
    "description": (
        "Screenshot a web page into a sendable image — returns image_url "
        "for chat_send. For page URLs (public CMS pages, diagrams), not "
        "raw image files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "http(s) page address to render — e.g. the "
                               "public_url cms_publish returned.",
            },
            "full_page": {
                "type": "boolean",
                "description": "Capture the whole scroll height (default "
                               "true) instead of just the first viewport.",
            },
            "slug": {
                "type": "string",
                "description": "Optional asset-name hint for the hosted "
                               "image (default page).",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slug(s: str) -> str:
    out = _SLUG_RE.sub("-", (s or "").strip().lower()).strip("-")
    return out[:48] or "page"


async def run(runner, **args) -> dict[str, Any]:
    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False,
                "error": "url must be an http(s) page address — a raw "
                         "image file goes straight to chat_send image_url, "
                         "a CMS slug needs its public_url first"}
    full = bool(args.get("full_page", True))
    slug = _slug(str(args.get("slug") or ""))
    sid = None
    title = ""
    try:
        async with httpx.AsyncClient(base_url=PLAYLIVED, timeout=30.0) as c:
            r = await c.post("/sessions", json={
                "type": "playwright-headless", "target": "local"})
            r.raise_for_status()
            sid = r.json()["session_id"]
            nav = await c.post(f"/sessions/{sid}/navigate",
                               json={"url": url})
            nav.raise_for_status()
            title = str(nav.json().get("title") or "")
            shot = await c.post(f"/sessions/{sid}/screenshot",
                                json={"fullPage": full}, timeout=60.0)
            shot.raise_for_status()
            png = base64.b64decode(shot.json()["base64"])
    except httpx.HTTPStatusError as exc:
        return {"ok": False,
                "error": f"playlived {exc.response.status_code} on "
                         f"{exc.request.url.path}: "
                         f"{exc.response.text[:160]}"}
    except Exception as exc:
        return {"ok": False,
                "error": f"playlived failed ({type(exc).__name__}): {exc} "
                         "— say the render service is unreachable"}
    finally:
        if sid:
            try:
                async with httpx.AsyncClient(
                        base_url=PLAYLIVED, timeout=10.0) as c:
                    await c.delete(f"/sessions/{sid}")
            except Exception:
                pass
    if len(png) < 1000:
        return {"ok": False,
                "error": f"screenshot too small ({len(png)}B) — the page "
                         "probably rendered blank"}
    published = await asyncio.to_thread(
        traffic_camera.publish_relay, png, slug, "image/png", "snap")
    if not published:
        return {"ok": False,
                "error": "screenshot taken but the asset publish hop "
                         "failed — say the image could not be prepared"}
    return {"ok": True, "image_url": published, "mime": "image/png",
            "bytes": len(png), "title": title,
            "note": "send image_url verbatim via chat_send — do not "
                    "rewrite or shorten it"}
