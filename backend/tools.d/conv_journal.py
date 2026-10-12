"""conv_journal — build a conversation-report page from the LINE relay's
per-room journal + media archive.

The relay journals every inbound/outbound text turn and archives
inbound media under LINE_MEDIA_ARCHIVE_DIR/<chat>/ (journal-YYYY-MM-DD
.jsonl + manifest.jsonl). This tool compiles one day's entries into a
markdown report — text lines plus embedded media URLs — and upserts it
into the lane CMS collection as kind=report, public unset. The
collection is enforced by construction (tool_runner.common
CMS_COLLECTION / ADA_CMS_COLLECTION) — conversation reports can never
land in another lane's CMS, and they are never site-published.

Reviewable on the CMS miniapp, recallable via cms_read/search — the
memory-scheme home for "what did we talk about".

action='build' (default): compile the report for chat+date.
action='list': archive chats with journal days + entry counts.

Env:
    LINE_MEDIA_ARCHIVE_DIR — must match the relay (default
        ~/.local/share/ada/line-media)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.tool_runner.common import CMS_COLLECTION

ARCHIVE_DIR = Path(os.environ.get(
    "LINE_MEDIA_ARCHIVE_DIR",
    os.path.expanduser("~/.local/share/ada/line-media")))

DECLARATION = {
    "name": "conv_journal",
    "description": (
        "Build a conversation report page (text + media) from the LINE "
        "room journal into the CMS — recall 'what did we discuss' later. "
        "action='build' compiles it; 'list' shows journal days."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["build", "list"],
                "description": "build (default) compiles the report; "
                               "list shows archived chats/days.",
            },
            "chat": {
                "type": "string",
                "description": "Room dir name under the archive (see "
                               "action='list'); default = first chat "
                               "found.",
            },
            "date": {
                "type": "string",
                "description": "YYYY-MM-DD journal day; default today.",
            },
            "title": {
                "type": "string",
                "description": "Optional report title override.",
            },
        },
        "additionalProperties": False,
    },
}


def _list_chats() -> list[dict[str, Any]]:
    out = []
    for d in sorted(ARCHIVE_DIR.iterdir()) if ARCHIVE_DIR.is_dir() else []:
        if not d.is_dir():
            continue
        days = {}
        for j in d.glob("journal-*.jsonl"):
            day = j.name[len("journal-"):-len(".jsonl")]
            try:
                days[day] = sum(1 for _ in j.open())
            except Exception:
                days[day] = -1
        media = sum(1 for f in d.iterdir()
                    if f.is_file() and not f.name.startswith("journal-"))
        out.append({"chat": d.name, "days": days, "media_files": media})
    return out


def _compose(entries: list[dict], chat: str, day: str) -> str:
    lines = [f"# Conversation — {chat} — {day}", "",
             f"Compiled {datetime.now().isoformat(timespec='seconds')} "
             f"from {len(entries)} journal entries.", ""]
    for e in entries:
        ts = str(e.get("ts") or "")[-8:]
        who = e.get("from") or e.get("sender") or "?"
        kind = e.get("kind") or "text"
        if kind == "text":
            arrow = "→" if e.get("dir") == "out" else "•"
            lines.append(f"- `{ts}` {arrow} **{who}**: "
                         f"{str(e.get('text') or '').strip()}")
        elif e.get("url"):
            lines.append(f"- `{ts}` **{who}** sent {kind}:")
            lines.append(f"  ![]({e['url']})")
        else:
            lines.append(f"- `{ts}` **{who}** sent {kind}")
    return "\n".join(lines) + "\n"


async def run(runner, **args) -> dict[str, Any]:
    action = str(args.get("action") or "build").strip().lower()
    if action == "list":
        chats = _list_chats()
        if not chats:
            return {"ok": True, "chats": [],
                    "note": "archive empty — no journaled rooms yet"}
        return {"ok": True, "chats": chats}
    if action != "build":
        return {"ok": False, "error": f"unknown action {action!r} "
                "— expected build|list"}
    chat = str(args.get("chat") or "").strip()
    if not chat:
        chats = _list_chats()
        if not chats:
            return {"ok": False,
                    "error": "no archived rooms yet — the relay writes "
                             "journal files as messages flow"}
        chat = chats[0]["chat"]
    day = str(args.get("date") or
              datetime.now().strftime("%Y-%m-%d")).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return {"ok": False, "error": "date must be YYYY-MM-DD"}
    jf = ARCHIVE_DIR / chat / f"journal-{day}.jsonl"
    if not jf.is_file():
        return {"ok": False,
                "error": f"no journal for {chat} on {day} — try "
                         "action='list' to see what days exist"}
    entries = []
    for line in jf.open():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
    if not entries:
        return {"ok": False, "error": f"journal {chat}/{day} is empty"}
    key = f"conv-{chat[:24].lower()}-{day}"
    title = str(args.get("title") or
                f"Conversation {chat[:16]} {day}")
    content = _compose(entries, chat, day)
    meta = {
        "kind": ["report"], "domain": ["journal"], "slug": [key],
        "title": [title], "subject": [key],
        "summary": [f"{len(entries)} journal entries for {chat} on "
                    f"{day}"],
        "status": ["active"], "format": ["markdown"],
        "checked_at": [day], "scope": ["conversation"],
        "written_by": ["conv_journal"],
        "entries": [str(len(entries))],
        # no 'public' — conversation reports are never site-published
    }
    mddb = getattr(getattr(runner, "context", None), "mddb", None)
    if mddb is None:
        return {"ok": False, "error": "mddb unavailable — cannot write "
                "the report page"}
    try:
        await mddb.update_document(
            CMS_COLLECTION, key, "en", content_md=content, meta=meta,
            tool="conv_journal")
    except Exception as exc:
        return {"ok": False,
                "error": f"report write failed: {exc}"}
    return {"ok": True, "key": key, "collection": CMS_COLLECTION,
            "entries": len(entries), "title": title,
            "note": f"report saved — recall it later with "
                    f"cms_read get {key}"}
