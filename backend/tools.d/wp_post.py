"""wp_post — publish a LINE chat message as a post on the boards
WordPress site and hand back the link to send in chat.

The kk lane members chat in LINE ("post to my site: ..." or a photo with
a caption) and expect a live page back — card line-wp-site-manager
(OPS-0640). This tool does the deterministic half: ensure the member tag
exists, optionally upload archived LINE media, create the post, and
return the public URL. The REST plumbing lives in backend/wp_client.py.

Env (wp-boards.env on the backend host):
    WP_SITE_URL     — e.g. https://boards.surf-thailand.com/wp
    WP_APP_USER     — WP username the app password belongs to
    WP_APP_PASSWORD — application password (24 chars, revocable)
"""

from __future__ import annotations

from typing import Any

import httpx

from backend import wp_client as wp

DECLARATION = {
    "name": "wp_post",
    "description": (
        "Post a free-form entry to a member's WordPress page on the "
        "boards site (persons: tony, kk, nhong) — webboard/'ลงเว็บ' asks. "
        "Structured saves (expense/contact/reminder/asset) go to "
        "cpt_post. Returns the public URL."),
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


async def run(runner: Any, **args: Any) -> dict:
    person = str(args.get("person") or "").strip().lower()
    title = str(args.get("title") or "").strip()
    content = str(args.get("content") or "").strip()
    publish = bool(args.get("publish", True))
    media_url = str(args.get("media_url") or "").strip()

    if person not in wp.PEOPLE:
        return {"ok": False, "error": f"person must be one of {wp.PEOPLE}"}
    if not title or not content:
        return {"ok": False, "error": "title and content required"}
    env = wp.env()
    if not wp.configured(env):
        return {"ok": False,
                "error": "wp-boards.env missing WP_SITE_URL/"
                         "WP_APP_USER/WP_APP_PASSWORD on this host"}

    try:
        async with httpx.AsyncClient(timeout=30,
                                     follow_redirects=True) as client:
            tag_id = await wp.ensure_tag(client, env, person)
            if not tag_id:
                return {"ok": False,
                        "error": f"tag create failed for {person}"}

            media_id, media_src, media_note = None, "", ""
            if media_url:
                local = wp.media_local_path(media_url)
                if not local:
                    media_note = " (photo skipped — not found in archive)"
                else:
                    media_id, media_src = await wp.upload_media(
                        client, env, local)
                    if not media_id:
                        media_note = " (photo upload failed)"

            body = (f'<p><img src="{media_src}" alt=""/></p>\n'
                    if media_src else "") + wp.paragraphs(content)

            res = await wp.create_post(
                client, env, title, body, tag_ids=[tag_id] if tag_id else [],
                status="publish" if publish else "draft",
                featured_media=media_id)
            if not res.get("ok"):
                return res
            return {
                "ok": True,
                "id": res.get("id"),
                "url": res.get("link"),
                "status": "publish" if publish else "draft",
                "media_url": media_src or None,
                "note": media_note or None,
                "tag_archive": f"{wp.PUBLIC_BASE}/index.php/tag/{person}/",
            }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
