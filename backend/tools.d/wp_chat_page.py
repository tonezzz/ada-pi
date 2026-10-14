"""wp_chat_page — create/update a member's private webchat page on the
boards WordPress site (card webchat-lane-boards).

The page lives under /chat/ (private status, same as /chat/line/*) and
embeds <ada-webchat-card> served by the member's Ada lane: the widget
opens ws://lane/ws?channel=web&chat_id=web-<member>&mode=text with the
member's invite-redeemed key, so chat turns run on the real lane with
persona, memory and tools — and outbound pushes are quota-free.

Env (wp-boards.env on the backend host): same as wp_post —
    WP_SITE_URL, WP_APP_USER, WP_APP_PASSWORD
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from backend import wp_client as wp

DECLARATION = {
    "name": "wp_chat_page",
    "description": (
        "Create/update a member's private webchat page on the boards WP "
        "site under /chat/ — embeds the ada-webchat widget on their "
        "lane. Returns the URL for chat."),
    "parameters": {
        "type": "object",
        "properties": {
            "member": {
                "type": "string",
                "description": (
                    "Member slug (e.g. tony, kk, nhong) — names the page "
                    "and its web-<member> chat_id for pushes."),
            },
            "lane": {
                "type": "string",
                "description": (
                    "Public base URL of the member's Ada lane, e.g. "
                    "https://ada-ha.surf-thailand.com — hosts /ws and "
                    "the card JS."),
            },
            "parent": {
                "type": "string",
                "description": "Parent page slug (default 'chat').",
            },
            "slug": {
                "type": "string",
                "description": ("Page slug under the parent (default "
                                "'web-<member>')."),
            },
            "status": {
                "type": "string",
                "enum": ["private", "publish", "draft"],
                "description": "Page status (default 'private').",
            },
        },
        "required": ["member", "lane"],
    },
}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

_PARENT_FALLBACK_BODY = (
    "<!-- wp:paragraph -->\n<p>Members' private chat pages live under "
    "this page.</p>\n<!-- /wp:paragraph -->")


def _embed_html(lane: str, member: str) -> str:
    return (
        "<!-- wp:html -->\n"
        f'<script src="{lane}/cards/ada-webchat-card.js"></script>\n'
        f'<ada-webchat-card lane="{lane}" member="{member}" '
        'style="height:75vh"></ada-webchat-card>\n'
        "<!-- /wp:html -->")


async def run(runner: Any, **args: Any) -> dict:
    member = str(args.get("member") or "").strip().lower()
    lane = str(args.get("lane") or "").strip().rstrip("/")
    parent_slug = str(args.get("parent") or "chat").strip().lower()
    slug = str(args.get("slug") or f"web-{member}").strip().lower()
    status = str(args.get("status") or "private").strip().lower()

    if not _SLUG_RE.match(member):
        return {"ok": False,
                "error": "member must be a slug ([a-z0-9-], e.g. 'kk')"}
    if not _SLUG_RE.match(slug) or not _SLUG_RE.match(parent_slug):
        return {"ok": False, "error": "slug/parent must be slugs"}
    if not re.match(r"^https://[^/]+$", lane):
        return {"ok": False,
                "error": "lane must be a full https base URL, no path "
                         "(e.g. https://ada-ha.surf-thailand.com)"}
    if status not in ("private", "publish", "draft"):
        return {"ok": False,
                "error": "status must be private|publish|draft"}

    env = wp.env()
    if not wp.configured(env):
        return {"ok": False,
                "error": "wp-boards.env missing WP_SITE_URL/"
                         "WP_APP_USER/WP_APP_PASSWORD on this host"}

    try:
        async with httpx.AsyncClient(timeout=30,
                                     follow_redirects=True) as client:
            parent = await wp.ensure_page(
                client, env, parent_slug, "Chat", _PARENT_FALLBACK_BODY,
                status="private")
            if not parent.get("ok"):
                return {"ok": False,
                        "error": f"parent page failed: "
                                 f"{parent.get('error')}"}
            res = await wp.ensure_page(
                client, env, slug, f"Chat — {member.title()} (web)",
                _embed_html(lane, member),
                parent=int(parent.get("id") or 0), status=status)
            if not res.get("ok"):
                return res
            return {
                "ok": True,
                "id": res.get("id"),
                "url": res.get("link"),
                "status": status,
                "created": res.get("created"),
                "chat_id": f"web-{member}",
                "note": ("Private WP page embedding the lane webchat "
                         "widget — the member redeems an invite link "
                         "once on the page, then chats on their lane."),
            }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
