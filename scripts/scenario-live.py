#!/usr/bin/env python3
"""Live scenario driver for Ada voice sessions.

Replays scripted user text turns against a running Ada backend's /ws
endpoint and checks expectations against tool_call/tool_result trace
events and assistant transcripts.

Usage:
  python3 scripts/scenario-live.py tests/scenarios-live/michael_technician.yaml \
      --url ws://127.0.0.1:8003/ws --api-key "$ADA_API_KEY"

  --url defaults to $ADA_LIVE_URL or ws://127.0.0.1:8002/ws
  --api-key defaults to $ADA_API_KEY (matched against ADA_API_KEYS server-side)

Turn expectations (all optional, all must pass):
  calls_any: [tool, ...]       at least one of these tools was invoked
  calls: [tool, ...]           all of these tools were invoked
  no_calls: true               no tools were invoked
  no_calls_except: [tool, ...] no_calls, but these tools don't count (e.g. set_facial_expression)
  max_calls: N                 fail if more than N tool_call events fired this turn
  result_contains: [s, ...]    each substring appears in some tool_result
  result_contains_any: [s, ...]  at least one substring appears in a result
  response_contains: [s, ...]  each substring appears in the spoken transcript
  response_contains_any: [s, ...]  at least one substring appears (paraphrase-tolerant)
  response_nonempty: true      any spoken transcript at all
  timeout_s: N                 per-turn hard timeout (default 90)
  settle_s: N                  quiet period after last response (default 5)

Reconnect step (tests/scenarios-live/reconnect_continuity.yaml):
  - reconnect: {away_s: 300}   closes the ws, reconnects with
                               ?simulate_away_s=N, then checks the greeting
                               turn with the same expect vocabulary.

Isolation / negative assertions:
  response_not_contains: [s, ...]  each substring must NOT appear in speech
  result_not_contains: [s, ...]    each substring must NOT appear in results
  no_unattempted_refusal: [tool, ...]
      fail when the transcript claims inability ("can't", "ไม่สามารถ",
      "unable") yet none of the listed tools were even attempted.
  no_failed_result: [tool, ...]
      fail when a listed tool's result contains a nested {"ok": false}
      — e.g. gev_command delivering fly_to_location that the client
      rejected (phantom flight while Ada narrates arrival).

Geo/actuation ground truth:
  gev_view_near: [lat, lon, km]  with gev_screen: N
      out-of-band — asks the GEV bridge for live view state and requires
      some answering client's camera within km of the target. Multiple
      remote clients can answer; any matching camera passes, and failures
      report every camera seen. Proves the map MOVED, not that it was told to.
  result_geo_near: [lat, lon, km]
      softer in-band variant — some tool_result must carry a lat/lon
      within km of the target.
  call_args_contain: {tool: [sub | [alt1, alt2], ...]}
      the tool's call args must contain each required substring; a list
      entry is any-of alternates (e.g. Thai OR English place name).
      Proves narration named the place, not filler text.

Event assertions (any ws event, e.g. speaker/speaker_unrecognized):
  events_contain: [{type: speaker, name: "guest-tester"}, ...]
  events_not_contain: [{type: speaker}, ...]

Audio turns (speaker-ID / enrollment tests):
  - audio: tests/fixtures/voice-guest.pcm   streamed as binary PCM16 frames
    expect: {timeout_s: 120, settle_s: 10}
  .wav files are converted to PCM16 16 kHz mono automatically.

Upload turns (card attach flow — ada-voice-card/ada-chat-card 📎):
  - upload: tests/fixtures/doc-receipt.png
    filename: receipt.png   # optional override (default: basename)
    mode: both              # intake mode (default 'both', matching the cards)
    expect: {response_nonempty: true}
  The driver POSTs the image to {http_base}/api/documents/intake (with the
  scenario api key), then sends the same "[document uploaded via card]"
  session note the cards send — filename, intake_key, doc_type, size,
  warnings — as a ws text turn. The normal expect vocabulary checks Ada's
  response; the intake result lands in the turn log prompt.

Extra connect params (top-level):
  params: {simulate_unknown_speaker: 1}    appended to the ws URL on the
                                           first connect (reconnects keep
                                           their own params)

Persistence: the driver appends ?no_persist=1 so test sessions never write
transcripts, extraction drafts, summaries, or session-end markers. Pass
--persist to opt out (e.g. when the scenario itself exercises persistence).

Cleanup (runs even when expectations fail):
  cleanup:
    mddb_delete:
      - {collection: ada-ha-bank-general, key: "general/some-key"}
      - {collection: ada-ha-bank-general, contains: "marker zeta-9917"}
    speaker_remove: ["guest-tester"]      DELETE /api/speakers/<name>
  'contains' lists the collection, deletes every doc whose content or key
  matches the substring. MDDB base: $MDDB_BASE_URL or http://127.0.0.1:11023/v1

  'voice_restore: <instance|path>' restores the voice-preference file to its
  pre-run state — snapshot taken before the first turn (missing file is
  restored as absent). Use for scenarios that call ada_set_voice.

Turn fields:
  sleep_s: N         wait N seconds before running this turn — lets a pending
                     server-side reconnect (e.g. a voice switch) settle.

Failure taxonomy (decision: scenario-harness-false-negatives):
  Top-level `preflight:` gates the run before connecting —
    preflight:
      - {check: http_ok, url: "http://…/health", label: mddb}
      - {check: vcast_online, screens: [2]}        # listed screens connected
      - {check: vcast_online, min_online: 2}       # or at least N online
    A failed preflight exits INFRA (3) — environment down, not a model bug.
  Top-level `needs_tools: [name, ...]` compares against the live
  /api/tools declarations; absent tools exit UNIMPLEMENTED (4) — the
  scenario documents a planned feature, not a regression.
  `@family` names expand inside calls_any/calls/no_calls_except:
    @verify  — any state-verification tool (cctv_wall, vcast_snapshot,
               vcast_list, ada_ha_get_state)
    @cast    — any screen-cast tool (cast_to_screen, cctv_wall)
  `or_state:` — out-of-band outcome check; if the expected screen/relay
  state is already true, the turn passes even without a fresh cast call:
    or_state: {vcast_screen: 2, contains: "camwall"}
  Empty-transcript turns are retried once automatically (live-API
  interrupts mid-tool-call are a known race) before scoring.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import websockets
import yaml

# Tool families for @-expansion in calls_any / calls / no_calls_except.
TOOL_FAMILIES: dict[str, list[str]] = {
    "verify": ["cctv_wall", "vcast_snapshot", "vcast_list",
               "ada_ha_get_state", "display_state"],
    "cast": ["cast_to_screen", "cctv_wall", "vcast_screen_cast"],
    "gesture": ["vcast_gesture", "vcast_pose", "cast_to_screen"],
    "wall": ["cctv_wall"],
}

# Legacy tool names that tool_runner absorbs into canonical tools
# (backend/tool_runner.py _ALIASES). Ada can only ever call the canonical
# name, so an expectation written for the legacy name would fail on every
# correct call — accept either. Keep in sync with _ALIASES.
LEGACY_TOOL_ALIASES: dict[str, str] = {
    "guest_recall": "ada_memory_search",
    "vocab_note": "ada_remember",
    "report_habit_observation": "ada_remember",
    "guest_remember": "ada_remember",
    "guest_remember_private": "ada_remember",
    "ada_ha_recall": "ada_session_recall",
    "cctv_snapshot": "ada_camera_snapshot",
    "traffic_camera": "ada_camera_snapshot",
    "capture_frame": "vcast_snapshot",
    "cms_list_pages": "cms_read",
    "cms_get_page": "cms_read",
    "cms_verify_page": "cms_read",
    "cms_note_update": "cms_edit",
    "cms_delete_page": "cms_edit",
    "cms_automation": "cms_edit",
    # tools-merge-meta-voice (2026-10-05)
    "ada_set_voice": "ada_persona",
    "ada_outcome": "ada_ops",
    "ada_usage_summary": "ada_ops",
    "ada_mddb_health": "ada_ops",
    "ada_decision_check": "ada_ops",
    "ada_deep_research": "ada_ops",
    "guest_register": "ada_enroll_speaker",
}

_VCAST_API = os.environ.get(
    "VCAST_API", "https://tony-dell.taila0626a.ts.net/api/input-bridge")


def _expand_families(names: list | None) -> list[str]:
    out: list[str] = []
    for n in names or []:
        if isinstance(n, str) and n.startswith("@"):
            out.extend(TOOL_FAMILIES.get(n[1:], [n]))
        else:
            out.append(n)
            canonical = LEGACY_TOOL_ALIASES.get(n)
            if canonical:
                out.append(canonical)
    return out

# Date tokens expand in Asia/Bangkok — the container/host clock may be UTC
# while Ada's local timezone is Bangkok; relative-day assertions must match
# HER calendar, not the runner's.
_DOW_TH = ["วันจันทร์", "วันอังคาร", "วันพุธ", "วันพฤหัสบดี",
           "วันศุกร์", "วันเสาร์", "วันอาทิตย์"]
_DOW_EN = ["Monday", "Tuesday", "Wednesday", "Thursday",
           "Friday", "Saturday", "Sunday"]
_MON_TH = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม",
           "มิถุนายน", "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม",
           "พฤศจิกายน", "ธันวาคม"]
_MON_EN = ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November",
           "December"]


def _expand_tokens(obj: Any) -> Any:
    """Substitute {today}, {tomorrow}, {today_dow}, {tomorrow_dow},
    {today_dow_th}, {tomorrow_dow_th}, {today_dom}, {tomorrow_dom},
    {today_date_th}/{tomorrow_date_th} ("30 กันยายน"),
    {today_date_en}/{tomorrow_date_en} ("September 30") and
    {real_screen} in all strings of the loaded scenario.

    {real_screen} = the real-browser lab display (vcast-real@N on idc02;
    VCAST_REAL_SCREEN env, default 6). A string that is ONLY the token
    expands to int so `gev_screen: "{real_screen}"` / `screens: [...]`
    stay numeric; embedded uses ("fly screen {real_screen} to…") stay
    strings."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Bangkok")).date()
    tomo = today + timedelta(days=1)
    real = os.environ.get("VCAST_REAL_SCREEN") or "6"
    table = {
        "{today}": today.isoformat(),
        "{tomorrow}": tomo.isoformat(),
        "{today_dow}": _DOW_EN[today.weekday()],
        "{tomorrow_dow}": _DOW_EN[tomo.weekday()],
        "{today_dow_th}": _DOW_TH[today.weekday()],
        "{tomorrow_dow_th}": _DOW_TH[tomo.weekday()],
        "{today_dom}": str(today.day),
        "{tomorrow_dom}": str(tomo.day),
        "{today_date_th}": f"{today.day} {_MON_TH[today.month - 1]}",
        "{tomorrow_date_th}": f"{tomo.day} {_MON_TH[tomo.month - 1]}",
        "{today_date_en}": f"{_MON_EN[today.month - 1]} {today.day}",
        "{tomorrow_date_en}": f"{_MON_EN[tomo.month - 1]} {tomo.day}",
        "{real_screen}": real,
    }
    if isinstance(obj, str):
        for k, v in table.items():
            obj = obj.replace(k, v)
        if obj.strip() == real:
            return int(real)
        return obj
    if isinstance(obj, list):
        return [_expand_tokens(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _expand_tokens(v) for k, v in obj.items()}
    return obj


def check_turn(events: list[dict], expect: dict) -> list[str]:
    failures: list[str] = []
    calls = [e for e in events if e.get("type") == "tool_call"]
    results = [e for e in events if e.get("type") == "tool_result"]
    names = {c.get("name") for c in calls}
    transcript = "".join(
        str(e.get("text") or "")
        for e in events
        if e.get("type") == "assistant_transcript_delta"
    )
    calls_any = _expand_families(expect.get("calls_any"))
    if calls_any and not names & set(calls_any):
        failures.append(f"calls_any: none of {calls_any} in {sorted(names)}")
    for want in expect.get("calls") or []:
        alts = _expand_families([want])
        if not any(a in names for a in alts):
            failures.append(f"calls: {want!r} not in {sorted(names)}")
    if expect.get("no_calls"):
        exempt = set(_expand_families(expect.get("no_calls_except")))
        unexpected = sorted(names - exempt)
        if unexpected:
            failures.append(f"no_calls: got {unexpected} (exempt: {sorted(exempt)})")
    max_calls = expect.get("max_calls")
    if max_calls is not None and len(calls) > int(max_calls):
        failures.append(
            f"max_calls: {len(calls)} tool calls exceed limit {max_calls} "
            f"({sorted(names)})")
    for sub in expect.get("result_contains") or []:
        if not any(sub in json.dumps(r.get("result") or {}, default=str) for r in results):
            failures.append(f"result_contains: {sub!r} not in any tool_result")
    any_res = expect.get("result_contains_any") or []
    if any_res and not any(
            s in json.dumps(r.get("result") or {}, default=str)
            for s in any_res for r in results):
        failures.append(
            f"result_contains_any: none of {any_res} in any tool_result")
    for sub in expect.get("result_not_contains") or []:
        if any(sub in json.dumps(r.get("result") or {}, default=str) for r in results):
            failures.append(f"result_not_contains: {sub!r} leaked into a tool_result")
    for sub in expect.get("response_contains") or []:
        if sub.lower() not in transcript.lower():
            failures.append(
                f"response_contains: {sub!r} not in transcript ({transcript[:160]!r})"
            )
    any_subs = expect.get("response_contains_any") or []
    if any_subs and not any(s.lower() in transcript.lower() for s in any_subs):
        failures.append(
            f"response_contains_any: none of {any_subs} in transcript ({transcript[:160]!r})"
        )
    for sub in expect.get("response_not_contains") or []:
        if sub.lower() in transcript.lower():
            failures.append(f"response_not_contains: {sub!r} leaked into transcript")
    if expect.get("response_nonempty") and not transcript.strip():
        failures.append("response_nonempty: empty transcript")
    for want in expect.get("events_contain") or []:
        if not any(all(e.get(k) == v for k, v in want.items()) for e in events):
            failures.append(f"events_contain: no event matching {want}")
    for bad in expect.get("events_not_contain") or []:
        if any(all(e.get(k) == v for k, v in bad.items()) for e in events):
            failures.append(f"events_not_contain: matched {bad}")
    # claim-vs-action drift: transcript refuses ("can't / ไม่สามารถ /
    # unable") yet none of the listed tools were even attempted — the
    # 2026-09-29 ZA tour skipped fly_route this way.
    refusal_tools = expect.get("no_unattempted_refusal") or []
    if refusal_tools and not names & set(refusal_tools):
        if re.search(
                r"(ไม่สามารถ|cannot|can't|unable to|not able to|ไม่ได้)",
                transcript, re.I):
            failures.append(
                "unattempted_refusal: transcript claims inability but "
                f"none of {refusal_tools} were attempted "
                f"(calls: {sorted(names)})")
    # geo-fidelity: a tool_result must contain a lat/lon pair within km of
    # the target — catches "delivered" flies that landed somewhere else
    geo = expect.get("result_geo_near")
    if geo:
        want_lat, want_lon = float(geo[0]), float(geo[1])
        km = float(geo[2]) if len(geo) > 2 else 25.0
        if not any(_result_geo_within(r.get("result"), want_lat,
                                      want_lon, km)
                   for r in results):
            failures.append(
                f"result_geo_near: no tool_result within {km} km of "
                f"({want_lat}, {want_lon})")
    # nested failure: a listed tool's result contains an inner
    # {"ok": false} — e.g. gev_command fly_to_location returned
    # ok:false (query unresolvable) yet the outer call succeeded and
    # Ada narrated success. result_not_contains misses structured
    # falses; walk the payload. (2026-09-30 Cape Town phantom flight)
    failed_tools = expect.get("no_failed_result") or []
    if failed_tools:
        def _has_false_ok(x):
            if isinstance(x, dict):
                if x.get("ok") is False:
                    return True
                return any(_has_false_ok(v) for v in x.values())
            if isinstance(x, list):
                return any(_has_false_ok(v) for v in x)
            return False
        for r in results:
            if r.get("name") in failed_tools and _has_false_ok(r.get("result")):
                failures.append(
                    f"no_failed_result: {r.get('name')} result contains "
                    f"ok:false — action did not execute "
                    f"({str(r.get('result'))[:160]})")
    # out-of-band ground truth: ask the GEV bridge for the live camera
    # position — verifies where the map ACTUALLY is, not what a tool
    # result claimed. Needs expect.gev_screen for targeting.
    view = expect.get("gev_view_near")
    if view:
        want_lat, want_lon = float(view[0]), float(view[1])
        km = float(view[2]) if len(view) > 2 else 25.0
        positions = _gev_view_positions(expect.get("gev_screen"))
        if not positions:
            failures.append(
                "gev_view_near: no GEV view state — no client on "
                f"screen {expect.get('gev_screen')}")
        else:
            import math
            def _dist(p):
                dlat = math.radians(p[0] - want_lat)
                dlon = math.radians(p[1] - want_lon)
                aa = (math.sin(dlat / 2) ** 2
                      + math.cos(math.radians(want_lat))
                      * math.cos(math.radians(p[0]))
                      * math.sin(dlon / 2) ** 2)
                return 6371 * 2 * math.asin(math.sqrt(aa))
            dists = [_dist(p) for p in positions]
            if min(dists) > km:
                report = ", ".join(
                    f"({p[0]:.4f},{p[1]:.4f})={d:.0f}km"
                    for p, d in zip(positions, dists))
                failures.append(
                    f"gev_view_near: no client camera within {km} km of "
                    f"target — cameras: {report}")
    # narration content: a listed tool's call args must contain each
    # substring — proves the say/prompt carried the place, not filler
    for tool, subs in (expect.get("call_args_contain") or {}).items():
        for sub in subs:
            # a str entry is required; a list entry is any-of alternates
            # (e.g. narration may name the stop in Thai OR English)
            alts = sub if isinstance(sub, list) else [sub]
            if not any(c.get("name") == tool and
                       any(a in json.dumps(c.get("args") or {},
                                           ensure_ascii=False,
                                           default=str)
                           for a in alts)
                       for c in calls):
                failures.append(
                    f"call_args_contain: none of {alts} in "
                    f"{tool} args")
    # or_state: out-of-band outcome check — when the expected relay/screen
    # state already holds, waive tool-call assertions (the model may have
    # correctly declined a redundant cast after checking state).
    or_state = expect.get("or_state")
    if or_state and or_state.get("vcast_screen") is not None:
        scr = _vcast_screen(int(or_state["vcast_screen"]))
        contains = str(or_state.get("contains") or "")
        if scr is None:
            failures.append(
                f"or_state: screen {or_state['vcast_screen']} "
                "not registered on the relay")
        else:
            ok = True
            if contains:
                detail = str(scr.get("state_detail") or scr.get("state") or "")
                if contains not in detail:
                    ok = False
                    failures.append(
                        f"or_state: screen {or_state['vcast_screen']} state "
                        f"{detail[:100]!r} does not contain {contains!r}")
            if or_state.get("panes") is not None:
                if int(scr.get("panes") or 0) != int(or_state["panes"]):
                    ok = False
                    failures.append(
                        f"or_state: screen {or_state['vcast_screen']} panes="
                        f"{scr.get('panes')} want {or_state['panes']}")
            if or_state.get("state"):
                if scr.get("state") != or_state["state"]:
                    ok = False
                    failures.append(
                        f"or_state: screen {or_state['vcast_screen']} "
                        f"state={scr.get('state')!r} want "
                        f"{or_state['state']!r}")
            if ok:
                failures = [f for f in failures
                            if not f.startswith(
                                ("calls_any:", "calls:", "max_calls:"))]
    if or_state and or_state.get("camwall_settings"):
        # relay ground truth: the zone's stored settings must equal each
        # k:v — the tool result doesn't echo applied settings
        want_zone, want_kv = next(iter(or_state["camwall_settings"].items()))
        zone_state = _camwall_zone(str(want_zone))
        cur = (zone_state or {}).get("settings") or {}
        # list values match by membership (any order/extras ok); scalars
        # match exactly
        bad = {}
        for k, v in dict(want_kv).items():
            got = cur.get(k)
            if isinstance(v, list):
                if not v:                      # [] asserts empty
                    if isinstance(got, list) and got:
                        bad[k] = v
                else:
                    # wanted item matches an element exactly OR as a
                    # substring (effect strings carry suffixes like
                    # "yolo:person,car@0.35")
                    def _hit(x):
                        return (isinstance(got, list) and
                                any(x == g or str(x) in str(g)
                                    for g in got))
                    if any(not _hit(x) for x in v):
                        bad[k] = v
            elif got != v:
                bad[k] = v
        if zone_state is None:
            failures.append(f"or_state: camwall zone {want_zone!r} "
                            "absent from relay")
        elif bad:
            failures.append(
                f"or_state: camwall {want_zone} settings {cur} "
                f"missing/mismatched {bad}")
        else:
            failures = [f for f in failures
                        if not f.startswith(
                            ("calls_any:", "calls:", "max_calls:",
                             "result_contains:"))]
    return failures


def _camwall_zone(zone: str) -> dict | None:
    import urllib.request
    try:
        out = json.loads(urllib.request.urlopen(
            f"{_VCAST_API}/camwall", timeout=10).read())
    except Exception:
        return None
    return (out.get("zones") or {}).get(zone)


def _vcast_screen(n: int) -> dict | None:
    """Fetch the relay's display registry and return screen `n` (or None)."""
    import urllib.request
    try:
        out = json.loads(urllib.request.urlopen(
            f"{_VCAST_API}/displays", timeout=10).read())
    except Exception:
        return None
    for s in out.get("screens") or []:
        if int(s.get("screen") or -1) == n:
            return s
    return None


def run_preflight(spec: dict) -> list[str]:
    """Top-level scenario `preflight:` — environment checks that gate the
    run. Each failed check means the result class is INFRA, not a model
    regression."""
    import urllib.request
    fails: list[str] = []
    for pf in spec.get("preflight") or []:
        check = pf.get("check")
        label = pf.get("label") or pf.get("url") or check
        if check == "http_ok":
            url = pf.get("url")
            codes = set(pf.get("codes") or [200])
            try:
                req = urllib.request.Request(
                    url, headers={"x-api-key": pf.get("api_key") or ""})
                code = urllib.request.urlopen(req, timeout=10).status
            except urllib.error.HTTPError as e:
                code = e.code
            except Exception as e:
                fails.append(f"preflight {label}: {e}")
                continue
            if code not in codes:
                fails.append(f"preflight {label}: HTTP {code}")
        elif check == "vcast_online":
            import urllib.request as _ur
            try:
                out = json.loads(_ur.urlopen(
                    f"{_VCAST_API}/displays", timeout=10).read())
            except Exception as e:
                fails.append(f"preflight vcast_online: {e}")
                continue
            screens = {int(s.get("screen") or -1): s
                       for s in out.get("screens") or []}
            want = pf.get("screens")
            if want:
                off = [n for n in want
                       if not (screens.get(int(n)) or {}).get("connected")]
                if off:
                    fails.append(
                        f"preflight vcast_online: screens {off} offline")
            else:
                online = sum(1 for s in screens.values()
                             if s.get("connected"))
                if online < int(pf.get("min_online") or 1):
                    fails.append(
                        f"preflight vcast_online: {online} online, "
                        f"need {pf.get('min_online') or 1}")
        else:
            fails.append(f"preflight {label}: unknown check {check!r}")
    return fails


def missing_declared_tools(spec: dict, http_base: str,
                           api_key: str) -> list[str]:
    """Compare `needs_tools:` against the server's live /api/tools
    declarations — absent tools mean the scenario describes an
    unimplemented feature, not a regression."""
    import urllib.request
    want = spec.get("needs_tools") or []
    want_any = spec.get("needs_any") or []
    if not want and not want_any:
        return []
    try:
        req = urllib.request.Request(
            f"{http_base}/api/tools",
            headers={"x-api-key": api_key})
        have = set(json.loads(
            urllib.request.urlopen(req, timeout=10).read()).get("tools") or [])
    except Exception as e:
        print(f"needs_tools: /api/tools lookup failed ({e}) — skipping gate")
        return []
    missing = [t for t in want if t not in have]
    if want_any and not (have & set(want_any)):
        missing.append("one of " + "/".join(str(t) for t in want_any))
    return missing


_GEV_CMD_URL = os.environ.get(
    "GEV_CMD_URL",
    "https://tony-dell.taila0626a.ts.net/apps/gev-cmd/command")


def _gev_view_positions(screen: Any) -> list[tuple[float, float]]:
    """POST get_current_view_state to the GEV command relay; returns every
    answering client's camera (lat, lon). Multiple remote clients can be
    registered for one screen (stale tabs) — each answer counts."""
    import urllib.request
    try:
        req = urllib.request.Request(
            _GEV_CMD_URL,
            data=json.dumps({
                "name": "get_current_view_state", "args": {},
                "screen": screen, "wait": 5}).encode(),
            headers={"Content-Type": "application/json"})
        out = json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception:
        return None
    # the response nests the camera at responses[].response.camera
    # — only that position counts; annotation path/waypoint coords
    # elsewhere in the payload are NOT the camera (2026-09-29: a route's
    # path coords were mistaken for the camera mid-flight)
    cams = []
    def walk(x):
        if isinstance(x, dict):
            cam = x.get("camera")
            if isinstance(cam, dict):
                la, lo = cam.get("latitude"), cam.get("longitude")
                if isinstance(la, (int, float)) and isinstance(
                        lo, (int, float)):
                    cams.append((float(la), float(lo)))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(out)
    return cams


def _result_geo_within(result: Any, want_lat: float, want_lon: float,
                       km: float) -> bool:
    """True if `result` (nested dict/list) holds a lat/lon float pair
    within `km` of the target — loose equirectangular distance is fine
    at tour scales."""
    import math
    lats: list[float] = []
    lons: list[float] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                lk = str(k).lower()
                if isinstance(v, (int, float)):
                    if lk in ("latitude", "lat"):
                        lats.append(float(v))
                    elif lk in ("longitude", "lon", "lng"):
                        lons.append(float(v))
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(result)
    for la in lats:
        for lo in lons:
            dlat = math.radians(la - want_lat)
            dlon = math.radians(lo - want_lon)
            a = (math.sin(dlat / 2) ** 2
                 + math.cos(math.radians(want_lat))
                 * math.cos(math.radians(la)) * math.sin(dlon / 2) ** 2)
            if 6371 * 2 * math.asin(math.sqrt(a)) <= km:
                return True
    return False


def intake_upload(http_base: str, api_key: str, path: Path,
                  filename: str, mode: str) -> dict:
    """Mirror of the cards' _uploadDoc: POST the image to
    /api/documents/intake and return the parsed intake result."""
    import base64
    import urllib.request

    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    req = urllib.request.Request(
        f"{http_base}/api/documents/intake",
        data=json.dumps({
            "image_b64": base64.b64encode(path.read_bytes()).decode(),
            "image_mime": mime,
            "filename": filename,
            "mode": mode,
        }).encode(),
        headers={"Content-Type": "application/json", "x-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def doc_upload_note(filename: str, out: dict) -> str:
    """The exact session note the cards inject after a successful intake —
    keep byte-identical semantics with ada-voice-card/ada-chat-card."""
    measured = out.get("measured") or {}
    w = measured.get("width") or "?"
    h = measured.get("height") or "?"
    warns = [str(x) for x in (out.get("warnings") or [])]
    return (
        f"[document uploaded via card] file={filename} "
        f"intake_key={out.get('key')} type={out.get('doc_type')} size={w}x{h}"
        + (f" warnings={'; '.join(warns)}" if warns else "")
        + " — ask what to do with it (archive/print);"
          " the intake key is held in RAM only."
    )


async def fire_notify(http_base: str, api_key: str, spec: dict,
                      session_id: str | None = None) -> None:
    """POST /api/notify after spec.delay_s — a data-package arrival landing
    mid-response, concurrent with the in-flight turn. session_id pins the
    notify to our own ws — newest-session targeting would otherwise hand it
    to whichever client connected most recently."""
    import urllib.request
    await asyncio.sleep(float(spec.get("delay_s") or 0))
    payload = {"text": str(spec.get("text") or "notification")}
    if spec.get("urgent"):
        payload["urgent"] = True
    if session_id:
        payload["session"] = session_id
    req = urllib.request.Request(
        f"{http_base}/api/notify",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": api_key},
        method="POST")
    try:
        await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=30).read())
    except Exception as exc:
        print(f"      notify POST failed: {exc}")


def load_audio(path: Path) -> bytes:
    """Return raw PCM16 16 kHz mono bytes from .pcm or .wav."""
    if path.suffix.lower() == ".wav":
        import wave
        with wave.open(str(path), "rb") as w:
            if w.getframerate() != 16000 or w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise ValueError(
                    f"{path}: wav must be 16 kHz 16-bit mono "
                    f"(got {w.getframerate()}Hz/{w.getsampwidth() * 8}bit/{w.getnchannels()}ch)"
                )
            return w.readframes(w.getnframes())
    return path.read_bytes()


async def run_turn(
    ws: Any, text: str | None, expect: dict, verbose: bool,
    audio: bytes | None = None,
) -> tuple[list[dict], list[str]]:
    timeout = float(expect.get("timeout_s", 90))
    settle = float(expect.get("settle_s", 5))
    events: list[dict] = []
    t0 = time.monotonic()
    if text is not None:
        await ws.send(json.dumps({"type": "text", "text": text}))
    if audio is not None:
        # ~50 ms frames — same cadence a real mic produces.
        frame = 1600
        for off in range(0, len(audio), frame):
            await ws.send(audio[off: off + frame])
            await asyncio.sleep(0.05)
        # Trailing silence: the server VAD measures silence_duration_ms on
        # the incoming stream — with no frames after the utterance the turn
        # never closes and the model never responds (learned 2026-09-24).
        silence = bytes(frame)
        for _ in range(30):  # ~1.5 s
            await ws.send(silence)
            await asyncio.sleep(0.05)
    deadline = time.monotonic() + timeout
    last_completed = 0.0
    completed = 0
    while time.monotonic() < deadline:
        # Once a response has completed, wait out the settle window for a
        # possible follow-up turn (recall results arrive as injected turns).
        wait = (
            min(settle, deadline - time.monotonic())
            if completed
            else deadline - time.monotonic()
        )
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(wait, 0.1))
        except asyncio.TimeoutError:
            break
        if isinstance(raw, bytes):
            continue  # audio
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg["_t"] = round(time.monotonic() - t0, 3)
        events.append(msg)
        if verbose:
            t = msg.get("type")
            if t == "tool_call":
                print(f"      tool_call {msg.get('name')} {msg.get('args')}")
            elif t == "tool_result":
                print(f"      tool_result {msg.get('name')}")
            elif t in ("error", "live_reconnecting"):
                print(f"      {t}: {msg}")
        if msg.get("type") == "response_completed":
            completed += 1
            last_completed = time.monotonic()
        if completed and time.monotonic() - last_completed > settle:
            break
    return events, check_turn(events, expect)


def _voice_file_for(value: str) -> Path:
    """Resolve cleanup.voice_restore — an instance name ('tony') or a path."""
    v = str(value or "").strip()
    if "/" in v or v.endswith(".json"):
        return Path(os.path.expanduser(v))
    return Path.home() / ".config" / "ada" / f"voice-{v}.json"


def run_cleanup(spec: dict, mddb_url: str, verbose: bool) -> None:
    """Best-effort post-run cleanup — deletes test docs from MDDB."""
    import urllib.request

    def post(path: str, payload: dict) -> Any:
        req = urllib.request.Request(
            f"{mddb_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    for item in (spec.get("cleanup") or {}).get("mddb_delete") or []:
        col = item.get("collection")
        if not col:
            continue
        keys: list[str] = []
        if item.get("key"):
            keys = [item["key"]]
        elif item.get("contains"):
            sub = str(item["contains"]).lower()
            try:
                docs = post("/search", {"collection": col, "limit": 300})
            except Exception as exc:
                print(f"  cleanup: list {col} failed: {exc}")
                continue
            keys = [
                d["key"] for d in docs
                if sub in (str(d.get("key") or "") + (d.get("contentMd") or "")).lower()
            ]
        langs = item.get("langs") or [item.get("lang") or "en"]
        for key in keys:
            for lang in langs:
                try:
                    post("/delete", {"collection": col, "key": key, "lang": lang})
                    if verbose:
                        print(f"  cleanup: deleted {col}/{key} ({lang})")
                except Exception as exc:
                    print(f"  cleanup: delete {col}/{key} ({lang}) failed: {exc}")


def run_speaker_cleanup(spec: dict, http_base: str, api_key: str, verbose: bool) -> None:
    """DELETE /api/speakers/<name> for each cleanup.speaker_remove entry."""
    import urllib.parse
    import urllib.request

    for name in (spec.get("cleanup") or {}).get("speaker_remove") or []:
        try:
            req = urllib.request.Request(
                f"{http_base}/api/speakers/{urllib.parse.quote(str(name), safe='')}",
                method="DELETE", headers={"x-api-key": api_key},
            )
            urllib.request.urlopen(req, timeout=10).read()
            if verbose:
                print(f"  cleanup: removed speaker '{name}'")
        except Exception as exc:
            print(f"  cleanup: speaker '{name}' remove failed: {exc}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", type=Path)
    ap.add_argument("--url", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--persist", action="store_true",
                    help="allow the test session to persist transcripts/markers "
                         "(default: appends no_persist=1)")
    ap.add_argument("--no-cleanup", action="store_true",
                    help="skip the scenario's cleanup block")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-t", "--timing", action="store_true",
                    help="print per-turn event timeline (send->event offsets)")
    ap.add_argument("--events-json", default=None,
                    help="write per-turn records [{n,ts,kind,prompt,tools,"
                         "transcript,ok,failures}] to this path — the "
                         "scenario-report runner renders them as a timeline")
    args = ap.parse_args()

    import os

    url = args.url or os.environ.get("ADA_LIVE_URL") or "ws://127.0.0.1:8002/ws"
    api_key = args.api_key or os.environ.get("ADA_API_KEY") or ""
    if api_key:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}api_key={api_key}"

    # One live scenario per host at a time — concurrent runs share the Ada
    # session AND the vcast screens, silently corrupting each other
    # (2026-09-30: casting suite scored 0.0 while the hourly smoke tier
    # held the session; every scenario "failed" with 0 tool calls).
    # Lock is a TCP bind, not a file lock — the smoke tier runs in a
    # host-network container whose /tmp is private. Wait rather than fail
    # so queued runs still execute.
    import socket
    _lock = socket.socket()
    _locked = False
    for _ in range(120):  # wait up to 10 min for a wedged holder
        try:
            # 8198 = per-scenario turn lock; 8199 = whole-suite lock held
            # by scenario-benchmark/scenario-report for their full run.
            _lock.bind(("127.0.0.1", 8198))
            _locked = True
            break
        except OSError:
            await asyncio.sleep(5)
    if not _locked:
        print("scenario-live: lock held >10 min — giving up")
        return 3
    _lock.listen(1)
    if not args.persist:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}no_persist=1"

    spec = _expand_tokens(yaml.safe_load(args.scenario.read_text()))
    for k, v in (spec.get("params") or {}).items():
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{k}={v}"
    http_base = (url.split("?")[0]
                 .replace("ws://", "http://").replace("wss://", "https://")
                 .rsplit("/ws", 1)[0])

    my_session: list[str] = [""]

    async def connect(target: str) -> Any:
        ws = await websockets.connect(target, max_size=8 * 1024 * 1024)
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            if isinstance(raw, str):
                msg = json.loads(raw)
                if msg.get("type") == "ready":
                    my_session[0] = str(msg.get("session") or "")
                    return ws

    turns = spec.get("turns") or []
    print(f"scenario: {spec.get('name') or args.scenario.stem} -> {url.split('?')[0]}")

    # Failure taxonomy gates — run before connecting so environment and
    # unimplemented-feature cases never produce misleading FAILs.
    missing = missing_declared_tools(spec, http_base, api_key)
    if missing:
        print(f"UNIMPLEMENTED: tools not in live declarations: {missing}")
        return 4
    pf_fails = run_preflight(spec)
    if pf_fails:
        for f in pf_fails:
            print(f"  {f}")
        print("INFRA: preflight failed — scenario not run")
        return 3

    # voice_restore: snapshot the preference file before any turn can change it.
    voice_restore = (spec.get("cleanup") or {}).get("voice_restore")
    voice_snapshot: tuple[Path, bytes | None] | None = None
    if voice_restore:
        vf = _voice_file_for(str(voice_restore))
        voice_snapshot = (vf, vf.read_bytes() if vf.exists() else None)

    total_fail = 0
    turn_log: list[dict[str, Any]] = []
    run_started = time.time()
    ws = None
    try:
        try:
            ws = await connect(url)
        except asyncio.TimeoutError:
            print("FATAL: no ready event from server")
            return 2
        print("connected (ready)")

        for i, turn in enumerate(turns):
            expect = dict(turn.get("expect") or {})
            expect.setdefault("timeout_s", turn.get("timeout_s", 90))
            expect.setdefault("settle_s", turn.get("settle_s", 5))
            if turn.get("sleep_s"):
                await asyncio.sleep(float(turn["sleep_s"]))
            turn_t0 = time.time()
            kind, prompt = "text", ""
            if "reconnect" in turn:
                away = (turn.get("reconnect") or {}).get("away_s")
                rurl = url
                if away is not None:
                    sep = "&" if "?" in rurl else "?"
                    rurl = f"{rurl}{sep}simulate_away_s={away}"
                print(f"turn {i + 1}: reconnect (away_s={away})")
                kind, prompt = "reconnect", f"away_s={away}"
                await ws.close()
                ws = await connect(rurl)
                # The reconnect prime triggers the greeting turn unprompted —
                # collect it like a normal response window.
                events, failures = await run_turn(ws, None, expect, args.verbose)
                text = None
            else:
                audio_bytes = None
                upload_error = None
                if turn.get("audio"):
                    # Try tests/fixtures/<name> first (scenarios-live -> tests),
                    # then a path relative to the scenario file itself.
                    audio_path = (args.scenario.parent / ".." / str(turn["audio"])).resolve()
                    if not audio_path.exists():
                        audio_path = args.scenario.parent / str(turn["audio"])
                    audio_bytes = load_audio(audio_path)
                    kind = "audio"
                    prompt = audio_path.name
                    print(f"turn {i + 1}: audio {audio_path.name} "
                          f"({len(audio_bytes) / 32000:.1f}s)")
                    text = str(turn.get("user") or "") or None
                elif turn.get("speech"):
                    # Client-VAD speech frames with no audio payload —
                    # reproduces the dead-turn stall (user heard, no turn
                    # ever reaches the model). The speech-stall nudge in
                    # pwa_server should make Ada voice a one-liner.
                    kind = "speech"
                    burst_ms = int((turn.get("speech") or {}).get("burst_ms") or 800)
                    prompt = f"speech burst {burst_ms}ms (no audio)"
                    print(f"turn {i + 1}: {prompt}")
                    await ws.send(json.dumps({"type": "local_speech_started"}))
                    await asyncio.sleep(burst_ms / 1000.0)
                    await ws.send(json.dumps({"type": "local_speech_stopped"}))
                    text = None
                elif turn.get("upload"):
                    kind = "upload"
                    img_path = (args.scenario.parent / ".." / str(turn["upload"])).resolve()
                    if not img_path.exists():
                        img_path = args.scenario.parent / str(turn["upload"])
                    fname = str(turn.get("filename") or img_path.name)
                    print(f"turn {i + 1}: upload {fname}")
                    try:
                        out = await asyncio.to_thread(
                            intake_upload, http_base, api_key, img_path,
                            fname, str(turn.get("mode") or "both"),
                        )
                        text = doc_upload_note(fname, out)
                        prompt = f"{fname} → {out.get('doc_type')} ({out.get('key')})"
                        if args.verbose:
                            print(f"      intake: {prompt}")
                    except Exception as exc:
                        upload_error = str(exc)
                        text = None
                        prompt = f"{fname} (intake failed)"
                else:
                    text = str(turn.get("user") or "")
                    prompt = text[:120]
                    print(f"turn {i + 1}: {text!r}")
                if upload_error:
                    events, failures = [], [f"upload: {upload_error}"]
                else:
                    # turn.notify: {text, urgent, delay_s} — fires
                    # POST /api/notify concurrently while the response is
                    # in flight (mid-response interruption test).
                    notify_task = None
                    if turn.get("notify"):
                        notify_task = asyncio.create_task(
                            fire_notify(http_base, api_key,
                                        dict(turn["notify"]),
                                        session_id=my_session[0] or None))
                    try:
                        events, failures = await run_turn(
                            ws, text, expect, args.verbose, audio=audio_bytes
                        )
                        # Live-API interrupts mid-tool-call can produce an
                        # empty turn; retry once before scoring it. Any
                        # expectation that demands speech counts.
                        transcript_now = "".join(
                            str(e.get("text") or "") for e in events
                            if e.get("type") == "assistant_transcript_delta")
                        expects_speech = any(
                            expect.get(k) for k in
                            ("response_nonempty", "response_contains",
                             "response_contains_any"))
                        if (not transcript_now.strip()
                                and expects_speech and text is not None):
                            print("      empty turn — retrying once")
                            events, failures = await run_turn(
                                ws, text, expect, args.verbose)
                            if not failures:
                                events = events + [{
                                    "type": "_note",
                                    "text": "passed on empty-turn retry"}]
                    finally:
                        if notify_task:
                            notify_task.cancel()
            if args.timing:
                for e in events:
                    t = e.get("type")
                    if t in ("tool_call", "tool_result", "response_completed",
                             "error", "live_reconnecting"):
                        extra = f" {e.get('name')}" if e.get("name") else ""
                        print(f"      +{e.get('_t', 0):6.1f}s {t}{extra}")
            n_tools = sum(1 for e in events if e.get("type") == "tool_call")
            transcript = "".join(
                str(e.get("text") or "")
                for e in events
                if e.get("type") == "assistant_transcript_delta"
            )
            turn_log.append({
                "n": i + 1,
                "ts": round(turn_t0 - run_started, 1),
                "wall": time.strftime("%H:%M:%S", time.localtime(turn_t0)),
                "kind": kind, "prompt": prompt,
                "tools": [str(e.get("name")) for e in events
                          if e.get("type") == "tool_call"],
                "transcript": transcript[:200],
                "ok": not failures,
                "failures": [str(f) for f in (failures or [])],
            })
            if failures:
                total_fail += len(failures)
                print(f"  FAIL ({n_tools} tool calls)")
                for f in failures:
                    print(f"    - {f}")
                print(f"    transcript: {transcript[:300]!r}")
            else:
                print(f"  ok ({n_tools} tool calls) ada: {transcript[:140]!r}")
    finally:
        if ws is not None:
            await ws.close()
        if not args.no_cleanup:
            mddb_url = os.environ.get("MDDB_BASE_URL") or "http://127.0.0.1:11023/v1"
            run_cleanup(spec, mddb_url.rstrip("/"), args.verbose)
            run_speaker_cleanup(spec, http_base, api_key, args.verbose)
            if voice_snapshot is not None:
                vf, data = voice_snapshot
                try:
                    if data is None:
                        vf.unlink(missing_ok=True)
                    else:
                        vf.parent.mkdir(parents=True, exist_ok=True)
                        vf.write_bytes(data)
                    if args.verbose:
                        print(f"  cleanup: restored voice file {vf}")
                except Exception as exc:
                    print(f"  cleanup: voice restore failed: {exc}")

    if args.events_json:
        try:
            Path(args.events_json).write_text(json.dumps({
                "scenario": spec.get("name") or args.scenario.stem,
                "started": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(run_started)),
                "duration_s": round(time.time() - run_started, 1),
                "turns": turn_log,
                "passed": total_fail == 0,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            print(f"events-json write failed: {exc}")
    print(f"{'PASS' if total_fail == 0 else 'FAIL'}: {total_fail} failed expectations")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
