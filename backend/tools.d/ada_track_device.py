"""ada_track_device — lost-device locate from the device-telemetry beacon
(collection `device-telemetry` in MDDB; beacons post <device>/<ts>
history + <device>/latest) with `tailscale status` as the always-on
fallback source. Read-only.

The beacon (chaba scripts/device-telemetry/) posts every ~5min:
wan ip + city geo, tailnet online+ips, battery, logged-in users,
top processes, uptime. This tool merges that with tailscale last-seen
so "where is my ipad" answers even when the device is silent.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from typing import Any

import httpx

DECLARATION = {
    "name": "ada_track_device",
    "description": (
        "Locate a user's device or list the tracked fleet. Sources: the "
        "device-telemetry beacon (GPS-ish WAN geo, battery, users, top "
        "apps) merged with tailscale last-seen. Answer with how fresh "
        "the signal is — never claim live GPS; say 'last seen X ago in "
        "<city>' when data is stale."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "'where' (default — locate one device), "
                               "'list' (fleet summary), 'history' "
                               "(recent sightings).",
            },
            "device": {
                "type": "string",
                "description": "Device name, e.g. 'iphone-15', 'tony-ipad', "
                               "'kk-macbook'. Fuzzy — partial names ok. "
                               "Not needed for action=list.",
            },
            "limit": {
                "type": "integer",
                "description": "Max entries for action=history "
                               "(default 8).",
            },
        },
    },
}

_MDBB = os.environ.get("MDDB_BASE_URL",
                       "http://100.102.134.91:11023/v1").rstrip("/")
_COLL = "device-telemetry"
_STALE_WARN_S = 15 * 60      # older than 15min -> say it's stale
_HIST_DEFAULT = 8


def _age_s(ts: int) -> str:
    ago = int(time.time()) - ts
    for div, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if ago >= div:
            return f"{ago // div}{unit} ago"
    return f"{ago}s ago"


async def _mddb_docs(limit: int = 400) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{_MDBB}/search",
                             json={"collection": _COLL, "query": "",
                                   "limit": limit})
            docs = r.json()
            return docs if isinstance(docs, list) else []
    except Exception:
        return []


async def _mddb_get(key: str) -> dict[str, Any] | None:
    """Point-read <device>/latest. /v1/search ordering is not guaranteed and
    `latest` keys fall out of the window as hist docs accumulate — never
    rely on search for the live pointer."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{_MDBB}/get",
                             json={"collection": _COLL, "key": key,
                                   "lang": "en"})
            if r.status_code != 200:
                return None
            return json.loads(r.json().get("contentMd") or "{}")
    except Exception:
        return None


def _tailnet() -> dict[str, dict[str, Any]]:
    try:
        st = json.loads(subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True,
            text=True, timeout=8).stdout or "{}")
    except Exception:
        return {}
    nodes: dict[str, dict[str, Any]] = {}
    for n in ([st.get("Self") or {}] + list((st.get("Peer") or {}).values())):
        name = (n.get("HostName") or "").split(".")[0].lower()
        if not name:
            continue
        online = bool(n.get("Online"))
        ls = n.get("LastSeen") or ""
        nodes[name] = {"online": online, "last_seen": ls,
                       "os": n.get("OS") or "",
                       "ips": n.get("TailscaleIPs") or []}
    return nodes


def _match(name: str, pool: list[str]) -> list[str]:
    name = name.lower().strip()
    exact = [p for p in pool if p == name]
    if exact:
        return exact
    partial = [p for p in pool if name in p or p in name]
    if partial:
        return sorted(partial, key=len)
    toks = re.split(r"[-_\s]+", name)
    return sorted(p for p in pool if all(t in p for t in toks if t))


def _describe(dev: str, tele: dict[str, Any] | None,
              node: dict[str, Any] | None) -> dict[str, Any]:
    now = int(time.time())
    out: dict[str, Any] = {"device": dev}
    if tele:
        ts = int(tele.get("ts") or 0)
        age = _age_s(ts) if ts else "?"
        stale = bool(ts) and (now - ts) > _STALE_WARN_S
        wan = tele.get("wan") or {}
        batt = tele.get("battery") or {}
        place = ", ".join(x for x in [wan.get("city"), wan.get("region"),
                                      wan.get("country")] if x)
        bits = []
        if place:
            bits.append(f"last seen {age} in {place}")
        elif ts:
            bits.append(f"last seen {age}")
        if batt.get("pct") is not None:
            bits.append(f"battery {batt['pct']}% ({batt.get('status','')})")
        if tele.get("users"):
            bits.append(f"logged in: {', '.join(tele['users'])}")
        out["summary"] = "; ".join(bits) or f"last seen {age}"
        out.update({"age": age, "stale": stale, "ts": ts,
                    "wan_ip": wan.get("ip"), "geo": wan.get("loc"),
                    "place": place or None,
                    "top_procs": (tele.get("top_procs") or [])[:5],
                    "source": "beacon"})
    elif node:
        st = "online now" if node.get("online") else \
            f"offline (last seen {node.get('last_seen', '?')[:19]})"
        out["summary"] = f"no beacon data — tailnet says {st}"
        out.update({"stale": True, "source": "tailnet",
                    "tailnet_online": node.get("online")})
    else:
        out["summary"] = "no telemetry — device never phoned home"
        out.update({"stale": True, "source": "none"})
    return out


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    action = str(args.get("action") or "where").lower()
    docs, nodes = await asyncio.gather(_mddb_docs(),
                                       asyncio.to_thread(_tailnet))
    pool = sorted(set(nodes) |
                  {str(d.get("key", "")).split("/")[0] for d in docs
                   if "/" in str(d.get("key", ""))})

    if action == "list":
        tele = dict(zip(pool, await asyncio.gather(
            *(_mddb_get(f"{n}/latest") for n in pool))))
        rows = [_describe(n, tele.get(n), nodes.get(n)) for n in pool]
        return {"ok": True, "devices": rows, "count": len(rows),
                "hint": "ask 'where is <device>' for detail"}

    if action == "history":
        dev_in = str(args.get("device") or "").strip()
        names = _match(dev_in, pool) if dev_in else pool
        lim = int(args.get("limit") or _HIST_DEFAULT)
        hist = [d for d in docs if any(str(d.get("key", "")).startswith(
                f"{n}/") and not d["key"].endswith("/latest")
                for n in names)]
        hist.sort(key=lambda d: str(d.get("key")), reverse=True)
        return {"ok": True, "device": names[0] if names else dev_in,
                "history": [{"key": h["key"],
                             "ts": (h.get("meta") or {}).get("ts", [""])[0]}
                            for h in hist[:lim]],
                "count": min(len(hist), lim)}

    # action=where
    dev_in = str(args.get("device") or "").strip()
    if not dev_in:
        return {"ok": False,
                "error": "which device? e.g. ada_track_device "
                         "(device='iphone-15') or action='list'"}
    names = _match(dev_in, pool)
    if not names:
        return {"ok": False,
                "error": f"no device matching '{dev_in}' — try "
                         "action='list' for the fleet"}
    dev = names[0]
    out = _describe(dev, await _mddb_get(f"{dev}/latest"), nodes.get(dev))
    if len(names) > 1:
        out["also_matched"] = names[1:4]
    out["ok"] = True
    return out
