"""ada_board_write — drop-in tool.

Ada participates in the kanban comms loop herself: posts comments on
cards as 'ada', answers open card requests, and reads compact board
state to find card and request ids. Backed by the board-api on
tony-dell — POST /apps/board-api/{comment,respond}, GET .../cards
(scripts/board/board-api.py in chaba-tony-dell).

Policy split (manifest policy can't vary per action):
- comment/read — 'read' policy, any identified session: outbound comms
  are auditable on the board, same exposure as chat_send.
- respond — owner-gated here: /respond flips a request to 'answered'
  and the server logs the comms line under 'tony' (from is hardcoded
  server-side; 'ada' is passed for forward-compat). Only identities
  with {full: true} person policy may speak a request closed.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

DECLARATION = {
    "name": "ada_board_write",
    "description": (
        "Post to the kanban board's comms loop as 'ada' — action='comment'|"
        "'respond'|'read'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "'comment' (default), 'respond', or 'read'.",
            },
            "id": {
                "type": "string",
                "description": "Card id, e.g. 'mha-log-noise'.",
            },
            "text": {
                "type": "string",
                "description": "Comment body (action=comment).",
            },
            "request_id": {
                "type": "string",
                "description": "Id of the card's open request (action=respond).",
            },
            "answer": {
                "type": "string",
                "description": "Answer text for the request (action=respond).",
            },
            "column": {
                "type": "string",
                "description": "Optional column filter for action=read.",
            },
            "limit": {
                "type": "integer",
                "description": "Max cards for action=read (default 12).",
            },
        },
    },
}

_BASE_URL_ENV = "ADA_BOARD_API_URL"
_DEFAULT_BASE_URL = "https://tony-dell.taila0626a.ts.net/apps/board-api"
_CARD_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$", re.IGNORECASE)
_READ_LIMIT_DEFAULT = 12
_READ_LIMIT_MAX = 40
_TEXT_MAX = 480  # server truncates comms/answers at 500
_TITLE_MAX = 80


def _base_url() -> str:
    return (os.environ.get(_BASE_URL_ENV) or _DEFAULT_BASE_URL).rstrip("/")


def _is_owner(runner: Any) -> bool:
    try:
        ident = runner.policy_identity()
        policy = runner.banks.policy_for(ident) or {}
    except Exception:
        return False
    return bool(policy.get("full"))


def _card_id(args: dict[str, Any]) -> str | None:
    cid = str(args.get("id") or "").strip()
    return cid if _CARD_ID.match(cid) else None


async def _request(client: httpx.AsyncClient, method: str, path: str,
                   **kw: Any) -> tuple[dict[str, Any] | None, str | None]:
    try:
        resp = await client.request(method, f"{_base_url()}{path}", **kw)
    except (httpx.HTTPError, TimeoutError) as exc:
        return None, f"I couldn't reach the board ({exc.__class__.__name__})"
    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if resp.status_code >= 400:
        return None, str(data.get("error") or
                         f"the board returned HTTP {resp.status_code}")
    return data, None


async def _comment(args: dict[str, Any]) -> dict[str, Any]:
    cid = _card_id(args)
    text = str(args.get("text") or "").strip()
    if not cid:
        return {"ok": False, "error": "a valid card id is required"}
    if not text:
        return {"ok": False, "error": "comment text is required"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        data, err = await _request(
            client, "POST", "/comment",
            json={"id": cid, "from": "ada", "text": text[:_TEXT_MAX]})
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "id": cid, "posted": text[:80],
            "message": (data or {}).get("message", "comment added")}


async def _respond(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    if not _is_owner(runner):
        return {"ok": False,
                "error": "answering board requests is restricted to the "
                         "owner — suggest the answer aloud instead"}
    cid = _card_id(args)
    rid = str(args.get("request_id") or "").strip()
    answer = str(args.get("answer") or "").strip()
    if not cid:
        return {"ok": False, "error": "a valid card id is required"}
    if not rid:
        return {"ok": False, "error": "request_id is required"}
    if not answer:
        return {"ok": False, "error": "answer text is required"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        data, err = await _request(
            client, "POST", "/respond",
            json={"id": cid, "request_id": rid,
                  "answer": answer[:_TEXT_MAX], "from": "ada"})
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "id": cid, "request_id": rid,
            "message": (data or {}).get("message", "answer saved"),
            "note": "the board logs respond comms under 'tony' — a "
                    "board-api limitation, not something you did wrong"}


_ASK_MAX = 100


def _compact_card(c: dict[str, Any]) -> dict[str, Any]:
    open_reqs = [r for r in (c.get("requests") or [])
                 if r.get("status") != "answered"]
    row: dict[str, Any] = {
        "id": c.get("id"),
        "title": str(c.get("title") or "")[:_TITLE_MAX],
        "column": c.get("column") or "backlog",
    }
    if c.get("updated"):
        row["updated"] = c["updated"]
    if open_reqs:
        row["open_requests"] = len(open_reqs)
        row["requests"] = [
            {"id": r.get("id"),
             "ask": str(r.get("ask") or "")[:_ASK_MAX]}
            for r in open_reqs
        ]
    comms = c.get("comms") or []
    if comms:
        last = comms[-1]
        row["last_comm"] = (
            f"{last.get('from', '?')}: "
            f"{str(last.get('text') or '')[:80]}")
    return row


async def _read(args: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        data, err = await _request(client, "GET", "/cards")
    if err:
        return {"ok": False, "error": err}
    assert data is not None
    want_col = str(args.get("column") or "").strip().lower()
    try:
        limit = int(args.get("limit") or _READ_LIMIT_DEFAULT)
    except (TypeError, ValueError):
        limit = _READ_LIMIT_DEFAULT
    limit = max(1, min(limit, _READ_LIMIT_MAX))
    cards = data.get("cards") or []
    columns: dict[str, int] = {}
    open_requests = 0
    rows = []
    for c in cards:
        col = str(c.get("column") or "backlog")
        columns[col] = columns.get(col, 0) + 1
        open_requests += sum(
            1 for r in (c.get("requests") or [])
            if r.get("status") != "answered")
        if want_col and col != want_col:
            continue
        rows.append(_compact_card(c))
    rows.sort(key=lambda r: str(r.get("updated") or ""), reverse=True)
    out: dict[str, Any] = {
        "ok": True,
        "columns": columns,
        "open_requests": open_requests,
        "total": len(rows),
        "cards": rows[:limit],
    }
    if len(rows) > limit:
        out["truncated"] = len(rows) - limit
    return out


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    action = str(args.get("action") or "comment").strip().lower()
    if action == "read":
        return await _read(args)
    if action == "comment":
        return await _comment(args)
    if action == "respond":
        return await _respond(runner, args)
    return {"ok": False,
            "error": f"unknown action {action!r} — use comment, respond, "
                     "or read"}
