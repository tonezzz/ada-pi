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

import re
from typing import Any

import httpx

from backend import board_client

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
                "description": "'comment' (default), 'respond', 'create', "
                               "'move', or 'read'.",
            },
            "column": {
                "type": "string",
                "description": "Target column for action=move "
                               "(backlog|doing|review|done).",
            },
            "evidence": {
                "type": "string",
                "description": "Required for action=move column=done — "
                               "the checkable fact you verified "
                               "(e.g. 'service active', 'page live'). "
                               "Logged as a comment so the audit trail "
                               "shows why it closed.",
            },
            "title": {
                "type": "string",
                "description": "Card title (action=create).",
            },
            "note": {
                "type": "string",
                "description": "One-line card note (action=create).",
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
            "report": {
                "type": "string",
                "description": "CMS page slug for action=read — returns "
                               "the card whose 'report' field links it "
                               "(the report-opinion lookup).",
            },
        },
    },
}

_CARD_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$", re.IGNORECASE)
_READ_LIMIT_DEFAULT = 12
_READ_LIMIT_MAX = 40
_TEXT_MAX = 480  # server truncates comms/answers at 500
_TITLE_MAX = 80


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
    data, err, _status = await board_client.request(
        client, method, path, **kw)
    return data, err


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


async def _create(args: dict[str, Any]) -> dict[str, Any]:
    """Drop a new card onto the board — the voice-side capture of the
    request lifecycle. Any caller the tool's policy admits may create;
    the card lands in backlog for triage, comms record 'ada'."""
    title = str(args.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "a card title is required"}
    body: dict[str, Any] = {"title": title[:_TITLE_MAX * 2],
                            "from": "ada"}
    note = str(args.get("note") or "").strip()
    if note:
        body["note"] = note[:_TEXT_MAX]
        body["text"] = note[:_TEXT_MAX]
    col = str(args.get("column") or "").strip().lower()
    if col:
        body["column"] = col
    async with httpx.AsyncClient(timeout=10.0) as client:
        data, err = await _request(client, "POST", "/card", json=body)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True,
            "message": (data or {}).get("message", "card created"),
            "note": "the card is on the board in backlog — triage picks "
                    "it up from there"}


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
    if c.get("report"):
        row["report"] = str(c["report"])
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


def _match_report_card(cards: list[dict[str, Any]], slug: str
                       ) -> dict[str, Any]:
    """Opinion-loop lookup: find the kanban cards whose `report` field
    links a CMS page slug. Open (non-'done' column) cards outrank done
    ones; within each tier the most recently `updated` wins. Returns
    {'open': [...], 'done': [...]}, each sorted updated-desc."""
    want = slug.strip().lower()
    open_, done = [], []
    for c in cards:
        if str(c.get("report") or "").strip().lower() != want:
            continue
        col = str(c.get("column") or "backlog")
        (done if col == "done" else open_).append(c)
    key = lambda c: str(c.get("updated") or "")
    open_.sort(key=key, reverse=True)
    done.sort(key=key, reverse=True)
    return {"open": open_, "done": done}


def _report_lookup(cards: list[dict[str, Any]], slug: str
                   ) -> dict[str, Any]:
    """action=read report=<slug> — returns the single best card linking
    the slug plus counts so the model can narrate ambiguity aloud or
    offer to file a card when nothing links it."""
    match = _match_report_card(cards, slug)
    open_, done = match["open"], match["done"]
    out: dict[str, Any] = {
        "ok": True,
        "report": slug.strip().lower(),
        "open_matches": len(open_),
        "done_matches": len(done),
    }
    if open_:
        out["card"] = _compact_card(open_[0])
        if len(open_) > 1:
            out["also"] = [str(c.get("id")) for c in open_[1:]]
            out["note"] = (
                "several open cards link this report — the most "
                "recently updated was picked; say which card got the "
                "comment")
    elif done:
        out["card"] = _compact_card(done[0])
        out["note"] = (
            "only done cards link this report — the comment still "
            "lands on it, or offer to file a fresh card "
            "(action='create')")
    else:
        out["card"] = None
        out["note"] = (
            "no card links this report — answer aloud and offer to "
            "file one (action='create')")
    return out


_COLUMNS = {"backlog", "doing", "review", "done"}
_DOING_CAP = 2  # same soft cap as agent sessions


async def _move(args: dict[str, Any]) -> dict[str, Any]:
    """action=move — triage authority, not judgment. Ada may move
    backlog<->doing and close with evidence; priority:high and
    review_kind:decide stay Tony's."""
    cid = _card_id(args)
    col = str(args.get("column") or "").strip().lower()
    evidence = str(args.get("evidence") or "").strip()
    if not cid:
        return {"ok": False, "error": "a valid card id is required"}
    if col not in _COLUMNS:
        return {"ok": False,
                "error": f"column must be one of {sorted(_COLUMNS)}"}
    if col == "done" and not evidence:
        return {"ok": False,
                "error": "closing needs evidence — what did you verify "
                         "(service live, page rendered, test passed)? "
                         "If nothing is checkable, comment instead"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        data, err = await _request(client, "GET", "/cards")
        if err:
            return {"ok": False, "error": err}
        assert data is not None
        cards = data.get("cards") or []
        card = next((c for c in cards if c.get("id") == cid), None)
        if not card:
            return {"ok": False, "error": f"no card {cid}"}
        if card.get("priority") == "high":
            return {"ok": False,
                    "error": "priority:high cards stay Tony's — "
                             "comment your finding instead"}
        if card.get("review_kind") == "decide":
            return {"ok": False,
                    "error": "review_kind:decide cards need Tony's "
                             "judgment — comment instead"}
        if col == "doing":
            doing = sum(1 for c in cards
                        if c.get("column") == "doing" and c.get("id") != cid)
            if doing >= _DOING_CAP:
                return {"ok": False,
                        "error": f"{doing} cards already doing — the "
                                 f"cap is {_DOING_CAP}; finish or hold "
                                 "one first"}
        if evidence:
            cdata, cerr = await _request(
                client, "POST", "/comment",
                json={"id": cid, "from": "ada",
                      "text": f"verified: {evidence[:400]}"})
            if cerr:
                return {"ok": False, "error": cerr}
        data, err = await _request(
            client, "POST", "/action",
            json={"id": cid, "do": "move", "column": col, "from": "ada"})
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "id": cid, "column": col,
            "message": (data or {}).get("message", f"moved to {col}")}


async def _read(args: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        data, err = await _request(client, "GET", "/cards")
    if err:
        return {"ok": False, "error": err}
    assert data is not None
    slug = str(args.get("report") or "").strip()
    if slug:
        return _report_lookup(data.get("cards") or [], slug)
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
    if action == "create":
        return await _create(args)
    if action == "move":
        return await _move(args)
    return {"ok": False,
            "error": f"unknown action {action!r} — use comment, respond, "
                     "create, move, or read"}
