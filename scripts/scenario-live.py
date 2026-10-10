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
  no_calls: [tool, ...]        blacklist — these tools must NOT be invoked
  no_calls_except: [tool, ...] no_calls:true, but these tools don't count (e.g. set_facial_expression)
  max_calls: N                 fail if more than N tool_call events fired this turn
  result_contains: [s, ...]    each substring appears in some tool_result
                     (or tool_result_full under ADA_TRACE_FULL=1 — the
                     complete payload, so result_* checks assert on
                     ground truth instead of the 300-char stump)
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
      - {check: gev_relay}                          # gev-cmd relay answering —
      #   tours create their own map client by casting, so this only checks
      #   the endpoint is alive (a 404/down relay = guaranteed-fail turns)
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
  Accepts a list — every entry must hold for the call-assertion waiver
  to apply (multi-screen turns):
    or_state: [{vcast_screen: 6, contains: apps}, {vcast_screen: 7, contains: apps}]
  Empty-transcript turns are retried once automatically (live-API
  interrupts mid-tool-call are a known race) before scoring.

Bridge-driver turns (GEV — no Ada ws needed):
  - gev_bridge: {text: "fly to Bangkok", respond: {ok: true}}
      plays the GEV client's half of the gev-gemini bridge protocol on
      $GEV_LIVE_WS (default wss://tony-dell.taila0626a.ts.net/apps/gev-live/ws,
      ws://127.0.0.1:8789 locally): connect -> status greeting -> send
      {"type":"text"} -> collect function_call msgs -> answer each with
      {"type":"tool_response","responses":[{id,name,response:respond}]}
      -> collect text deltas + done. Events are normalized into the
      standard vocabulary (function_call -> tool_call, our reply ->
      tool_result, text -> assistant_transcript_delta, done ->
      response_completed) so calls_any / call_args_contain /
      response_nonempty / events_contain all work unmodified. The card
      verify "text -> function_call -> tool_response over wss
      /apps/gev-live/ws" maps to calls_any + response_nonempty.
      Connect/greeting failure aborts the run as INFRA.
  - http_check: {url|page, status, contains, not_contains, regex,
                 not_regex, capture}
      HTTP guard turn — fetch a URL, or `page:` an HTML document and
      follow its first <script type="module" src> to fetch the served
      bundle; then assert status/contains/not_contains/regex/not_regex
      on the body (page_contains/page_regex/… assert on the HTML when
      `page:` is used). Used by the served-code guards: sw.js
      stale-cache regression and the GEV bundle mute-wiring invariant.
      `capture: {name: "<regex>"}` lifts the first capture group of each
      match into the turn's "metrics" in --events-json — scenario-benchmark
      records them on the benchmark doc, so artifact metrics (e.g. the
      dub-metrics/1 sidecars) are trended, not just pass/failed.

Ada ws is opened lazily — a scenario whose turns are all driver-side
(vcast_display / gev_bridge / http_check) never connects to /ws, so a
down Ada doesn't misclassify a GEV/infra probe as FATAL.

Display-driver turns (no prompt to Ada — the driver itself actuates):
  - vcast_display: {action: claim, name: flap-test}   rides the real
      input-bridge pending→claim→paired→registered flow; the screen
      number it lands on becomes the {flap_screen} token for later
      turns. Claim failure aborts the run as INFRA (needs tailnet
      trust + ADA_ADMIN_KEY on the relay).
  - vcast_display: {action: flap, times: 3, down_s: 0.5}  drops and
      re-registers the display ws — the relay must supersede the old
      socket and replay remembered casts; asserts same-screen
      re-registration and connected:true afterwards.
  - vcast_display: {action: drop}    ws closes and stays down; asserts
      the registry marks the screen offline promptly (no stale room).
  - vcast_display: {action: reattach}  reconnects; lastCast replays.
  - vcast_display: {action: pub, url: ...}  driver-side nav cast into the
      display's room — while down it exercises the relay's
      remember-and-replay-on-reconnect path (delivered:0, lastCast set).
  - vcast_display: {action: release}   unclaims the name + revokes the
      issued ada key; also auto-run in cleanup if a display is held.
  The fake display applies play/image/nav/stop/layout msgs and
  re-reports state like vcast-headless.mjs, so /displays keeps
  reflecting what it 'shows'.
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
    "calendar_list_events": "calendar_read",
    "calendar_list_calendars": "calendar_read",
    # tools-merge-calendar-plan (missed by that card — resynced with
    # tool_runner._ALIASES here)
    "calendar_list_calendars": "calendar_read",
    "calendar_list_events": "calendar_read",
    "calendar_freebusy": "calendar_read",
    "calendar_create_event": "calendar_write",
    "calendar_delete_event": "calendar_write",
    "calendar_shift_overdue": "calendar_write",
    "ada_daily_summary": "plan_day",
    "ada_weekly_comparison": "plan_day",
    "ada_doc_search": "docs",
    "ada_doc_get": "docs",
    "ada_doc_print": "docs",
    "ada_doc_archive": "docs",
    "drive_search": "drive",
    "drive_show": "drive",
    "drive_get": "drive",
    "drive_update": "drive",
    "gev_tour": "gev_command",
    # ha family — tools-merge-ha (2026-10-05)
    "search_home_devices": "home_search",
    "list_home_devices": "home_search",
    "search_devices": "home_search",
    "search_sensors": "home_search",
    "list_sensors": "home_search",
    "ada_ha_search_devices": "home_search",
    "ada_ha_search_sensors": "home_search",
    "ada_ha_search_events": "home_search",
    "ada_ha_get_state": "get_home_state",
    "get_logbook": "home_history",
    "get_sensor_history": "home_history",
    "get_entity_events": "home_history",
    "get_recent_events": "home_history",
    "ada_ha_history": "home_history",
    "control_cover": "control_entity",
    "control_media_player": "control_entity",
    "press_button": "control_entity",
    "ada_ha_get_device_confidence": "ha_confidence",
    "ada_ha_set_device_confidence": "ha_confidence",
    "vcast_say": "cast_to_screen",
    "vcast_list": "cast_to_screen",
    "vcast_status": "cast_to_screen",
    "vcast_shortcut": "cast_to_screen",
    # ada-kanban-access (2026-10-08)
    "ada_board_write": "kanban",
}

# Implied args an absorbed name carries into the canonical call
# (mirror of tool_runner._ALIAS_ARG_DEFAULTS). call_args_contain uses
# these to tighten canonical-side matches: a vcast_say arg check must
# only match cast_to_screen(action='say') calls, not a stray nav cast.
# Keep in sync with _ALIAS_ARG_DEFAULTS.
LEGACY_ARG_DEFAULTS: dict[str, dict] = {
    "vcast_say": {"action": "say"},
    "vcast_list": {"action": "list"},
    "vcast_status": {"action": "status"},
    "vcast_shortcut": {"action": "shortcut"},
    # tools-merge-meta-voice (2026-10-05)
    "ada_set_voice": "ada_persona",
    "ada_outcome": "ada_ops",
    "ada_usage_summary": "ada_ops",
    "ada_mddb_health": "ada_ops",
    "ada_decision_check": "ada_ops",
    "ada_deep_research": "ada_ops",
    "guest_register": "ada_enroll_speaker",
    # tools-merge-tasks-status (2026-10-05)
    "tasks_add": "tasks",
    "tasks_list": "tasks",
    "tasks_complete": "tasks",
    "tasks_move": "tasks",
    "get_battery_status": "home_status",
    "get_battery_detail": "home_status",
    "get_power_summary": "home_status",
    "get_inverter_status": "home_status",
    "get_pool_status": "home_status",
    "get_dashboard_tab": "home_status",
    "get_habit_status": "home_status",
    "photos_pick": "chat_send",
    "photos_picked": "chat_send",
    "sys_show_uploaded_document": "chat_send",
    "process_document_upload": "chat_send",
    "doc_upload_card_action": "chat_send",
    "yt_cast": "yt",
    "yt_cast_status": "yt",
    "yt_cast_stop": "yt",
    "yt_transcript": "yt",
}

_VCAST_API = os.environ.get(
    "VCAST_API", "https://tony-dell.taila0626a.ts.net/api/input-bridge")

_GEV_LIVE_WS = os.environ.get(
    "GEV_LIVE_WS",
    "wss://tony-dell.taila0626a.ts.net/apps/gev-live/ws")


def _expand_families(names: list | None) -> list[str]:
    # Alias expansion applies to family members too — '@verify' carries
    # vcast_list, which now lands as cast_to_screen(action='list')
    # (tools-merge-display).
    flat: list[str] = []
    for n in names or []:
        if isinstance(n, str) and n.startswith("@"):
            flat.extend(TOOL_FAMILIES.get(n[1:], [n]))
        else:
            flat.append(n)
    out: list[str] = []
    for n in flat:
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
    VCAST_REAL_SCREEN env, default 6). {real_screen2} = the second
    real-browser display (VCAST_REAL_SCREEN2 env, default 7) — used by
    multi-device scenarios. A string that is ONLY the token expands to
    int so `gev_screen: "{real_screen}"` / `screens: [...]` stay numeric;
    embedded uses ("fly screen {real_screen} to…") stay strings."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Bangkok")).date()
    tomo = today + timedelta(days=1)
    real = os.environ.get("VCAST_REAL_SCREEN") or "6"
    real2 = os.environ.get("VCAST_REAL_SCREEN2") or "7"
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
        "{real_screen2}": real2,
    }
    if isinstance(obj, str):
        for k, v in table.items():
            obj = obj.replace(k, v)
        if obj.strip() in (real, real2):
            return int(obj.strip())
        return obj
    if isinstance(obj, list):
        return [_expand_tokens(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _expand_tokens(v) for k, v in obj.items()}
    return obj


def check_turn(events: list[dict], expect: dict) -> list[str]:
    failures: list[str] = []
    calls = [e for e in events if e.get("type") == "tool_call"]
    # tool_result_full rides beside tool_result when the provider runs
    # ADA_TRACE_FULL=1 (card ada-trace-full-result) — same {name, result}
    # shape but the complete payload, so result_* assertions check
    # ground truth instead of the 300-char stump. Off-flag runs see only
    # the stump and behave exactly as before.
    results = [e for e in events
               if e.get("type") in ("tool_result", "tool_result_full")]
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
    nc = expect.get("no_calls")
    if isinstance(nc, list):
        # blacklist form: these tools must not appear in the turn
        banned = set(_expand_families(nc))
        bad = sorted(names & banned)
        if bad:
            failures.append(f"no_calls: banned tools called {bad}")
    elif nc:
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
    # token shape (card ada-context-budget): 'usage' events carry per-turn
    # {"in": N, "out": N, "total_in": N}. input_tokens_below asserts the
    # context ceiling; input_tokens_above asserts traffic actually flowed
    # (a below-assertion on a silent turn is vacuous otherwise).
    usage_in = [e.get("in") for e in events
                if e.get("type") == "usage" and isinstance(e.get("in"), int)]
    cap = expect.get("input_tokens_below")
    if cap is not None:
        if not usage_in:
            failures.append("input_tokens_below: no usage events seen")
        elif max(usage_in) > int(cap):
            failures.append(
                f"input_tokens_below: peak {max(usage_in)} > {int(cap)}")
    floor = expect.get("input_tokens_above")
    if floor is not None:
        if not usage_in:
            failures.append("input_tokens_above: no usage events seen")
        elif max(usage_in) <= int(floor):
            failures.append(
                f"input_tokens_above: peak {max(usage_in)} <= {int(floor)}")
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
    # failed tool result (2026-09-30 Cape Town phantom flight): the
    # uniform {ok: bool} contract (tool-error-contract) normalizes every
    # failure shape — {"error"}, {"err"}, buried responses[].response.ok,
    # raised exceptions — to top-level ok:false at the runner/provider
    # boundary, so this check reads the canonical field directly.
    failed_tools = expect.get("no_failed_result") or []
    if failed_tools:
        for r in results:
            res = r.get("result")
            if (r.get("name") in failed_tools and isinstance(res, dict)
                    and res.get("ok") is False):
                failures.append(
                    f"no_failed_result: {r.get('name')} returned "
                    f"ok:false — action did not execute "
                    f"({str(res)[:160]})")
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
        # absorbed names land as their canonical tool (tools-merge-*):
        # a vcast_say arg check matches cast_to_screen calls that carry
        # the implied action='say' seat, not just any cast_to_screen.
        canonical = LEGACY_TOOL_ALIASES.get(tool)
        implied = LEGACY_ARG_DEFAULTS.get(tool) or {}
        for sub in subs:
            # a str entry is required; a list entry is any-of alternates
            # (e.g. narration may name the stop in Thai OR English)
            alts = sub if isinstance(sub, list) else [sub]
            ok = False
            for c in calls:
                cname, cargs = c.get("name"), c.get("args") or {}
                if cname == tool:
                    seat = True
                elif canonical and cname == canonical:
                    seat = all(cargs.get(k) == v
                               for k, v in implied.items())
                else:
                    continue
                if seat and any(
                        a in json.dumps(cargs, ensure_ascii=False,
                                        default=str)
                        for a in alts):
                    ok = True
                    break
            if not ok:
                failures.append(
                    f"call_args_contain: none of {alts} in "
                    f"{tool} args")
    # or_state: out-of-band outcome check — when the expected relay/screen
    # state already holds, waive tool-call assertions (the model may have
    # correctly declined a redundant cast after checking state). Accepts
    # a single spec or a list — each passing entry strips its own prefix
    # set, so a multi-target turn waives only when ALL targets hold.
    or_state = expect.get("or_state")
    specs = or_state if isinstance(or_state, list) else [or_state] if or_state else []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        f, strip = _eval_or_state(spec)
        if f:
            failures += f
        else:
            failures = [x for x in failures if not x.startswith(strip)]
    return failures


def _eval_or_state(spec: dict) -> tuple[list[str], tuple]:
    """One or_state spec → (failures, prefixes stripped when it passes)."""
    if spec.get("vcast_screen") is not None:
        return _or_state_vcast(spec), ("calls_any:", "calls:", "max_calls:")
    if spec.get("camwall_settings"):
        return _or_state_camwall(spec), (
            "calls_any:", "calls:", "max_calls:", "result_contains:")
    return ["or_state: unsupported keys " + ",".join(sorted(spec))], ()


def _or_state_vcast(or_state: dict) -> list[str]:
    scr = _vcast_screen(int(or_state["vcast_screen"]))
    contains = str(or_state.get("contains") or "")
    out = []
    if scr is None:
        return [f"or_state: screen {or_state['vcast_screen']} "
                "not registered on the relay"]
    if contains:
        detail = str(scr.get("state_detail") or scr.get("state") or "")
        if contains not in detail:
            out.append(
                f"or_state: screen {or_state['vcast_screen']} state "
                f"{detail[:100]!r} does not contain {contains!r}")
    contains_any = or_state.get("contains_any")
    if contains_any:
        detail = str(scr.get("state_detail") or scr.get("state") or "")
        if not any(c in detail for c in contains_any):
            out.append(
                f"or_state: screen {or_state['vcast_screen']} state "
                f"{detail[:100]!r} contains none of {contains_any}")
    if or_state.get("panes") is not None:
        if int(scr.get("panes") or 0) != int(or_state["panes"]):
            out.append(
                f"or_state: screen {or_state['vcast_screen']} panes="
                f"{scr.get('panes')} want {or_state['panes']}")
    if or_state.get("state"):
        if scr.get("state") != or_state["state"]:
            out.append(
                f"or_state: screen {or_state['vcast_screen']} "
                f"state={scr.get('state')!r} want "
                f"{or_state['state']!r}")
    return out


def _or_state_camwall(or_state: dict) -> list[str]:
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
        return [f"or_state: camwall zone {want_zone!r} "
                "absent from relay"]
    if bad:
        return [f"or_state: camwall {want_zone} settings {cur} "
                f"missing/mismatched {bad}"]
    return []


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


class _InfraAbort(Exception):
    """A driver-side primitive couldn't run — the environment can't
    execute the scenario (e.g. vcast_display claim needs tailnet trust +
    ADA_ADMIN_KEY on the relay). Reported as INFRA (exit 3)."""


class _VcastDisplay:
    """Driver-side fake display on the real input-bridge — the flap e2e
    rides the genuine pending→claim→paired→registered flow, then
    flaps/drops/reattaches the ws so the relay's supersede +
    lastCast-replay path is exercised for real while Ada sees a normal
    screen in /displays. State reporting mirrors vcast-headless.mjs."""

    def __init__(self):
        self.api = _VCAST_API.rstrip("/")
        self.ws_url = re.sub(r"^http", "ws", self.api) + "/ws"
        self.name = ""
        self.device_id = ""
        self.screen: int | None = None
        self.api_key = ""
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.state, self.detail, self.panes = "idle", "", 1
        self.replays: list[dict] = []    # msgs in the post-register window
        self._replay_until = 0.0

    async def _open(self):
        self.ws = await websockets.connect(self.ws_url,
                                           max_size=8 * 1024 * 1024,
                                           open_timeout=15)

    async def _send(self, obj: dict):
        await self.ws.send(json.dumps(obj))

    async def _wait_type(self, typ: str, timeout: float = 20.0) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            raw = await asyncio.wait_for(
                self.ws.recv(), max(0.5, end - time.monotonic()))
            m = json.loads(raw)
            if m.get("type") == typ:
                return m
        raise TimeoutError(f"no '{typ}' from relay within {timeout}s")

    def _post(self, path: str, body: dict) -> dict:
        import urllib.request
        req = urllib.request.Request(
            self.api + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=15).read())

    def _start_reader(self):
        self._replay_until = time.monotonic() + 3.0
        self.reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        """Apply cast msgs like vcast-headless and re-report state — the
        registry entry keeps reflecting what this fake display 'shows'."""
        try:
            async for raw in self.ws:
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                t = m.get("type")
                if t in ("ping", "presence", "registered"):
                    continue
                if time.monotonic() < self._replay_until:
                    self.replays.append(m)
                if t in ("play", "image", "nav", "audio"):
                    self.state, self.detail = t, str(m.get("url") or "")
                elif t == "layout":
                    self.panes = max(1, int(m.get("panes") or 1))
                    self.state, self.detail = "layout", f"{self.panes}panes"
                elif t == "stop":
                    self.state, self.detail, self.panes = "idle", "", 1
                elif t == "snap-request":
                    await asyncio.to_thread(
                        self._post, "/frame",
                        {"screen": self.screen, "error": "simulated"})
                    continue
                else:
                    continue
                try:
                    await self._send({"type": "state", "state": self.state,
                                      "detail": self.detail})
                except Exception:
                    return
        except Exception:
            pass

    async def _register(self):
        await self._open()
        await self._send({"type": "register-display",
                          "api_key": self.api_key, "label": self.name,
                          "device_id": self.device_id})
        rm = await self._wait_type("registered")
        n = int(rm.get("screen") or 0)
        if n != self.screen:
            raise RuntimeError(
                f"re-registered as screen {n}, was {self.screen}")
        self._start_reader()
        # report the client's current state like the real page does on
        # connect (it persists locally across ws reconnects)
        await self._send({"type": "state", "state": self.state,
                          "detail": self.detail})
        # the relay replays remembered lastCast ~0.4s after 'registered'
        await asyncio.sleep(1.5)

    async def _drop_ws(self):
        if self.reader:
            self.reader.cancel()
            self.reader = None
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    def _connected(self) -> bool:
        s = _vcast_screen(int(self.screen or -1))
        return bool(s and s.get("connected"))

    async def claim(self, name: str, label: str) -> str:
        self.name, self.device_id = name, f"flapdrv-{name}"
        # clear a stale registration for this name (crashed prior run)
        try:
            await asyncio.to_thread(self._post, "/release", {"name": name})
        except Exception:
            pass
        await self._open()
        await self._send({"type": "register-display", "api_key": "",
                          "label": label, "device_id": self.device_id})
        pm = await self._wait_type("pending")
        await asyncio.to_thread(
            self._post, "/claim",
            {"sid": pm["sid"], "name": name, "force": True})
        pd = await self._wait_type("paired", 30)
        self.api_key = str(pd.get("api_key") or "")
        if not self.api_key:
            raise RuntimeError("paired without an api_key")
        await self._send({"type": "register-display",
                          "api_key": self.api_key, "label": label,
                          "device_id": self.device_id})
        rm = await self._wait_type("registered")
        self.screen = int(rm.get("screen") or 0)
        if not self.screen:
            raise RuntimeError("registered without a screen number")
        self._start_reader()
        await self._send({"type": "state", "state": self.state,
                          "detail": self.detail})
        return f"claimed screen {self.screen} as '{name}'"

    async def flap(self, times: int = 1, down_s: float = 0.5) -> str:
        for i in range(int(times)):
            await self._drop_ws()
            await asyncio.sleep(max(0.0, float(down_s)))
            self.replays.clear()
            await self._register()
            if not self._connected():
                raise RuntimeError(
                    f"flap {i + 1}: screen {self.screen} still offline "
                    "after re-register")
        return (f"flapped x{int(times)} → same screen {self.screen}, "
                f"replayed {len(self.replays)} msg(s)")

    async def drop(self) -> str:
        """Close the ws and stay down — flap detection: the relay must
        mark the screen offline promptly, not leave a stale room."""
        await self._drop_ws()
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if not self._connected():
                return f"screen {self.screen} went offline"
            await asyncio.sleep(0.5)
        raise RuntimeError(
            f"screen {self.screen} still connected after ws drop — "
            "stale socket kept the registration live")

    async def reattach(self) -> str:
        if self.ws is not None:
            await self._drop_ws()
        self.replays.clear()
        await self._register()
        return (f"reattached as screen {self.screen}, "
                f"replayed {len(self.replays)} msg(s)")

    async def pub(self, url: str) -> str:
        """Driver-side cast into the display's room — while the display is
        down this exercises the relay's remember-and-replay-on-reconnect
        path (delivered:0 but lastCast stored)."""
        out = await asyncio.to_thread(
            self._post, "/pub",
            {"screen": self.screen, "msg": {"type": "nav", "url": url}})
        return f"pub {url} → delivered={out.get('delivered')}"

    async def release(self) -> str:
        name = self.name
        await self._drop_ws()
        self.screen = None
        out = await asyncio.to_thread(self._post, "/release", {"name": name})
        return f"released '{name}' (revoked={out.get('revoked')})"


async def _vcast_display_turn(disp, spec) -> tuple:
    """Run one `vcast_display:` action → (display, result-summary)."""
    spec = dict(spec or {})
    action = str(spec.get("action") or "").lower()
    name = str(spec.get("name") or "flap-test")
    if action == "claim":
        d = _VcastDisplay()
        try:
            msg = await d.claim(name, label=str(spec.get("label") or name))
        except Exception as e:
            try:
                await d.release()   # revoke the key if claim got that far
            except Exception:
                pass
            raise _InfraAbort(
                f"vcast_display claim '{name}' failed (needs tailnet "
                f"trust + ADA_ADMIN_KEY on the relay): {e}")
        return d, msg
    if disp is None:
        raise RuntimeError(f"vcast_display {action} before any claim")
    if action == "flap":
        return disp, await disp.flap(int(spec.get("times") or 1),
                                     float(spec.get("down_s") or 0.5))
    if action == "drop":
        return disp, await disp.drop()
    if action == "reattach":
        return disp, await disp.reattach()
    if action == "pub":
        return disp, await disp.pub(str(spec["url"]))
    if action == "release":
        return None, await disp.release()
    raise RuntimeError(f"unknown vcast_display action {action!r}")


async def _gev_bridge_turn(spec: dict, expect: dict, verbose: bool):
    """One text turn on the gev-gemini bridge ($GEV_LIVE_WS) — the GEV
    client's half of the protocol: {type:text} in, function_call out,
    tool_response back, text deltas + done to close. Bridge msgs are
    normalized into the ada event vocabulary so check_turn applies:
    function_call -> tool_call (+ synthesized tool_result carrying the
    `respond` payload we answered), text -> assistant_transcript_delta,
    done -> response_completed; status/error pass through raw."""
    respond = spec.get("respond")
    if respond is None:
        respond = {"ok": True}
    timeout = float(expect.get("timeout_s") or spec.get("timeout_s") or 60)
    settle = float(expect.get("settle_s") or spec.get("settle_s") or 3)
    try:
        ws = await websockets.connect(
            _GEV_LIVE_WS, max_size=8 * 1024 * 1024, open_timeout=15)
    except Exception as exc:
        raise _InfraAbort(f"gev_bridge connect {_GEV_LIVE_WS}: {exc}")
    events: list[dict] = []
    try:
        # greeting — the bridge announces {"type":"status",...} on connect
        greeted = False
        end = time.monotonic() + 15
        while time.monotonic() < end and not greeted:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), max(0.5, end - time.monotonic()))
            except asyncio.TimeoutError:
                break
            if isinstance(raw, str):
                m = json.loads(raw)
                events.append(m)
                if m.get("type") == "status":
                    greeted = True
        if not greeted:
            raise _InfraAbort(
                "gev_bridge: no status greeting within 15s — "
                "bridge is up but not answering")
        text = str(spec.get("text") or "")
        await ws.send(json.dumps({"type": "text", "text": text}))
        t0 = time.monotonic()
        deadline = t0 + timeout
        done_at = 0.0
        while time.monotonic() < deadline:
            wait = (min(settle, deadline - time.monotonic())
                    if done_at else deadline - time.monotonic())
            try:
                raw = await asyncio.wait_for(ws.recv(), max(wait, 0.1))
            except asyncio.TimeoutError:
                break
            if isinstance(raw, bytes):
                # bridge-side audio frames — recorded so a leak shows in
                # --timing/events output even though nothing asserts it
                events.append({"type": "_audio_frame", "bytes": len(raw)})
                continue
            try:
                m = json.loads(raw)
            except json.JSONDecodeError:
                continue
            m["_t"] = round(time.monotonic() - t0, 3)
            t = m.get("type")
            if t == "function_call":
                events.append({"type": "tool_call", "name": m.get("name"),
                               "args": m.get("args") or {}, "_t": m["_t"]})
                await ws.send(json.dumps({
                    "type": "tool_response",
                    "responses": [{"id": m.get("id"),
                                   "name": m.get("name"),
                                   "response": respond}]}))
                events.append({"type": "tool_result",
                               "name": m.get("name"),
                               "result": respond, "_t": m["_t"]})
            elif t == "text":
                events.append({"type": "assistant_transcript_delta",
                               "text": m.get("text"), "_t": m["_t"]})
            elif t == "done":
                events.append({"type": "response_completed",
                               "_t": m["_t"]})
                done_at = time.monotonic()
            else:
                events.append(m)   # status / error / anything else
            if verbose:
                if t == "function_call":
                    print(f"      function_call {m.get('name')} "
                          f"{m.get('args')}")
                elif t == "text":
                    pass  # transcript deltas stream; run_turn prints match
                elif t == "done":
                    print("      done")
            if done_at and time.monotonic() - done_at > settle:
                break
        return events, check_turn(events, expect)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def _http_check(spec: dict) -> tuple[list[str], dict[str, str]]:
    """HTTP guard turn — fetch `url` directly, or fetch `page` HTML and
    follow its first module <script src> to the served bundle. Assertions:
    status (default 200), contains/not_contains (substrings), regex /
    not_regex (must / must-not match). With `page:`, page_contains /
    page_not_contains / page_regex / page_not_regex assert on the HTML
    and the plain fields assert on the bundle body.
    `capture: {name: regex}` lifts the first capture group per name out of
    the asserted body into the turn's metrics (events-json -> benchmark
    meta) — e.g. dub-metrics sidecar values for trend tracking.
    Returns (failures, captured_metrics)."""
    import urllib.parse
    import urllib.request
    label = str(spec.get("label") or spec.get("url") or spec.get("page"))
    want = int(spec.get("status") or 200)

    def fetch(url: str) -> tuple[int, str]:
        req = urllib.request.Request(
            url, headers={"User-Agent": "scenario-live/http_check"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")

    def check_body(body: str, scope: dict, where: str,
                   out: list[str]) -> None:
        for sub in scope.get("contains") or []:
            if str(sub) not in body:
                out.append(f"{label}: {where} missing {sub!r}")
        for sub in scope.get("not_contains") or []:
            if str(sub) in body:
                out.append(
                    f"{label}: {where} unexpectedly contains {sub!r}")
        for pat in scope.get("regex") or []:
            if not re.search(str(pat), body):
                out.append(f"{label}: {where} no match for /{pat}/")
        for pat in scope.get("not_regex") or []:
            if re.search(str(pat), body):
                out.append(f"{label}: {where} matched /{pat}/")

    failures: list[str] = []
    captured: dict[str, str] = {}
    try:
        if spec.get("page"):
            status, html = fetch(str(spec["page"]))
            if status != want:
                failures.append(
                    f"{label}: page HTTP {status} want {want}")
            check_body(html, {
                "contains": spec.get("page_contains"),
                "not_contains": spec.get("page_not_contains"),
                "regex": spec.get("page_regex"),
                "not_regex": spec.get("page_not_regex"),
            }, "page", failures)
            m = (re.search(r'<script[^>]+type="module"[^>]+src="([^"]+)"',
                           html)
                 or re.search(r'<script[^>]+src="([^"]+\.js[^"]*)"',
                              html))
            if not m:
                failures.append(
                    f"{label}: no module script src in {spec['page']}")
                return failures, captured
            bundle_url = urllib.parse.urljoin(str(spec["page"]),
                                              m.group(1))
            status, body = fetch(bundle_url)
            if status != want:
                failures.append(
                    f"{label}: bundle {bundle_url} HTTP {status}")
            check_body(body, spec, "bundle", failures)
        else:
            status, body = fetch(str(spec["url"]))
            if status != want:
                failures.append(f"{label}: HTTP {status} want {want}")
            check_body(body, spec, "body", failures)
        # capture runs on the same body the assertions read — values are
        # evidence, not pass/fail, so a missing field is not a failure here
        for mname, pat in (spec.get("capture") or {}).items():
            cm = re.search(str(pat), body)
            if cm:
                captured[str(mname)] = (cm.group(1) if cm.groups()
                                        else cm.group(0))
    except Exception as exc:
        failures.append(f"{label}: {exc}")
    return failures, captured


def _subst_runtime(obj, rt: dict):
    """Runtime tokens resolved mid-scenario — {flap_screen} becomes the
    screen number a vcast_display claim actually got. A string that is
    ONLY the token expands to the raw value (int) so numeric assertions
    stay numeric."""
    def w(o):
        if isinstance(o, str):
            for k, v in rt.items():
                if v is None:
                    continue
                tok = "{" + k + "}"
                if o.strip() == tok:
                    return v
                o = o.replace(tok, str(v))
            return o
        if isinstance(o, list):
            return [w(x) for x in o]
        if isinstance(o, dict):
            return {k: w(v) for k, v in o.items()}
        return o
    return w(obj)


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
        elif check == "gev_bridge":
            # greeting probe on the gev-gemini bridge ws — a hung or
            # down bridge is an environment failure, not a model bug
            try:
                from websockets.sync.client import (
                    connect as _ws_sync_connect)
                got = {}
                with _ws_sync_connect(_GEV_LIVE_WS,
                                      open_timeout=15) as bws:
                    end = time.monotonic() + 15
                    while time.monotonic() < end:
                        m = json.loads(bws.recv(
                            timeout=max(0.5, end - time.monotonic())))
                        if m.get("type") == "status":
                            got = m
                            break
                if not got:
                    fails.append(
                        f"preflight {label}: no status greeting")
            except Exception as e:
                fails.append(f"preflight {label}: {e}")
        elif check == "gev_relay":
            # Is the gev-cmd relay answering at all? Tour/cast scenarios
            # create their own map client by casting /apps/gev/ to a
            # screen, so reachability is the gate — not clients attached.
            # A 404/down relay means every gev_command errors instantly
            # while the scenario still burns full per-turn timeouts
            # (2026-10-09: relay 404'd all night, ~18 gev scenarios each
            # ate ~8min x2 attempts inside the casting suite).
            try:
                req = urllib.request.Request(
                    _GEV_CMD_URL,
                    data=json.dumps({"name": "get_current_view_state",
                                     "args": {}, "wait": 1}).encode(),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as e:
                fails.append(f"preflight {label}: gev relay: {e}")
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
        try:
            await ws.send(json.dumps({"type": "text", "text": text}))
        except Exception as exc:
            events.append({
                "type": "connection_closed",
                "error": f"{type(exc).__name__}: {exc}",
            })
            return events, [f"send failed (connection closed): {exc}"]
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
        except Exception as exc:
            # Server closed mid-turn (forced reconnect, go_away, handler
            # end) — score it as a failure, don't crash the scenario run.
            events.append({
                "type": "connection_closed",
                "error": f"{type(exc).__name__}: {exc}",
            })
            failures = [f"connection closed mid-turn: {exc}"]
            return events, failures
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
            elif t in ("tool_result", "tool_result_full"):
                print(f"      {t} {msg.get('name')}")
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
        ws = await websockets.connect(target, max_size=8 * 1024 * 1024,
                                      open_timeout=15)
        # Total deadline on the ready-wait: a socket that stays chatty but
        # never sends 'ready' would otherwise loop forever and look like a
        # hang to the harness (found auditing the casting-suite stall).
        end = time.monotonic() + 120
        while time.monotonic() < end:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=min(30, max(0.5, end - time.monotonic())))
            if isinstance(raw, str):
                msg = json.loads(raw)
                if msg.get("type") == "ready":
                    my_session[0] = str(msg.get("session") or "")
                    return ws
        raise asyncio.TimeoutError("no ready event within 120s")

    async def ensure_ws() -> Any:
        """Lazy Ada /ws connect — driver-only scenarios (vcast_display /
        gev_bridge / http_check turns) never need it, so a down Ada can't
        FATAL a probe that isn't about Ada."""
        nonlocal ws
        if ws is not None:
            return ws
        try:
            ws = await connect(url)
        except asyncio.TimeoutError:
            print("FATAL: no ready event from server")
            return None
        except OSError as exc:
            print(f"FATAL: cannot connect to {url.split('?')[0]}: {exc}")
            return None
        print("connected (ready)")
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
    vdisp: _VcastDisplay | None = None      # claimed by vcast_display turns
    rtokens: dict = {"flap_screen": None}   # mid-scenario runtime tokens
    try:
        for i, turn in enumerate(turns):
            turn = _subst_runtime(turn, rtokens)
            expect = dict(turn.get("expect") or {})
            expect.setdefault("timeout_s", turn.get("timeout_s", 90))
            expect.setdefault("settle_s", turn.get("settle_s", 5))
            if turn.get("sleep_s"):
                await asyncio.sleep(float(turn["sleep_s"]))
            turn_t0 = time.time()
            kind, prompt = "text", ""
            turn_metrics: dict[str, str] = {}
            if "gev_bridge" in turn:
                # gev-gemini bridge probe — plays the GEV client's half
                # of the voice protocol; events normalize into the
                # standard vocabulary for check_turn.
                kind = "gev_bridge"
                bspec = dict(turn.get("gev_bridge") or {})
                prompt = str(bspec.get("text") or "")[:120]
                print(f"turn {i + 1}: gev_bridge {prompt!r}")
                try:
                    events, failures = await _gev_bridge_turn(
                        bspec, expect, args.verbose)
                except _InfraAbort:
                    raise
                except Exception as exc:
                    events = [{"type": "_note", "text": str(exc)}]
                    failures = [f"gev_bridge: {exc}"]
            elif "http_check" in turn:
                # driver-side HTTP guard — served-code/config checks
                # (sw.js non-caching worker, bundle invariants)
                kind = "http_check"
                hspec = dict(turn.get("http_check") or {})
                prompt = str(hspec.get("label") or hspec.get("url")
                             or hspec.get("page") or "")
                print(f"turn {i + 1}: http_check {prompt}")
                events = [{"type": "_note", "text": prompt}]
                try:
                    failures, turn_metrics = await asyncio.to_thread(
                        _http_check, hspec)
                    if turn_metrics:
                        print(f"      metrics: {turn_metrics}")
                except Exception as exc:
                    failures = [f"http_check: {exc}"]
            elif "vcast_display" in turn:
                # driver-side display action — not an Ada turn; the action
                # itself produces pass/fail (claim failure → INFRA abort).
                kind = "vcast_display"
                try:
                    vdisp, msg = await _vcast_display_turn(
                        vdisp, turn.get("vcast_display"))
                    if vdisp is not None and vdisp.screen:
                        rtokens["flap_screen"] = vdisp.screen
                    failures = []
                except _InfraAbort:
                    print(f"turn {i + 1}: vcast_display → INFRA")
                    raise
                except Exception as exc:
                    msg = str(exc)
                    failures = [f"vcast_display: {exc}"]
                prompt = f"{(turn.get('vcast_display') or {}).get('action')} → {msg}"
                print(f"turn {i + 1}: vcast_display {prompt}")
                events = [{"type": "_note", "text": prompt}]
            elif "reconnect" in turn:
                away = (turn.get("reconnect") or {}).get("away_s")
                rurl = url
                if away is not None:
                    sep = "&" if "?" in rurl else "?"
                    rurl = f"{rurl}{sep}simulate_away_s={away}"
                print(f"turn {i + 1}: reconnect (away_s={away})")
                kind, prompt = "reconnect", f"away_s={away}"
                if ws is not None:
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
                    if await ensure_ws() is None:
                        return 2
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
                    if await ensure_ws() is None:
                        return 2
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
                            retry_events, _ = await run_turn(
                                ws, text, expect, args.verbose)
                            # Score the union — a live-API interrupt can eat
                            # a transcript whose tool calls were real; merging
                            # keeps calls_any honest without hiding attempt-1
                            # behavior (banned calls still count).
                            events = events + retry_events
                            failures = check_turn(events, expect)
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
                "metrics": turn_metrics,
            })
            if failures:
                total_fail += len(failures)
                print(f"  FAIL ({n_tools} tool calls)")
                for f in failures:
                    print(f"    - {f}")
                print(f"    transcript: {transcript[:300]!r}")
                if any(e.get("type") == "connection_closed" for e in events):
                    print("  aborting scenario — websocket closed")
                    break
            else:
                print(f"  ok ({n_tools} tool calls) ada: {transcript[:140]!r}")
    except _InfraAbort as exc:
        print(f"INFRA: {exc}")
        return 3
    finally:
        if ws is not None:
            await ws.close()
        if vdisp is not None:
            try:
                await vdisp.release()
            except Exception as exc:
                print(f"  cleanup: vcast_display release failed: {exc}")
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
