"""percussion — control the Nest sonify page (style/mood/instruments).

Ada voice: "make percussion funky", "give it blue chords", "add bass",
"play polyphia". POSTs to the input-bridge /percussion endpoint; the
web app polls control on its 30s state cadence and the groove changes
on the next bar. No restart, no page reload.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

BRIDGE = "https://tony-dell.taila0626a.ts.net/api/input-bridge"
STYLES = {"rock","ballad","funky","electronic","edm",
          "hardrock","rockballad","polyphia"}
MOODS = {"blue","bright","tense","calm"}
INS = {"bass","stab","pad","lead","guitar","clap","ride","tamb","rim"}

DECLARATION = {
    "name": "percussion",
    "description": (
        "Nest percussion display music layer: style/mood/instruments. "
        "See tool_guide."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "style": {"type": "string",
                      "description": "Style preset name, or  to clear."},
            "mood": {"type": "string",
                     "description": "Harmonic mood: blue/bright/tense/calm."},
            "instruments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Instrument layers to add — guitar gives "
                               "riffs+bends (polyphia/hardrock styles)."},
            "action": {"type": "string",
                       "description": "status (default) or set."},
        },
    },
}


def _post(body: dict) -> dict:
    req = urllib.request.Request(
        BRIDGE + "/percussion", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    action = str(args.get("action") or "set")
    style = str(args.get("style", "")).strip().lower()
    mood = str(args.get("mood", "")).strip().lower()
    ins = args.get("instruments")
    body = {}
    if "style" in args:
        if style and style not in STYLES:
            return {"ok": False, "error":
                    f"unknown style {style!r} — "
                    + "/".join(sorted(STYLES))}
        body["style"] = style
    if "mood" in args and mood:
        if mood not in MOODS:
            return {"ok": False, "error":
                    f"unknown mood {mood!r} — " + "/".join(sorted(MOODS))}
        body["mood"] = mood
    if isinstance(ins, list):
        bad = [i for i in ins if i not in INS]
        if bad:
            return {"ok": False, "error":
                    f"unknown instruments {bad} — " + "/".join(sorted(INS))}
        body["instruments"] = ins
    try:
        res = _post(body)
    except Exception as e:
        return {"ok": False, "error": f"percussion bridge unreachable: {e}"}
    c = res.get("control", {})
    return {"ok": True,
            "output": (f"percussion: {c.get(style) or sonify} "
                       f"+ {c.get(mood) or nomood} "
                       f"+ {c.get(instruments) or []}"),
            "control": c}
