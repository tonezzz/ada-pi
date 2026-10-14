"""wp_client — shared WordPress REST plumbing for tools.d writers.

boards.surf-thailand.com runs behind a Plesk vhost that does not rewrite
/wp-json/ (404s), while index.php?rest_route= works unconditionally —
all calls go through that route. Params are appended to the URL by hand:
httpx's params merge re-encodes the literal '/' in rest_route and WP
falls back to the front page.

Auth is a per-site WP Application Password from
~/.config/secrets/wp-boards.env — never the master login password.

Env (wp-boards.env on the backend host):
    WP_SITE_URL     — e.g. https://boards.surf-thailand.com/wp
    WP_APP_USER     — WP username the app password belongs to
    WP_APP_PASSWORD — application password (24 chars, revocable)
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import urllib.parse
from typing import Any

import httpx

ENV_FILE = os.path.expanduser("~/.config/secrets/wp-boards.env")
MEDIA_ROOT = os.path.expanduser("~/.local/share/ada/line-media")
PUBLIC_BASE = "https://boards.surf-thailand.com/wp"
PEOPLE = ("tony", "kk", "nhong")

_MEDIA_URL_RE = re.compile(r"^https?://[^/]+/relay-img/(?:arch/)?(.+)$")
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_EXT_BY_MIME = {"image/jpeg": ".jpg", "image/png": ".png",
                "image/gif": ".gif", "image/webp": ".webp",
                "application/pdf": ".pdf", "video/mp4": ".mp4"}


def env() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(ENV_FILE) as fh:
            for ln in fh:
                ln = ln.strip()
                if "=" in ln and not ln.startswith("#"):
                    k, _, v = ln.partition("=")
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def configured(env: dict[str, str]) -> bool:
    return all(env.get(k)
               for k in ("WP_SITE_URL", "WP_APP_USER", "WP_APP_PASSWORD"))


async def api(client: httpx.AsyncClient, env: dict[str, str],
              method: str, route: str, **kw: Any):
    base = env["WP_SITE_URL"].rstrip("/")
    url = (f"{base}/index.php?rest_route="
           + urllib.parse.quote("/wp/v2/" + route.lstrip("/"), safe="/"))
    params = kw.pop("params", None)
    if params:
        url += "&" + urllib.parse.urlencode(params)
    auth = base64.b64encode(
        f"{env['WP_APP_USER']}:{env['WP_APP_PASSWORD']}".encode()).decode()
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Basic {auth}"
    headers.setdefault("User-Agent", _UA)
    return await client.request(method, url, headers=headers, **kw)


def media_local_path(media_url: str) -> str | None:
    m = _MEDIA_URL_RE.match(media_url.strip())
    if not m:
        return None
    rel = urllib.parse.unquote(m.group(1)).lstrip("/")
    if ".." in rel:
        return None
    p = os.path.join(MEDIA_ROOT, rel)
    return p if os.path.isfile(p) else None


def https(url: str) -> str:
    """Normalise a WP-returned URL for public display: force https and
    rewrite the internal siteurl host (boards.h3.gizmo-thailand.com) to
    the public surf domain — WP's siteurl used to point at the origin."""
    u = re.sub(r"^http://", "https://", url or "")
    u = re.sub(r"^https://boards\.h3\.gizmo-thailand\.com",
               "https://boards.surf-thailand.com", u)
    return u


def sniff_media(path: str) -> tuple[str, str]:
    """(mime, ext) — LINE archive saves unknown blobs as .bin, which WP
    rejects on extension; sniff magic bytes instead of trusting it."""
    with open(path, "rb") as fh:
        head = fh.read(12)
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", ".png"
    if head[:4] == b"GIF8":
        return "image/gif", ".gif"
    if head[:4] == b"%PDF":
        return "application/pdf", ".pdf"
    if head[4:8] == b"ftyp":
        return "video/mp4", ".mp4"
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return mime, os.path.splitext(path)[1] or ""


async def ensure_tag(client: httpx.AsyncClient, env: dict[str, str],
                     slug: str) -> int | None:
    r = await api(client, env, "GET", "tags", params={"slug": slug})
    tags = r.json() if r.status_code == 200 else []
    if tags:
        return tags[0]["id"]
    r = await api(client, env, "POST", "tags", json={"name": slug})
    if r.status_code in (200, 201):
        return r.json().get("id")
    return None


async def upload_media(client: httpx.AsyncClient, env: dict[str, str],
                       path: str) -> tuple[int | None, str]:
    mime, ext = sniff_media(path)
    fname = os.path.basename(path)
    if ext and not fname.lower().endswith(ext):
        fname = os.path.splitext(fname)[0] + ext
    with open(path, "rb") as fh:
        data = fh.read()
    r = await api(client, env, "POST", "media", content=data, headers={
        "Content-Type": mime,
        "Content-Disposition": f'attachment; filename="{fname}"'})
    if r.status_code not in (200, 201):
        return None, ""
    return r.json().get("id"), https(r.json().get("source_url") or "")


async def create_post(client: httpx.AsyncClient, env: dict[str, str],
                      title: str, content_html: str,
                      tag_ids: list[int] | None = None,
                      status: str = "publish",
                      featured_media: int | None = None,
                      post_type: str = "posts",
                      meta: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": title, "content": content_html, "status": status,
        "tags": tag_ids or []}
    if featured_media:
        payload["featured_media"] = featured_media
    if meta:
        payload["meta"] = meta
    r = await api(client, env, "POST", post_type, json=payload)
    if r.status_code not in (200, 201):
        return {"ok": False,
                "error": f"post create failed {r.status_code}: "
                         f"{r.text[:160]}"}
    return {"ok": True, "id": r.json().get("id"),
            "link": https(r.json().get("link") or "")}


def paragraphs(text: str) -> str:
    return "".join(f"<p>{ln}</p>\n"
                   for ln in text.split("\n") if ln.strip())


# -- Pages (the /chat/ tree — webchat widget hosting) ------------------------


async def find_page(client: httpx.AsyncClient, env: dict[str, str],
                    slug: str, parent: int | None = None
                    ) -> dict[str, Any] | None:
    """First page matching slug (+ optional parent) at any status."""
    params: dict[str, Any] = {"slug": slug, "status": "any",
                              "per_page": 5}
    if parent:
        params["parent"] = parent
    r = await api(client, env, "GET", "pages", params=params)
    pages = r.json() if r.status_code == 200 else []
    if not isinstance(pages, list) or not pages:
        return None
    return pages[0]


async def ensure_page(client: httpx.AsyncClient, env: dict[str, str],
                      slug: str, title: str, content_html: str, *,
                      parent: int = 0, status: str = "private"
                      ) -> dict[str, Any]:
    """Create a WP page or update the existing one with the same slug.

    Pages (not posts) carry the /chat/ hierarchy — the webchat widget
    embeds live on private pages per member (card webchat-lane-boards).
    """
    existing = await find_page(client, env, slug, parent or None)
    payload: dict[str, Any] = {
        "title": title, "content": content_html,
        "status": status, "slug": slug}
    if parent:
        payload["parent"] = parent
    if existing:
        r = await api(client, env, "POST",
                      f"pages/{existing['id']}", json=payload)
    else:
        r = await api(client, env, "POST", "pages", json=payload)
    if r.status_code not in (200, 201):
        return {"ok": False,
                "error": f"page {'update' if existing else 'create'} "
                         f"failed {r.status_code}: {r.text[:160]}"}
    return {"ok": True, "id": r.json().get("id"),
            "link": https(r.json().get("link") or ""),
            "created": not existing}
