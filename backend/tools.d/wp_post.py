"""wp_post — publish a LINE chat message as a post on the boards
WordPress site and hand back the link to send in chat.

The kk lane members chat in LINE ("post to my site: ..." or a photo with
a caption) and expect a live page back — card line-wp-site-manager
(OPS-0640). This tool does the deterministic half: ensure the member tag
exists, optionally upload archived LINE media, create the post, and
return the public URL. Auth uses a per-site WP Application Password read
from ~/.config/secrets/wp-boards.env — never the master login password.

REST calls go through ?rest_route= instead of /wp-json/ — the Plesk
vhost does not rewrite /wp-json/ (404s) while index.php?rest_route=
works unconditionally.

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

_ENV_FILE = os.path.expanduser("~/.config/secrets/wp-boards.env")
_PEOPLE = ("tony", "kk", "nhong")
_MEDIA_ROOT = os.path.expanduser("~/.local/share/ada/line-media")
_MEDIA_URL_RE = re.compile(r"^https?://[^/]+/relay-img/(?:arch/)?(.+)$")
_PUBLIC_BASE = "https://boards.surf-thailand.com/wp"

DECLARATION = {
    "name": "wp_post",
    "description": (
        "Publish a post to a member's WordPress page on the boards site "
        "(persons: tony, kk, nhong) and return the public URL to send in "
        "chat. publish=false creates a preview draft first."),
    "parameters": {
        "type": "object",
        "properties": {
            "person": {
                "type": "string",
                "description": "Member page: tony, kk, or nhong.",
            },
            "title": {"type": "string", "description": "Post title."},
            "content": {
                "type": "string",
                "description": "Post body text (Thai ok).",
            },
            "publish": {
                "type": "boolean",
                "description": "Publish immediately (default true); "
                               "false creates a preview draft.",
            },
            "media_url": {
                "type": "string",
                "description": "Optional /relay-img/arch/... URL of a "
                               "photo the user just sent — embedded at "
                               "the top of the post.",
            },
        },
        "required": ["person", "title", "content"],
    },
}


def _env() -> dict:
    env: dict[str, str] = {}
    try:
        with open(_ENV_FILE) as fh:
            for ln in fh:
                ln = ln.strip()
                if "=" in ln and not ln.startswith("#"):
                    k, _, v = ln.partition("=")
                    env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def _api(env: dict, method: str, route: str, **kw: Any):
    base = env["WP_SITE_URL"].rstrip("/")
    url = (f"{base}/index.php?rest_route="
           + urllib.parse.quote("/wp/v2/" + route.lstrip("/"), safe="/"))
    auth = base64.b64encode(
        f"{env['WP_APP_USER']}:{env['WP_APP_PASSWORD']}".encode()).decode()
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Basic {auth}"
    return httpx.request(method, url, headers=headers, timeout=30,
                         follow_redirects=True, **kw)


def _media_local_path(media_url: str) -> str | None:
    m = _MEDIA_URL_RE.match(media_url.strip())
    if not m:
        return None
    rel = urllib.parse.unquote(m.group(1)).lstrip("/")
    if ".." in rel:
        return None
    p = os.path.join(_MEDIA_ROOT, rel)
    return p if os.path.isfile(p) else None


def _https(url: str) -> str:
    return re.sub(r"^http://", "https://", url or "")


def run(args: dict) -> dict:
    person = str(args.get("person") or "").strip().lower()
    title = str(args.get("title") or "").strip()
    content = str(args.get("content") or "").strip()
    publish = bool(args.get("publish", True))
    media_url = str(args.get("media_url") or "").strip()

    if person not in _PEOPLE:
        return {"ok": False, "error": f"person must be one of {_PEOPLE}"}
    if not title or not content:
        return {"ok": False, "error": "title and content required"}
    env = _env()
    if not all(env.get(k)
               for k in ("WP_SITE_URL", "WP_APP_USER", "WP_APP_PASSWORD")):
        return {"ok": False,
                "error": "wp-boards.env missing WP_SITE_URL/"
                         "WP_APP_USER/WP_APP_PASSWORD on this host"}

    try:
        # member tag — find or create
        r = _api(env, "GET", "tags", params={"slug": person})
        tags = r.json() if r.status_code == 200 else []
        tag_id = tags[0]["id"] if tags else None
        if not tag_id:
            r = _api(env, "POST", "tags", json={"name": person})
            if r.status_code not in (200, 201):
                return {"ok": False,
                        "error": f"tag create failed {r.status_code}"}
            tag_id = r.json().get("id")

        # optional media from the LINE archive
        media_id, media_src = None, ""
        media_note = ""
        if media_url:
            local = _media_local_path(media_url)
            if not local:
                media_note = " (photo skipped — not found in archive)"
            else:
                mime = (mimetypes.guess_type(local)[0] or
                        "application/octet-stream")
                fname = os.path.basename(local)
                with open(local, "rb") as fh:
                    data = fh.read()
                r = _api(env, "POST", "media", content=data, headers={
                    "Content-Type": mime,
                    "Content-Disposition":
                        f'attachment; filename="{fname}"',
                })
                if r.status_code in (200, 201):
                    media_id = r.json().get("id")
                    media_src = _https(
                        (r.json().get("source_url") or ""))
                else:
                    media_note = (f" (photo upload failed "
                                  f"{r.status_code})")

        body = ""
        if media_src:
            body += f'<p><img src="{media_src}" alt=""/></p>\n'
        body += "".join(
            f"<p>{ln}</p>\n" for ln in content.split("\n") if ln.strip())

        payload: dict[str, Any] = {
            "title": title,
            "content": body,
            "status": "publish" if publish else "draft",
            "tags": [tag_id] if tag_id else [],
        }
        if media_id:
            payload["featured_media"] = media_id
        r = _api(env, "POST", "posts", json=payload)
        if r.status_code not in (200, 201):
            return {"ok": False,
                    "error": f"post create failed {r.status_code}: "
                             f"{r.text[:160]}"}
        link = _https(r.json().get("link") or "")
        return {
            "ok": True,
            "id": r.json().get("id"),
            "url": link,
            "status": "publish" if publish else "draft",
            "media_url": media_src or None,
            "note": media_note or None,
            "tag_archive": f"{_PUBLIC_BASE}/tag/{person}/",
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
