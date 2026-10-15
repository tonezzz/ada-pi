"""Per-caller 'last rendered artifact' memory (card render-flow-quality-pass).

tools.d modules load by file path, so a module-level dict inside
ada_render_image/ada_render_video would be private to each — this tiny
backend module holds the shared store so the image tool remembers its
renders and the video tool can animate the same artifact
('ref_url=last').

Keying: a caller is "the same person asking" — the relay chat binding
first (survives ws reconnects; group chats share one key across
members, so the session id is stored too and wins when both exist),
then the per-call session id. Entries carry the published URL and the
local artifact path; resolving 'last' prefers the on-disk file (no
network hop, no dependency on the bridge serving the URL).
"""

from __future__ import annotations

import mimetypes
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import logging

logger = logging.getLogger("tools.render_state")

# ref_url spellings that mean "the thing I just made".
LAST_ALIASES = {"last", "latest", "previous", "prev", "same"}

_CAP = int(os.environ.get("ADA_RENDER_LAST_CAP", "128"))
_TTL_S = float(os.environ.get("ADA_RENDER_LAST_TTL_S", "7200"))
# Auto-feed ("make it warmer" reroll prevention) uses a shorter window —
# an hours-old render is not what an ambiguous edit cue means.
AUTO_TTL_S = float(os.environ.get("ADA_RENDER_AUTO_REF_TTL_S", "900"))

# key -> {"kind","url","file","prompt","at"}
_LAST: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _caller_keys(runner: Any) -> list[str]:
    """Stable 'same asker' keys: bound relay chats, then session id."""
    keys: list[str] = []
    try:
        from backend.tool_runner.common import session_chat_id
        for ch in ("line", "telegram"):
            cid = session_chat_id(runner, ch)
            if cid:
                keys.append(f"chat:{ch}:{cid}")
    except Exception:
        pass
    sid = ""
    try:
        sid = str(getattr(getattr(runner, "context", None),
                          "session_id", "") or "")
    except Exception:
        sid = ""
    if not sid or sid.startswith("<"):
        try:
            sid = str(getattr(runner, "session_id", "") or "")
        except Exception:
            sid = ""
    if sid and not sid.startswith("<"):
        keys.append(f"sess:{sid}")
    return keys


def remember(runner: Any, info: dict[str, Any]) -> None:
    """Record a finished render under every caller key. Never raises."""
    try:
        keys = _caller_keys(runner)
        if not keys:
            return
        ent = dict(info)
        ent["at"] = time.time()
        for k in keys:
            _LAST[k] = ent
            _LAST.move_to_end(k)
        while len(_LAST) > _CAP:
            _LAST.popitem(last=False)
    except Exception as exc:
        logger.info("render remember skipped: %s", exc)


def last(runner: Any, kinds: set[str] | None = None,
         max_age_s: float | None = None) -> dict[str, Any] | None:
    """Most recent remembered render for this caller, or None."""
    horizon = _TTL_S if max_age_s is None else max_age_s
    now = time.time()
    best: dict[str, Any] | None = None
    for k in _caller_keys(runner):
        ent = _LAST.get(k)
        if not ent or now - ent.get("at", 0.0) > horizon:
            continue
        if kinds and ent.get("kind") not in kinds:
            continue
        if best is None or ent.get("at", 0.0) > best.get("at", 0.0):
            best = ent
    return best


def resolve_last(runner: Any, kinds: set[str] | None = None
                 ) -> tuple[tuple[str, Any] | None, str | None]:
    """-> (("bytes", (data, mime)) | ("url", url) | None, error).

    Prefers the on-disk artifact — the published /frame URL also works
    but costs an HTTP hop and dies if the bridge dropped the asset.
    """
    ent = last(runner, kinds=kinds)
    if not ent:
        return None, ("nothing rendered this session to iterate on — "
                      "render a fresh one or pass a ref_url")
    f = str(ent.get("file") or "")
    if f and Path(f).is_file():
        try:
            p = Path(f)
            if p.stat().st_size <= 15 * 1024 * 1024:
                mime = mimetypes.guess_type(f)[0] or "image/png"
                return ("bytes", (p.read_bytes(), mime)), None
        except OSError as exc:
            logger.info("last-render file read failed: %s", exc)
    url = str(ent.get("url") or "")
    if url:
        return ("url", url), None
    return None, ("the session's last render isn't fetchable anymore — "
                  "render a fresh one")


def clear() -> None:
    """Test hook."""
    _LAST.clear()
