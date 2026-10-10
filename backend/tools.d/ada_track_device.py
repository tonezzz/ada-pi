"""ada_track_device — lost-device locate. Read-only merge layer over the
sources that already exist (card device-tracking, 2026-10-08):

  1. HA companion app — device_tracker.* entities (GPS + battery_level +
     gps_accuracy), person.* links, and the companion sensor suite
     (sensor.<slug>_battery_level/state, _ssid/_bssid, _geocoded_location,
     _last_update_trigger). iOS has NO app-usage sensor — last_used_app is
     Android-only, so "what was it doing" is only answered for beaconed
     hosts via top_procs.
  2. device-telemetry beacon — MDDB `device-telemetry` (<dev>/latest +
     <dev>/<ts> history): WAN ip + city geo, battery, login users,
     top processes, tailnet online+ips.
  3. Tailscale node table — `tailscale status --json`, keyed on DNSName
     (the stable fleet name — iOS nodes report HostName "localhost", so
     HostName must never be the key).
  4. LAN scan digest — <ha>/local/ha/network-scan-latest.json (written by
     chaba network-scan-tony.timer): ip/mac/hostname/vendor presence on
     the home LAN. Device matching via MAC needs the chaba MAC registry
     (mddb infrastructure-ssot doc); hostname match works without it.
  5. HA logbook — location history for action='history'.

Answers carry confidence + per-sighting age — never claim live GPS; say
"last seen X ago" when data is stale.

Lost-mode (action='lost') arms an in-process watcher: re-merge on a
timer, push deltas to LINE/TG via the relay /send endpoints (same
loopback API chat_send uses). Armed watches persist to mddb
device-telemetry as _watch/<device> docs (armed_at, interval, channel,
until, last-pushed sighting + fingerprint, and a resume claim) so they
survive restarts: on_service_start (and lazily the first tool call in a
process) re-arms docs whose 'until' is still live and deletes expired
ones. The live loop re-stamps the doc every poll — a fresh resumed_at
means another process owns the watch, so peers skip it; 'found' deletes
the doc, which is also how a watcher in another process learns it was
disarmed. Optional GEV pin via gev_command annotate_map when
coordinates are known (gev_pin=true).

Fixture env (tests/scenarios + live debugging):
  ADA_TRACK_TAILSCALE_JSON  'tailscale status --json' output, inline or file
  ADA_TRACK_LANSCAN_JSON    network-scan-latest.json content, inline or file
  ADA_TRACK_LANSCAN_URL     override the /local/ digest URL
  ADA_TRACK_PUSH_DRYRUN=1   lost-mode pushes logged to runner._track_push_log
                            instead of hitting the relays

MDDB access goes through runner.context.mddb — the runner's routed
client (ops-split + read-replica failover live inside it). Unit tests
inject a fake client there (runner-facade, 2026-10-10); the old
ADA_TRACK_MDBB endpoint-override fallback is gone — in chaba mode
(context.mddb is None) the beacon source simply goes silent.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime
from typing import Any

import httpx

DECLARATION = {
    "name": "ada_track_device",
    "description": (
        "Locate a device or list the tracked fleet — merges HA "
        "device_tracker, the telemetry beacon, tailnet, and the LAN "
        "scan. Always state signal freshness; never claim live GPS. "
        "action='lost' watches and pushes location deltas to LINE/TG."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "'where' (default — locate one device), "
                               "'list' (fleet summary), 'history' "
                               "(recent sightings), 'lost' (arm lost-mode "
                               "watcher — persists across restarts), "
                               "'found' (disarm), 'watches' "
                               "(armed watchers).",
            },
            "device": {
                "type": "string",
                "description": "Device name, e.g. 'iphone-15', 'tony-ipad', "
                               "'kk-macbook', 'tony-omen'. Fuzzy — partial "
                               "names ok. Not needed for action=list/watches.",
            },
            "limit": {
                "type": "integer",
                "description": "Max entries for action=history "
                               "(default 8).",
            },
            "hours": {
                "type": "integer",
                "description": "history window / lost-mode lifetime in "
                               "hours (history default 24, lost default 12).",
            },
            "interval_s": {
                "type": "integer",
                "description": "lost-mode poll interval seconds "
                               "(default 300, min 60).",
            },
            "channel": {
                "type": "string",
                "description": "lost-mode push channel: 'both' (default), "
                               "'line', 'telegram'.",
            },
            "gev_pin": {
                "type": "boolean",
                "description": "pin the fix on the GEV map via "
                               "gev_command annotate_map when coordinates "
                               "are known.",
            },
        },
    },
}

_COLL = "device-telemetry"
_MACREG_KEY = "infrastructure-ssot.mac-address-registry.tony"
_MACREG_COLL = "infrastructure-ssot"
_LANSCAN_PATH = "/local/ha/network-scan-latest.json"
_STALE_WARN_S = 15 * 60      # older than 15min -> say it's stale
_HIST_DEFAULT = 8
_LOST_INTERVAL_S = 300
_LOST_MIN_INTERVAL_S = 60
_LOST_LIFETIME_H = 12.0
_PUSH_TIMEOUT_S = 20.0
_WATCH_PREFIX = "_watch/"
_CLAIM_MIN_S = 300.0    # live watchers re-stamp each poll; a stale
                        # resumed_at means the owning process is gone
_DOC_MISS_LIMIT = 3     # consecutive missing watch docs -> disarmed

# Known cross-source name equivalences (verified 2026-10-08):
# tailnet DNSName is canonical; iOS HostName is 'localhost' and unusable.
_ALIASES = {
    "tony-ip": "iphone-15", "tony-iphone": "iphone-15",
    "iphone": "iphone-15", "iphone15": "iphone-15",
    "tony-iphone-15": "iphone-15",
    "ipad": "tony-ipad", "tonys-ipad": "tony-ipad",
    "ipad-2": "kk-ipad", "kk-ipad-2": "kk-ipad", "kks-ipad": "kk-ipad",
    "macbook": "kk-macbook", "kk-macbook-pro": "kk-macbook",
    "kks-macbook-pro": "kk-macbook", "kks-macbook": "kk-macbook",
    "tony-mn": "mn01",
    "homeassistant": "michael-ha",
}


def _mddb(runner: Any) -> Any | None:
    """The routed mddb client via the runner.context facade — None in
    chaba guest mode (no beacon store; those sources go silent)."""
    return getattr(getattr(runner, "context", None), "mddb", None)


def _canon(name: Any) -> str:
    """Normalize a name across sources: lowercase, apostrophes/space/punct
    -> '-', then apply the explicit alias table."""
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return _ALIASES.get(s, s)


def _alias_names(canon: str) -> set[str]:
    """Every raw alias spelling that maps to a canonical device."""
    return {k for k, v in _ALIASES.items() if v == canon}


def _age_s(ts: int | float) -> str:
    ago = max(0, int(time.time()) - int(ts))
    for div, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if ago >= div:
            return f"{ago // div}{unit} ago"
    return f"{ago}s ago"


def _ts_of(value: Any) -> int:
    """Parse an ISO timestamp or epoch into int seconds; 0 on failure."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text or text.startswith("0001-01-01"):
        return 0
    try:
        return int(datetime.fromisoformat(
            text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


# ---------------------------------------------------------------- sources

def _env_or_file(var: str) -> Any:
    """Fixture override: env value is inline JSON when it starts with
    '{'/'[' else a file path to read."""
    raw = os.environ.get(var)
    if not raw:
        return None
    raw = raw.strip()
    try:
        if raw[:1] in ("{", "["):
            return json.loads(raw)
        with open(raw, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _tailnet() -> dict[str, dict[str, Any]]:
    """tailnet node table keyed on DNSName (stable fleet name). iOS nodes
    report HostName 'localhost' — never key on HostName."""
    st = _env_or_file("ADA_TRACK_TAILSCALE_JSON")
    if st is None:
        try:
            st = json.loads(subprocess.run(
                ["tailscale", "status", "--json"], capture_output=True,
                text=True, timeout=8).stdout or "{}")
        except Exception:
            return {}
    if not isinstance(st, dict):
        return {}
    nodes: dict[str, dict[str, Any]] = {}
    for n in ([st.get("Self") or {}] + list((st.get("Peer") or {}).values())):
        name = (n.get("DNSName") or "").split(".")[0].lower() \
            or (n.get("HostName") or "").split(".")[0].lower()
        if not name:
            continue
        nodes[name] = {
            "online": bool(n.get("Online")),
            "last_seen": n.get("LastSeen") or "",
            "last_seen_ts": _ts_of(n.get("LastSeen")),
            "os": n.get("OS") or "",
            "ips": n.get("TailscaleIPs") or [],
            "hostname": n.get("HostName") or "",
        }
    return nodes


async def _mddb_get(runner: Any, key: str) -> dict[str, Any] | None:
    """Point-read <device>/latest — /v1/search ordering is not guaranteed
    and 'latest' keys fall out of the window as history accumulates."""
    cli = _mddb(runner)
    if cli is None:
        return None
    try:
        doc = await cli.get_document(_COLL, key, "en")
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    try:
        return json.loads(doc.get("contentMd") or "{}")
    except Exception:
        return None


async def _mddb_docs(runner: Any, limit: int = 400) -> list[dict[str, Any]]:
    cli = _mddb(runner)
    if cli is None:
        return []
    try:
        docs = await cli.search_documents(_COLL, query="*", limit=limit)
        return docs if isinstance(docs, list) else []
    except Exception:
        return []


async def _mac_registry(runner: Any) -> dict[str, dict[str, Any]]:
    """device_id -> {hostname, macs} from the chaba MAC registry SSOT
    (synced to mddb). Empty when unreachable — LAN matching just falls
    back to hostname."""
    out: dict[str, dict[str, Any]] = {}
    cli = _mddb(runner)
    if cli is None:
        return out
    try:
        doc = await cli.get_document(_MACREG_COLL, _MACREG_KEY, "en")
    except Exception:
        return out
    if not isinstance(doc, dict):
        return out
    text = str(doc.get("contentMd") or "")
    m = re.search(r"```yaml\s*(.*?)```", text, re.S)
    if not m:
        return out
    try:
        import yaml
        data = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return out
    for rec in (data.get("config") or {}).get("mac_registry") or []:
        if not isinstance(rec, dict):
            continue
        did = str(rec.get("device_id") or "").strip()
        if not did:
            continue
        macs = [str(i.get("mac")).lower()
                for i in rec.get("interfaces") or []
                if isinstance(i, dict) and i.get("mac")]
        out[did] = {
            "hostname": str((rec.get("local_ids") or {}).get("hostname")
                            or ""),
            "macs": macs,
        }
    return out


async def _lan_scan(ha: Any) -> dict[str, Any]:
    """Latest LAN scan digest served unauthenticated from HA's /local/.
    Returns {discovered_at, hosts:[{ip,mac,hostname,vendor}]}."""
    fixed = _env_or_file("ADA_TRACK_LANSCAN_JSON")
    if isinstance(fixed, dict):
        return fixed
    url = os.environ.get("ADA_TRACK_LANSCAN_URL", "").strip()
    if not url:
        base = str(getattr(ha, "base_url", "") or "").rstrip("/")
        if not base:
            return {}
        url = base + _LANSCAN_PATH
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return {}
            doc = r.json()
            return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


async def _ha_states(ha: Any) -> list[dict[str, Any]]:
    try:
        states = await ha._states()
    except Exception:
        return []
    return states if isinstance(states, list) else []


async def _ha_logbook(ha: Any, entity_id: str, hours: int
                      ) -> list[dict[str, Any]]:
    try:
        out = await ha.logbook(entity_id=entity_id, hours=hours)
    except Exception:
        return []
    return out if isinstance(out, list) else []


# ------------------------------------------------------------- merge core

def _ha_fleet(states: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """device_tracker + person + companion-sensor states folded into
    per-canonical-device HA sightings."""
    by_eid = {str(s.get("entity_id")): s for s in states
              if isinstance(s, dict) and s.get("entity_id")}
    # person.<p>.device_trackers links person state to tracker entities.
    person_for_tracker: dict[str, dict[str, Any]] = {}
    for eid, s in by_eid.items():
        if not eid.startswith("person."):
            continue
        attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
        for tr in attrs.get("device_trackers") or []:
            person_for_tracker[str(tr)] = s
    out: dict[str, dict[str, Any]] = {}
    for eid, s in by_eid.items():
        if not eid.startswith("device_tracker."):
            continue
        attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
        oid = eid.split(".", 1)[1]
        fname = str(attrs.get("friendly_name") or oid)
        canon = _canon(oid)
        if canon == re.sub(r"[^a-z0-9]+", "-", oid.lower()).strip("-"):
            canon = _canon(fname)      # no explicit oid alias — try name
        person = person_for_tracker.get(eid)
        sight = out.setdefault(canon, {"trackers": []})
        ts = _ts_of(s.get("last_changed")) or _ts_of(s.get("last_updated"))
        comp: dict[str, Any] = {}
        comp_ts = 0
        for suffix, key in (("battery_level", "battery_pct"),
                            ("battery_state", "battery_state"),
                            ("ssid", "ssid"), ("bssid", "bssid"),
                            ("geocoded_location", "geocoded"),
                            ("last_update_trigger", "update_trigger")):
            srow = by_eid.get(f"sensor.{oid}_{suffix}")
            if srow is not None:
                comp[key] = srow.get("state")
                comp_ts = max(comp_ts, _ts_of(srow.get("last_changed"))
                              or _ts_of(srow.get("last_updated")))
        brow = by_eid.get(f"binary_sensor.{oid}_charging")
        if brow is not None:
            comp["charging"] = str(brow.get("state")) == "on"
            comp_ts = max(comp_ts, _ts_of(brow.get("last_changed"))
                          or _ts_of(brow.get("last_updated")))
        sight["trackers"].append({
            "entity_id": eid, "name": fname,
            "state": str(s.get("state") or "unknown"),
            "lat": attrs.get("latitude"), "lon": attrs.get("longitude"),
            "gps_accuracy": attrs.get("gps_accuracy"),
            "battery_level": attrs.get("battery_level"),
            "ts": ts, "comp_ts": comp_ts, **comp,
        })
        if person is not None:
            sight["person"] = {
                "entity_id": str(person.get("entity_id")),
                "state": str(person.get("state") or "unknown"),
                "ts": _ts_of(person.get("last_changed"))
                      or _ts_of(person.get("last_updated")),
            }
    return out


def _lan_hits(canon: str, scan: dict[str, Any],
              registry: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Scan hosts matching this device by hostname or registry MAC."""
    names = {canon} | _alias_names(canon)
    macs: set[str] = set()
    for did, rec in registry.items():
        if _canon(did) == canon or _canon(rec.get("hostname")) == canon:
            macs.update(rec.get("macs") or [])
    hits = []
    for h in scan.get("hosts") or []:
        if not isinstance(h, dict):
            continue
        hmac = str(h.get("mac") or "").lower()
        if _canon(h.get("hostname")) in names \
                or (hmac and hmac in macs):
            hits.append({"ip": h.get("ip"), "mac": hmac or None,
                         "hostname": h.get("hostname"),
                         "vendor": h.get("vendor")})
    return hits


def _describe(canon: str, *, tele: dict[str, Any] | None,
              node: dict[str, Any] | None, ha: dict[str, Any] | None,
              lan_hits: list[dict[str, Any]], lan_ts: int,
              now: int) -> dict[str, Any]:
    """Merge every sighting into one answer: freshest best-precision
    location + battery + online flags + confidence."""
    out: dict[str, Any] = {"device": canon, "sources": {}}
    candidates: list[tuple[int, int, dict[str, Any]]] = []  # (rank, ts, loc)
    batt_cands: list[tuple[int, dict[str, Any]]] = []

    tr = sorted((ha or {}).get("trackers") or [],
                key=lambda t: t.get("ts") or 0, reverse=True)
    if tr:
        t = tr[0]
        ts = int(t.get("ts") or 0)
        ha_src: dict[str, Any] = {
            "entity_id": t["entity_id"], "state": t["state"],
            "ts": ts, "age": _age_s(ts) if ts else "?",
        }
        if t.get("ssid") not in (None, "unknown", "unavailable"):
            ha_src["ssid"] = t["ssid"]
        if t.get("geocoded") not in (None, "unknown", "unavailable"):
            ha_src["geocoded"] = str(t["geocoded"]).replace("\n", ", ")
        if (ha.get("person") or {}).get("state"):
            ha_src["person"] = ha["person"]["state"]
        out["sources"]["ha"] = ha_src
        geo_ts = max(ts, int(t.get("comp_ts") or 0))
        if t.get("lat") is not None and t.get("lon") is not None:
            candidates.append((4, ts, {
                "kind": "gps", "lat": t["lat"], "lon": t["lon"],
                "accuracy_m": t.get("gps_accuracy"),
                "via": t["entity_id"], "ts": ts,
                "place": (str(t["geocoded"]).split("\n")[0]
                          if t.get("geocoded") else None) or t["state"],
            }))
        elif t.get("state") not in ("unknown", "unavailable", ""):
            candidates.append((3, ts, {
                "kind": "zone", "place": t["state"],
                "via": t["entity_id"], "ts": ts}))
        if t.get("ssid") not in (None, "unknown", "unavailable"):
            # companion app on wifi — a presence signal even without GPS
            candidates.append((3, geo_ts, {
                "kind": "wifi", "place": f"on wifi {t['ssid']}",
                "via": t["entity_id"], "ts": geo_ts}))
        for pct, st, bts in (
                (t.get("battery_level"), None, ts),
                (t.get("battery_pct"), t.get("battery_state"), geo_ts)):
            if pct is not None and str(pct) not in ("unknown", "unavailable"):
                try:
                    pct = int(float(pct))
                except (TypeError, ValueError):
                    pass
                batt_cands.append((bts, {
                    "pct": pct, "state": st or t.get("battery_state"),
                    "charging": t.get("charging"),
                    "via": t["entity_id"], "ts": bts}))

    if tele:
        ts = int(tele.get("ts") or 0)
        wan = tele.get("wan") or {}
        place = ", ".join(x for x in [wan.get("city"), wan.get("region"),
                                      wan.get("country")] if x)
        out["sources"]["beacon"] = {
            "ts": ts, "age": _age_s(ts) if ts else "?",
            "wan_ip": wan.get("ip"), "place": place or None,
            "users": tele.get("users") or [],
            "top_procs": (tele.get("top_procs") or [])[:5],
        }
        loc: dict[str, Any] = {
            "kind": "city", "place": place or "unknown area",
            "via": "beacon wan-geo", "ts": ts}
        latlon = str(wan.get("loc") or "").split(",")
        if len(latlon) == 2:
            try:
                loc["lat"], loc["lon"] = (float(latlon[0]),
                                          float(latlon[1]))
            except (TypeError, ValueError):
                pass
        candidates.append((2, ts, loc))
        b = tele.get("battery") or {}
        if b.get("pct") is not None:
            batt_cands.append((ts, {"pct": b["pct"],
                                    "state": b.get("status"),
                                    "via": "beacon", "ts": ts}))

    if lan_hits:
        h = lan_hits[0]
        out["sources"]["lan"] = {
            "present": True, "ip": h.get("ip"), "mac": h.get("mac"),
            "vendor": h.get("vendor"), "scan_ts": lan_ts,
            "age": _age_s(lan_ts) if lan_ts else "?",
        }
        candidates.append((3, lan_ts, {
            "kind": "home", "place": "home LAN",
            "via": f"lan-scan {h.get('ip')}", "ts": lan_ts}))
    elif lan_ts:
        out["sources"]["lan"] = {"present": False, "scan_ts": lan_ts,
                                 "age": _age_s(lan_ts)}

    if node:
        nts = int(node.get("last_seen_ts") or 0)
        out["sources"]["tailnet"] = {
            "online": node.get("online"), "os": node.get("os"),
            "ips": node.get("ips"), "ts": nts,
            "age": (_age_s(nts) if nts
                    else ("now" if node.get("online") else "?")),
        }
        if node.get("online"):
            candidates.append((1, now, {"kind": "online",
                                        "place": "tailnet online",
                                        "via": "tailscale", "ts": now}))
        elif nts:
            candidates.append((1, nts, {
                "kind": "last-seen", "place": "tailnet last-seen",
                "via": "tailscale", "ts": nts}))

    if batt_cands:
        out["battery"] = max(batt_cands, key=lambda b: b[0])[1]

    if not candidates:
        out.update({"summary": f"{canon}: no telemetry — device never "
                              "phoned home",
                    "stale": True, "source": "none", "confidence": "none"})
        return out
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _rank, best_ts, best = candidates[0]
    out["location"] = best
    out["sightings"] = [c[2] for c in candidates[:4]]
    age = now - best_ts if best_ts else None
    stale = age is None or age > _STALE_WARN_S
    out["ts"] = best_ts
    out["age"] = _age_s(best_ts) if best_ts else "?"
    out["stale"] = stale
    kind = best["kind"]
    if kind == "last-seen":
        conf = "low"        # aliveness evidence, not a location
    elif not stale and kind in ("gps", "home", "zone", "wifi"):
        conf = "high"
    elif not stale and kind in ("city", "online"):
        conf = "medium"
    elif age is not None and age < 2 * 3600:
        conf = "medium"
    else:
        conf = "low"
    out["confidence"] = conf
    out["source"] = best.get("via")

    bits = []
    if kind == "gps":
        acc = best.get("accuracy_m")
        where = best.get("place") or "unknown place"
        try:
            bits.append(f"at {where} (GPS ±{int(float(acc))}m)"
                        if acc else f"at {where} (GPS)")
        except (TypeError, ValueError):
            bits.append(f"at {where} (GPS)")
    elif kind in ("home", "wifi", "zone"):
        bits.append(str(best.get("place") or "home"))
    elif kind == "city":
        bits.append(f"last seen in {best.get('place')} (WAN geo)")
    elif kind == "online":
        bits.append("online on tailnet")
    else:
        bits.append("last tailnet sighting")
    bits.append("now" if kind == "online" else f"as of {out['age']}")
    if out.get("battery"):
        b = out["battery"]
        bs = f"battery {b['pct']}%"
        if b.get("state"):
            bs += f" ({b['state']})"
        bits.append(bs)
    if node:
        bits.append("tailnet online" if node.get("online")
                    else f"tailnet last seen {out['sources']['tailnet']['age']}")
    if (out["sources"].get("lan") or {}).get("present"):
        bits.append(f"on LAN at {out['sources']['lan'].get('ip')}")
    if stale and kind not in ("online",):
        bits.append("STALE — not a live fix")
    out["summary"] = f"{canon}: " + "; ".join(bits)
    return out


def _fleet_pool(nodes: dict[str, dict[str, Any]],
                docs: list[dict[str, Any]],
                ha_fleet: dict[str, dict[str, Any]],
                scan: dict[str, Any],
                registry: dict[str, dict[str, Any]]
                ) -> tuple[list[str], dict[str, str]]:
    """Union of every device name across sources, canonicalized, plus a
    canon->raw-beacon-name map."""
    pool: set[str] = {_canon(n) for n in nodes}
    beacon_key: dict[str, str] = {}
    for d in docs:
        key = str(d.get("key") or "")
        if "/" not in key:
            continue
        dev = key.split("/")[0]
        if dev.startswith("_"):        # _watch/* etc — not devices
            continue
        beacon_key.setdefault(_canon(dev), dev)
        pool.add(_canon(dev))
    pool.update(ha_fleet)
    for h in scan.get("hosts") or []:
        if isinstance(h, dict) and h.get("hostname"):
            pool.add(_canon(h["hostname"]))
    for did in registry:
        if not did.startswith("discovered-"):
            pool.add(_canon(did))
    return sorted(pool), beacon_key


def _match(name: str, pool: list[str]) -> list[str]:
    c = _canon(name)
    for cand in (c, _canon(re.sub(r"^(my|the)-", "", c))):
        exact = [p for p in pool if p == cand]
        if exact:
            return exact
        partial = [p for p in pool if cand in p or p in cand]
        if partial:
            return sorted(partial, key=len)
        toks = [t for t in re.split(r"[-_\s]+", cand) if t]
        hits = sorted(p for p in pool if all(t in p for t in toks))
        if hits:
            return hits
    return []


# --------------------------------------------------------------- lost mode

def _resumer_id() -> str:
    """Unique per resume attempt — the doc's resumed_by is how a losing
    claim-write learns it lost the race."""
    return f"{socket.gethostname()}:{os.getpid()}:{time.time_ns()}"


def _watch_key(canon: str) -> str:
    return f"{_WATCH_PREFIX}{canon}"


async def _mddb_write(runner: Any, op: str, key: str,
                      body: dict[str, Any] | None = None) -> bool:
    """Upsert/delete a device-telemetry doc via the routed client
    (mddb /add upserts on collection+key+lang)."""
    meta = {"kind": ["track-watch"], "device": [key.split("/", 1)[-1]]}
    cli = _mddb(runner)
    if cli is None:
        return False
    try:
        if op == "delete":
            await cli.delete_document(_COLL, key)
        else:
            await cli.add_document(_COLL, key, "en",
                                   json.dumps(body or {}), meta=meta)
        return True
    except Exception:
        return False


async def _persist_watch(runner: Any, canon: str,
                         watch: dict[str, Any]) -> None:
    fp = watch.get("last_fp")
    await _mddb_write(runner, "add", _watch_key(canon), {
        "device": canon,
        "armed_at": watch.get("armed_at"),
        "interval_s": watch.get("interval_s"),
        "channel": watch.get("channel") or "both",
        "until": watch.get("until"),
        "resumed_at": watch.get("resumed_at"),
        "resumed_by": watch.get("resumed_by"),
        "last_pushed": watch.get("last_pushed"),
        "last_sighting": watch.get("last_sighting"),
        "last_fp": list(fp) if fp else None,
    })


async def _drop_watch_doc(runner: Any, canon: str) -> None:
    await _mddb_write(runner, "delete", _watch_key(canon))


async def _watch_doc(runner: Any, canon: str) -> dict[str, Any] | None:
    """The persisted watch doc. Leader-read — a follower read right
    after our own write can lag into a false 'deleted' verdict."""
    cli = _mddb(runner)
    if cli is None:
        return None
    try:
        doc = await cli.get_document(_COLL, _watch_key(canon), "en",
                                     prefer_leader=True)
    except TypeError:
        doc = await cli.get_document(_COLL, _watch_key(canon), "en")
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    try:
        return json.loads(doc.get("contentMd") or "{}")
    except Exception:
        return None


def _fp_restore(raw: Any) -> tuple | None:
    """Persisted fingerprint (json list) back to the tuple _fingerprint
    emits — the nested geo pair becomes a tuple so equality works."""
    if not isinstance(raw, list):
        return None
    return tuple(tuple(x) if isinstance(x, list) else x for x in raw)


def _claim_fresh(doc: dict[str, Any], now: float) -> bool:
    """Another process owns this watch while its persisted heartbeat is
    fresh — the live loop re-stamps resumed_at every poll."""
    interval = float(doc.get("interval_s") or _LOST_INTERVAL_S)
    return (now - float(doc.get("resumed_at") or 0)
            < max(3 * interval, _CLAIM_MIN_S))


def _watches(runner: Any) -> dict[str, dict[str, Any]]:
    w = getattr(runner, "_track_watches", None)
    if not isinstance(w, dict):
        w = {}
        try:
            runner._track_watches = w
        except Exception:
            pass
    return w


def _fingerprint(desc: dict[str, Any]) -> tuple:
    """What counts as a 'delta' worth pushing in lost-mode: location
    kind/place (rounded to ~100m when GPS), online flags, battery bucket."""
    loc = desc.get("location") or {}
    geo = None
    try:
        if loc.get("lat") is not None and loc.get("lon") is not None:
            geo = (round(float(loc["lat"]), 3),
                   round(float(loc["lon"]), 3))
    except (TypeError, ValueError):
        pass
    b = desc.get("battery") or {}
    try:
        bpct = int(float(b["pct"])) // 20 \
            if b.get("pct") is not None else None
    except (TypeError, ValueError):
        bpct = None
    srcs = desc.get("sources") or {}
    return (
        loc.get("kind"), loc.get("place"), geo,
        bool((srcs.get("tailnet") or {}).get("online")),
        bool((srcs.get("lan") or {}).get("present")),
        bpct, bool(b.get("charging")),
    )


async def _push(runner: Any, channel: str, text: str) -> dict[str, bool]:
    """Push via the relay /send loopback APIs — same path chat_send uses.
    ADA_TRACK_PUSH_DRYRUN=1 records to runner._track_push_log instead."""
    targets = {"line": "http://127.0.0.1:8912/send",
               "telegram": "http://127.0.0.1:8911/send"}
    sent: dict[str, bool] = {}
    chans = [c.strip() for c in (channel or "both").lower().split(",")]
    if any(c in ("both", "all") for c in chans):
        chans = list(targets)
    if os.environ.get("ADA_TRACK_PUSH_DRYRUN"):
        log = getattr(runner, "_track_push_log", None)
        if not isinstance(log, list):
            log = []
            try:
                runner._track_push_log = log
            except Exception:
                pass
        for ch in chans:
            if ch in targets:
                log.append({"channel": ch, "text": text,
                            "ts": int(time.time())})
                sent[ch] = True
        return sent
    for ch in chans:
        ep = targets.get(ch)
        if not ep:
            continue
        env = "ADA_LINE_SEND_URL" if ch == "line" else "ADA_TG_SEND_URL"
        ep = os.environ.get(env, ep)
        try:
            async with httpx.AsyncClient(timeout=_PUSH_TIMEOUT_S) as c:
                r = await c.post(ep, json={"text": text})
                sent[ch] = bool(r.status_code == 200
                                and (r.json() or {}).get("ok"))
        except Exception:
            sent[ch] = False
    return sent


async def _merge_one(runner: Any, canon: str, *,
                     nodes: dict[str, dict[str, Any]] | None = None,
                     ha_fleet: dict[str, dict[str, Any]] | None = None,
                     scan: dict[str, Any] | None = None,
                     registry: dict[str, dict[str, Any]] | None = None,
                     beacon_names: set[str] | None = None
                     ) -> dict[str, Any]:
    """Re-merge one device — the shared core of 'where' and lost-mode."""
    if nodes is None:
        nodes = await asyncio.to_thread(_tailnet)
    if ha_fleet is None:
        ha_fleet = _ha_fleet(await _ha_states(runner.context.ha_client))
    if scan is None:
        scan = await _lan_scan(runner.context.ha_client)
    if registry is None:
        registry = await _mac_registry(runner)
    lan_ts = _ts_of(scan.get("discovered_at"))
    # beacon lookup: canon, any raw name the docs listing showed, and
    # every explicit alias spelling (e.g. 'tony-mn' for canon 'mn01')
    tele = None
    for cand in dict.fromkeys(
            [canon, *(beacon_names or ()), *_alias_names(canon)]):
        tele = await _mddb_get(runner, f"{cand}/latest")
        if tele:
            break
    node = nodes.get(canon) or next(
        (n for k, n in nodes.items() if _canon(k) == canon), None)
    hits = _lan_hits(canon, scan, registry)
    return _describe(canon, tele=tele, node=node,
                     ha=ha_fleet.get(canon), lan_hits=hits,
                     lan_ts=lan_ts, now=int(time.time()))


async def _watch_loop(runner: Any, canon: str, watch: dict[str, Any]
                      ) -> None:
    interval = float(watch["interval_s"])
    chan = str(watch.get("channel") or "both")
    until = float(watch["until"])
    last_fp: tuple | None = watch.get("last_fp")  # seed from a resume
    doc_misses = 0
    try:
        while time.time() < until and canon in _watches(runner):
            if await _watch_doc(runner, canon) is None:
                doc_misses += 1
                if doc_misses >= _DOC_MISS_LIMIT:
                    break                # 'found' deleted it elsewhere
                # skip this poll — persisting now would resurrect a
                # just-deleted doc before the miss limit trips
                await asyncio.sleep(interval)
                continue
            doc_misses = 0
            try:
                desc = await _merge_one(runner, canon)
            except Exception as exc:
                desc = {"summary": f"track merge failed: {exc}"}
            fp = _fingerprint(desc)
            if fp != last_fp:
                tag = "lost-mode" if last_fp is None else "delta"
                last_fp = fp
                await _push(runner, chan,
                            f"[{tag} {canon}] "
                            f"{desc.get('summary') or desc}")
                watch["last_pushed"] = int(time.time())
                watch["last_sighting"] = desc.get("summary")
            watch["last_desc"] = desc
            watch["last_fp"] = last_fp
            watch["resumed_at"] = time.time()   # claim heartbeat
            if canon not in _watches(runner):
                break                # disarmed mid-iteration — don't
                                     # resurrect the doc via persist
            await _persist_watch(runner, canon, watch)
            await asyncio.sleep(interval)
    finally:
        _watches(runner).pop(canon, None)
        await _drop_watch_doc(runner, canon)


def _watch_info(canon: str, w: dict[str, Any]) -> dict[str, Any]:
    return {"device": canon, "interval_s": w["interval_s"],
            "channel": w["channel"], "armed_at": w["armed_at"],
            "until": int(w["until"]),
            "resumed": bool(w.get("resumed")),
            "last_pushed": w.get("last_pushed"),
            "summary": (w.get("last_desc") or {}).get("summary")}


async def _arm_lost(runner: Any, canon: str, *, interval_s: int,
                    channel: str, hours: float) -> dict[str, Any]:
    watches = _watches(runner)
    if canon in watches:
        return {"ok": True, "already_armed": True,
                "watch": _watch_info(canon, watches[canon])}
    watch: dict[str, Any] = {
        "device": canon,
        "interval_s": max(_LOST_MIN_INTERVAL_S, int(interval_s)),
        "channel": channel or "both",
        "armed_at": int(time.time()),
        "until": time.time() + hours * 3600,
        "resumed_at": time.time(),
        "resumed_by": _resumer_id(),
    }
    watches[canon] = watch
    await _persist_watch(runner, canon, watch)
    try:
        watch["task"] = asyncio.ensure_future(
            _watch_loop(runner, canon, watch))
    except Exception as exc:
        watches.pop(canon, None)
        await _drop_watch_doc(runner, canon)
        return {"ok": False, "error": f"could not arm watcher: {exc}"}
    return {"ok": True, "armed": True, "watch": _watch_info(canon, watch),
            "note": "watcher persists to mddb _watch/<device> — it "
                    "resumes on restart; polls push deltas to the "
                    "relay /send API"}


async def resume_watches(runner: Any) -> list[str]:
    """Re-arm watchers persisted as _watch/<device> docs — called at
    service start (on_service_start) and lazily on the first tool call.
    Docs past 'until' are stale: delete them. Docs with a fresh
    resumed_at belong to a live process — skip."""
    resumed: list[str] = []
    now = time.time()
    for d in await _mddb_docs(runner):
        key = str(d.get("key") or "")
        if not key.startswith(_WATCH_PREFIX):
            continue
        canon = _canon(key.split("/", 1)[1])
        if canon in _watches(runner):
            continue
        doc = await _mddb_get(runner, key)
        if not isinstance(doc, dict):
            continue
        until = float(doc.get("until") or 0)
        if until <= now:
            await _drop_watch_doc(runner, canon)     # stale cleanup
            continue
        if _claim_fresh(doc, now):
            continue
        watch: dict[str, Any] = {
            "device": canon,
            "interval_s": max(_LOST_MIN_INTERVAL_S,
                              int(doc.get("interval_s")
                                  or _LOST_INTERVAL_S)),
            "channel": str(doc.get("channel") or "both"),
            "armed_at": int(doc.get("armed_at") or now),
            "until": until,
            "last_pushed": doc.get("last_pushed"),
            "last_sighting": doc.get("last_sighting"),
            "last_fp": _fp_restore(doc.get("last_fp")),
            "resumed_at": now,
            "resumed_by": _resumer_id(),
            "resumed": True,
        }
        _watches(runner)[canon] = watch
        await _persist_watch(runner, canon, watch)   # write the claim
        try:
            cli = _mddb(runner)
            fresh = (await cli.get_document(
                _COLL, key, "en", prefer_leader=True)) \
                if cli is not None else await _mddb_get(runner, key)
        except Exception:
            fresh = await _mddb_get(runner, key)
        if isinstance(fresh, dict) and "contentMd" in fresh:
            try:
                fresh = json.loads(fresh.get("contentMd") or "{}")
            except Exception:
                fresh = {}
        if isinstance(fresh, dict) and fresh.get("resumed_by") \
                and fresh.get("resumed_by") != watch["resumed_by"]:
            _watches(runner).pop(canon, None)        # lost a claim race
            continue
        try:
            watch["task"] = asyncio.ensure_future(
                _watch_loop(runner, canon, watch))
        except Exception:
            _watches(runner).pop(canon, None)
            continue
        resumed.append(canon)
    return resumed


async def on_service_start(runner: Any) -> list[str]:
    """tools.d service-start hook — pwa_server calls this once at boot
    with the shared runner to re-arm persisted watches."""
    return await resume_watches(runner)


# -------------------------------------------------------------------- run

async def run(runner: Any, **args: Any) -> dict[str, Any]:
    action = str(args.get("action") or "where").lower()
    # Lazy resume: processes without an on_service_start hook re-arm
    # persisted _watch/* docs on the first track call after boot.
    if getattr(runner, "_track_resumed", None) is not True:
        try:
            runner._track_resumed = True
        except Exception:
            pass
        try:
            await resume_watches(runner)
        except Exception:
            pass
    nodes, docs, states, scan, registry = await asyncio.gather(
        asyncio.to_thread(_tailnet),
        _mddb_docs(runner),
        _ha_states(runner.context.ha_client),
        _lan_scan(runner.context.ha_client),
        _mac_registry(runner),
    )
    ha_fleet = _ha_fleet(states)
    pool, beacon_key = _fleet_pool(nodes, docs, ha_fleet, scan, registry)

    if action == "watches":
        ws = _watches(runner)
        out = [_watch_info(c, w) for c, w in ws.items()]
        # persisted docs owned by another live process show as remote
        for d in docs:
            key = str(d.get("key") or "")
            if not key.startswith(_WATCH_PREFIX):
                continue
            canon = _canon(key.split("/", 1)[1])
            if canon in ws:
                continue
            doc = await _mddb_get(runner, key)
            if isinstance(doc, dict):
                out.append({"device": canon, "remote": True,
                            "resumed_by": doc.get("resumed_by"),
                            "armed_at": doc.get("armed_at"),
                            "until": int(float(doc.get("until") or 0)),
                            "interval_s": doc.get("interval_s"),
                            "channel": doc.get("channel"),
                            "last_pushed": doc.get("last_pushed")})
        return {"ok": True, "watches": out, "count": len(out)}

    if action in ("found", "unlost", "unwatch"):
        dev_in = str(args.get("device") or "").strip()
        if dev_in:
            names = _match(dev_in, pool) or [_canon(dev_in)]
        else:
            names = list({_canon(k.split("/", 1)[1])
                          for k in (str(d.get("key") or "")
                                    for d in docs)
                          if k.startswith(_WATCH_PREFIX)}
                         | set(_watches(runner)))
        disarmed = [c for c in names if _watches(runner).pop(c, None)]
        for c in names:          # delete persisted docs; a live watcher
            await _drop_watch_doc(runner, c)   # elsewhere sees it gone
        return {"ok": True, "disarmed": disarmed, "docs_cleared": names,
                "count": len(disarmed)}

    if action == "list":
        rows = []
        for c in pool:
            desc = await _merge_one(
                runner, c, nodes=nodes, ha_fleet=ha_fleet, scan=scan,
                registry=registry,
                beacon_names={beacon_key.get(c, c)})
            rows.append({k: desc.get(k) for k in
                         ("device", "summary", "age", "stale",
                          "confidence", "source")})
        ws = _watches(runner)
        return {"ok": True, "devices": rows, "count": len(rows),
                "watches": [_watch_info(c, w) for c, w in ws.items()],
                "hint": "ask 'where is <device>' for detail; "
                        "action='lost' arms delta push"}

    if action == "history":
        dev_in = str(args.get("device") or "").strip()
        names = _match(dev_in, pool) if dev_in else pool
        lim = int(args.get("limit") or _HIST_DEFAULT)
        hours = int(args.get("hours") or 24)
        canon = names[0] if names else _canon(dev_in)
        raw = beacon_key.get(canon, canon)
        hist = [d for d in docs
                if str(d.get("key", "")).startswith(f"{raw}/")
                and not str(d.get("key", "")).endswith("/latest")]
        hist.sort(key=lambda d: str(d.get("key")), reverse=True)
        out_hist = [{"key": h["key"],
                     "ts": (h.get("meta") or {}).get("ts", [""])[0]}
                    for h in hist[:lim]]
        # HA logbook for the matched tracker entity = location history
        tr = (ha_fleet.get(canon) or {}).get("trackers") or []
        log_entries = []
        if tr:
            for e in await _ha_logbook(runner.context.ha_client,
                                       tr[0]["entity_id"], hours):
                log_entries.append({
                    "when": e.get("when"), "name": e.get("name"),
                    "message": e.get("message"),
                    "entity_id": e.get("entity_id")})
        return {"ok": True, "device": canon,
                "history": out_hist, "count": len(out_hist),
                "ha_logbook": log_entries[:lim]}

    # actions that need a device: where / lost
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
    canon = names[0]
    beacon_names = {beacon_key.get(canon, canon)}
    out = await _merge_one(runner, canon, nodes=nodes, ha_fleet=ha_fleet,
                           scan=scan, registry=registry,
                           beacon_names=beacon_names)
    if len(names) > 1:
        out["also_matched"] = names[1:4]

    if args.get("gev_pin"):
        loc = out.get("location") or {}
        if loc.get("lat") is not None and loc.get("lon") is not None:
            try:
                out["gev_pin"] = await runner.gev_command(
                    name="annotate_map",
                    args={"annotations": [{
                        "type": "pin",
                        "latitude": float(loc["lat"]),
                        "longitude": float(loc["lon"]),
                        "label": f"{canon} — {out.get('age')}"}],
                        "flyTo": True})
            except Exception as exc:
                out["gev_pin"] = {"ok": False, "error": str(exc)}
        else:
            out["gev_pin"] = {"ok": False,
                              "error": "no coordinates to pin"}

    if action == "lost":
        armed = await _arm_lost(
            runner, canon,
            interval_s=int(args.get("interval_s") or _LOST_INTERVAL_S),
            channel=str(args.get("channel") or "both"),
            hours=float(args.get("hours") or _LOST_LIFETIME_H))
        armed["device"] = canon
        armed["last_seen"] = out
        return armed

    out["ok"] = True
    return out
