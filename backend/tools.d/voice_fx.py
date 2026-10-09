"""voice_fx — sci-fi narration toggle (per caller).

Ada narration layer: when sci_fx is on, deep-memory tool calls emit a
`bark` ws event the client speaks in a flat machine voice ("Accessing
level one memory…"). Voice commands like "sci-fi mode off" / "turn off
memory narration" land here. Per-caller, persisted in
~/.config/ada/voice-fx.json via backend.voice_fx.
"""

from __future__ import annotations

from typing import Any

from backend import voice_fx

DECLARATION = {
    "name": "voice_fx",
    "description": (
        "Sci-fi narration / voice FX settings — the mechanical 'Accessing "
        "level N memory' style asides. action='status' shows the caller's "
        "flags; 'set' toggles (feature='sci_fx', enabled=true/false). "
        "Voice triggers: 'sci-fi mode on/off', 'memory narration on/off'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "'status' (default) or 'set'.",
            },
            "feature": {
                "type": "string",
                "description": "Flag to change — 'sci_fx'.",
            },
            "enabled": {
                "type": "boolean",
                "description": "New value for action=set.",
            },
        },
    },
}


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    caller = getattr(runner, "session_caller_name", None)
    action = str(args.get("action") or "status").strip().lower()
    if action == "status":
        flags = voice_fx.get(caller)
        return {"ok": True, "caller": caller, "flags": flags,
                "output": "sci-fi narration " +
                          ("on" if flags.get("sci_fx") else "off")}
    if action == "set":
        res = voice_fx.set_feature(
            caller,
            str(args.get("feature") or "sci_fx").strip(),
            bool(args.get("enabled")),
        )
        if res.get("ok"):
            res["output"] = (f"{res['feature']} "
                             + ("enabled" if res["enabled"]
                                else "disabled"))
        return res
    return {"ok": False, "error": f"unknown action {action!r} — use "
                                  "'status' or 'set'"}
