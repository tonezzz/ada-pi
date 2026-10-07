"""Shared client for chaba's kanban board-api — the single reach path.

board-api (chaba scripts/board/board-api.py) binds 127.0.0.1:8787 on
tony-dell behind the Caddy /apps/board-api route. Write auth is the
request's tailnet identity or a direct loopback caller — no Ada key ever
crosses this path (card board-api-auth), so every caller — tools.d
tools, pwa_server routes, scripts — MUST come through this module rather
than bolting on a second credential scheme.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

_BASE_URL_ENV = "ADA_BOARD_API_URL"
_DEFAULT_BASE_URL = "https://tony-dell.taila0626a.ts.net/apps/board-api"
_BOARD_PAGE_URL = "https://tony-dell.taila0626a.ts.net/apps/board/"


def base_url() -> str:
    return (os.environ.get(_BASE_URL_ENV) or _DEFAULT_BASE_URL).rstrip("/")


def board_page_url() -> str:
    """Human-facing board URL — derived from the api base when it is the
    Caddy /apps/board-api route, else the canonical tailnet location."""
    base = base_url()
    if base.endswith("/apps/board-api"):
        return base.removesuffix("-api") + "/"
    return _BOARD_PAGE_URL


async def request(client: httpx.AsyncClient, method: str, path: str,
                  **kw: Any) -> tuple[dict[str, Any] | None, str | None, int]:
    """One board-api call -> (data, err, http_status).

    status 0 means the board was unreachable — err carries a short
    reason and data is None. On HTTP >= 400 err holds the server's
    'error' field (or a synthesized message).

    Tailscale-User-Login labels the caller 'ada' — it's the write gate's
    identity label, not a credential: the Caddy route only reaches
    board-api from tailnet peers, and every comms line lands under
    'ada' regardless. Without it non-GET 403s at the edge."""
    headers = dict(kw.pop("headers", None) or {})
    headers["Tailscale-User-Login"] = "ada"
    kw["headers"] = headers
    try:
        resp = await client.request(method, f"{base_url()}{path}", **kw)
    except (httpx.HTTPError, TimeoutError) as exc:
        return None, (f"I couldn't reach the board "
                      f"({exc.__class__.__name__})"), 0
    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if resp.status_code >= 400:
        return (None,
                str(data.get("error") or
                    f"the board returned HTTP {resp.status_code}"),
                resp.status_code)
    return data, None, resp.status_code


async def post(path: str, body: dict[str, Any],
               timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
    """POST to board-api -> (http_status, parsed_json).

    status 0 means unreachable; the dict then carries {"error": ...}.
    The raw response body is preserved so callers can read fields like
    'message' alongside 'error'."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        data, err, status = await request(client, "POST", path, json=body)
    if data is None:
        data = {"error": err} if err else {}
    elif err:
        data.setdefault("error", err)
    return status, data
