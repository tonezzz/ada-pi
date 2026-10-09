"""Voice FX — sci-fi narration layer for Ada sessions.

When enabled for a caller, selected tool calls and link events emit a
`bark` websocket event; the client speaks it through speechSynthesis
(flat machine voice — deliberately not Ada's voice, same channel as
boot/system notices).

Idea 2026-10-09 (Tony): "Accessing Level 1 memory" style cues, expanded —
system-operation notifications are their own class.

Feature flags (per caller, ~/.config/ada/voice-fx.json):
  sci_fx   — MASTER: off silences every bark
  sys_ops  — system-operation notices (cast, camera, dispatch, relink)
Memory barks are always-on under the master; ops barks need sys_ops too.
Defaults: all on. "sci-fi mode off" kills everything; "system operation
on/off" toggles the ops class via the voice_fx tool.
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path
from typing import Any

_FX_PATH = Path.home() / ".config" / "ada" / "voice-fx.json"
_lock = threading.Lock()

DEFAULT_FX = {"sci_fx": True, "sys_ops": True}

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

# Memory tiers — class "memory" (master gate only)
_MEM_POOLS = {
    1: [
        "Accessing level one memory.",
        "Scanning surface memory banks.",
        "Level one recall in progress.",
    ],
    2: [
        "Accessing level two memory — session archive.",
        "Descending to level two. Session records.",
        "Level two archive engaged.",
    ],
    3: [
        "Accessing level three memory — restricted archive.",
        "Opening sealed archive, level three.",
        "Deep storage. Clearance verified.",
    ],
}

# System operations — class "sys_ops" (master + sys_ops gate)
_OPS_POOLS = {
    "cast": [
        "Engaging display uplink.",
        "Cast channel open. Routing media.",
        "Display uplink established.",
    ],
    "camera": [
        "Optical sensor acquiring.",
        "Frame capture in progress.",
        "Visual feed sampled.",
    ],
    "dispatch": [
        "Dispatching autonomous agent.",
        "Devin unit deployed.",
        "Task handed to the machine layer.",
    ],
    "relink": [
        "Re-establishing neural link.",
        "Uplink interrupted — reconnecting.",
        "Signal lost. Reacquiring.",
    ],
    "control": [
        "Home system actuation.",
        "Physical layer command sent.",
        "Device state changing.",
    ],
    "cctv": [
        "Surveillance grid queried.",
        "Camera wall status check.",
    ],
}


def _match(name: str, args: dict[str, Any]) -> tuple[str, list[str]] | None:
    """Tool call -> (class, pool). Memory first (its own tiers); ops tools
    map to themed pools. Extend pools here — one line, no call sites."""
    a = args or {}
    bank = str(a.get("bank") or "").lower()
    scope = str(a.get("scope") or "").lower()
    action = str(a.get("action") or "").lower()

    if name in ("ada_memory_search", "memory_search"):
        if a.get("include_inactive") or bank in _ARCHIVAL_BANKS:
            return "memory", _MEM_POOLS[3]
        if scope == "sessions":
            return "memory", _MEM_POOLS[2]
        return "memory", _MEM_POOLS[1]
    if name == "ada_session_recall":
        return "memory", _MEM_POOLS[2]

    if name in ("yt",) and action in ("cast", "play"):
        return "sys_ops", _OPS_POOLS["cast"]
    if name == "cast_to_screen" and action in ("play", "image", "cast"):
        return "sys_ops", _OPS_POOLS["cast"]
    if name in ("ada_camera_snapshot", "cctv_snapshot"):
        return "sys_ops", _OPS_POOLS["camera"]
    if name == "cctv_wall" and action in ("status", "wall"):
        return "sys_ops", _OPS_POOLS["cctv"]
    if name == "devin" and action in ("dispatch", "followup"):
        return "sys_ops", _OPS_POOLS["dispatch"]
    if name == "control_entity" and action in (
            "turn_on", "turn_off", "toggle", "open_cover", "close_cover",
            "lock", "unlock", "media_play", "media_pause"):
        return "sys_ops", _OPS_POOLS["control"]
    return None


def bark_for(name: str, args: dict[str, Any] | None,
             caller: str | None) -> str | None:
    """The bark line to speak for this tool call, or None.
    sci_fx is the master gate; sys_ops-class barks need sys_ops too."""
    flags = get(caller)
    if not flags.get("sci_fx"):
        return None
    found = _match(name, args or {})
    if not found:
        return None
    cls, pool = found
    if cls == "sys_ops" and not flags.get("sys_ops"):
        return None
    return random.choice(pool)


def bark_event(kind: str, caller: str | None) -> str | None:
    """Non-tool events (link state) — relink only, for now."""
    flags = get(caller)
    if not flags.get("sci_fx") or not flags.get("sys_ops"):
        return None
    pool = _OPS_POOLS.get(kind)
    return random.choice(pool) if pool else None
