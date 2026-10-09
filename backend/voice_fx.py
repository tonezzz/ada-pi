"""Voice FX — sci-fi narration layer for Ada sessions.

When enabled for a caller, selected tool calls emit a `bark` websocket
event; the client speaks it through speechSynthesis (flat machine voice —
deliberately not Ada's voice, same channel as boot/system notices).

Idea 2026-10-09 (Tony): "Accessing Level 1 memory" style cues when Ada
reaches into deeper memory tiers; the on/off + insertion pattern is a
standard for Ada interactions and should evolve.

Persistence: ~/.config/ada/voice-fx.json — {"<caller>": {"sci_fx": bool}}.
Default is ON; say "sci-fi mode off" / "turn off memory narration" and the
voice_fx tool flips it.
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any

_FX_PATH = Path.home() / ".config" / "ada" / "voice-fx.json"
_lock = threading.Lock()

DEFAULT_FX = {"sci_fx": True}

# ---------------------------------------------------------------- prefs

def _load() -> dict[str, Any]:
    try:
        return json.loads(_FX_PATH.read_text())
    except Exception:
        return {}


def _save(d: dict[str, Any]) -> None:
    _FX_PATH.parent.mkdir(parents=True, exist_ok=True)
    _FX_PATH.write_text(json.dumps(d, indent=2, ensure_ascii=False))


def get(caller: str | None) -> dict[str, bool]:
    """Feature flags for a caller — absent callers get defaults."""
    if not caller:
        return dict(DEFAULT_FX)
    with _lock:
        entry = _load().get(caller) or {}
    out = dict(DEFAULT_FX)
    out.update({k: bool(v) for k, v in entry.items() if k in DEFAULT_FX})
    return out


def set_feature(caller: str | None, feature: str, enabled: bool) -> dict:
    if not caller:
        return {"ok": False, "error": "no caller identity on this session"}
    if feature not in DEFAULT_FX:
        return {"ok": False,
                "error": f"unknown feature {feature!r} "
                         f"(have: {sorted(DEFAULT_FX)})"}
    with _lock:
        d = _load()
        d.setdefault(caller, {})[feature] = bool(enabled)
        _save(d)
    return {"ok": True, "caller": caller, "feature": feature,
            "enabled": bool(enabled)}


# ---------------------------------------------------------------- barks

_ARCHIVAL_BANKS = {"devin", "chaba-archive", "ops-scenarios"}

# (level, pools) — first matching rule wins. Pools keep it evolving: add a
# line, never edit call sites.
_RULES = [
    # deeper first: archival dumps / explicit archive banks
    (3, [
        "Accessing level three memory — restricted archive.",
        "Opening sealed archive, level three.",
        "Deep storage. Clearance verified.",
    ]),
    (2, [
        "Accessing level two memory — session archive.",
        "Descending to level two. Session records.",
        "Level two archive engaged.",
    ]),
    (1, [
        "Accessing level one memory.",
        "Scanning surface memory banks.",
        "Level one recall in progress.",
    ]),
]


def _level_and_pool(name: str, args: dict[str, Any]) -> tuple[int, list[str]] | None:
    """Map a tool call to a bark level. Memory-tier tiers:
    L1 = live banks (fast recall), L2 = session/deep summaries,
    L3 = restricted archival dumps. Other tools get themed pools too —
    extend this map, don't hardcode elsewhere."""
    a = args or {}
    bank = str(a.get("bank") or "").lower()
    scope = str(a.get("scope") or "").lower()
    if name in ("ada_memory_search", "memory_search"):
        if a.get("include_inactive") or bank in _ARCHIVAL_BANKS:
            return 3, _RULES[0][1]
        if scope == "sessions":
            return 2, _RULES[1][1]
        return 1, _RULES[2][1]
    if name == "ada_session_recall":
        return 2, _RULES[1][1]
    return None


def bark_for(name: str, args: dict[str, Any] | None,
             caller: str | None) -> str | None:
    """The bark line to speak for this tool call, or None. Caller flag
    gate lives here so every emit path gets the same behavior."""
    if not get(caller).get("sci_fx"):
        return None
    found = _level_and_pool(name, args or {})
    if not found:
        return None
    _level, pool = found
    return random.choice(pool)
