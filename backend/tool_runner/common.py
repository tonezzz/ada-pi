"""FastAPI-style tool runner for Ada's Home Assistant, memory, and habit tools."""

from __future__ import annotations

import asyncio
import collections
import hashlib
import inspect
import json
import logging
import contextvars
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend import memory_ops
from backend import chaba_memory
from backend import gemini_pool
from backend import speech_sanitize
from backend import write_outbox
from backend import devin_dispatch as devin_dispatch_mod
from backend import tools_loader
from backend import doc_archive_client
from backend import summary_rollups
from backend.calendar_providers import CalendarService
from backend.decision_check import DecisionCheckEngine
from backend.event_recorder import HaEventRecorder
from backend.home_assistant import HomeAssistantClient
from backend.instance import ada_instance_id
from backend.mddb_client import MddbClient
from backend.memory_banks import (
    MemoryBankRegistry,
    get_registry,
)
from backend.report_meta import (
    fresh_for_seconds,
    missing_publish_fields,
    validate_report_meta,
)
from backend.usage_tracker import usage_ledger

logger = logging.getLogger("tools")

# Tools that physically actuate a device. These are gated server-side:
# dangerous devices require confirmed=true, all are rate-limited, and all
# are blocked entirely when ADA_READ_ONLY=true.
CONTROL_TOOLS = {
    # tools-merge-ha (2026-10-05): control_cover/press_button/
    # control_media_player collapsed into control_entity — the canonical
    # name holds the seat; every action= actuates, so the whole tool
    # stays gated.
    "control_entity", "tv_action", "cast_to_screen", "gev_command",
}

# Tools that mutate curated memory banks. Each bank's write_policy decides
# whether confirmed=true is required (same gate pattern as CONTROL_TOOLS).
# tools-merge-meta-voice (2026-10-05): ada_outcome collapsed into ada_ops —
# the canonical name holds the seat; only action='outcome' is a bank write
# (the action-scoped guard in _execute_gated keeps usage/health free).
MEMORY_WRITE_TOOLS = {"ada_remember", "ada_forget", "ada_ops"}

# Calendar/task writes: creating or deleting events and tasks mutates the
# user's real calendar, so all writes require confirmed=true. tools-merge-
# calendar-plan (2026-10-05): create/delete/shift collapsed into
# calendar_write — the canonical name holds the seat; every action=
# (create|delete|shift) is a write, so the whole tool stays gated.
CALENDAR_WRITE_TOOLS = {
    "calendar_write",
    # tools-merge-tasks-status (2026-10-05): tasks_add/tasks_complete/
    # tasks_move collapsed into tasks — the canonical name holds the seat;
    # _check_calendar_write_allowed keys the per-action split
    # (action='list' stays free, like the absorbed tasks_list) off args.
    "tasks",
}

# Miniapp/CMS page writes: publishing or deleting a page changes what the
# miniapp renders, so all writes require confirmed=true. tools-merge-cms
# (2026-10-05): cms_delete_page/cms_automation collapsed into cms_edit —
# the canonical name holds the seat; _check_cms_write_allowed keys the
# per-action split (note + automation reads stay free) off args.
CMS_WRITE_TOOLS = {"cms_publish_page", "cms_edit"}

# Devin session control: dispatching an unattended agent session,
# injecting a follow-up message into one, or delivering the user's
# answer to a blocked job all cause autonomous code changes, so they
# require confirmed=true. tools-merge-devin-mcp (2026-10-05): the three
# names collapsed into `devin` (action=dispatch|followup|answer) — the
# canonical name holds the seat; every action is a session write, so the
# whole tool stays gated. devin_read is read-only except action='report'
# with publish=true, which re-checks confirmation internally.
DEVIN_CONFIRMED_TOOLS = {"devin"}

# Camera captures/casts visible on screens — gated by action inside
# _check_capture_confirmed (uplink, wall start, camera snap-to-display).
# cctv_snapshot was absorbed into ada_camera_snapshot (tools-merge-camera):
# the canonical name holds the seat, gated only when the call pushes the
# frame to a display (screen=/target=) — the describe path stays open.
CAPTURE_CONFIRMED_TOOLS = {
    "cast_to_screen", "cctv_wall", "ada_camera_snapshot"}

# Job ledger collection: job/<id> docs (status running|awaiting-user|
# answered|done|failed) written by devin-dispatch-watch and job-run.sh;
# answer/<id> docs are the user's refined replies. Lives in the
# devin-handoff bank — render-handoff-inbox.py skips both prefixes.
DEVIN_JOBS_COLLECTION = "ada-ha-bank-devin-handoff"

# Job ledger collection: job/<id> docs (status running|awaiting-user|
# answered|done|failed) written by devin-dispatch-watch on each dispatch
# host. The devin-job-report CMS page is composed from this ledger.
DEVIN_JOBS_COLLECTION = "ada-ha-bank-devin-handoff"
DEVIN_JOB_REPORT_SLUG = "devin-job-report"
# Ledger statuses that mean the job is finished and successful-ish vs
# terminal-but-dead vs still in flight.
_JOB_DONE_STATUSES = {"done", "success", "completed"}
_JOB_STALE_STATUSES = {"superseded", "cancel", "cancelled", "stale"}
# A ledger-"running" job whose unit is entirely gone (collected) and that
# never wrote a transcript is a spawn failure — but a brand-new dispatch
# can briefly look the same before systemd loads the unit, so the rule
# only applies past this age.
_JOB_GONE_GRACE_S = 1800.0

# Document archive/print: docs action=archive writes pages to
# gdrive:ada-documents and action=print sends real pages to the printer —
# both require confirmed=true. tools-merge-docs-drive (2026-10-05): the
# four ada_doc_* names collapsed into docs — the canonical name holds the
# seat; _check_doc_confirmed keys the per-action split (search/get are
# read-only) off args.
DOC_CONFIRMED_TOOLS = {"docs"}
# ALL doc actions (incl. read-only) are scoped to identities that can see
# the `documents` bank — the tool hits MDDB/the archive service directly
# and would otherwise bypass person_policies (e.g. a KK/guest session
# reading document metadata for an ID card).
DOC_TOOLS = {"docs"}
DOC_BANK = "documents"

# Upper bound for list_home_devices — an unbounded HA entity dump stays in
# the live-voice context for the rest of the session (see the 2026-10-01
# 1M-token tool storm). search_home_devices is the precise path.
LIST_HOME_DEVICES_MAX = int(os.environ.get("ADA_LIST_HOME_DEVICES_MAX", "60"))

# Google Drive / Photos tools — same access scope as DOC_TOOLS (the whole
# Drive is owner-tier data). drive action=update replaces file content in
# place, so it needs confirmed=true; search/get/show/pick are read-side.
# tools-merge-docs-drive (2026-10-05): the four drive_* names collapsed
# into drive — the canonical holds the seat; _check_drive_confirmed keys
# the per-action split off args.
DRIVE_CONFIRMED_TOOLS = {"drive"}
DRIVE_TOOLS = DRIVE_CONFIRMED_TOOLS | {
    "photos_pick", "photos_picked",
    "drive_search", "drive_get", "drive_show",
    # chat_send absorbed photos_pick/photos_picked + the doc-upload card
    # actions (tools-merge-tasks-status): the seat is per-flow — only
    # photo=/doc= calls take the owner-tier bank gate, plain sends never
    # had it (see the chat_send carve-outs in _execute_gated).
    "chat_send",
}
# Sentinel: identity unset → fall back to runner-level _memory_identity();
# None is a real identity (anonymous) and must be distinguishable.
_IDENTITY_UNSET = object()

# Session-security policy (docs/session-security-policy.md P4/P7): while a
# non-owner speaker's voice is identified, the turn is "secondary" — guests
# may converse but never write, actuate, enroll, or read the owner's private
# stores; anything useful becomes a proposal for the owner to confirm.
SECONDARY_BLOCKED_TOOLS = (
    CONTROL_TOOLS | MEMORY_WRITE_TOOLS | CALENDAR_WRITE_TOOLS
    | CMS_WRITE_TOOLS | DEVIN_CONFIRMED_TOOLS | DOC_TOOLS | DRIVE_TOOLS
    | {
        "ada_enroll_speaker", "ada_memory_search",
        # tools-merge-ha: ha_confidence absorbed the confidence pair; the
        # resolved name holds the seat, and _execute_gated carves the
        # read path (no status/safety args) back out so a guest can still
        # ask what is broken — only writes stay blocked.
        "ha_confidence", "ada_resolve_action",
    }
)

# An identified speaker label is only enforced while fresh voice chunks
# keep re-confirming it. Far-field speech can sit just under the match
# threshold for minutes — 2026-10-01: one 80% KK hit pinned the label,
# then Tony's chunks scored 0.26-0.39 (never switching), so every
# cast_to_screen was denied as "secondary speaker" while the actual
# owner talked. Past this age the label is treated as unrecognized —
# an unknown voice keeps owner rights anyway, so expiry cannot widen
# what a real guest could already do while unidentified.
SPEAKER_STALE_S = float(os.environ.get("ADA_SPEAKER_STALE_S", "90"))

# Group tokens accepted in rendered `session_security.secondary_blocked`
# config — SSOT declares groups, code owns the tool-name expansion.
# "persona_write" is a pseudo-token gating ada_persona set/reset only.
_SECONDARY_BLOCKED_GROUPS = {
    "control": CONTROL_TOOLS,
    "memory_write": MEMORY_WRITE_TOOLS,
    "calendar_write": CALENDAR_WRITE_TOOLS,
    "cms_write": CMS_WRITE_TOOLS,
    "devin_confirmed": DEVIN_CONFIRMED_TOOLS,
    "doc": DOC_TOOLS | DRIVE_TOOLS,
}


def _slug(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")


# Uniform tool-result contract (kanban tool-error-contract, 2026-10-05):
# every value that leaves ToolRunner.execute() is a dict carrying
# top-level ok: bool — {ok: True, ...} on success, {ok: False,
# error: str, ...} on failure. Until then five conventions coexisted:
# bare raises, {"error": ...}, {"ok": False}, {"err": ...}, and failures
# buried inside responses[].response.ok — the "did it honestly succeed"
# checks couldn't be uniform. normalize_tool_result wraps the legacy
# shapes at the runner boundary; dispatch-layer denials (the _check_*
# gates, unknown-tool KeyError) still raise — a refusal is not a tool
# result.
_RESULT_FAIL_STATUSES = frozenset({"error", "failed", "not_found", "denied"})


def normalize_tool_result(result: Any) -> dict[str, Any]:
    """Coerce a tool method's return into the canonical
    {ok: bool, error?: str, ...} shape. Pure — never raises."""
    if not isinstance(result, dict):
        return {"ok": True, "output": result}
    out = dict(result)
    # Buried fan-out failure (the gev_command pattern, 0bb8e9b,
    # generalized): a delivered command whose every client response is
    # ok:false is a top-level failure, not a success with bad parts.
    resps = out.get("responses")
    if isinstance(resps, list) and resps and out.get("ok") is not False:
        errs = [
            r["response"] for r in resps
            if isinstance(r, dict) and isinstance(r.get("response"), dict)
            and r["response"].get("ok") is False
        ]
        if errs and len(errs) == len(resps):
            out["ok"] = False
            out["error"] = (
                errs[0].get("error") or out.get("error")
                or "all client responses failed")
    if out.get("ok") is None:
        out["ok"] = not (
            out.get("error") or out.get("err") or out.get("needs_confirm")
            or str(out.get("status") or "").lower() in _RESULT_FAIL_STATUSES)
    else:
        out["ok"] = bool(out["ok"])
    # `err` is a legacy spelling of `error` — surface it canonically.
    if out["ok"] is False and not out.get("error") and out.get("err"):
        out["error"] = str(out["err"])
    return out


# Per-call identity propagated through a ContextVar: the runner is shared
# across concurrent sessions, so every policy/memory check inside a tool
# call must see the SESSION's identity, not the runner's mutable fields
# (which can be overwritten by another session's speaker-ID callback mid-
# call). execute() sets it; _memory_identity() prefers it.
# The session's SpeakerSession — set per execute() call by the provider.
# 2026-09-29 bug: a shared `runner.speaker_session` slot meant a second
# ws connect (browser tab, HA satellite) stomped it mid-conversation and
# ada_enroll_speaker read the *other* session's empty buffer — Tony's
# enrollment reported "no audio arrived" while he was actively speaking.
# default=_IDENTITY_UNSET (same convention as _CALLER_SPEAKER): a provider
# passing an explicit None means THIS session has no voice buffer — it
# must not fall back to the shared field and read another session's.
_CALLER_SPEAKER_SESSION: contextvars.ContextVar = contextvars.ContextVar(
    "caller_speaker_session", default=_IDENTITY_UNSET)

# Same shared-runner race for session_owner_identity: a second ws connect
# overwrote the owner mid-session (2026-09-29 — Tony on the HA satellite
# was denied as "secondary speaker" because a browser 'admin' session
# stomped the shared field). Provider passes the pinned owner per call.
_CALLER_OWNER: contextvars.ContextVar = contextvars.ContextVar(
    "caller_owner", default=None)

_CALLER_IDENTITY: contextvars.ContextVar = contextvars.ContextVar(
    "ada_caller_identity", default=None)

# Per-call identified speaker (person.*): the provider passes the SESSION's
# identified speaker so a concurrent session's speaker-ID can't bleed into
# this one's secondary-owner check — same hazard class as _CALLER_IDENTITY.
_CALLER_SPEAKER: contextvars.ContextVar = contextvars.ContextVar(
    "ada_caller_speaker", default=_IDENTITY_UNSET)

# Provider-verified user affirmation for this call — the model's
# confirmed=true was checked against actual user speech before dispatch.
# Lets gates skip the register→re-ask round-trip without weakening the
# policy (a bare confirmed=true still goes through the full check).
_CALLER_VERIFIED_AFFIRM: contextvars.ContextVar = contextvars.ContextVar(
    "ada_verified_affirm", default=False)

# Per-call session key for the storm breaker — the provider passes its
# own session_id so breaker state is per-session even though the runner
# is shared (runner.session_id is a mutable field the LAST connected
# provider wins — it cannot scope a shared breaker race-free).
_CALLER_SESSION: contextvars.ContextVar = contextvars.ContextVar(
    "ada_caller_session", default=None)

CMS_COLLECTION = os.environ.get("ADA_CMS_COLLECTION", "ada-cms-pages")
CMS_FORMATS = {"markdown", "html", "yaml", "slides"}
# Per-page automation registry: one doc per CMS slug holding the switches
# (enabled, run_now) and knobs (interval_min, feeds, require, …) the
# flood-news worker honors, plus worker write-back state (last_run…).
CMS_AUTOMATION_COLLECTION = os.environ.get(
    "ADA_CMS_AUTOMATION_COLLECTION", "ada-cms-automation")
CMS_AUTOMATION_READ_ACTIONS = {"list", "get"}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Confirmation gate: `confirmed` is a self-asserted flag — the server cannot
# verify the user actually said yes, the flag exists to force the
# propose -> restate -> explicit-yes -> execute protocol and to leave an
# audit trail. Two acceptance forms:
#   confirmed: truthy spellings ('yes', 'true', '1', 'confirmed'). Strict-bool
#     parsing only produced spurious denials (the model re-asking an already
#     answered question) without changing the self-asserted semantics.
#   confirm_token: the bound form. A denial mints a single-use token tied to
#     the exact tool + args fingerprint, so a 'yes' can only arm the action it
#     was shown — not a rephrased or unrelated retry — and cannot be replayed.
_CONFIRM_TRUE = frozenset({"true", "1", "yes", "y", "confirm", "confirmed"})
# 300s (speaker-profile-tool, 2026-10-07): handoff flows — where the
# session owner differs from the speaker being confirmed (e.g. Ada asks
# the owner to approve a guest's profile fix) — need the token to
# outlive a multi-turn spoken exchange; 120s expired mid-handoff.
CONFIRM_TOKEN_TTL_S = float(os.environ.get("ADA_CONFIRM_TOKEN_TTL_S", "300"))
_CONFIRM_TOKEN_MAX = 64
_CONFIRM_AUDIT_MAX = 200

CONTROL_RATE_WINDOW_S = float(os.environ.get("ADA_CONTROL_RATE_WINDOW_S", "60"))
CONTROL_MAX_PER_ENTITY = int(os.environ.get("ADA_CONTROL_MAX_PER_ENTITY", "5"))
CONTROL_MAX_GLOBAL = int(os.environ.get("ADA_CONTROL_MAX_GLOBAL", "30"))

# Per-session tool-storm circuit breaker (card ada-tool-retry-storm;
# journal 2026-10-10 16:31:03-16:32:18 ICT — a dead calendar token
# produced ~75s of identical calendar_read failures the model kept
# retrying). Once the same tool fails TOOL_BREAKER_TRIP times with the
# same error class inside TOOL_BREAKER_WINDOW_S, execute() returns a
# synthesized outage result instead of running the tool again; the
# circuit stays open for TOOL_BREAKER_OPEN_S, then lets one real call
# through (half-open — success resets, another failure re-trips).
TOOL_BREAKER_TRIP = int(os.environ.get("ADA_TOOL_BREAKER_TRIP", "2"))
TOOL_BREAKER_WINDOW_S = float(os.environ.get("ADA_TOOL_BREAKER_WINDOW_S", "120"))
TOOL_BREAKER_OPEN_S = float(os.environ.get("ADA_TOOL_BREAKER_OPEN_S", "300"))

# Arg-shape failures (model typos — the tool_guide usage hint already
# answers them) never count toward the breaker.
_BREAKER_ARG_ERRORS = {"ValueError", "KeyError", "TypeError", "AttributeError"}

# Fallback error-class buckets for failures that carry no exception
# type (bare {"error": ...} dicts). First match wins.
_BREAKER_TEXT_CLASSES = (
    ("auth", re.compile(
        r"auth|credential|unauthorized|\b401\b|revoked|expired", re.I)),
    ("timeout", re.compile(r"timeout|timed out|deadline", re.I)),
    ("acl", re.compile(
        r"permission|denied|forbidden|\b403\b|access policy|control policy|acl",
        re.I)),
)


def _storm_error_class(outcome: Any) -> str | None:
    """Coarse error class for storm-breaker accounting, or None when the
    outcome doesn't count. The exception type name wins verbatim —
    CalendarAuthError, ReadTimeout and PermissionError (gate ACL
    denials) land as their own classes. Confirm proposals — denials
    whose message itself tells the caller how to retry (confirmed=true
    or a minted cfm-/confirm_token) — and needs_confirm dicts are the
    confirm flow working, not an outage; arg typos are model errors.
    Bare {error} dicts fall back to a text signature."""
    if isinstance(outcome, BaseException):
        cls = type(outcome).__name__
        msg = str(outcome)
    elif isinstance(outcome, dict) and outcome.get("ok") is False:
        if outcome.get("needs_confirm"):
            return None
        cls = str(outcome.get("error_type") or "")
        msg = str(outcome.get("error") or outcome.get("err") or "")
    else:
        return None
    if cls in _BREAKER_ARG_ERRORS:
        return None
    if cls == "PermissionError" and (
            "confirmed=true" in msg or "confirm_token" in msg
            or "cfm-" in msg):
        return None
    if cls:
        return cls
    for label, rx in _BREAKER_TEXT_CLASSES:
        if rx.search(msg):
            return label
    return "error"

# LLM callers occasionally use a synonym for a declared parameter. Map the
# alias to the real name only when the method declares it and the caller did
# not already pass the canonical name.
_ARG_ALIASES = {"question": "query", "q": "query"}

# Tool-NAME aliases for the consolidation program
# (docs/assessments/tool-consolidation-spec-2026-10-04.md): when a merge
# card retires a tool name into a canonical action=/kind=/scope= tool,
# the old name stays callable here as a soft alias — registered but
# hidden from the declared surface (x-legacy semantics) so prompts,
# scenarios and habits keep working. A key must never be a declared
# tool name, every value must be one, and every absorbed name in
# docs/ssot/ssot.tool-surface.yml must land a row once it leaves the
# surface — scripts/tool-lint.py fails on drift. Populated by the
# tools-merge-* cards.
_ALIASES: dict[str, str] = {
    # memory family — tools-merge-memory (2026-10-04): 8 -> 4.
    "guest_recall": "ada_memory_search",
    "vocab_note": "ada_remember",
    "report_habit_observation": "ada_remember",
    "guest_remember": "ada_remember",
    "guest_remember_private": "ada_remember",
    "ada_ha_recall": "ada_session_recall",
    # camera family (tools-merge-camera): cctv_snapshot + traffic_camera
    # retire into ada_camera_snapshot's source= param; capture_frame
    # (the GEV remote command) retires into vcast_snapshot, which fires
    # it internally as part of the display self-capture flow.
    "cctv_snapshot": "ada_camera_snapshot",
    "traffic_camera": "ada_camera_snapshot",
    "capture_frame": "vcast_snapshot",
    # cms family — tools-merge-cms (2026-10-05): 7 -> 3. cms_publish_page
    # stays declared (the big writer); the three readers route to
    # cms_read, the three writers/registry-ops route to cms_edit.
    "cms_list_pages": "cms_read",
    "cms_get_page": "cms_read",
    "cms_verify_page": "cms_read",
    "cms_note_update": "cms_edit",
    "cms_delete_page": "cms_edit",
    "cms_automation": "cms_edit",
    # calendar+plan family — tools-merge-calendar-plan (2026-10-05): 9 -> 3.
    # The three readers route to calendar_read, the three gated writers to
    # calendar_write, and the two summary rollups to plan_day.
    "calendar_list_events": "calendar_read",
    "calendar_list_calendars": "calendar_read",
    "calendar_freebusy": "calendar_read",
    "calendar_create_event": "calendar_write",
    "calendar_delete_event": "calendar_write",
    "calendar_shift_overdue": "calendar_write",
    "ada_daily_summary": "plan_day",
    "ada_weekly_comparison": "plan_day",
    # docs+drive family — tools-merge-docs-drive (2026-10-05): 8 -> 2.
    # The four ada_doc_* archive tools route to docs(action=...), the
    # four drive_* Drive tools route to drive(action=...).
    "ada_doc_search": "docs",
    "ada_doc_get": "docs",
    "ada_doc_print": "docs",
    "ada_doc_archive": "docs",
    "drive_search": "drive",
    "drive_show": "drive",
    "drive_get": "drive",
    "drive_update": "drive",
    # gev family — tools-merge-gev (2026-10-05): 2 -> 1. gev_tour retires
    # into gev_command's tour= param — same arg name, so no
    # _ALIAS_ARG_DEFAULTS/_alias_call_args shim is needed.
    "gev_tour": "gev_command",
    # ha family — tools-merge-ha (2026-10-05): 19 names -> 6 canonical
    # (home_search / get_home_state / home_history / control_entity /
    # ha_confidence; tv_action kept its own seat — the cmd/text/selector
    # param surface doesn't fold into action=).
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
    # display family — tools-merge-display (2026-10-05): 8 -> 2.
    # cast_to_screen keeps its name and absorbs the vcast_* narrator/
    # readers into action= (say|list|status|shortcut); vcast_snapshot
    # stays separate (the verify-after-act loop needs its own name) and
    # vcast_gesture is a distinct subsystem. vcast_status/vcast_shortcut
    # were census-counted journal names (2 and 1 calls in 7d) with no
    # committed implementation — they alias to the matching action= seats
    # so a stale call still lands somewhere sane.
    "vcast_say": "cast_to_screen",
    "vcast_list": "cast_to_screen",
    "vcast_status": "cast_to_screen",
    "vcast_shortcut": "cast_to_screen",
    # meta/voice family — tools-merge-meta-voice (2026-10-05): 8 -> 3.
    # ada_set_voice folds into ada_persona (*_voice actions); the five
    # ops/meta tools fold into the new ada_ops action= tool; guest_register
    # folds into ada_enroll_speaker who='guest'.
    "ada_set_voice": "ada_persona",
    "ada_outcome": "ada_ops",
    "ada_usage_summary": "ada_ops",
    "ada_mddb_health": "ada_ops",
    "ada_decision_check": "ada_ops",
    "ada_deep_research": "ada_ops",
    # report-graph refresh (report-graph P3) — a literal call name that
    # lands on ada_ops action='report' (freshness + background refresh).
    "report_refresh": "ada_ops",
    "guest_register": "ada_enroll_speaker",
    # tasks+status family — tools-merge-tasks-status (2026-10-05): 13 -> 3
    # on the declared surface (10 card-counted + chat_send's five absorbed
    # comms names). tasks_* collapse into tasks(action=add|list|done|move),
    # the seven home status getters into home_status(what=...), and the
    # photos picker pair + the three chaba-side doc-upload card actions
    # (never declared here — forward aliases) into chat_send.
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
    # yt family — tools-merge-yt (2026-10-05): 4 -> 1. All four YouTube
    # tools retire into yt's action= param; the display card will fold
    # yt itself into ada_display later.
    "yt_cast": "yt",
    "yt_cast_status": "yt",
    "yt_cast_stop": "yt",
    "yt_transcript": "yt",
    # devin family — tools-merge-devin-mcp (2026-10-05): 8 -> 2. The
    # session-write names route to devin (action=dispatch|followup|
    # answer, confirmed-gated); the ledger/report readers plus the
    # devteam spec-review pipeline route to devin_read
    # (action=status|jobs|pending|report|review).
    "devin_dispatch": "devin",
    "devin_followup": "devin",
    "devin_answer": "devin",
    "devin_status": "devin_read",
    "devin_jobs": "devin_read",
    "devin_pending": "devin_read",
    "devin_job_report": "devin_read",
    "ada_devteam_review": "devin_read",
    # ada-kanban-access (2026-10-08): ada_board_write folds into the
    # kanban tool — the design's full board surface under Ada's triage
    # authority (its action names map through inside the module).
    "ada_board_write": "kanban",
}

# Args an aliased call carries implicitly — the absorbed name implies the
# canonical tool's action=/kind=/scope= (e.g. cctv_snapshot -> ada_camera
# implies action="snapshot", guest_recall -> ada_memory_search implies
# scope="guest"). Merged under the caller's args, never overriding them.
# scripts/tool-lint.py checks every key here has an _ALIASES row.
_ALIAS_ARG_DEFAULTS: dict[str, dict[str, Any]] = {
    "guest_recall": {"scope": "guest"},
    "vocab_note": {"kind": "vocab"},
    "report_habit_observation": {"kind": "habit"},
    "report_refresh": {"action": "report"},
    "guest_remember": {"kind": "guest"},
    "guest_remember_private": {"kind": "guest", "private": True},
    "ada_ha_recall": {"scope": "history"},
    # cctv_snapshot's declared default was target='tv' — keep it so an
    # old-style call still lands the frame on the living-room TV.
    "cctv_snapshot": {"source": "vms", "target": "tv"},
    "traffic_camera": {"source": "traffic"},
    "cms_list_pages": {"action": "list"},
    "cms_get_page": {"action": "get"},
    "cms_verify_page": {"action": "verify"},
    "cms_note_update": {"action": "note"},
    "cms_delete_page": {"action": "delete"},
    "cms_automation": {"action": "automate"},
    "calendar_list_events": {"action": "events"},
    "calendar_list_calendars": {"action": "calendars"},
    "calendar_freebusy": {"action": "freebusy"},
    "calendar_create_event": {"action": "create"},
    "calendar_delete_event": {"action": "delete"},
    "calendar_shift_overdue": {"action": "shift"},
    # 'digest' is an alias-internal period: plan_day returns the bare
    # daily digest so ada_daily_summary keeps its exact return contract.
    "ada_daily_summary": {"period": "digest"},
    "ada_weekly_comparison": {"period": "week"},
    # ada_doc_*/drive_* args map 1:1 onto the canonical schemas — only
    # the implied action= is needed.
    "ada_doc_search": {"action": "search"},
    "ada_doc_get": {"action": "get"},
    "ada_doc_print": {"action": "print"},
    "ada_doc_archive": {"action": "archive"},
    "drive_search": {"action": "search"},
    "drive_show": {"action": "show"},
    "drive_get": {"action": "get"},
    "drive_update": {"action": "update"},
    # ha family — tools-merge-ha (2026-10-05). kind= carries which of the
    # absorbed finders/history readers the old name meant; press_button's
    # implied action lets control_entity skip domain probing.
    "search_home_devices": {"kind": "device"},
    "list_home_devices": {"kind": "device"},
    "search_devices": {"kind": "device"},
    "ada_ha_search_devices": {"kind": "device"},
    "search_sensors": {"kind": "sensor"},
    "list_sensors": {"kind": "sensor"},
    "ada_ha_search_sensors": {"kind": "sensor"},
    "ada_ha_search_events": {"kind": "event"},
    # 'memory' is an alias-internal domain: get_home_state returns the
    # stored AdaMemoryStore overview so ada_ha_get_state keeps its exact
    # return contract.
    "ada_ha_get_state": {"domain": "memory"},
    "get_logbook": {"kind": "logbook"},
    "get_sensor_history": {"kind": "series"},
    "get_entity_events": {"kind": "timeline"},
    "get_recent_events": {"kind": "events"},
    "ada_ha_history": {"kind": "snapshots"},
    "press_button": {"action": "press"},
    # control_cover/control_media_player and the confidence pair already
    # speak the canonical arg shape (action/source, entity_id/status/
    # safety) — no implied args needed.
    # display: the absorbed name implies cast_to_screen's action=.
    "vcast_say": {"action": "say"},
    "vcast_list": {"action": "list"},
    "vcast_status": {"action": "status"},
    "vcast_shortcut": {"action": "shortcut"},
    # ada_set_voice's caller-supplied action (set|show|list) collides with
    # ada_persona's own actions — the implied action plus the
    # _alias_call_args remap land it on the *_voice forms instead.
    "ada_set_voice": {"action": "set_voice"},
    "ada_outcome": {"action": "outcome"},
    "ada_usage_summary": {"action": "usage"},
    "ada_mddb_health": {"action": "health"},
    "ada_decision_check": {"action": "check"},
    "ada_deep_research": {"action": "research"},
    "guest_register": {"who": "guest"},
    # tasks+status family — absorbed names' args map 1:1 onto the merged
    # schemas; only the implied action=/what=/photo=/doc= is needed.
    # get_battery_detail keeps its battery_index=1 default so what=battery
    # plus an index reads as the per-battery detail view.
    "tasks_add": {"action": "add"},
    "tasks_list": {"action": "list"},
    "tasks_complete": {"action": "done"},
    "tasks_move": {"action": "move"},
    "get_battery_status": {"what": "battery"},
    "get_battery_detail": {"what": "battery", "battery_index": 1},
    "get_power_summary": {"what": "power"},
    "get_inverter_status": {"what": "inverter"},
    "get_pool_status": {"what": "pool"},
    "get_dashboard_tab": {"what": "dashboard"},
    "get_habit_status": {"what": "habit"},
    "photos_pick": {"photo": "pick"},
    "photos_picked": {"photo": "picked"},
    "sys_show_uploaded_document": {"doc": "show"},
    "process_document_upload": {"doc": "process"},
    "doc_upload_card_action": {"doc": "card"},
    "yt_cast": {"action": "cast"},
    "yt_cast_status": {"action": "status"},
    "yt_cast_stop": {"action": "stop"},
    "yt_transcript": {"action": "transcript"},
    # devin family — every absorbed name implies its action=; all arg
    # names already match the canonical schemas 1:1 (no shim needed).
    "devin_dispatch": {"action": "dispatch"},
    "devin_followup": {"action": "followup"},
    "devin_answer": {"action": "answer"},
    "devin_status": {"action": "status"},
    "devin_jobs": {"action": "jobs"},
    "devin_pending": {"action": "pending"},
    "devin_job_report": {"action": "report"},
    "ada_devteam_review": {"action": "review"},
}

def _resolve_alias(name: str) -> tuple[str, dict[str, Any]]:
    """Map a retired tool name to (canonical tool, implied arg defaults)."""
    canonical = _ALIASES.get(name)
    if canonical:
        logger.info("tool alias %s -> %s", name, canonical)
        return canonical, dict(_ALIAS_ARG_DEFAULTS.get(name) or {})
    return name, {}


# Per-tool recovery/usage guidance (card ada-tools-desc-slim,
# 2026-10-05). The shipped schema description is a <=2-line routing
# blurb — the detailed contract lives in backend/tool_guide.yml and is
# attached to failed/denied results as a "usage" key (same pattern as
# needs_confirm/verify_warn), so it costs session context only when a
# call goes wrong.
# package files sit one level below backend/ — anchor at backend/.
_GUIDE_PATH = Path(__file__).resolve().parent.parent / "tool_guide.yml"
_TOOL_GUIDE: dict[str, str] | None = None


def _tool_guide() -> dict[str, str]:
    global _TOOL_GUIDE
    if _TOOL_GUIDE is None:
        try:
            import yaml
            data = yaml.safe_load(
                _GUIDE_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        _TOOL_GUIDE = {
            str(k): str(v) for k, v in data.items()
            if isinstance(v, str) and v.strip()}
    return _TOOL_GUIDE


def _alias_call_args(alias: str, args: dict[str, Any]) -> dict[str, Any]:
    """Surrogate-arg mapping for absorbed names whose parameters don't
    match the canonical tool 1:1 — the spec keeps this explicit per
    family, not generic. Runs on the merged args (implied defaults under
    caller args) before dispatch."""
    if alias == "vocab_note":
        # vocab_note(term, correct, note) -> ada_remember(text, kind=vocab)
        term = str(args.pop("term", "") or "").strip()
        correct = str(args.pop("correct", "") or "").strip()
        note = str(args.pop("note", "") or "").strip()
        if term or correct:
            args.setdefault("text", f"{term} → {correct}".strip(" →"))
        if note:
            args.setdefault("note", note)
    elif alias == "ada_ha_recall":
        # ada_ha_recall(query) -> ada_session_recall(question, scope=history)
        if "question" not in args and "query" in args:
            args["question"] = args.pop("query")
    elif alias in ("cms_get_page", "cms_verify_page"):
        # cms_*_page(slug, ...) -> cms_read(action, key, ...)
        if "key" not in args and "slug" in args:
            args["key"] = args.pop("slug")
    elif alias == "cms_automation":
        # cms_automation(action=list|get|set|...) -> cms_edit(action=
        # "automate", op=<registry action>) — the implied action merges
        # under caller args, so the caller's own action= lands in the
        # slot and must move to op before the canonical is re-stamped.
        sub = str(args.pop("action", "") or "")
        args["action"] = "automate"
        if sub and sub != "automate":
            args.setdefault("op", sub)
    elif alias == "ada_weekly_comparison":
        # ada_weekly_comparison(end, days) -> plan_day(period='week',
        # day=<window end>, days) — 'end' isn't a plan_day param.
        if "day" not in args and "end" in args:
            args["day"] = args.pop("end")
    elif alias == "vcast_shortcut":
        # The phantom census name's arg spellings (name/app/shortcut)
        # all funnel into url — cast_to_screen resolves an app short
        # name to its /apps/<name>/ page.
        if "url" not in args:
            for k in ("name", "app", "shortcut"):
                if k in args:
                    args["url"] = args.pop(k)
                    break
    elif alias == "ada_set_voice":
        # ada_set_voice(action=set|show|list, voice) -> ada_persona
        # (action=<sub>_voice, voice). The absorbed action names collide
        # with persona's own set/show/list, so remap onto the *_voice
        # forms; an already-remapped or unknown action passes through
        # untouched (bogus actions must not silently become voice sets).
        sub = str(args.pop("action", "") or "set").strip().lower()
        args["action"] = {
            "set": "set_voice", "show": "show_voice", "list": "list_voices",
        }.get(sub, sub or "set_voice")
    elif alias in ("sys_show_uploaded_document", "process_document_upload",
                   "doc_upload_card_action"):
        # chaba-side doc-upload card actions -> chat_send(doc=..., key, op):
        # the card's own action= (archive/print/discard) collides with the
        # implied doc= value's slot differently — 'action' means the card
        # button here, so it moves to op; intake_key -> key.
        if "key" not in args and "intake_key" in args:
            args["key"] = args.pop("intake_key")
        if "op" not in args and "action" in args:
            args["op"] = args.pop("action")
    return args


# ada_remember kinds that don't write a curated memory bank: 'vocab'
# appends the speaker's own vocab log (append-only, ungated like the
# absorbed vocab_note), 'guest' writes the chaba guest store (gated by
# chaba promotion), 'habit' is provider-dispatched telemetry. These skip
# the MEMORY_WRITE_TOOLS bank gate and the secondary-speaker block so the
# absorbed names keep exactly the access they had before the merge.
_REMEMBER_NONBANK_KINDS = frozenset({"vocab", "habit", "guest"})

# ada_persona actions that touch the actual speaking voice — the absorbed
# ada_set_voice seat (tools-merge-meta-voice). The provider intercepts
# these on live sessions (the swap needs an idle-gated reconnect); the
# runner's ada_persona serves the REST/alias path.
_PERSONA_VOICE_ACTIONS = frozenset(
    {"set_voice", "show_voice", "list_voices"})


def _first(value: list[str] | None) -> str | None:
    if not value:
        return None
    return str(value[0]) if value[0] else None


def _int_or_none(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _confirmed_truthy(value: Any) -> bool:
    """Accept the spellings LLM callers actually emit. The flag is
    self-asserted either way, so 'yes'/'true'/1 carry the same weight as a
    strict boolean — rejecting them only manufactured retry loops."""
    if value is True:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 1
    return isinstance(value, str) and value.strip().lower() in _CONFIRM_TRUE


def _confirm_fingerprint(tool: str, args: dict[str, Any]) -> str:
    """Stable digest binding a confirmation to one exact tool call."""
    blob = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(f"{tool}\0{blob}".encode()).hexdigest()



@dataclass
class ToolContext:
    ha_client: HomeAssistantClient
    habit_state_getter: Any | None = None


class AdaMemoryStore:
    """Lightweight in-memory Home Assistant snapshot for fast recall.

    Optionally persists snapshots to MDDB so they survive restarts and can be
    queried for history.
    """

    def __init__(
        self,
        ha_client: HomeAssistantClient,
        mddb_client: MddbClient | None = None,
        instance_id: str | None = None,
    ) -> None:
        self.ha_client = ha_client
        self.mddb = mddb_client
        self.instance = instance_id or ada_instance_id()
        self._collection = f"ada-ha-snapshots-{self.instance}"
        self._confidence_collection = f"ada-ha-device-confidence-{self.instance}"
        self._safety_collection = f"ada-ha-device-safety-{self.instance}"
        self._devices: list[dict[str, Any]] | None = None
        self._sensors: list[dict[str, Any]] | None = None
        self._overview: dict[str, Any] | None = None
        self._confidence: dict[str, str] = {}
        self._confidence_groups: dict[str, list[dict[str, Any]]] | None = None
        self._safety: dict[str, str] = {}
        self._safety_groups: dict[str, list[dict[str, Any]]] | None = None
        self._controllable_fetched_at = 0.0
        self._controllable_ttl = 60.0
        self._last_refresh: datetime | None = None

    async def _ensure(self) -> None:
        if self._devices is not None:
            return
        await self.refresh()

    async def _ensure_confidence(self) -> None:
        """Ensure controllable devices and confidence groups are fresh within TTL."""
        if self._devices is not None and (time.monotonic() - self._controllable_fetched_at) < self._controllable_ttl:
            return
        await self._refresh_controllable()

    async def _refresh_controllable(self) -> None:
        """Fetch only controllable devices and recompute confidence groups."""
        controllable = await self.ha_client.entities()
        known_confidence = await self._load_confidence()
        known_safety = await self._load_safety()
        self._devices = controllable
        self._classify_and_sync(controllable, known_confidence, known_safety)
        self._controllable_fetched_at = time.monotonic()

    async def refresh(self) -> None:
        states = await self.ha_client._states()
        controllable = await self.ha_client.entities()
        sensors = await self.ha_client.sensors(limit=200)
        self._devices = controllable
        self._sensors = sensors
        known_confidence = await self._load_confidence()
        known_safety = await self._load_safety()
        self._classify_and_sync(controllable, known_confidence, known_safety)
        self._overview = {
            "person_entity": self.ha_client.person_entity,
            "home_plugs": list(self.ha_client.home_plug_entities),
            "total_entities": len(states),
            "controllable_count": len(controllable),
            "sensor_count": len(sensors),
            "source": self.ha_client.base_url,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "confidence_summary": self._build_confidence_summary(),
        }
        self._last_refresh = datetime.now(timezone.utc)
        self._controllable_fetched_at = time.monotonic()
        if self.mddb is not None:
            await self._persist(states, controllable, sensors)

    def _build_content_md(self, states: list, controllable: list, sensors: list) -> str:
        overview = self._overview or {}
        lines = [
            f"# Home Assistant snapshot — {overview.get('source', 'unknown')}",
            "",
            "## Overview",
            f"- Person entity: `{overview.get('person_entity', 'unknown')}`",
            f"- Total entities: {overview.get('total_entities', 0)}",
            f"- Controllable devices: {overview.get('controllable_count', 0)}",
            f"- Sensors: {overview.get('sensor_count', 0)}",
            f"- Refreshed at: {overview.get('refreshed_at', '')}",
            "",
            "## Controllable devices",
        ]
        for dev in controllable[:50]:
            name = dev.get("name") or dev.get("entity_id", "unknown")
            lines.append(f"- {name} (`{dev.get('entity_id', 'unknown')}`): {dev.get('state', '')}")
        lines.extend(["", "## Sensors"])
        for s in sensors[:50]:
            name = s.get("name") or s.get("entity_id", "unknown")
            lines.append(f"- {name} (`{s.get('entity_id', 'unknown')}`): {s.get('state', '')} {s.get('unit', '')}")
        return "\n".join(lines)

    async def _persist(self, states: list, controllable: list, sensors: list) -> None:
        if self._last_refresh is None:
            return
        ts = self._last_refresh.strftime("%Y%m%d%H%M%S%f")
        key = f"snapshot-{self.instance}-{ts}"
        content_md = self._build_content_md(states, controllable, sensors)
        await self.mddb.add_document(
            collection=self._collection,
            key=key,
            lang="en",
            content_md=content_md,
            meta={
                "instance": [self.instance],
                "source": [str(self.ha_client.base_url)],
                "kind": ["snapshot"],
                "person_entity": [self.ha_client.person_entity],
                "total_entities": [str(len(states))],
                "controllable_count": [str(len(controllable))],
                "sensor_count": [str(len(sensors))],
                "refreshed_at": [self._last_refresh.isoformat()],
            },
        )

    # -- Device confidence --

    async def _load_confidence(self) -> dict[str, str]:
        if self.mddb is None:
            return {}
        docs = await self.mddb.search_documents(
            collection=self._confidence_collection,
            query="*",
            limit=1000,
        )
        known = {}
        for doc in docs:
            meta = doc.get("meta", {})
            eid = _first(meta.get("entity_id"))
            status = _first(meta.get("confidence"))
            if eid and status:
                known[eid] = status
        return known

    async def _load_safety(self) -> dict[str, str]:
        if self.mddb is None:
            return {}
        docs = await self.mddb.search_documents(
            collection=self._safety_collection,
            query="*",
            limit=1000,
        )
        known = {}
        for doc in docs:
            meta = doc.get("meta", {})
            eid = _first(meta.get("entity_id"))
            safety = _first(meta.get("safety"))
            if eid and safety:
                known[eid] = safety
        return known

    def _classify_and_sync(self, devices: list[dict[str, Any]], known_confidence: dict[str, str], known_safety: dict[str, str]) -> None:
        confidence_groups: dict[str, list[dict[str, Any]]] = {
            "trusted_working": [],
            "trusted_broken": [],
            "learning": [],
            "needs_integration": [],
        }
        safety_groups: dict[str, list[dict[str, Any]]] = {
            "safe": [],
            "caution": [],
            "dangerous": [],
        }
        for dev in devices:
            eid = dev.get("entity_id")
            state = str(dev.get("state", "unknown"))
            available = bool(dev.get("available"))
            status = known_confidence.get(eid) if eid else None
            if status not in confidence_groups:
                status = None
            if not available or state in {"unavailable", "unknown"}:
                status = "trusted_broken"
            elif status is None:
                status = "needs_integration"
            dev["confidence"] = status
            confidence_groups.setdefault(status, []).append(dev)

            # Default safety: covers/shutters and mains plugs are dangerous; everything else cautious until marked safe.
            raw_safety = known_safety.get(eid) if eid else None
            if raw_safety not in safety_groups:
                raw_safety = None
            if raw_safety is None:
                if isinstance(eid, str) and (eid.startswith("cover.")
                        or re.match(r"switch\.(plug|plak|usb_test_hub)", eid)):
                    raw_safety = "dangerous"
                else:
                    raw_safety = "caution"
            dev["safety"] = raw_safety
            safety_groups.setdefault(raw_safety, []).append(dev)
        self._confidence = {d.get("entity_id", ""): d.get("confidence", "needs_integration") for d in devices if d.get("entity_id")}
        self._confidence_groups = confidence_groups
        self._safety = {d.get("entity_id", ""): d.get("safety", "caution") for d in devices if d.get("entity_id")}
        self._safety_groups = safety_groups

    async def _save_confidence(self, entity_id: str, status: str) -> None:
        if self.mddb is None:
            return
        key = entity_id.replace(".", "-").replace("/", "-")
        await self.mddb.add_document(
            collection=self._confidence_collection,
            key=key,
            lang="en",
            content_md=f"# {entity_id}\n\nconfidence: {status}",
            meta={
                "entity_id": [entity_id],
                "confidence": [status],
                "instance": [self.instance],
                "source": [str(self.ha_client.base_url)],
            },
        )

    async def _save_safety(self, entity_id: str, safety: str) -> None:
        if self.mddb is None:
            return
        key = entity_id.replace(".", "-").replace("/", "-")
        await self.mddb.add_document(
            collection=self._safety_collection,
            key=key,
            lang="en",
            content_md=f"# {entity_id}\n\nsafety: {safety}",
            meta={
                "entity_id": [entity_id],
                "safety": [safety],
                "instance": [self.instance],
                "source": [str(self.ha_client.base_url)],
            },
        )

    async def set_confidence(self, entity_id: str, status: str, safety: str | None = None) -> str:
        if status not in {"trusted_working", "trusted_broken", "learning", "needs_integration"}:
            raise ValueError(f"Invalid confidence status: {status}")
        if safety is not None and safety not in {"safe", "caution", "dangerous"}:
            raise ValueError(f"Invalid safety level: {safety}")
        if self._devices is None:
            await self._ensure_confidence()
        for dev in self._devices or []:
            if dev.get("entity_id") == entity_id:
                dev["confidence"] = status
                if safety is not None:
                    dev["safety"] = safety
                break
        if self._confidence_groups is not None:
            # Rebuild groups quickly
            for group in self._confidence_groups.values():
                for dev in group:
                    if dev.get("entity_id") == entity_id:
                        dev["confidence"] = status
                        if safety is not None:
                            dev["safety"] = safety
                        break
        if self._safety_groups is not None and safety is not None:
            for group in self._safety_groups.values():
                for dev in group:
                    if dev.get("entity_id") == entity_id:
                        dev["safety"] = safety
                        break
        self._confidence[entity_id] = status
        if safety is not None:
            self._safety[entity_id] = safety
        if self.mddb is not None:
            await self._save_confidence(entity_id, status)
            if safety is not None:
                await self._save_safety(entity_id, safety)
        parts = [f"confidence: {status}"]
        if safety is not None:
            parts.append(f"safety: {safety}")
        self._controllable_fetched_at = 0.0  # force next confidence call to reclassify
        return f"{entity_id} is now {', '.join(parts)}"

    def confidence_groups(self) -> dict[str, list[dict[str, Any]]]:
        return self._confidence_groups or {
            "trusted_working": [],
            "trusted_broken": [],
            "learning": [],
            "needs_integration": [],
        }

    def _build_confidence_summary(self) -> str:
        groups = self.confidence_groups()
        counts = {k: len(v) for k, v in groups.items()}
        parts = [
            f"{counts['trusted_working']} trusted",
            f"{counts['trusted_broken']} broken",
            f"{counts['learning']} learning",
            f"{counts['needs_integration']} need setup",
        ]
        return f"{sum(counts.values())} devices: {', '.join(parts)}"

    def _match(self, query: str, items: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
        q = str(query).strip().lower()
        if not q:
            return []
        tokens = q.split()
        scored = []
        for item in items:
            text = " ".join(str(item.get(k, "")).lower() for k in keys)
            if q in text:
                score = 100
            else:
                score = sum(10 for token in tokens if token in text)
            if score:
                scored.append((score, item))
        scored.sort(key=lambda pair: -pair[0])
        return [item for _, item in scored]

    async def overview(self) -> dict[str, Any]:
        await self._ensure()
        await self._ensure_confidence()
        if self._overview is not None and self._confidence_groups is not None:
            self._overview["confidence_summary"] = self._build_confidence_summary()
        return self._overview or {}

    async def search_devices(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        await self._ensure()
        matches = self._match(query, self._devices or [], ("name", "entity_id"))
        return matches[:limit]

    async def search_sensors(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        await self._ensure()
        matches = self._match(query, self._sensors or [], ("name", "entity_id"))
        return matches[:limit]

    async def search_all(self, query: str, limit: int = 10) -> dict[str, Any]:
        await self._ensure()
        return {
            "query": query,
            "devices": self._match(query, self._devices or [], ("name", "entity_id"))[:limit],
            "sensors": self._match(query, self._sensors or [], ("name", "entity_id"))[:limit],
        }




# Every non-dunder name here is re-exported into the package
# namespace (backend/tool_runner/__init__.py) and each domain mixin —
# the wildcard keeps each module's globals identical to the original
# single-file layout, so `from backend.tool_runner import X` and
# patch("backend.tool_runner.X") targets all still resolve.
__all__ = [n for n in dir() if not n.startswith('__')]
