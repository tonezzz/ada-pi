"""ada_look — drop-in tool (card ada-look-tool; nest-edge-vision phase 4).

Answers "what do you see" from the /apps/eye/ page: a browser tab runs
in-page object detection (zero server compute) and republishes its
latest frame's detections every ?pub=N seconds as the MDDB doc
ada-ha-scenario-reports/eye/latest — a markdown page whose ```json
block carries {ts, pub_s, src, model, detections[{cls,score,box}], ua}.

Read-only by design: it reports what the eye saw and never acts on it —
the confirm-gate model is preserved because acting on a detection is
another tool's call. Honest-empty is the contract: a missing doc means
no page is publishing (the chaba-side /apps/eye/ route and its mddb
edge path may not even be deployed yet), which is a normal answer —
never an error, never invented objects.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

DECLARATION = {
    "name": "ada_look",
    "description": (
        "What the /apps/eye/ page's in-browser detector sees right now "
        "— 'what do you see' while an eye page publishes. Honest-empty "
        "when none does; for an actual camera image use "
        "ada_camera_snapshot."
    ),
    "parameters": {"type": "object", "properties": {}},
}

_COLLECTION = "ada-ha-scenario-reports"
_KEY = "eye/latest"
_STALE_FACTOR = 3.0
# Payload pub_s wins; a doc too old to carry it gets this conservative
# interval — a live page rewrites far more often than this anyway.
_DEFAULT_PUB_S = 5.0
_JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.DOTALL)
_PLURALS = {"person": "people", "child": "children", "mouse": "mice"}


def _payload(content_md: str) -> dict[str, Any] | None:
    """Extract the ```json block; tolerate a doc that is bare JSON."""
    text = (content_md or "").strip()
    if not text:
        return None
    raw = None
    m = _JSON_BLOCK.search(text)
    if m:
        raw = m.group(1).strip()
    elif text.startswith("{"):
        raw = text
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _ts_epoch(raw: Any) -> float | None:
    """Publish timestamp — epoch seconds or ISO-8601."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _class_counts(detections: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in detections if isinstance(detections, list) else []:
        if not isinstance(d, dict):
            continue
        cls = str(d.get("cls") or d.get("label") or "object")
        counts[cls] = counts.get(cls, 0) + 1
    return dict(
        sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _plural(cls: str, n: int) -> str:
    if n == 1:
        return cls
    return _PLURALS.get(cls, cls + "s")


def _objects_phrase(counts: dict[str, int]) -> str:
    parts = [f"{n} {_plural(cls, n)}" for cls, n in counts.items()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _age_phrase(age_s: float) -> str:
    if age_s < 90:
        return f"{int(age_s)} seconds"
    if age_s < 5400:
        return f"{int(age_s / 60)} minutes"
    return f"{age_s / 3600:.1f} hours"


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    mddb = getattr(runner, "mddb", None)
    if mddb is None:
        return {"ok": False,
                "error": "my memory store isn't available in this mode"}
    doc = await mddb.get_document(_COLLECTION, _KEY)
    if doc is None:
        return {"ok": True, "live": False, "count": 0, "objects": {},
                "summary": "No eye page is publishing right now."}
    payload = _payload(str(doc.get("contentMd") or ""))
    if payload is None:
        return {"ok": True, "live": False, "count": 0, "objects": {},
                "summary": "No eye page is publishing right now."}
    if not payload:
        return {"ok": False,
                "error": ("an eye page doc exists but I couldn't read "
                          "its detection payload")}

    counts = _class_counts(payload.get("detections"))
    total = sum(counts.values())
    objects = _objects_phrase(counts)
    out: dict[str, Any] = {
        "ok": True, "live": True, "count": total, "objects": counts,
    }
    for k in ("src", "model"):
        if payload.get(k):
            out[k] = str(payload[k])

    ts = _ts_epoch(payload.get("ts"))
    try:
        pub_s = float(payload.get("pub_s") or payload.get("pub")
                      or _DEFAULT_PUB_S)
    except (TypeError, ValueError):
        pub_s = _DEFAULT_PUB_S
    if pub_s <= 0:
        pub_s = _DEFAULT_PUB_S
    out["pub_s"] = pub_s

    if ts is None:
        # No timestamp — freshness unknowable; report what's there and
        # say so rather than claiming 'right now'.
        out["freshness"] = "unknown"
        out["summary"] = (
            f"The eye page is publishing but its data has no timestamp"
            f" — it reports {objects}." if objects else
            "The eye page is publishing but its data has no timestamp "
            "and reports nothing in view.")
        return out

    age_s = max(0.0, time.time() - ts)
    out["age_s"] = round(age_s, 1)
    if age_s > _STALE_FACTOR * pub_s:
        out["stale"] = True
        out["summary"] = (
            f"The eye page stopped publishing — last update "
            f"{_age_phrase(age_s)} ago"
            + (f", showing {objects}." if objects else
               ", with nothing in view."))
        return out

    out["stale"] = False
    out["summary"] = (
        f"I see {objects}." if objects else
        "The eye page is live but nothing is in view right now.")
    return out
