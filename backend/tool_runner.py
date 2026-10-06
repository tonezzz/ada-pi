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
_CALLER_SPEAKER_SESSION: contextvars.ContextVar = contextvars.ContextVar(
    "caller_speaker_session", default=None)

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
CONFIRM_TOKEN_TTL_S = float(os.environ.get("ADA_CONFIRM_TOKEN_TTL_S", "120"))
_CONFIRM_TOKEN_MAX = 64
_CONFIRM_AUDIT_MAX = 200

CONTROL_RATE_WINDOW_S = float(os.environ.get("ADA_CONTROL_RATE_WINDOW_S", "60"))
CONTROL_MAX_PER_ENTITY = int(os.environ.get("ADA_CONTROL_MAX_PER_ENTITY", "5"))
CONTROL_MAX_GLOBAL = int(os.environ.get("ADA_CONTROL_MAX_GLOBAL", "30"))

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
_GUIDE_PATH = Path(__file__).resolve().parent / "tool_guide.yml"
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


class ToolRunner:
    """Execute Ada tools for FastAPI and the voice provider."""

    def __init__(self, ha_client: HomeAssistantClient, habit_state_getter: Any | None = None, instance_id: str | None = None) -> None:
        self.context = ToolContext(ha_client=ha_client, habit_state_getter=habit_state_getter)
        # CHABA_MEMORY=1 guest mode: file-backed public memory, no MDDB.
        self.chaba = chaba_memory.get_store() if chaba_memory.enabled() else None
        if self.chaba is not None:
            self.mddb = None
            self.memory = None
            self.events = None
        else:
            self.mddb = MddbClient()
            self.memory = AdaMemoryStore(ha_client, mddb_client=self.mddb, instance_id=instance_id)
            self.events = HaEventRecorder(ha_client, mddb_client=self.mddb, instance_id=instance_id)
        self._instance_id = instance_id
        # Dedup breaker: tracks identical needs_confirm denials so a model
        # that retries the same gated call instead of asking the user gets
        # a hard stop. {(tool, args-key): [timestamps]}
        self._denials: dict[tuple[str, str], list[float]] = {}
        # Voice session that invoked the current tool call; the realtime
        # provider sets this so memory writes carry provenance.
        self.session_id: str | None = None
        # HA person entity of the current speaker (set by the realtime
        # provider from speaker ID). Used to route personal memory to the
        # speaker's person-scoped bank instead of the instance default.
        self.current_speaker_ha_person: str | None = None
        # Issued-key/caller name for this session (set by pwa_server at ws
        # connect). Fallback memory-policy identity when speaker ID is off.
        self.session_caller_name: str | None = None
        # HA person entity bound to the session's issued key at connect
        # (auth.ha_person_for_key). Bridges key -> person.<id> identity so
        # unenrolled speakers still land in their person-scoped bank.
        self.session_caller_ha_person: str | None = None
        # Session owner identity pinned at websocket connect
        # (session_caller_ha_person or session_caller_name). Immutable for
        # the session's lifetime — ALL permission checks evaluate this;
        # a recognized guest voice can never widen authorization (P1/P3).
        self.session_owner_identity: str | None = None
        # Shared L0 doc-work log — the provider assigns this to the
        # conversation's doc_items list so ada_doc_* calls land in the
        # session report timeline. None = doc actions not recorded.
        self.doc_log: list[dict[str, Any]] | None = None
        # Shared L0 session-mechanics log — same pattern as doc_log, for
        # policy denials and security-relevant events.
        self.event_log: list[dict[str, Any]] | None = None
        # Active SpeakerSession for voice enrollment — set by pwa_server
        # when the WebSocket session opens. Used by ada_enroll_speaker to
        # capture the user's voice from the buffered audio.
        self.speaker_session: Any | None = None
        self._banks: MemoryBankRegistry | None = None
        self._decision_engine: DecisionCheckEngine | None = None
        self._calendar: CalendarService | None = None
        self._calendar_loaded = False
        self._control_calls: list[float] = []
        self._control_entity_calls: dict[str, list[float]] = {}
        # Bound confirmations minted on denial: token -> {fp, at, tool}.
        # Single-use, TTL'd; see _mint_confirm_token/_consume_confirm_token.
        self._confirm_tokens: dict[str, dict[str, Any]] = {}
        # Structured ledger of confirm proposals/grants/consumptions — the
        # audit trail for "which action did the user actually approve?".
        self._confirm_audit: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=_CONFIRM_AUDIT_MAX
        )

    def _memory_identity(self) -> str | None:
        """Memory-policy identity: the in-flight call's identity first
        (ContextVar set by execute()), then the identified speaker's HA
        person, then the key's bound HA person / caller name, else None."""
        v = _CALLER_IDENTITY.get()
        if v is not None:
            return v
        return (self._current_speaker()
                or self.session_caller_ha_person
                or self.session_caller_name)

    def _current_speaker(self) -> str | None:
        """Session-scoped identified speaker — the ContextVar set by
        execute() wins (a session with NO identified speaker passes None,
        which must NOT fall back to the shared field); the shared field is
        only a fallback for non-ws callers (REST/tests)."""
        v = _CALLER_SPEAKER.get()
        if v is _IDENTITY_UNSET:
            return self.current_speaker_ha_person
        return v

    def _owner(self) -> str | None:
        """The calling session's pinned owner — per-call contextvar first
        (shared runner can't race it), shared field as fallback."""
        return _CALLER_OWNER.get() or self.session_owner_identity

    def policy_identity(self) -> str | None:
        """Authorization identity: the session owner pinned at connect.
        Falls back to the live speaker identity only when no session owner
        exists (REST calls, anonymous/test sessions)."""
        return self._owner() or self._memory_identity()

    def _is_secondary_turn(self) -> bool:
        """True while the identified speaker is not the session owner.

        Only a positively-identified different voice counts — an
        unrecognized voice can never be proven non-owner, so it keeps the
        owner's rights (unenrolled owners must not lock themselves out).
        """
        speaker = self._current_speaker()
        owner = self._owner()
        if not speaker or not owner or speaker == owner:
            return False
        # Stale label check: when the buffer has seen voiced chunks since
        # the label last re-confirmed, an aged-out identification is just
        # a stale guess — drop it (SPEAKER_STALE_S rationale above).
        ss = _CALLER_SPEAKER_SESSION.get()
        if ss is not None:
            age = getattr(ss, "speaker_age_s", lambda: None)()
            if age is not None and age > SPEAKER_STALE_S:
                logger.info(
                    "speaker label %r expired (%.0fs unconfirmed) — "
                    "treating turn as unrecognized", speaker, age)
                return False
        caller = self.session_caller_name
        aliases = {owner, caller, f"person.{_slug(caller)}" if caller else None}
        return speaker not in aliases

    def _secondary_blocked_tools(self) -> set[str]:
        """Blocked tool set for secondary turns. Rendered SSOT config
        (`session_security.secondary_blocked`) overrides the default when a
        list is present; absent/malformed config fails closed to the
        default. Entries may be group names or literal tool names."""
        try:
            spec = self.banks.session_security.get("secondary_blocked")
        except Exception:
            spec = None
        if not isinstance(spec, list):
            return SECONDARY_BLOCKED_TOOLS | {"persona_write"}
        blocked: set[str] = set()
        for tok in spec:
            tok = str(tok)
            blocked |= _SECONDARY_BLOCKED_GROUPS.get(tok, {tok})
        return blocked

    def _log_session_event(self, kind: str, **fields: Any) -> None:
        if self.event_log is not None:
            self.event_log.append({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": str(kind), **fields})

    @property
    def banks(self) -> MemoryBankRegistry:
        """Memory-bank registry, loaded on first use so a missing
        ADA_INSTANCE_ID only fails when memory tools are actually invoked."""
        if self._banks is None:
            if self._instance_id:
                self._banks = MemoryBankRegistry(instance=self._instance_id)
            else:
                self._banks = get_registry()
        return self._banks

    @property
    def decision_engine(self) -> DecisionCheckEngine:
        """Shared purchase-check pipeline for POST /api/decision/check and the
        ada_decision_check voice tool — same persist + event side-effects."""
        if self._decision_engine is None:
            self._decision_engine = DecisionCheckEngine(
                self.mddb, self.banks, self.memory.instance,
                ha_client=self.context.ha_client,
            )
        return self._decision_engine

    async def execute(self, name: str, args: dict[str, Any] | None = None,
                      *, identity: Any = _IDENTITY_UNSET,
                      speaker: Any = _IDENTITY_UNSET,
                      speaker_session: Any = _IDENTITY_UNSET,
                      owner: Any = _IDENTITY_UNSET) -> dict[str, Any]:
        """Run a tool by name. Always returns the canonical
        {ok: bool, error?: str, ...} dict (normalize_tool_result wraps
        legacy shapes). Gate denials (confirmation required, secondary
        speaker, policy) still raise PermissionError — a refused call is
        not a tool result."""
        # Retired names from the tool consolidation resolve to their
        # canonical action= tool first — the alias table is the contract,
        # phonetic normalization below is just typo repair. The alias's
        # implied args (its action=) go under the caller's, not over.
        _implied: dict[str, Any] = {}
        _alias_src = name
        if isinstance(name, str):
            name, _implied = _resolve_alias(name)
        # Gemini sometimes spells underscore-heavy tool names phonetically
        # (c_c_t_v_wall) — normalize back so the call lands.
        method = getattr(self, name, None)
        if not method and isinstance(name, str) and "_" in name:
            bare = name.replace("_", "")
            for attr in dir(self):
                if attr.replace("_", "") == bare:
                    logger.info("tool name normalized %s -> %s",
                                name, attr)
                    name = attr
                    method = getattr(self, name, None)
                    break
        dyn_spec = None
        if not method:
            dyn_spec = self._dynamic_tools.tools.get(name)
            if dyn_spec is None:
                raise KeyError(f"Unknown tool: {name}")
            method = self._bind_dynamic(dyn_spec)
        call_args = {**_implied, **dict(args or {})}
        if _alias_src != name:
            call_args = _alias_call_args(str(_alias_src), call_args)
        # Resume the write outbox after a restart — entries queued before the
        # process died retry on the first tool call of the new process.
        if self.mddb is not None:
            write_outbox.ensure_running(self.mddb)
        # Per-call identity override: the provider passes the SESSION's
        # resolved identity — the runner is shared across sessions, so its
        # mutable identity fields can race when sessions overlap.
        ident = self._memory_identity() if identity is _IDENTITY_UNSET else identity
        _ident_token = _CALLER_IDENTITY.set(ident)
        # Same for the identified speaker: another session's voice ID must
        # not flip this session's secondary-owner gate mid-call.
        _spk_token = (
            _CALLER_SPEAKER.set(speaker)
            if speaker is not _IDENTITY_UNSET else None
        )
        # Same for the speaker session: the provider passes its own so a
        # concurrent ws connect can't swap the buffer out from under an
        # in-flight enroll call.
        _ss_token = (
            _CALLER_SPEAKER_SESSION.set(speaker_session)
            if speaker_session is not _IDENTITY_UNSET else None
        )
        # And the pinned owner: another session's connect must not re-pin
        # the owner this call authorizes against.
        _owner_token = (
            _CALLER_OWNER.set(owner)
            if owner is not _IDENTITY_UNSET else None
        )
        try:
            return await self._execute_gated(name, call_args, ident,
                                             method=method, dyn_spec=dyn_spec)
        except Exception as exc:
            # Gate denials and arg errors raise instead of returning a
            # dict — append the error-path guide to the exception text so
            # the provider's {"error": ...} wrapper still carries it.
            hint = _tool_guide().get(str(name))
            if hint:
                exc.args = (f"{exc}\nusage: {hint}",)
            raise
        finally:
            _CALLER_IDENTITY.reset(_ident_token)
            _CALLER_VERIFIED_AFFIRM.set(False)
            if _spk_token is not None:
                _CALLER_SPEAKER.reset(_spk_token)
            if _ss_token is not None:
                _CALLER_SPEAKER_SESSION.reset(_ss_token)
            if _owner_token is not None:
                _CALLER_OWNER.reset(_owner_token)

    @property
    def _dynamic_tools(self) -> tools_loader.ToolRegistry:
        return tools_loader.registry()

    def _bind_dynamic(self, spec: "tools_loader.DynamicTool") -> Any:
        """Wrap a tools.d run(runner, **args) as a method-like callable with
        the manifest's timeout enforced."""
        async def _bound(**kwargs: Any) -> Any:
            import asyncio
            return await asyncio.wait_for(
                spec.run(self, **kwargs), timeout=spec.timeout_s)
        return _bound

    async def _execute_gated(self, name: str, call_args: dict[str, Any],
                             ident: str | None, method: Any = None,
                             dyn_spec: Any = None) -> Any:
        if method is None:
            method = getattr(self, name, None)
        confirm: tuple[Any, Any] = (
            call_args.pop("confirmed", None),
            call_args.pop("confirm_token", None),
        )
        # Provider-verified user affirmation: the user actually said yes in
        # this session, so a confirmed call may satisfy a register-then-
        # confirm gate in one step (the pending key is still recorded).
        _affirm_token = _CALLER_VERIFIED_AFFIRM.set(
            bool(call_args.pop("_verified_affirm", None)))
        policy_ident = self.policy_identity()
        # tools-merge-display (2026-10-05): cast_to_screen's absorbed
        # seats — action='list'/'status' were ungated reads (vcast_list /
        # the phantom vcast_status) and 'say' (vcast_say) was actuating
        # but never gated or secondary-blocked. The merged actions keep
        # exactly the access they had; screen-ownership ACL still runs
        # inside the method where it always did.
        cast_ungated = (
            name == "cast_to_screen" and str(
                call_args.get("action") or "").lower()
            in {"list", "status", "say"})
        if self._is_secondary_turn():
            action = str(call_args.get("action") or "").lower()
            blocked = self._secondary_blocked_tools()
            # Dynamic (tools.d) tools default to owner-only on secondary
            # turns — the manifest must explicitly set secondary_allowed.
            if dyn_spec is not None and not dyn_spec.secondary_allowed:
                blocked = blocked | {name}
            # ada_remember's non-bank kinds (vocab/guest/habit) absorbed
            # tools that were never secondary-blocked — the carve-out keeps
            # a guest's own notes and vocab log reachable on their turn.
            nonbank_remember = (
                name == "ada_remember" and str(
                    call_args.get("kind") or "").lower()
                in _REMEMBER_NONBANK_KINDS)
            # scope='guest' absorbed guest_recall — a public-store read that
            # was never secondary-blocked.
            guest_scope_search = (
                name == "ada_memory_search" and str(
                    call_args.get("scope") or "").lower() == "guest")
            # cms_edit action='note' absorbed cms_note_update — a merge-only
            # append that was never gated or secondary-blocked.
            cms_note_edit = (
                name == "cms_edit" and str(
                    call_args.get("action") or "").lower() == "note")
            # ha_confidence without status/safety absorbed
            # ada_ha_get_device_confidence — a read that was never
            # secondary-blocked; only the set path stays gated.
            ha_confidence_read = (
                name == "ha_confidence" and not (
                    call_args.get("status") or call_args.get("safety")))
            # ada_ops absorbed five meta tools (tools-merge-meta-voice) with
            # mixed postures — the name carries ada_outcome's blocked seat,
            # but action='usage' was an open read and 'research' was never
            # secondary-blocked, so those stay reachable on a guest turn.
            ops_read = (
                name == "ada_ops" and action in ("usage", "research"))
            # tools-merge-tasks-status carve-outs: tasks action='list' is
            # the absorbed tasks_list (a free read) and a plain chat_send
            # (no photo=/doc= flow) was never secondary-blocked — only the
            # absorbed photos picker / doc-upload flows are owner-tier.
            tasks_list_read = name == "tasks" and action == "list"
            chat_doc_flow = name == "chat_send" and (
                call_args.get("photo") or call_args.get("doc"))
            # ada_enroll_speaker is exempt here — its own check is smarter:
            # the owner can re-enroll even while a secondary voice is
            # identified, as long as the buffer voice isn't the secondary's
            # (2026-09-29 deadlock: Tony locked out of his own session
            # because KK's identification was sticky and enroll refused).
            if (name in blocked and name != "ada_enroll_speaker"
                    and not nonbank_remember
                    and not guest_scope_search
                    and not cms_note_edit
                    and not ha_confidence_read
                    and not cast_ungated
                    and not ops_read
                    and not tasks_list_read
                    and (name != "chat_send" or chat_doc_flow)) or (
                name == "ada_persona" and "persona_write" in blocked
                # *_voice actions absorbed ada_set_voice — that tool was
                # never runner-side secondary-blocked (the provider gate
                # covers live turns), so only set/reset keep the seat.
                and action in ("set", "reset")
            ):
                self._log_session_event(
                    "tool_denied", tool=name,
                    speaker=self._current_speaker(),
                    owner=self.session_owner_identity)
                logger.warning(
                    "denied %s: secondary speaker %r (owner %r)",
                    name, self._current_speaker(),
                    self.session_owner_identity)
                raise PermissionError(
                    "Only the session owner can run this action — propose it "
                    "to them aloud and let them confirm in their own voice.")
        try:
            if name in CAPTURE_CONFIRMED_TOOLS:
                # camera-capture gate first (uplink/wall-start/cctv-snapshot);
                # cast_to_screen then still runs the control gate (read-only,
                # rate limit) — except the absorbed read/narrate actions,
                # which were never control-gated.
                self._check_capture_confirmed(name, call_args, confirm[0])
                if name == "cast_to_screen" and not cast_ungated:
                    await self._check_control_allowed(name, call_args, *confirm)
            elif name in CONTROL_TOOLS:
                await self._check_control_allowed(name, call_args, *confirm)

            elif name in MEMORY_WRITE_TOOLS:
                # ada_remember kind=vocab/guest/habit writes outside the
                # curated banks (own vocab log, chaba guest store, habit
                # telemetry) — the absorbed tools were never bank-gated.
                if not (name == "ada_remember" and str(
                        call_args.get("kind") or "").lower()
                        in _REMEMBER_NONBANK_KINDS) and not (
                        # ada_ops holds ada_outcome's write seat — only
                        # action='outcome' is a bank write; usage/health/
                        # check/research carry no bank arg and skip the gate.
                        name == "ada_ops" and str(
                            call_args.get("action") or "").lower()
                        != "outcome"):
                    self._check_memory_write_allowed(name, call_args, *confirm)
            elif name in CALENDAR_WRITE_TOOLS:
                self._check_calendar_write_allowed(name, call_args, *confirm)
            elif name in CMS_WRITE_TOOLS:
                self._check_cms_write_allowed(name, call_args, *confirm)
            elif name in DEVIN_CONFIRMED_TOOLS:
                self._check_devin_confirmed(name, call_args, *confirm)
            elif dyn_spec is not None:
                self._check_dynamic_allowed(name, dyn_spec, call_args, *confirm)
            elif name in DOC_TOOLS:
                if not self.banks.bank_allowed(DOC_BANK, policy_ident):
                    logger.warning(
                        "denied %s for identity %r: documents bank policy",
                        name, ident)
                    raise PermissionError(
                        "document tools are outside this session's access policy")
                if name in DOC_CONFIRMED_TOOLS:
                    self._check_doc_confirmed(name, call_args, *confirm)
            elif name in DRIVE_TOOLS:
                # Whole-Drive + Photos access is owner-tier: same bank
                # policy as the document tools. chat_send absorbed the
                # photos picker + doc-upload card flow (tools-merge-
                # tasks-status) — only photo=/doc= calls take this gate;
                # a plain text/image send was never bank-scoped.
                if name == "chat_send" and not (
                        call_args.get("photo") or call_args.get("doc")):
                    pass
                else:
                    if not self.banks.bank_allowed(DOC_BANK, policy_ident):
                        logger.warning(
                            "denied %s for identity %r: documents bank policy",
                            name, ident)
                        raise PermissionError(
                            "drive/photos tools are outside this session's access policy")
                    if name in DRIVE_CONFIRMED_TOOLS:
                        self._check_drive_confirmed(name, call_args, *confirm)
        except PermissionError as exc:
            # Phantom-save guard (2026-09-28): the model papered over refused
            # writes and claimed success aloud. Every gate denial now carries
            # a blunt prefix the narration layer cannot miss.
            raise PermissionError(
                "NOT EXECUTED — the action did not happen and must not be "
                f"described as done/saved/published: {exc}"
            ) from exc
        # confirmed/confirm_token were popped for the gates above — hand them
        # back to methods that declare them (e.g. devin_job_report re-checks
        # confirmation internally on its publish path).
        _params = inspect.signature(method).parameters
        if "confirmed" in _params and confirm[0] is not None:
            call_args["confirmed"] = confirm[0]
        if "confirm_token" in _params and confirm[1] is not None:
            call_args["confirm_token"] = confirm[1]
        call_args = self._normalize_args(name, method, call_args)
        logger.info("tool %s args=%r", name, call_args)
        try:
            result = await method(**call_args)
        except Exception as exc:
            # The tools.d contract ("never raise into the model") applies
            # to native methods too — a tool that fails mid-execution
            # reports {ok: False, error} like every other failure shape.
            # Dispatch-layer denials (the _check_* gates above, an
            # unknown tool name) still raise: the tool never ran, so
            # there is no tool result.
            logger.warning("tool %s raised: %s", name, exc)
            result = {
                "ok": False,
                "error": str(exc) or type(exc).__name__,
                "error_type": type(exc).__name__,
            }
        result = normalize_tool_result(result)
        self._log_change_request(name, call_args, result, ident)
        result = self._denial_breaker(name, call_args, result)
        result = self._usage_hint(name, result)
        return await self._capture_reminder(name, result)

    def _usage_hint(self, name: str, result: Any) -> Any:
        """Attach the tool's error-path guide (backend/tool_guide.yml) to
        a failed or denied call — the slim schema description carries
        only routing, so on error the model gets the detailed contract
        here (same shape as the needs_confirm/verify_warn payloads)."""
        if not isinstance(result, dict) or "usage" in result:
            return result
        if not (result.get("error") or result.get("needs_confirm")
                or result.get("ok") is False):
            return result
        hint = _tool_guide().get(name)
        if hint:
            result["usage"] = hint
        return result

    # User-visible state changes Ada applies herself. Transient home
    # controls (lights, media) and memory-bank writes are deliberately
    # excluded — noisy, and already tracked elsewhere.
    _CHANGE_LOG_TOOLS = {
        "cms_publish_page", "cms_edit", "docs", "ada_forget",
        "ha_confidence", "devin", "devin_read",
    }

    def _log_change_request(self, name: str, args: dict[str, Any],
                            result: Any, ident: Any) -> None:
        """Mirror every applied system change into the Ada events feed
        (events.md -> ada-review -> Devin memory render) so changes Tony
        asks Ada for vocally reach Devin sessions — the team-sync lane."""
        if name not in self._CHANGE_LOG_TOOLS:
            return
        # ada_doc_archive's old seat — only the archive action is a
        # state change worth logging, not the read actions.
        if (name == "docs" and str(args.get("action") or "").lower()
                != "archive"):
            return
        # ha_confidence holds the absorbed ada_ha_set_device_confidence
        # seat — reads (no status/safety) aren't changes.
        if name == "ha_confidence" and not (
                args.get("status") or args.get("safety")):
            return
        # devin_read is mostly reads — only its write actions are changes
        # (devin_job_report logged every call pre-merge; review files a
        # spec doc into the handoff bank).
        if name == "devin_read" and str(
                args.get("action") or "") not in ("report", "review"):
            return
        if isinstance(result, dict) and result.get("ok") is False:
            return
        detail = args.get("slug") or args.get("title") or \
            args.get("task_id") or args.get("key") or \
            str(args.get("task") or "")[:80] or name
        try:
            from backend.event_log import log_event
            actor = getattr(ident, "name", None) or str(ident or "?")
            log_event("change-request", actor, name, str(detail)[:120])
        except Exception:
            logger.debug("change-request event log failed", exc_info=True)

    def _denial_breaker(self, name: str, args: dict[str, Any],
                        result: Any) -> Any:
        """The model sometimes retries an identical needs_confirm-gated
        call over and over (self-asserted confirmed=true gets stripped, so
        it can never pass) instead of asking the user aloud and waiting a
        turn. After the 3rd identical denial in 3min, return a hard stop."""
        if not isinstance(result, dict) or "needs_confirm" not in result:
            return result
        import hashlib
        key_args = json.dumps(
            {k: args.get(k) for k in ("screen", "action", "url")},
            sort_keys=True, default=str)
        key = (name, hashlib.md5(key_args.encode()).hexdigest())
        now = time.time()
        hits = [t for t in self._denials.get(key, []) if now - t < 180]
        hits.append(now)
        self._denials[key] = hits
        if len(hits) >= 3:
            self._denials[key] = []
            return {"ok": False, "error": (
                "STOP RETRYING — this exact call was refused " +
                str(len(hits)) +
                " times: the screen is busy and self-asserted confirmed=true "
                "does not count. Do NOT call this tool again right now — "
                "tell the user aloud what is running on the screen, ask if "
                "they want it replaced, and wait for their spoken yes in "
                "the next turn.")}
        return result

    # Tools that already carry capture state — a reminder on them would
    # be noise (the capture IS the subject of these calls). The absorbed
    # vcast_list/vcast_say names ride in as cast_to_screen actions, so
    # the canonical name covers them (tools-merge-display).
    _CAPTURE_AWARE_TOOLS = {
        "cast_to_screen", "cctv_wall", "ada_camera_snapshot",
    }
    _capture_reminded_ts = 0.0

    async def _capture_reminder(self, name: str, result: Any) -> Any:
        """While a camera capture/uplink/wall is live, tag unrelated tool
        results with a one-line reminder so Ada can't silently walk away
        from a running capture mid-conversation (2026-09-29 transcript:
        topic-shift to calendar left the zone-a wall unacknowledged).
        Throttled to once a minute."""
        if (name in self._CAPTURE_AWARE_TOOLS
                or not isinstance(result, dict)
                or result.get("capture_reminder")
                or time.time() - self._capture_reminded_ts < 60):
            return result
        try:
            import asyncio
            caps = await asyncio.to_thread(self._vcast_api, "/capture")
            live = {s: c for s, c in (caps.get("captures") or {}).items()
                    if c.get("active")}
            if not live:
                return result
            self._capture_reminded_ts = time.time()
            desc = ", ".join(
                f"screen {s}: {c.get('source') or 'cam'}"
                + (f" ({c['ch']})" if c.get("ch") else "")
                for s, c in live.items())
            result["capture_reminder"] = (
                f"heads-up — camera capture still live ({desc}); "
                "acknowledge it to the user or ask before ending "
                "the topic.")
        except Exception:
            pass
        return result

    @staticmethod
    def _normalize_args(name: str, method: Any, call_args: dict[str, Any]) -> dict[str, Any]:
        params = inspect.signature(method).parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return call_args
        for alias, canonical in _ARG_ALIASES.items():
            if alias in call_args and canonical in params and canonical not in call_args:
                call_args[canonical] = call_args.pop(alias)
        extra = [k for k in call_args if k not in params]
        if extra:
            logger.warning("tool %s: ignoring unexpected args %r", name, extra)
            for key in extra:
                call_args.pop(key, None)
        return call_args

    # -- confirmation gate machinery -------------------------------------

    def _audit_confirmation(
        self, event: str, tool: str, fingerprint: str, via: str
    ) -> None:
        self._confirm_audit.append({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "tool": tool,
            "fingerprint": fingerprint[:12],
            "via": via,
            "identity": self._memory_identity(),
        })

    def confirmation_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest-last ledger of confirm proposals, grants, consumptions,
        and denials — the auditable answer to 'approved which action?'."""
        entries = list(self._confirm_audit)
        return entries[-max(1, int(limit)):]

    def _mint_confirm_token(self, tool: str, args: dict[str, Any]) -> str:
        """Mint a single-use token bound to this exact tool + args."""
        now = time.monotonic()
        for tok, rec in list(self._confirm_tokens.items()):
            if now - rec["at"] > CONFIRM_TOKEN_TTL_S:
                self._confirm_tokens.pop(tok, None)
        while len(self._confirm_tokens) >= _CONFIRM_TOKEN_MAX:
            self._confirm_tokens.pop(next(iter(self._confirm_tokens)))
        token = "cfm-" + secrets.token_hex(5)
        fp = _confirm_fingerprint(tool, args)
        self._confirm_tokens[token] = {"fp": fp, "at": now, "tool": tool}
        self._audit_confirmation("proposed", tool, fp, "token")
        return token

    def _consume_confirm_token(
        self, tool: str, args: dict[str, Any], token: Any
    ) -> str | None:
        """Validate and consume a minted token; None = accepted, else reason."""
        if not isinstance(token, str) or not token.strip():
            return "not a string"
        rec = self._confirm_tokens.get(token)
        if rec is None:
            return "unknown or expired confirm_token"
        if time.monotonic() - rec["at"] > CONFIRM_TOKEN_TTL_S:
            self._confirm_tokens.pop(token, None)
            return "confirm_token expired"
        if rec["tool"] != tool:
            return "confirm_token was issued for a different tool"
        if rec["fp"] != _confirm_fingerprint(tool, args):
            return "confirm_token was issued for different arguments"
        self._confirm_tokens.pop(token, None)  # single-use
        return None

    def _require_confirmation(
        self,
        name: str,
        args: dict[str, Any],
        confirmed: Any,
        token: Any,
        instruction: str,
    ) -> None:
        """Shared confirm gate. Accepts a truthy `confirmed` (user said yes
        before the call) or a bound `confirm_token` minted by an earlier
        denial. Otherwise raises PermissionError carrying a fresh token."""
        fp = _confirm_fingerprint(name, args)
        if _confirmed_truthy(confirmed):
            if token is not None:
                # Burn the token even though the flag sufficed — a token left
                # unconsumed here could otherwise be replayed later.
                self._consume_confirm_token(name, args, token)
            self._audit_confirmation("granted", name, fp, "confirmed")
            return
        rejected = ""
        if token is not None:
            reason = self._consume_confirm_token(name, args, token)
            if reason is None:
                self._audit_confirmation("consumed", name, fp, "token")
                return
            rejected = f" ({reason})"
            self._audit_confirmation("denied", name, fp, f"token:{reason}")
        else:
            self._audit_confirmation("denied", name, fp, "unconfirmed")
        fresh = self._mint_confirm_token(name, args)
        logger.warning("denied %s %r: confirmation required%s", name, args, rejected)
        raise PermissionError(
            f"{instruction}{rejected} A bound alternative: replay the same "
            f"call with confirm_token='{fresh}' "
            f"(single use, expires in {int(CONFIRM_TOKEN_TTL_S)}s)."
        )

    async def _check_control_allowed(
        self, name: str, args: dict[str, Any], confirmed: Any,
        confirm_token: Any = None,
    ) -> None:
        """Server-side gate for actuating tools. Raises PermissionError on denial."""
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("control tools are disabled (ADA_READ_ONLY=true)")
        now = time.monotonic()
        self._control_calls = [t for t in self._control_calls if now - t < CONTROL_RATE_WINDOW_S]
        if len(self._control_calls) >= CONTROL_MAX_GLOBAL:
            logger.warning("denied %s %r: global rate limit", name, args)
            raise PermissionError(
                f"rate limit exceeded: more than {CONTROL_MAX_GLOBAL} control calls in {int(CONTROL_RATE_WINDOW_S)}s"
            )
        entity_id = str(args.get("entity_id") or "")
        if entity_id:
            ident = self.policy_identity()
            if not self.banks.control_allowed(entity_id, ident):
                logger.warning(
                    "denied %s on %r for identity %r: control policy",
                    name, entity_id, ident,
                )
                raise PermissionError(
                    f"'{entity_id}' is outside this session's control policy"
                )
            calls = self._control_entity_calls.setdefault(entity_id, [])
            calls[:] = [t for t in calls if now - t < CONTROL_RATE_WINDOW_S]
            if len(calls) >= CONTROL_MAX_PER_ENTITY:
                logger.warning("denied %s %r: per-entity rate limit", name, args)
                raise PermissionError(
                    f"rate limit exceeded for {entity_id}: more than {CONTROL_MAX_PER_ENTITY} "
                    f"control calls in {int(CONTROL_RATE_WINDOW_S)}s"
                )
            if self.chaba is not None:
                # Guest mode has no MDDB safety map — deny anything that can
                # move or open: locks, covers, buttons, gates, garages, doors.
                if re.search(r"(?:lock|cover|button|gate|garage|door|siren|alarm)", entity_id):
                    logger.warning("denied %s on %r: chaba guest deny-pattern", name, entity_id)
                    raise PermissionError(
                        f"guests cannot control '{entity_id}' (locks/covers/gates are off-limits)"
                    )
            else:
                await self.memory._ensure_confidence()
            safety = self.memory._safety.get(entity_id) if self.memory is not None else None
            if safety != "dangerous" and self.memory is not None:
                # A related entity can inherit danger: button.gate_motor_my_position
                # physically jogs the dangerous cover.gate_motor.
                object_id = entity_id.split(".", 1)[-1]
                for eid, level in self.memory._safety.items():
                    if level == "dangerous" and object_id.startswith(eid.split(".", 1)[-1] + "_"):
                        safety = "dangerous"
                        break
            if safety == "dangerous":
                self._require_confirmation(
                    name, args, confirmed, confirm_token,
                    f"{entity_id} is marked dangerous. Call again with "
                    "confirmed=true only after explicit user confirmation.",
                )
            calls.append(now)
        self._control_calls.append(now)

    def _check_capture_confirmed(
        self, name: str, args: dict[str, Any], confirmed: Any,
    ) -> None:
        """Server-side gate for camera captures/casts (Tony's rule):
        starting a camera capture requires an explicit user yes."""
        gated = (
            (name == "cast_to_screen"
             and str(args.get("action") or "").lower() == "uplink")
            or (name == "cctv_wall"
                and str(args.get("action") or "").lower()
                not in {"stop", "settings", "status"})
            # cctv_snapshot's seat: gated only when the call puts the
            # frame on a display — a describe-only snapshot stays open.
            or (name == "ada_camera_snapshot"
                and (args.get("screen")
                     or str(args.get("target") or "").strip().lower()
                     in {"tv", "screen"}))
        )
        if gated and confirmed is not True and self._is_secondary_turn():
            logger.warning(
                "denied %s %r: camera capture by secondary speaker "
                "without confirmed=true", name, args)
            raise PermissionError(
                f"{name} starts a camera capture — a guest asked for it. "
                "Confirm with the session owner first, then call again "
                "with confirmed=true."
            )

    def _check_memory_write_allowed(
        self, name: str, args: dict[str, Any], confirmed: Any,
        confirm_token: Any = None,
    ) -> None:
        """Server-side gate for memory-bank writes. Raises PermissionError on denial."""
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("memory writes are disabled (ADA_READ_ONLY=true)")
        bank_name = str(args.get("bank") or "")
        try:
            bank = self.banks.bank(bank_name)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        if not self.banks.bank_allowed(bank.name, self.policy_identity()):
            logger.warning(
                "denied %s on %r for identity %r",
                name, bank.name, self.policy_identity(),
            )
            allowed = ", ".join(
                self.banks.banks_for_person(self.policy_identity())
            )
            raise PermissionError(
                f"memory bank '{bank.name}' is not available for this speaker"
                + (f" — allowed banks: {allowed}" if allowed else "")
            )
        if not bank.writable:
            logger.warning("denied %s on %r: bank not writable", name, bank_name)
            raise PermissionError(f"memory bank '{bank_name}' is read-only")
        # Bank configs may still name an absorbed tool (ada_outcome held
        # the outcome-write seat pre-merge) — resolve entries through
        # _ALIASES so the canonical tool inherits the same authorization.
        allowed_tools = {_ALIASES.get(t, t) for t in bank.allowed_tools}
        if name not in allowed_tools:
            logger.warning("denied %s on %r: tool not in allowed_tools", name, bank_name)
            raise PermissionError(f"tool {name} is not allowed on memory bank '{bank_name}'")
        if bank.write_policy == "confirmed":
            self._require_confirmation(
                name, args, confirmed, confirm_token,
                f"memory bank '{bank_name}' requires confirmation. Ask the user "
                "explicitly first, then call again with confirmed=true only "
                "after they say yes — do not write it to a different bank "
                "to get around the confirmation step.",
            )

    def _check_calendar_write_allowed(
        self, name: str, args: dict[str, Any], confirmed: Any,
        confirm_token: Any = None,
    ) -> None:
        """Server-side gate for calendar/task writes. Raises PermissionError on denial."""
        if name == "tasks":
            # Per-action split after the tools-merge-tasks-status collapse
            # — alias resolution ran before this gate, so name is
            # canonical: action='list' absorbed tasks_list, a free read
            # that was never confirm-gated (or READ_ONLY-blocked).
            if str(args.get("action") or "").strip().lower() == "list":
                return
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("calendar writes are disabled (ADA_READ_ONLY=true)")
        self._require_confirmation(
            name, args, confirmed, confirm_token,
            f"{name} requires confirmation. Restate the details to the user, "
            "get an explicit yes, then call again with confirmed=true.",
        )

    def _check_cms_write_allowed(
        self, name: str, args: dict[str, Any], confirmed: Any,
        confirm_token: Any = None,
    ) -> None:
        """Stateful gate for miniapp page writes. First attempt registers a
        pending confirmation; a resubmit with confirmed=true must match it —
        the model cannot jump straight to confirmed without the ask-step.
        Raises PermissionError on denial."""
        if name == "cms_edit":
            # Per-action split after the tools-merge-cms collapse — alias
            # resolution ran before this gate, so name is canonical:
            #   note     — absorbed cms_note_update: a merge-only timeline
            #              append that was never confirm-gated
            #   automate — absorbed cms_automation: op list/get read the
            #              registry, free like before; set/enable/disable/
            #              run fall through to confirmation
            #   delete   — absorbed cms_delete_page: confirmed
            eaction = str(args.get("action") or "").lower()
            if eaction == "note":
                return
            if (eaction == "automate" and str(args.get("op") or "").lower()
                    in CMS_AUTOMATION_READ_ACTIONS):
                return
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("CMS writes are disabled (ADA_READ_ONLY=true)")
        if name == "cms_publish_page":
            # Report meta contract (ssot.apps.ada-cms-reports.yml): reject
            # BEFORE the confirm handshake registers so the model fixes the
            # call instead of asking the user to approve a write that would
            # land without the fields reports-index/verify rely on.
            missing = missing_publish_fields(args)
            if missing:
                raise ValueError(
                    "cms_publish_page: report meta contract — missing or "
                    f"invalid: {', '.join(missing)}. Pass summary (one-line "
                    "brief, ~240 chars), domain (grouping tag), fresh_for "
                    "('30m'/'1h'/'6h'/'1d'), and confidence "
                    "(high|medium|low|unverified); updated and timeline are "
                    "stamped automatically.")
            # Normalize the slug before keying — the model may resubmit the
            # confirm call with different casing/spacing than the register
            # call, and a raw-args key would miss the pending request.
            try:
                slug = self._cms_slug(str(args.get("slug") or ""))
            except ValueError:
                # Register/deny must not raise on an invalid slug — the
                # publish path itself validates and rejects with ValueError.
                slug = str(args.get("slug") or "")
            # Keyed by slug:lang — confirming an EN publish does not unlock
            # a different-language variant of the same slug.
            pkey = f"{slug}:{args.get('lang') or 'en'}"
            pending = getattr(self, "_cms_pending", None)
            if pending is None:
                pending = self._cms_pending = {}
            if confirmed is True:
                if pending.get(pkey) is not None or _CALLER_VERIFIED_AFFIRM.get():
                    # A provider-verified user affirmation already covers the
                    # ask-step — the register round-trip is friction, not
                    # safety (2026-09-29: user said 'อนุมัติ', model called
                    # confirmed=true, denied 'no pending' twice).
                    pending.pop(pkey, None)
                    return
                logger.warning("denied %s %r: confirmed without pending request", name, args)
                raise PermissionError(
                    f"{name}: confirmed=true has no pending request for '{slug}'. "
                    "First call without confirmed to register the request, ask the "
                    "user to confirm, then resubmit with confirmed=true."
                )
            pending[pkey] = True
            logger.info("cms pending-confirm registered: %s", pkey)
            raise PermissionError(
                f"{name} requires confirmation — request registered. Now tell the "
                "user the page slug and title, ask for an explicit yes, then "
                "call again with the SAME args plus confirmed=true."
            )
        self._require_confirmation(
            name, args, confirmed, confirm_token,
            f"{name} requires confirmation. Restate the page slug, title, and "
            "what will change, get an explicit yes, then call again with confirmed=true.",
        )

    def _check_dynamic_allowed(
        self, name: str, spec: Any, args: dict[str, Any],
        confirmed: Any, confirm_token: Any = None,
    ) -> None:
        """Policy gate for tools.d drop-in tools. Raises PermissionError."""
        if spec.policy == "owner_only":
            ident = self.policy_identity()
            policy = self.banks.policy_for(ident) or {}
            if not policy.get("full"):
                logger.warning("denied %s for identity %r: owner_only tool",
                               name, ident)
                raise PermissionError(
                    f"{name} is restricted to the owner's identities")
        if spec.policy == "confirmed":
            self._require_confirmation(
                name, args, confirmed, confirm_token,
                f"{name} requires confirmation. Describe what it will do, "
                "get an explicit yes, then call again with confirmed=true.",
            )

    def _check_devin_confirmed(
        self, name: str, args: dict[str, Any], confirmed: Any,
        confirm_token: Any = None,
    ) -> None:
        """Server-side gate for devin session control (the canonical
        `devin` tool — dispatch/followup/answer). PermissionError on denial."""
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("devin tools are disabled (ADA_READ_ONLY=true)")
        self._require_confirmation(
            name, args, confirmed, confirm_token,
            f"{name} requires confirmation. Restate the repo, task, and "
            "that an unattended Devin session will make code changes, get an "
            "explicit yes, then call again with confirmed=true.",
        )

    # -- Devin dispatch tools (headless sessions on tony-dell; job SSOT:
    #    docs/ssot/jobs/ada/2026-09-22-ada-devin-dispatch.yml) --

    async def devin(
        self,
        action: str,
        repo: str = "",
        task: str = "",
        task_id: str = "",
        message: str = "",
    ) -> Any:
        """Devin session control — tools-merge-devin-mcp consolidated
        devin_dispatch / devin_followup / devin_answer. Every action
        mutates a dispatched session — the DEVIN_CONFIRMED_TOOLS seat
        keeps confirmed=true mandatory."""
        action = (action or "").strip().lower()
        if action == "dispatch":
            if not repo or not task:
                raise ValueError("dispatch needs repo and task")
            return await self.devin_dispatch(repo=repo, task=task)
        if action in ("followup", "answer"):
            if not task_id or not message:
                raise ValueError(f"{action} needs task_id and message")
        if action == "followup":
            return await self.devin_followup(task_id=task_id, message=message)
        if action == "answer":
            return await self.devin_answer(task_id=task_id, message=message)
        raise ValueError(
            f"invalid action {action!r}: expected dispatch|followup|answer")

    async def devin_read(
        self,
        action: str,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 30,
        publish: bool = False,
        confirmed: bool = False,
        confirm_token: str | None = None,
        request: str = "",
        title: str = "",
    ) -> Any:
        """Devin job ledger reads — tools-merge-devin-mcp consolidated
        devin_status / devin_jobs / devin_pending / devin_job_report,
        plus the absorbed ada_devteam_review (action='review',
        owner-only). 'report' with publish=true re-checks confirmation
        inside devin_job_report — confirmed/confirm_token are declared
        here so the execute gate hands them back."""
        action = (action or "").strip().lower()
        if action == "status":
            return await self.devin_status(task_id=task_id or None)
        if action == "jobs":
            return await self.devin_jobs(status=status, limit=limit)
        if action == "pending":
            return await self.devin_pending()
        if action == "report":
            return await self.devin_job_report(
                publish=publish, confirmed=confirmed,
                confirm_token=confirm_token, limit=limit)
        if action == "review":
            # The tools.d manifest carried timeout_s=300 — the expert
            # panel runs multiple model calls; keep the cap now that the
            # drop-in wrapper is gone.
            return await asyncio.wait_for(
                self.ada_devteam_review(request=request, title=title),
                timeout=300)
        raise ValueError(
            f"invalid action {action!r}: "
            "expected status|jobs|pending|report|review")

    async def devin_dispatch(
        self, repo: str, task: str | None = None,
        playbook: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start an unattended Devin session on tony-dell for a task.

        Runs in a dedicated git worktree as a systemd unit; completion is
        reported back via chaba-admin event + iPhone notification. With
        playbook=<name> (registry in backend/playbooks/) the task renders
        from the playbook's template and its gate is enforced — e.g.
        build-tool refuses unless params.spec_key names a spec-review doc
        whose status is pass.
        """
        caller = self.policy_identity()
        return await devin_dispatch_mod.dispatch(
            repo, task, playbook=playbook, params=params,
            mddb=self.mddb, caller=caller,
            is_owner=self._persona_admin(caller))

    async def devin_status(self, task_id: str | None = None) -> str:
        """List dispatched tasks, or show one task's unit state."""
        return await devin_dispatch_mod.status(task_id or None)

    async def devin_followup(self, task_id: str, message: str) -> str:
        """Send a follow-up message into a dispatched session."""
        return await devin_dispatch_mod.followup(task_id, message)

    @staticmethod
    def _job_detail(doc: dict[str, Any]) -> str:
        """One-line detail for a job doc: first body line after the standard
        'Job <id> on <host>: <status>.' preamble, truncated for the report."""
        for line in (doc.get("contentMd") or "").splitlines():
            line = line.strip()
            if not line or line.startswith("Job "):
                continue
            return line[:160]
        return ""

    def _classify_job(
        self, doc: dict[str, Any], dispatch: dict[str, dict[str, str]],
    ) -> tuple[str, dict[str, Any]]:
        """Bucket a job/<id> doc as active|failed|done|stale.

        The ledger is the cross-host source of truth; for tasks on this
        dispatch host we verify a 'running' ledger entry against
        `devin-dispatch status` — a dead unit that never wrote a transcript
        is a spawn failure (the ledger only converges once the watch timer
        notices, and pre-exit_code spawn failures it never did).
        """
        meta = doc.get("meta") or {}
        job_id = (_first(meta.get("job_id"))
                  or (doc.get("key") or "").split("/", 1)[-1])
        ledger = (_first(meta.get("status")) or "").lower()
        job = {
            "task_id": job_id,
            "host": _first(meta.get("host")),
            "status": ledger or "unknown",
            "ts": _first(meta.get("ts")),
            "question": _first(meta.get("question")),
            "detail": self._job_detail(doc),
        }
        if ledger in _JOB_STALE_STATUSES:
            return "stale", job
        if ledger in _JOB_DONE_STATUSES:
            return "done", job
        if ledger == "failed":
            return "failed", job
        rec = dispatch.get(job_id)
        if rec:
            state = (rec.get("state") or "").lower()
            result = (rec.get("result") or "").lower()
            transcript = (rec.get("transcript") or "").strip()
            no_transcript = transcript in {"", "never"}
            if state == "failed" or result.startswith("exit"):
                job["detail"] = job["detail"] or (
                    f"unit {state or 'dead'} (result {result or 'unknown'})"
                )
                return "failed", job
            if no_transcript and (
                state in {"inactive", "dead"}
                or (state == "gone" and self._job_age_s(job) > _JOB_GONE_GRACE_S)
            ):
                job["detail"] = job["detail"] or (
                    "died at spawn — unit is down and no transcript was written"
                )
                return "failed", job
            if state == "inactive" and not no_transcript:
                job["detail"] = "finished per dispatch status — ledger update pending"
                return "done", job
        return "active", job

    @staticmethod
    def _job_age_s(job: dict[str, Any]) -> float:
        """Seconds since the ledger doc's ts; unknown ts counts as old so a
        stale 'running' marker can't hide a spawn failure forever."""
        ts = job.get("ts")
        if not ts:
            return _JOB_GONE_GRACE_S + 1
        try:
            started = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - started).total_seconds()
        except ValueError:
            return _JOB_GONE_GRACE_S + 1

    async def devin_job_report(
        self, publish: bool = False, confirmed: bool = False,
        confirm_token: str | None = None, limit: int = 60,
    ) -> dict[str, Any]:
        """Compose the Devin job report page content.

        Reads the job/<id> ledger and verifies entries against
        `devin-dispatch status`, so failed jobs — including spawn failures
        the ledger still marks running — land in a dedicated 'Failed jobs'
        section instead of the active list. publish=true writes the page
        (CMS write: requires confirmed=true).
        """
        if self.mddb is None:
            return {"status": "error", "error": "mddb unavailable (guest mode)"}
        docs = await self.mddb.search_documents(
            DEVIN_JOBS_COLLECTION, filter_meta={"kind": ["job"]}, limit=limit)
        dispatch: dict[str, dict[str, str]] = {}
        try:
            dispatch = {
                r["task_id"]: r for r in await devin_dispatch_mod.tasks()
            }
        except Exception as exc:
            logger.info("devin_job_report: dispatch status unavailable: %s", exc)
        buckets: dict[str, list[dict[str, Any]]] = {
            "active": [], "failed": [], "done": [], "stale": [],
        }
        for doc in docs:
            bucket, job = self._classify_job(doc, dispatch)
            buckets[bucket].append(job)
        for jobs in buckets.values():
            jobs.sort(key=lambda j: j.get("ts") or "", reverse=True)

        updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines = [
            "# Devin Job Report", "",
            f"*Updated {updated} — composed from the devin-handoff job ledger.*",
            "",
        ]

        def _item(j: dict[str, Any], bucket: str) -> str:
            host = f" ({j['host']})" if j.get("host") else ""
            detail = j.get("detail") or ""
            if j.get("question"):
                note = f"**needs input**: {j['question']}"
                if detail.lower().startswith("needs input"):
                    detail = ""
            elif bucket == "stale":
                # One status label is enough — job bodies carry long
                # transcript tails that just bury the section.
                note, detail = f"{j['status']}", ""
            elif j["status"] in {"running", "failed", "done"}:
                note = ""
            else:
                note = f"{j['status']}"
            tail = " — ".join(p for p in (note, detail) if p)
            return f"- `{j['task_id']}`{host}" + (f" — {tail}" if tail else "")

        for heading, key in (
            ("Active jobs", "active"), ("Failed jobs", "failed"),
            ("Done", "done"), ("Stale / superseded", "stale"),
        ):
            jobs = buckets[key]
            lines.append(f"## {heading} — {len(jobs)}")
            lines.append("")
            if jobs:
                lines.extend(_item(j, key) for j in jobs)
            else:
                lines.append("_None._")
            lines.append("")
        markdown = "\n".join(lines)

        result: dict[str, Any] = {
            "status": "composed",
            "slug": DEVIN_JOB_REPORT_SLUG,
            "counts": {k: len(v) for k, v in buckets.items()},
            "jobs": buckets,
            "markdown": markdown,
            "note": (
                "Publish via cms_publish_page (slug 'devin-job-report') or "
                "call again with publish=true and confirmed=true."
            ),
        }
        if publish:
            self._check_cms_write_allowed(
                "devin_read", {"slug": DEVIN_JOB_REPORT_SLUG},
                confirmed, confirm_token)
            counts = result["counts"]
            pub = await self.cms_publish_page(
                DEVIN_JOB_REPORT_SLUG, "Devin Job Report", markdown,
                summary=(f"{counts['active']} active, {counts['failed']} "
                         f"failed, {counts['done']} done, "
                         f"{counts['stale']} stale — Devin dispatch job "
                         "ledger"),
                domain="devin", fresh_for="1h", confidence="high")
            result["publish"] = pub
            result["status"] = pub.get("status", "error")
        return result

    async def devin_pending(self) -> list[dict[str, Any]]:
        """Dispatched jobs blocked waiting for a user answer.

        Reads job/<id> docs (kind=job, status=awaiting-user) from the
        devin-handoff collection — the same ledger the watch timer, job-run
        wrapper, and Report tab dispatch layer use.
        """
        if self.mddb is None:
            return []
        docs = await self.mddb.search_documents(
            DEVIN_JOBS_COLLECTION,
            filter_meta={"kind": ["job"], "status": ["awaiting-user"]},
            limit=20,
        )
        out = []
        for d in docs:
            meta = d.get("meta") or {}
            out.append({
                "task_id": _first(meta.get("job_id"))
                           or (d.get("key") or "").split("/", 1)[-1],
                "question": _first(meta.get("question")) or "",
                "host": _first(meta.get("host")),
                "ts": _first(meta.get("ts")),
                "detail": (d.get("contentMd") or "")[:600],
            })
        out.sort(key=lambda j: j.get("ts") or "", reverse=True)
        return out

    async def devin_jobs(self, status: str | None = None,
                         limit: int = 30) -> list[dict[str, Any]]:
        """Dispatch ledger: all job docs (running/done/failed/awaiting-user)
        — the summary path. devin_pending is awaiting-user only; use this
        for "what's the status of my tasks" questions."""
        if self.mddb is None:
            return []
        fm: dict[str, Any] = {"kind": ["job"]}
        if status:
            fm["status"] = [status]
        docs = await self.mddb.search_documents(
            DEVIN_JOBS_COLLECTION, filter_meta=fm, limit=min(limit, 50))
        out = []
        for d in docs:
            meta = d.get("meta") or {}
            out.append({
                "task_id": _first(meta.get("job_id"))
                           or (d.get("key") or "").split("/", 1)[-1],
                "status": _first(meta.get("status")),
                "host": _first(meta.get("host")),
                "ts": _first(meta.get("ts")),
                "question": _first(meta.get("question")) or "",
            })
        out.sort(key=lambda j: j.get("ts") or "", reverse=True)
        return out

    async def devin_answer(self, task_id: str, message: str) -> dict[str, Any]:
        """Deliver the user's refined answer to a blocked job.

        Writes answer/<task_id> to the ledger, flips job/<task_id> to
        answered, and for devin-dispatch task ids also injects the message
        straight into the session via devin_followup.
        """
        if self.mddb is None:
            raise PermissionError("devin_answer needs MDDB (unavailable in guest mode)")
        now = datetime.now(timezone.utc).isoformat()
        delivered = False
        via = "mailbox"
        # Devin-dispatch task ids (YYYYMMDD-HHMMSS-slug) resume in-place.
        if re.match(r"^\d{8}-\d{6}-[a-z0-9-]+$", task_id):
            res = await devin_dispatch_mod.followup(task_id, message)
            delivered = True
            via = "followup"
            logger.info("devin_answer: followup to %s -> %s", task_id, res)
        wrote = await self.mddb.add_document(
            DEVIN_JOBS_COLLECTION,
            key=f"answer/{task_id}",
            lang="en",
            content_md=f"Answer for job {task_id} ({now}):\n\n{message}",
            meta={
                "kind": ["job-answer"], "status": ["answered"],
                "job_id": [task_id], "ts": [now],
                "subject": [f"answer-{task_id}"],
                "source": ["voice"], "written_by": ["ada"],
                "scope": ["tony"], "bank": ["devin-handoff"],
            },
            durable=True, tool="devin_answer", session_id=self.session_id,
        )
        queued = write_outbox.is_queued(wrote)
        if queued:
            self._log_session_event(
                "write_queued", tool="devin_answer",
                key=f"answer/{task_id}")
        job = await self.mddb.get_document(DEVIN_JOBS_COLLECTION, f"job/{task_id}")
        if job:
            meta = dict(job.get("meta") or {})
            meta["status"] = ["answered"]
            meta["answered_at"] = [now]
            await self.mddb.update_document(
                DEVIN_JOBS_COLLECTION, f"job/{task_id}", meta=meta,
                durable=True, tool="devin_answer",
                session_id=self.session_id)
        elif queued:
            # Same outage took the read too — queue the status flip so the
            # replay re-reads and merges once mddb is back.
            write_outbox.enqueue(
                mddb=self.mddb, op="update", collection=DEVIN_JOBS_COLLECTION,
                key=f"job/{task_id}", lang="en",
                meta={"status": ["answered"], "answered_at": [now]},
                tool="devin_answer", session_id=self.session_id)
        note = ("Answer recorded" +
                (" and sent to the running session." if delivered
                 else " — the dispatcher picks it up."))
        if queued:
            # mddb is unreachable — the write is parked in the local outbox.
            # Say it plainly: NOT yet in the ledger, will land on retry.
            note = ("mddb is unreachable — the answer is queued in the local "
                    "write outbox and will land automatically within the "
                    "retry window" +
                    ("; it was already sent to the running session."
                     if delivered else "."))
        return {"task_id": task_id, "delivered": delivered, "via": via,
                "queued_for_retry": queued, "note": note}

    # Doc actions that never wrote state before the merge —
    # ada_doc_search/ada_doc_get were free reads.
    _DOC_READ_ACTIONS = frozenset({"search", "get"})
    async def ada_devteam_review(
        self, request: str, title: str = "",
    ) -> dict[str, Any]:
        """Dev-team spec review — the absorbed tools.d drop-in
        (tools-merge-devin-mcp). Ada describes a tool she wants; the
        devteam pipeline drafts a spec, runs the parallel expert panel
        (security/privacy, standards, QA, scope), revises it, and files
        the reviewed spec into the devin-handoff bank.

        Owner-only: the manifest's owner_only policy + secondary_allowed
        =false gates live here now that the drop-in wrapper is gone."""
        if self._is_secondary_turn():
            raise PermissionError(
                "dev-team reviews run on the owner's turn only")
        ident = self.policy_identity()
        policy = self.banks.policy_for(ident) or {}
        if not policy.get("full"):
            logger.warning(
                "denied devin_read review for identity %r: owner_only", ident)
            raise PermissionError(
                "devin_read action='review' is restricted to the owner's "
                "identities")
        request = (request or "").strip()
        if not request:
            return {"ok": False, "error": "request is required"}
        if self.mddb is None:
            return {"ok": False, "error": "mddb unavailable (guest mode)"}
        from backend import devteam
        result = await devteam.review(request)

        panel = result["panel"]
        lines = [
            f"# Dev-team review: {(title or request)[:80]}",
            "",
            f"- verdict: **{result['verdict']}**",
            f"- model: {result['model']}",
            f"- reviewed: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "",
            "## Panel",
        ]
        lines += [
            f"- **{r['role']}** — {r['verdict']}: {r['summary']}" +
            (f" ({'; '.join(r['comments'])})" if r["comments"] else "")
            for r in panel
        ]
        lines += ["", "## Revised spec", "", result["spec"]]

        now = datetime.now(timezone.utc)
        parts = re.findall(r"[a-z0-9]+", request.lower())[:5]
        slug = "-".join(parts) or "untitled"
        key = f"spec/{now.strftime('%Y%m%d-%H%M%S')}-{slug}"
        await self.mddb.add_document(
            DEVIN_JOBS_COLLECTION,
            key=key, lang="en", content_md="\n".join(lines),
            meta={
                "kind": ["spec-review"], "status": [result["verdict"]],
                "ts": [now.isoformat()], "source": ["voice"],
                "written_by": ["ada"], "scope": ["tony"],
                "bank": ["devin-handoff"],
                "subject": ["-".join(
                    re.findall(r"[a-z0-9]+", request.lower())[:8])
                    or "untitled"],
            },
        )
        return {
            "ok": True,
            "verdict": result["verdict"],
            "spec_key": key,
            "panel": {r["role"]: r["verdict"] for r in panel},
            "summary": " | ".join(
                f"{r['role']}: {r['summary']}" for r in panel)[:600],
        }

    def _check_doc_confirmed(
        self, name: str, args: dict[str, Any], confirmed: Any,
        confirm_token: Any = None,
    ) -> None:
        """Server-side gate for doc archive/print. Raises PermissionError on denial."""
        if name == "docs":
            # Per-action split after the tools-merge-docs-drive collapse —
            # alias resolution ran before this gate, so name is canonical:
            # search/get stay free reads; archive, print, and anything
            # unrecognized fall through to confirmation.
            if str(args.get("action") or "").lower() in self._DOC_READ_ACTIONS:
                return
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("document tools are disabled (ADA_READ_ONLY=true)")
        self._require_confirmation(
            name, args, confirmed, confirm_token,
            f"{name} requires confirmation. Restate the archive/print "
            "target, get an explicit yes, then call again with confirmed=true.",
        )

    def _check_drive_confirmed(
        self, name: str, args: dict[str, Any], confirmed: Any,
        confirm_token: Any = None,
    ) -> None:
        """Server-side gate for drive update. Raises PermissionError on denial."""
        if name == "drive":
            # Per-action split after the tools-merge-docs-drive collapse:
            # only action='update' mutated before the merge — search/get/
            # show were free (show's cast runs inside cast_to_screen/
            # tv_action, which carry their own gates).
            if str(args.get("action") or "").lower() in (
                    "search", "get", "show"):
                return
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("drive tools are disabled (ADA_READ_ONLY=true)")
        self._require_confirmation(
            name, args, confirmed, confirm_token,
            f"{name} modifies a Drive file. Restate the file "
            "and change, get an explicit yes, then call again "
            "with confirmed=true.",
        )

    # -- Document archive tools (doc-archive service on idc01 + MDDB
    #    `documents` collection — the shared Ada/Devin document index) --

    async def docs(
        self, action: str, query: str = "", slug: str = "",
        limit: int = 5, doc_type: str = "document",
        intake_key: str | None = None, intake_keys: list[str] | None = None,
        source_dir: str | None = None, pages: str | None = None,
        true_size_mm: str | None = None,
    ) -> Any:
        """Personal document archive surface (tools-merge-docs-drive):
        action='search'|'get' read the archive index (absorbed
        ada_doc_search/ada_doc_get); 'archive'|'print' write — the
        confirm gate runs in _execute_gated before dispatch."""
        action = (action or "").strip().lower()
        if action == "search":
            return await self.ada_doc_search(query, limit=limit)
        if action == "get":
            return await self.ada_doc_get(slug)
        if action == "archive":
            return await self.ada_doc_archive(
                slug, doc_type=doc_type, intake_key=intake_key,
                intake_keys=intake_keys, source_dir=source_dir)
        if action == "print":
            return await self.ada_doc_print(
                slug, pages=pages, true_size_mm=true_size_mm)
        raise ValueError(
            f"invalid action {action!r}: expected search|get|print|archive")

    def _doc_record(self, action: str, **fields: Any) -> None:
        """Append to the session's L0 doc-work timeline (conversation
        report substrate); no-op when no conversation is attached."""
        if self.doc_log is None:
            return
        self.doc_log.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action, **fields,
        })

    async def ada_doc_search(self, query: str, limit: int = 5) -> Any:
        """Search archived document sets (documents bank / MDDB index)."""
        hits = await doc_archive_client.doc_search(query, limit=limit)
        self._doc_record("search", query=query,
                         found=[h.get("slug") for h in hits])
        return hits

    async def ada_doc_get(self, slug: str) -> dict[str, Any]:
        """Manifest + index metadata for one archived document set."""
        out = await doc_archive_client.doc_get(slug)
        self._doc_record("get", slug=slug)
        return out

    async def ada_doc_archive(
        self, slug: str, doc_type: str = "document",
        intake_key: str | None = None,
        intake_keys: list[str] | None = None,
        source_dir: str | None = None,
    ) -> dict[str, Any]:
        """Archive a document set: from held intake results (intake_key or
        intake_keys for multi-page sets) or a directory (source_dir)."""
        pages: list[tuple[str, bytes]] = []
        keys = list(intake_keys or [])
        if intake_key:
            keys.insert(0, intake_key)
        if keys:
            from backend import document_check
            engine = document_check.engine()
            for i, k in enumerate(keys, 1):
                held = engine.held(k)
                if held is None:
                    raise RuntimeError(
                        f"unknown or expired intake key '{k}' — "
                        "upload the document again")
                blob = (held.meta or {}).get("archive_jpg")
                if not blob:
                    raise RuntimeError(f"held intake {k} has no archive image")
                name = (held.meta or {}).get("filename") or f"{slug}-p{i}.jpg"
                if len(keys) > 1:
                    stem, dot, ext = name.rpartition(".")
                    name = f"{stem or name}-p{i}.{ext or 'jpg'}"
                pages.append((name, bytes(blob)))
        elif source_dir:
            d = os.path.expanduser(source_dir)
            if not os.path.isdir(d):
                raise RuntimeError(f"no such directory: {source_dir}")
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith((".jpg", ".jpeg", ".png")):
                    with open(os.path.join(d, fn), "rb") as fh:
                        pages.append((fn, fh.read()))
            if not pages:
                raise RuntimeError(f"no images found in {source_dir}")
        else:
            raise RuntimeError("need intake_key or source_dir")
        out = await doc_archive_client.doc_archive(slug, doc_type, pages)
        self._doc_record("archive", slug=slug, doc_type=doc_type,
                         pages=len(pages), keys=keys,
                         duplicates=(out.get("duplicates") or
                                     out.get("dupe_pages")))
        return out

    async def ada_doc_print(
        self, slug: str, pages: str | None = None,
        true_size_mm: str | None = None,
    ) -> dict[str, Any]:
        """Print archived pages on the DeskJet via tony-dell CUPS.
        pages: 'all' | '1-3' | '2'; true_size_mm: '85.6x54' for ID-1."""
        ts = None
        if true_size_mm:
            try:
                a, b = str(true_size_mm).lower().split("x", 1)
                ts = (float(a), float(b))
            except ValueError as exc:
                raise RuntimeError(
                    f"bad true_size_mm '{true_size_mm}' — use '85.6x54'") from exc
        out = await doc_archive_client.doc_print_pdf(slug, pages, ts)
        self._doc_record("print", slug=slug, pages=out.get("pages"),
                         queue=out.get("queue"))
        return out

    # -- Google Drive / Photos tools (doc-archive /v1/drive + /v1/photos) --

    async def drive(
        self, action: str, query: str = "", mime: str | None = None,
        limit: int = 10, file_id: str = "", content: str = "",
        screen: int = 0, target: str = "screen",
    ) -> Any:
        """Google Drive surface (tools-merge-docs-drive): action=
        'search'|'get'|'show' read (absorbed drive_search/drive_get/
        drive_show); 'update' rewrites a file's content — the confirm
        gate runs in _execute_gated before dispatch."""
        action = (action or "").strip().lower()
        if action == "search":
            return await self.drive_search(query, mime=mime, limit=limit)
        if action == "get":
            return await self.drive_get(file_id)
        if action == "show":
            return await self.drive_show(
                file_id, screen=screen, target=target)
        if action == "update":
            return await self.drive_update(file_id, content)
        raise ValueError(
            f"invalid action {action!r}: expected search|show|get|update")

    async def drive_search(self, query: str, mime: str | None = None,
                           limit: int = 10) -> Any:
        """Search the whole Drive by name or content. mime narrows it:
        'image/', 'video/', 'application/pdf', 'text/'."""
        files = await doc_archive_client.drive_search(
            query, mime=mime, limit=limit)
        return {"files": files, "count": len(files)}

    async def drive_get(self, file_id: str) -> dict[str, Any]:
        """Read one Drive file by id (from drive_search). Text and Google
        docs come back inline; binary types return a castable media_url."""
        return await doc_archive_client.drive_get(file_id)

    async def drive_update(self, file_id: str, content: str,
                           confirmed: bool = False) -> dict[str, Any]:
        """Replace a regular Drive file's content in place (text/md/json/
        csv). Google-native docs can't be media-updated — the service
        returns 400 with the export/re-upload guidance."""
        out = await doc_archive_client.drive_update(file_id, content)
        self._doc_record("drive-update", file_id=file_id)
        return out

    async def drive_show(self, file_id: str, screen: int = 0,
                         target: str = "screen") -> dict[str, Any]:
        """Show a Drive photo/video/file on a vcast screen (default) or the
        TV (target='tv'). Picks image/play/nav from the file's mimeType —
        the media URL is minted server-side so the display needs no auth."""
        info = await doc_archive_client.drive_get(file_id)
        mime = str(info.get("mimeType") or "")
        media_url = info.get("media_url")
        if media_url:
            url = doc_archive_client.DOC_ARCHIVE_URL + str(media_url)
        else:
            # text/inline types: mint a media URL anyway so the display
            # can fetch the raw file
            url = await doc_archive_client.drive_media_url(file_id)
        if str(target or "screen").lower() == "tv":
            out = await self.tv_action(cmd="nav", text=url)
            if isinstance(out, dict):
                out.update({"name": info.get("name"), "mimeType": mime})
            return out
        if mime.startswith("video/") or mime.startswith("audio/"):
            action = "play"
        elif mime.startswith("image/"):
            action = "image"
        else:
            action = "nav"
        n = int(screen or 1)
        out = await self.cast_to_screen(screen=n, action=action, url=url)
        if isinstance(out, dict):
            out.update({"name": info.get("name"), "mimeType": mime})
        return out

    async def photos_pick(self) -> dict[str, Any]:
        """Start a Google Photos picker session — returns a picker_uri the
        user opens on their signed-in phone/browser to choose items, plus a
        session_id to pass to photos_picked. Google's Library API was
        limited to app-created data in 2025, so picking is the only way to
        reach library photos."""
        out = await doc_archive_client.photos_picker_create()
        out["note"] = ("Send the user picker_uri — it must be opened where "
                       "their Google account is signed in (phone/laptop). "
                       "Then call chat_send photo='picked' with session_id.")
        return out

    async def photos_picked(self, session_id: str, screen: int = 0,
                            show: bool = True) -> dict[str, Any]:
        """Poll a picker session. When the user has picked items, returns
        them and (show=true) casts the first item to the screen — images
        as image, videos as play, using the picker baseUrl directly."""
        out = await doc_archive_client.photos_picker_poll(session_id)
        if not out.get("picked"):
            return out
        items = out.get("items") or []
        if items and show:
            first = items[0]
            base = str(first.get("baseUrl") or "")
            if base:
                n = int(screen or 1)
                mime = str(first.get("mimeType") or "")
                action = ("play" if mime.startswith("video/")
                          else "image")
                # baseUrl modifiers: =dv fetches playable video bytes,
                # =w<N> resizes images for a screen.
                url = base + ("=dv" if mime.startswith("video/") else "=w2048")
                cast = await self.cast_to_screen(
                    screen=n, action=action, url=url)
                out["cast"] = cast
                out["shown"] = first.get("filename") or first.get("id")
        return out

    # -- Token usage reporting (usage_tracker.py) --

    async def ada_usage_summary(self, source: str = "all", reset: bool = False) -> dict[str, Any]:
        report = usage_ledger.snapshot(
            None if str(source).lower() in ("", "all") else str(source).lower()
        )
        if reset:
            usage_ledger.reset()
            report["reset"] = True
        return report

    # -- Summary rollup tools (backend/summary_rollups.py): daily digests
    #    and a weekly comparison over the per-session summaries in the
    #    recall-summary collection. Read-only against user-facing state —
    #    rollup writes land in the internal summary collection like the
    #    session summaries themselves, so no confirmed= gate.

    async def ada_daily_summary(self, day: str = "today", refresh: bool = False) -> dict[str, Any]:
        """Digest of all sessions on one day ('today'|'yesterday'|YYYY-MM-DD)."""
        return await summary_rollups.daily_summary(
            self.mddb, day=str(day), refresh=bool(refresh))

    async def ada_weekly_comparison(
        self, end: str = "today", days: int = 7, refresh: bool = False,
    ) -> dict[str, Any]:
        """Compare the daily digests of the last `days` days — weekly trends."""
        return await summary_rollups.weekly_comparison(
            self.mddb, end=str(end), days=int(days), refresh=bool(refresh))

    # -- Calendar / tasks tools (provider-agnostic; see ssot.apps.ada-calendar.yml) --

    @property
    def calendar(self) -> CalendarService | None:
        """Rendered provider registry, loaded on first use. None = not configured."""
        if not self._calendar_loaded:
            self._calendar_loaded = True
            try:
                self._calendar = CalendarService.load(instance=self._instance_id)
            except Exception as exc:
                logger.warning("calendar registry load failed: %s", exc)
                self._calendar = None
        return self._calendar

    def _calendar_svc(self) -> CalendarService:
        svc = self.calendar
        if svc is None:
            raise RuntimeError(
                "calendar is not configured on this instance — render "
                "ssot.apps.ada-calendar.yml to ~/.config/ada/calendar.json"
            )
        return svc

    async def calendar_read(
        self,
        action: str = "events",
        day: str = "today",
        days: int = 1,
        query: str | None = None,
        calendar: str | None = None,
    ) -> dict[str, Any]:
        """Calendar reads — tools-merge-calendar-plan consolidated
        calendar_list_events / calendar_list_calendars / calendar_freebusy.
        Free reads — no confirmation."""
        action = (action or "").strip().lower()
        if action == "calendars":
            return await self.calendar_list_calendars()
        if action == "events":
            return await self.calendar_list_events(
                day=day, days=days, query=query, calendar=calendar)
        if action == "freebusy":
            return await self.calendar_freebusy(day=day, days=days)
        raise ValueError(
            f"invalid action {action!r}: expected events|calendars|freebusy")

    async def calendar_write(
        self,
        action: str,
        title: str = "",
        start: str = "",
        end: str = "",
        notes: str | None = None,
        location: str | None = None,
        calendar: str | None = None,
        event_id: str = "",
        to: str = "tomorrow",
    ) -> Any:
        """Calendar writes — tools-merge-calendar-plan consolidated
        calendar_create_event / calendar_delete_event /
        calendar_shift_overdue. Every action mutates the real calendar —
        the CALENDAR_WRITE_TOOLS seat keeps confirmed=true mandatory."""
        action = (action or "").strip().lower()
        if action == "create":
            return await self.calendar_create_event(
                title=title, start=start, end=end, notes=notes,
                location=location, calendar=calendar)
        if action == "delete":
            return await self.calendar_delete_event(event_id=event_id)
        if action == "shift":
            return await self.calendar_shift_overdue(to=to)
        raise ValueError(
            f"invalid action {action!r}: expected create|delete|shift")

    async def calendar_list_calendars(self) -> dict[str, Any]:
        return await self._calendar_svc().list_calendars()

    async def calendar_list_events(
        self,
        day: str = "today",
        days: int = 1,
        query: str | None = None,
        calendar: str | None = None,
    ) -> dict[str, Any]:
        return await self._calendar_svc().list_events(
            day=str(day), days=int(days),
            query=str(query) if query else None,
            calendar=str(calendar) if calendar else None,
        )

    async def calendar_create_event(
        self,
        title: str,
        start: str,
        end: str,
        notes: str | None = None,
        location: str | None = None,
        calendar: str | None = None,
    ) -> dict[str, Any]:
        return await self._calendar_svc().create_event(
            str(title), str(start), str(end),
            calendar=str(calendar) if calendar else None,
            notes=str(notes) if notes else None,
            location=str(location) if location else None,
        )

    async def calendar_delete_event(self, event_id: str) -> str:
        return await self._calendar_svc().delete_event(str(event_id))

    async def calendar_freebusy(self, day: str = "today", days: int = 1) -> dict[str, Any]:
        return await self._calendar_svc().freebusy(day=str(day), days=int(days))

    async def tasks(
        self,
        action: str = "list",
        title: str = "",
        task_id: str = "",
        due: str | None = None,
        notes: str | None = None,
        task_list: str | None = None,
    ) -> Any:
        """Task list management — tools-merge-tasks-status consolidated
        tasks_list / tasks_add / tasks_complete / tasks_move into one
        action= tool. 'list' is a free read; 'add'/'done'/'move' mutate —
        the CALENDAR_WRITE_TOOLS seat keeps confirmed=true mandatory for
        them (per-action split in _check_calendar_write_allowed)."""
        action = (action or "list").strip().lower()
        if action == "list":
            return await self.tasks_list(task_list)
        if action == "add":
            return await self.tasks_add(
                str(title), due=due, notes=notes, task_list=task_list)
        if action == "done":
            return await self.tasks_complete(str(task_id))
        if action == "move":
            return await self.tasks_move(str(task_id), str(due or ""))
        raise ValueError(
            f"invalid action {action!r}: expected add|list|done|move")

    async def tasks_list(self, task_list: str | None = None) -> dict[str, Any]:
        return await self._calendar_svc().list_tasks(
            task_list=str(task_list) if task_list else None
        )

    async def tasks_add(
        self,
        title: str,
        due: str | None = None,
        notes: str | None = None,
        task_list: str | None = None,
    ) -> dict[str, Any]:
        return await self._calendar_svc().add_task(
            str(title),
            due=str(due) if due else None,
            notes=str(notes) if notes else None,
            task_list=str(task_list) if task_list else None,
        )

    async def tasks_complete(self, task_id: str) -> str:
        return await self._calendar_svc().complete_task(str(task_id))

    async def tasks_move(self, task_id: str, due: str) -> dict[str, Any]:
        """Reschedule one task's due date — same task, new date."""
        return await self._calendar_svc().move_task(
            str(task_id), str(due))

    async def calendar_shift_overdue(self, to: str = "tomorrow") -> dict[str, Any]:
        """Move every overdue task + already-ended event to a new day.
        Returns per-item old->new so Ada can report exactly what moved."""
        return await self._calendar_svc().shift_overdue(to=str(to))

    async def plan_day(
        self,
        period: str = "today",
        day: str | None = None,
        days: int = 7,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """One 'how does <period> look' entry — tools-merge-calendar-plan
        absorbed ada_daily_summary + ada_weekly_comparison here.

        period='today'|'tomorrow' → that day's merged events+tasks view,
        with the day's session digest folded in under 'digest' (the
        absorbed ada_daily_summary — for a day with no sessions it reports
        no_sessions). day= overrides the target ('yesterday', YYYY-MM-DD)
        and also works without a configured calendar — the digest alone
        is the day view then. period='week' → the weekly digest
        comparison (absorbed ada_weekly_comparison): day= sets the window
        end, days= the window size."""
        period = str(period or "today").strip().lower()
        if period == "week":
            return await self.ada_weekly_comparison(
                end=str(day or "today"), days=int(days or 7),
                refresh=bool(refresh))
        if period == "digest":
            # ada_daily_summary's alias seat — keeps the absorbed tool's
            # bare-digest contract for /api/tools/call consumers.
            return await self.ada_daily_summary(
                day=str(day or "today"), refresh=bool(refresh))
        target = str(day or period or "today")
        svc = self.calendar
        plan = await svc.plan_day(day=target) if svc is not None else None
        digest = await self.ada_daily_summary(
            day=target, refresh=bool(refresh))
        if plan is None:
            digest.setdefault("calendar", "not configured")
            return digest
        plan["digest"] = digest
        return plan

    async def ada_resolve_action(self, key: str, resolution: str) -> str:
        """Resolve a pending action proposal (applied|dismissed). Bookkeeping
        only — not a calendar write, so no confirmed gate."""
        from backend.conversation_memory import resolve_action_proposal
        return await resolve_action_proposal(str(key), str(resolution))

    # -- Home Assistant tools --

    async def get_home_state(
        self,
        entity_id: str | None = None,
        domain: str | None = None,
    ) -> Any:
        """Current home state — tools-merge-ha absorbed ada_ha_get_state
        here (domain='memory' returns the stored AdaMemoryStore overview,
        keeping the alias's exact contract). entity_id reads one entity
        live, domain= lists the entities under one HA domain."""
        entity_id = str(entity_id or "").strip()
        domain = str(domain or "").strip().lower()
        if entity_id:
            return await self.context.ha_client.get_state(entity_id)
        if domain == "memory":
            if self.memory is None:
                raise RuntimeError("stored home snapshot not available")
            return await self.memory.overview()
        if domain:
            states = await self.context.ha_client._states()
            out = []
            for item in states:
                eid = str(item.get("entity_id") or "")
                if eid.partition(".")[0] != domain:
                    continue
                attrs = item.get("attributes") or {}
                out.append({
                    "entity_id": eid,
                    "state": str(item.get("state", "unknown")),
                    "name": str(attrs.get("friendly_name") or eid),
                })
            return out[:LIST_HOME_DEVICES_MAX]
        snapshot = await self.context.ha_client.snapshot()
        plugs_on_named = [
            {"entity_id": e, "name": snapshot.plug_names.get(e, e)}
            for e in snapshot.plugs_on
        ]
        all_plugs = {
            e: {"name": snapshot.plug_names.get(e, e), "state": state}
            for e, state in snapshot.plug_states.items()
        }
        return {
            "person": snapshot.person_state,
            "plugs_on": plugs_on_named,
            "all_plugs": all_plugs,
        }

    async def home_search(
        self,
        query: str = "",
        kind: str = "device",
        limit: int = 10,
        hours: int = 24,
    ) -> Any:
        """Home Assistant finders — tools-merge-ha consolidated
        search_home_devices / list_home_devices / search_sensors /
        list_sensors / ada_ha_search_devices / ada_ha_search_sensors /
        ada_ha_search_events / search_devices behind kind=. A blank
        query lists (bounded) instead of searching."""
        kind = str(kind or "device").strip().lower()
        query = str(query or "").strip()
        if kind == "device":
            if query:
                return await self.search_home_devices(query)
            return await self.list_home_devices()
        if kind == "sensor":
            if query:
                return await self.context.ha_client.sensors(
                    search=query, limit=int(limit))
            return await self.list_sensors()
        if kind == "event":
            if self.events is None or self.mddb is None:
                raise RuntimeError("recorded event memory not available")
            return await self.ada_ha_search_events(
                query=query, hours=int(hours), limit=int(limit))
        raise ValueError(
            f"invalid kind {kind!r}: expected device|sensor|event")

    async def list_home_devices(self) -> list[dict[str, Any]]:
        # Bound the dump — an unbounded entity list stays in the live
        # context for the rest of the session and ballooned one past 1M
        # input tokens on 2026-10-01. search_home_devices is the precise
        # path; the marker tells the model so.
        devices = await self.context.ha_client.entities()
        if len(devices) > LIST_HOME_DEVICES_MAX:
            return devices[:LIST_HOME_DEVICES_MAX] + [{
                "_truncated": (
                    f"{LIST_HOME_DEVICES_MAX} of {len(devices)} devices shown "
                    "— call home_search with a name/keyword for the rest"),
            }]
        return devices

    async def search_home_devices(self, query: str) -> list[dict[str, Any]]:
        return await self.context.ha_client.search_entities(str(query))

    async def control_entity(
        self,
        entity_id: str,
        action: str = "",
        on: bool | None = None,
        source: str | None = None,
    ) -> Any:
        """Actuate one HA entity — tools-merge-ha consolidated
        control_cover / press_button / control_media_player here; the
        entity domain picks the path. on= is the absorbed on/off arg;
        action= carries the domain verbs (open|close|stop for cover.*,
        press for button.*, the media_player verbs, on|off otherwise)."""
        entity_id = str(entity_id or "")
        if not entity_id:
            raise ValueError("entity_id is required")
        domain = entity_id.partition(".")[0]
        action = str(action or "").strip().lower()
        if domain == "cover":
            return await self.control_cover(entity_id, action)
        if domain in ("button", "input_button") or action == "press":
            return await self.press_button(entity_id)
        if domain == "media_player":
            return await self.control_media_player(entity_id, action, source)
        if on is None:
            if action in ("on", "turn_on"):
                on = True
            elif action in ("off", "turn_off"):
                on = False
            else:
                raise ValueError(
                    "pass on=true/false or action=on|off for "
                    f"{domain or entity_id} entities")
        outcome = await self.context.ha_client.set_power(entity_id, bool(on))
        return f"Turned {'on' if on else 'off'} {entity_id}: {outcome}"

    async def list_sensors(self) -> list[dict[str, Any]]:
        # Keep the default small: every tool result stays in the live-voice
        # context for the rest of the session. search_sensors is the right
        # tool for a specific device; 25 covers a broad "what sensors" ask.
        return await self.context.ha_client.sensors(limit=25)

    async def search_sensors(self, query: str) -> list[dict[str, Any]]:
        return await self.context.ha_client.sensors(search=str(query), limit=10)

    async def control_cover(self, entity_id: str, action: str) -> dict[str, Any]:
        if not entity_id or not action:
            raise ValueError("entity_id and action are required")
        return await self.context.ha_client.control_cover(entity_id, action)

    async def press_button(self, entity_id: str) -> dict[str, Any]:
        if not entity_id:
            raise ValueError("entity_id is required")
        return await self.context.ha_client.press_button(entity_id)

    async def control_media_player(self, entity_id: str, action: str, source: str | None = None) -> dict[str, Any]:
        if not entity_id or not action:
            raise ValueError("entity_id and action are required")
        return await self.context.ha_client.control_media_player(entity_id, action, source)

    async def tv_action(self, cmd: str, text: str = "",
                        selector: str = "", role: str = "",
                        key: str = "", dx: float | None = None,
                        dy: float | None = None,
                        factor: float | None = None) -> dict[str, Any]:
        if not cmd:
            raise ValueError("cmd is required")
        # Screen-ownership ACL: personal desktop sources are owner-locked.
        # Deny non-owners here (rest_command swallows the controller's 403)
        # and pass the speaker so cast-browser enforces as backstop too.
        target = text or " ".join(str(cmd).split()[1:])
        self._check_tv_source_owner(target, self._memory_identity())
        out = await self.context.ha_client.tv_action(
            cmd, text, selector=selector or None, role=role or None,
            key=key or None, dx=dx, dy=dy, factor=factor,
            speaker=self._memory_identity() or "",
        )
        # Post-nav ground truth: cast-browser acking the nav only means
        # the browser took the URL — if the TV's foreground app is the
        # STB or another input, the page loaded but is invisible
        # (2026-10-04: 'casting' for 3min while the TV showed True STB).
        if isinstance(out, dict) and str(cmd).lower() == "nav" and text:
            try:
                await asyncio.sleep(1.0)  # let webOS foreground the app
                inp = await self._tv_input_state()
                if inp.get("on_cast_app") is not None:
                    out["tv_input"] = inp
                    if inp["on_cast_app"] is False:
                        out["input_warn"] = (
                            f"the TV is on '{inp.get('app') or inp.get('state')}' — "
                            "the page loaded in the TV browser but is NOT "
                            "visible; do not claim it is showing. Push the "
                            "URL again or switch the TV input.")
            except Exception:
                pass
        return out

    async def home_status(
        self,
        what: str = "",
        battery_index: int | None = None,
        hours: int = 24,
        tab: str = "",
    ) -> Any:
        """Home status reads — tools-merge-tasks-status consolidated
        get_battery_status / get_battery_detail / get_power_summary /
        get_inverter_status / get_pool_status / get_dashboard_tab /
        get_habit_status into one what= tool. All free reads; what=
        'battery' with battery_index reads one battery's detail."""
        what = (what or "").strip().lower()
        if what == "battery":
            if battery_index in (None, ""):
                return await self.get_battery_status()
            return await self.get_battery_detail(int(battery_index))
        if what == "power":
            return await self.get_power_summary(hours=int(hours))
        if what == "inverter":
            return await self.get_inverter_status()
        if what == "pool":
            return await self.get_pool_status()
        if what == "dashboard":
            return await self.get_dashboard_tab(str(tab))
        if what == "habit":
            return await self.get_habit_status()
        raise ValueError(
            f"invalid what {what!r}: expected "
            "battery|power|inverter|pool|dashboard|habit")

    async def get_battery_status(self) -> dict[str, Any]:
        return await self.context.ha_client.battery_status()

    async def get_battery_detail(self, battery_index: int = 1) -> dict[str, Any]:
        return await self.context.ha_client.battery_detail(int(battery_index))

    async def get_inverter_status(self) -> dict[str, Any]:
        return await self.context.ha_client.inverter_status()

    async def get_pool_status(self) -> dict[str, Any]:
        return await self.context.ha_client.pool_status()

    async def get_rk600_weather(self) -> dict[str, Any]:
        return await self.context.ha_client.rk600_weather()

    async def get_dashboard_tab(self, tab: str) -> dict[str, Any]:
        if not tab:
            raise ValueError("tab is required")
        return await self.context.ha_client.dashboard_tab(str(tab))

    async def get_power_summary(self, hours: int = 24) -> dict[str, Any]:
        return await self.context.ha_client.power_summary(hours=int(hours))

    async def get_sensor_history(self, entity_id: str, hours: int = 24) -> list[list[dict[str, Any]]]:
        if not entity_id:
            raise ValueError("entity_id is required")
        return await self.context.ha_client.history(str(entity_id), hours=int(hours))

    async def get_logbook(self, hours: int = 24, entity_id: str | None = None) -> dict[str, Any]:
        entries = await self.context.ha_client.logbook(
            entity_id=str(entity_id) if entity_id else None,
            hours=int(hours),
        )
        return {"hours": int(hours), "count": len(entries), "entries": entries[:100]}

    async def get_entity_events(self, entity_id: str, hours: int = 24) -> dict[str, Any]:
        if not entity_id:
            raise ValueError("entity_id is required")
        return await self.context.ha_client.state_transitions(str(entity_id), hours=int(hours))

    async def get_recent_events(self, hours: int = 24, query: str | None = None, limit: int = 25) -> dict[str, Any]:
        return await self.context.ha_client.recent_events(
            hours=int(hours),
            query=str(query) if query else None,
            limit=int(limit),
        )

    async def home_history(
        self,
        entity_id: str | None = None,
        domain: str | None = None,
        kind: str | None = None,
        hours: int = 24,
        query: str | None = None,
        limit: int = 25,
    ) -> Any:
        """Home history reads — tools-merge-ha consolidated get_logbook /
        get_sensor_history / get_entity_events / get_recent_events /
        ada_ha_history behind kind=. Default routing: entity_id -> the
        entity timeline ('series' for sensor.*, 'timeline' otherwise),
        query -> the whole-home events feed, domain='memory'|'snapshots'
        -> persisted memory snapshots, else the logbook."""
        entity_id = str(entity_id or "").strip()
        domain = str(domain or "").strip().lower()
        kind = str(kind or "").strip().lower()
        if not kind:
            if entity_id:
                kind = ("series" if entity_id.startswith("sensor.")
                        else "timeline")
            elif domain in ("memory", "snapshots"):
                kind = "snapshots"
            elif query:
                kind = "events"
            else:
                kind = "logbook"
        hours = int(hours or 24)
        limit = int(limit or 25)
        if kind == "series":
            if not entity_id:
                raise ValueError("entity_id is required for kind='series'")
            return await self.context.ha_client.history(
                entity_id, hours=hours)
        if kind == "timeline":
            if not entity_id:
                raise ValueError("entity_id is required for kind='timeline'")
            return await self.context.ha_client.state_transitions(
                entity_id, hours=hours)
        if kind == "logbook":
            entries = await self.context.ha_client.logbook(
                entity_id=entity_id or None, hours=hours)
            if domain:
                prefix = f"{domain}."
                entries = [e for e in entries if str(
                    e.get("entity_id") or "").startswith(prefix)]
            return {"hours": hours, "count": len(entries),
                    "entries": entries[:100]}
        if kind == "events":
            return await self.get_recent_events(
                hours=hours, query=query or (domain or None), limit=limit)
        if kind == "snapshots":
            if self.mddb is None:
                raise RuntimeError(
                    "persisted home snapshots not available")
            return await self.ada_ha_history(hours=hours, limit=limit)
        raise ValueError(
            f"invalid kind {kind!r}: expected logbook|events|timeline|"
            "series|snapshots")

    # -- Habit tools --

    async def get_habit_status(self) -> Any:
        if self.context.habit_state_getter is None:
            raise RuntimeError("habit tracking not available")
        return self.context.habit_state_getter()

    # -- Ada HA memory tools --

    async def ada_ha_get_state(self) -> dict[str, Any]:
        return await self.memory.overview()

    async def ada_ha_search_devices(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return await self.memory.search_devices(str(query), int(limit))

    async def ada_ha_search_sensors(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return await self.memory.search_sensors(str(query), int(limit))

    async def ada_ha_recall(self, query: str, limit: int = 10) -> dict[str, Any]:
        results = await self.memory.search_all(str(query), int(limit))
        results["events"] = self.events.recent(hours=24, query=str(query), limit=int(limit))
        return results

    async def ada_ha_search_events(self, query: str = "", hours: int = 24, limit: int = 20) -> dict[str, Any]:
        """Search recorded HA transitions in memory plus persisted MDDB batches."""
        q = str(query).strip().lower()
        persisted = []
        docs = await self.mddb.search_documents(
            collection=self.events.collection,
            query=str(query) or "*",
            filter_meta={"kind": ["events"]},
            limit=10,
        )
        for doc in docs:
            meta = doc.get("meta", {})
            lines = str(doc.get("contentMd") or doc.get("content_md") or "").splitlines()
            matched = [l for l in lines if l.startswith("-") and (not q or q in l.lower())]
            persisted.append({
                "key": doc.get("key"),
                "count": _int_or_none(_first(meta.get("count"))),
                "period_start": _first(meta.get("period_start")),
                "period_end": _first(meta.get("period_end")),
                "matching_lines": matched[:int(limit)],
            })
        return {
            "query": str(query),
            "hours": int(hours),
            "recorder": self.events.status(),
            "events": self.events.recent(hours=int(hours), query=str(query), limit=int(limit)),
            "persisted": persisted,
        }

    async def ada_ha_history(self, hours: int = 24, limit: int = 10) -> list[dict[str, Any]]:
        """Return recent persisted snapshots from MDDB for this HA instance."""
        cutoff = time.time() - (int(hours) * 3600)
        docs = await self.mddb.search_documents(
            collection=self.memory._collection,
            query="*",
            filter_meta={"source": [str(self.context.ha_client.base_url)]},
            limit=limit,
        )
        results = []
        for doc in sorted(docs, key=lambda d: d.get("addedAt", 0), reverse=True):
            if doc.get("addedAt", 0) < cutoff:
                continue
            meta = doc.get("meta", {})
            results.append({
                "key": doc.get("key"),
                "added_at": doc.get("addedAt"),
                "source": _first(meta.get("source")),
                "person_entity": _first(meta.get("person_entity")),
                "total_entities": _int_or_none(_first(meta.get("total_entities"))),
                "controllable_count": _int_or_none(_first(meta.get("controllable_count"))),
                "sensor_count": _int_or_none(_first(meta.get("sensor_count"))),
                "refreshed_at": _first(meta.get("refreshed_at")),
            })
        return results

    async def ha_confidence(
        self,
        entity_id: str = "",
        status: str = "",
        safety: str = "",
    ) -> Any:
        """Device trust/safety registry — tools-merge-ha consolidated
        ada_ha_get_device_confidence + ada_ha_set_device_confidence.

        No args → controllable devices grouped by confidence.
        entity_id alone → that device's entry. entity_id + status and/or
        safety → the write path (the absorbed set tool's contract)."""
        if self.memory is None:
            raise RuntimeError("device confidence not available")
        entity_id = str(entity_id or "").strip()
        status = str(status or "").strip()
        safety = str(safety or "").strip()
        if status or safety:
            if not entity_id:
                raise ValueError(
                    "entity_id is required to set confidence")
            if not status:
                raise ValueError(
                    "status is required — pass the confidence level "
                    "alongside safety")
            return await self.memory.set_confidence(
                entity_id, status, safety or None)
        await self.memory._ensure_confidence()
        groups = self.memory.confidence_groups()
        if not entity_id:
            return groups
        for group_name, devices in groups.items():
            for dev in devices:
                if dev.get("entity_id") == entity_id:
                    return {**dev, "confidence": group_name}
        return {"entity_id": entity_id, "confidence": "unknown"}

    async def ada_ha_get_device_confidence(self) -> dict[str, list[dict[str, Any]]]:
        """Return controllable devices grouped by user confidence."""
        await self.memory._ensure_confidence()
        return self.memory.confidence_groups()

    async def web_search(self, query: str, provider: str | None = None) -> dict[str, Any]:
        """Web search with provider selection.

        provider='gemini'  — grounded answer via Gemini google_search
                             (billed/quota-limited; the live audio model
                             cannot ground itself).
        provider='duckduckgo' — free HTML endpoint; returns top results,
                             no quota. Use when grounding is exhausted
                             or the user asks for DuckDuckGo.
        provider='auto' (default) — gemini, falling back to duckduckgo
                             on quota/error.
        """
        want = (provider or "auto").lower()
        if want not in ("auto", "gemini", "duckduckgo"):
            raise ValueError(f"unknown web_search provider {provider!r} "
                             "(auto|gemini|duckduckgo)")
        if want in ("auto", "gemini"):
            try:
                out = await self._web_search_gemini(query)
            except Exception as e:
                if want == "gemini":
                    raise
                # auto: quota/error → free fallback
                try:
                    out = await self._web_search_ddg(query)
                except Exception:
                    raise e
        else:
            out = await self._web_search_ddg(query)
        # Save-back standard — every outside-source answer carries the
        # reminder so the finding lands back in CMS and repeat questions
        # never need a re-search (card ada-cms-first-answers).
        out["note"] = (
            "outside-source answer — save-back standard: if this updates a "
            "tracked topic or may be asked again, write it to CMS this turn "
            "(cms_note_update on the matching page, or offer "
            "cms_publish_page for a new one)."
        )
        return out

    async def _web_search_gemini(self, query: str) -> dict[str, Any]:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("web search unavailable: GEMINI_API_KEY not set")
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        model = os.environ.get("ADA_WEB_SEARCH_MODEL", "gemini-2.5-flash")
        resp = await client.aio.models.generate_content(
            model=model,
            contents=str(query),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]),
        )
        text = (resp.text or "").strip()
        if not text:
            raise RuntimeError("web search returned no answer")
        sources = []
        try:
            gm = resp.candidates[0].grounding_metadata
            for ch in (gm.grounding_chunks or [])[:5]:
                w = getattr(ch, "web", None)
                if w is not None:
                    sources.append({
                        "title": getattr(w, "title", "") or "",
                        "uri": getattr(w, "uri", "") or "",
                    })
        except Exception:
            pass
        return {"answer": text, "sources": sources,
                "provider": "gemini", "model": model}

    async def _web_search_ddg(self, query: str) -> dict[str, Any]:
        """DuckDuckGo lite HTML — no API key, no quota. Returns top hits
        as a synthesized answer + source list."""
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (Ada-voice-assistant)"})
            r.raise_for_status()
        # results: <a rel="nofollow" class="result__a" href="redirect">title</a>
        hits = re.findall(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text)
        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
        strip = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()
        sources = []
        for url, title in hits[:5]:
            m = re.search(r"uddg=([^&]+)", url)
            if m:
                from urllib.parse import unquote
                url = unquote(m.group(1))
            sources.append({"title": strip(title), "uri": url})
        if not sources:
            raise RuntimeError("duckduckgo returned no results")
        top = strip(snippets[0]) if snippets else sources[0]["title"]
        answer = (top + " " if top else "") + "Top result: " + sources[0]["title"]
        return {"answer": answer.strip(), "sources": sources,
                "provider": "duckduckgo"}

    async def ada_ha_set_device_confidence(self, entity_id: str, status: str, safety: str | None = None) -> str:
        """Set a device's confidence and/or safety status."""
        return await self.memory.set_confidence(str(entity_id), str(status), str(safety) if safety is not None else None)

    # -- Memory bank tools (curated memory; see ssot.apps.ada-memory-*.yml) --
    # Implementation lives in backend.memory_ops — these delegates keep the
    # dispatch surface (method names/signatures) stable.

    async def ada_memory_search(
        self,
        query: str,
        bank: str = "all",
        limit: int = 5,
        include_inactive: bool = False,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Search memory across scopes (tools-merge-memory):

        banks    — the curated memory banks (memory_ops.memory_search)
        sessions — the per-instance recall-summary collection (session,
                   daily, weekly rollup docs)
        guest    — the chaba guest store (replaces guest_recall)
        all      — every scope available on this instance
        """
        scope_s = str(scope or "").strip().lower()
        if not scope_s:
            # An explicit bank name narrows to that bank only — the same
            # behavior the pre-merge tool had for bank-scoped callers.
            scope_s = "all" if str(bank or "all") == "all" else "banks"
        if scope_s not in ("all", "banks", "sessions", "guest"):
            raise ValueError(
                f"unknown memory search scope {scope!r} "
                "(all|banks|sessions|guest)")
        if scope_s == "guest" and self.chaba is None:
            self._require_chaba()  # raises: guest store is chaba-only
        if scope_s == "banks" and self.mddb is None:
            raise ValueError("no curated memory banks on this instance")
        hits: list[dict[str, Any]] = []
        degraded = False
        if scope_s in ("all", "banks") and self.mddb is not None:
            res = await memory_ops.memory_search(
                self.mddb, self.banks, bank, query, limit, include_inactive,
                person_entity=self._memory_identity(),
            )
            hits.extend(res.get("hits") or [])
            degraded = bool(res.get("degraded"))
        if scope_s in ("all", "sessions") and self.mddb is not None:
            hits.extend(await self._session_hits(str(query), int(limit)))
        if scope_s in ("all", "guest") and self.chaba is not None:
            for h in self.chaba.recall(
                    str(query), session_id=self.session_id,
                    limit=int(limit)):
                hits.append({
                    "bank": "guest", "key": h.get("key"),
                    "subject": h.get("name"), "score": h.get("score"),
                    "content": h.get("text"), "at": h.get("at"),
                })
        hits.sort(key=lambda h: float(h.get("score") or 0), reverse=True)
        hits = hits[: int(limit)]
        return {
            "bank": bank if scope_s == "banks" else scope_s,
            "scope": scope_s,
            "count": len(hits),
            "hits": hits,
            "degraded": degraded,
        }

    async def _session_hits(
        self, query: str, limit: int
    ) -> list[dict[str, Any]]:
        """scope=sessions — search the recall-summary collection
        (session/daily/weekly/monthly summary docs) that
        ada_session_recall's mid tier reads."""
        from backend.conversation_memory import _summary_collection
        coll = _summary_collection()
        q = str(query or "").strip()
        docs = None
        if q and q != "*" and not self.mddb.is_ops_routed(coll):
            docs = await self.mddb.vector_search(
                collection=coll, query=q, limit=int(limit) * 2)
        if docs is None:
            listing_filter = None
            if self.mddb.is_ops_routed(coll):
                # Ops listings are oldest-first — bound by `date` meta so
                # recent summaries enter the candidate window.
                days = [
                    (datetime.now(timezone.utc).date() - timedelta(days=i)).isoformat()
                    for i in range(14)
                ]
                listing_filter = {"date": days}
            listed = await self.mddb.search_documents(
                collection=coll, filter_meta=listing_filter,
                limit=max(int(limit) * 5, 20))
            docs = (memory_ops._keyword_rank(listed, q)
                    if q and q != "*" else listed)
        hits = []
        for d in docs or []:
            meta = d.get("meta") or {}
            content = str(d.get("contentMd") or d.get("content_md") or "")
            hits.append({
                "bank": "sessions",
                "key": d.get("key"),
                "score": d.get("score"),
                "kind": _first(meta.get("kind")),
                "date": _first(meta.get("date")),
                "content": content[:800],
            })
        return hits[: int(limit)]

    async def ada_remember(
        self,
        bank: str | None = None,
        text: str | None = None,
        key: str | None = None,
        subject: str | None = None,
        attribute: str | None = None,
        kind: str | None = None,
        valid_until: str | None = None,
        applies_to: list[str] | str | None = None,
        supersedes: str | None = None,
        prime: bool | None = None,
        private: bool = False,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Store a memory. `kind` selects the store (tools-merge-memory):

        fact/preference/person/procedure/note — curated bank write
        (bank required); vocab — append the caller's own vocab log
        (absorbed vocab_note); guest — the chaba guest store, private=true
        for a promoted user's private namespace (absorbed guest_remember /
        guest_remember_private); habit — observation telemetry, delivered
        by the live session (absorbed report_habit_observation).
        """
        k = str(kind or "").strip().lower()
        if not k and self.chaba is not None:
            k = "guest"
        if k == "vocab":
            return await self._vocab_append(text, note)
        if k == "guest":
            self._require_chaba()
            if not str(text or "").strip():
                raise ValueError("remember(kind='guest') requires text")
            slug = str(key or _slug(str(text))[:40] or "note")
            if private:
                return self.chaba.remember_private(
                    self.session_id, slug, str(text))
            return self.chaba.remember(self.session_id, slug, str(text))
        if k == "habit":
            # Provider-dispatched: the observation reaches the habit
            # monitor as a ProviderEvent. A store-side call gets a clear
            # answer rather than a silent no-op.
            return {"error": (
                "habit observations are delivered by the live session "
                "monitor — this call reached the store directly")}
        if self.mddb is None or self.memory is None:
            raise PermissionError(
                "curated memory banks are not available on this instance")
        if not str(bank or "").strip():
            raise ValueError(
                "ada_remember requires 'bank' for curated-bank writes "
                "(kind fact/preference/person/procedure/note)")
        if not str(text or "").strip():
            raise ValueError("ada_remember requires 'text'")
        return await memory_ops.remember(
            self.mddb, self.banks, self.memory.instance,
            str(bank), str(text), key, subject, attribute, kind, valid_until,
            applies_to, supersedes, session_id=self.session_id,
            person_entity=self._memory_identity(), prime=prime,
        )

    async def ada_session_recall(
        self,
        question: str = "",
        scope: str | None = None,
        limit: int = 10,
        **_unused: Any,
    ) -> dict[str, Any]:
        """Conversation/home-history recall (tools-merge-memory).

        scope='history' is the absorbed ada_ha_recall — inline search of
        the stored HA memory + recorded events. scope='sessions' (the
        deep NotebookLM ask) is provider-dispatched: outside a live voice
        session there is no conversation to attach the async answer to.
        """
        scope_s = str(scope or "sessions").strip().lower()
        if scope_s == "history":
            # Models trained on the absorbed ada_ha_recall schema sometimes
            # still send query= on the canonical name too.
            q = str(question or "") or str(_unused.get("query") or "") or "*"
            return await self.ada_ha_recall(q, int(limit))
        if scope_s != "sessions":
            raise ValueError(
                f"unknown recall scope {scope!r} (sessions|history)")
        return {"error": (
            "session recall runs inside a live voice session — for a "
            "direct lookup use ada_memory_search scope='sessions' to "
            "search session summaries")}

    async def ada_persona(
        self, action: str, knob: str | None = None, value: Any = None,
        person: str | None = None, voice: str | None = None,
    ) -> dict[str, Any]:
        """Read or adjust a speaker's stored style preferences.

        Without `person` this targets the current session identity. With
        `person` (name 'KK' or entity 'person.kk') it targets that HA
        person's profile — allowed for full-access identities, or when the
        target resolves to the caller's own persona bank (e.g. an
        unenrolled speaker addressing themselves by name via key_scope).
        """
        caller = self.policy_identity()
        action = str(action or "show").lower()
        if action == "list":
            return await self._persona_list()
        # *_voice actions absorbed ada_set_voice (tools-merge-meta-voice):
        # they manage the instance's speaking voice, not a per-person
        # profile — the provider intercepts them on live sessions (it owns
        # the idle-gated reconnect); this path serves REST/alias calls.
        if action in _PERSONA_VOICE_ACTIONS:
            return self._persona_voice(action, voice)
        # "Self" follows the speaking voice (personalization); `person`
        # targeting authorizes against the session owner (P3).
        identity = self._memory_identity() or caller
        if person:
            identity = await self._resolve_persona_target(person, caller)
            if (action in ("set", "reset") and identity != caller
                    and self.banks.personal_bank_name(identity) == "personal"):
                raise PermissionError(
                    f"{identity} has no person-scoped memory bank — refusing "
                    "to file their persona under the default 'personal' bank; "
                    "provision a scoped bank (e.g. personal-<id>) first")
        if action == "show":
            p = await memory_ops.get_persona(self.mddb, self.banks, identity)
            return {"verb": "show", "person": identity, **p}
        if action == "reset":
            return await memory_ops.reset_persona(self.mddb, self.banks, identity)
        if action == "set":
            if not knob:
                raise ValueError("set requires a knob name")
            result = await memory_ops.set_persona(
                self.mddb, self.banks, identity, str(knob), value
            )
            result["person"] = identity
            # Tell the model to apply it now — the persisted doc covers
            # future sessions via the prime injection.
            if identity == caller:
                result["apply"] = (
                    f"Preference saved and active now: {knob}={value}. "
                    "Honor it in your next replies without announcing the mechanism."
                )
            else:
                result["apply"] = (
                    f"Preference saved for {identity}: {knob}={value}. "
                    "It applies to their sessions, not this speaker's."
                )
            return result
        raise ValueError(
            f"unknown persona action {action!r} "
            "(set|show|reset|list|set_voice|show_voice|list_voices)")

    def _persona_voice(self, action: str, voice: Any) -> dict[str, Any]:
        """The absorbed ada_set_voice seat on ada_persona. A live-session
        call is intercepted provider-side (the provider owns the
        idle-gated reconnect); this runner path serves REST/alias calls —
        a set persists the choice for the next session."""
        from backend import voice_config
        if action == "set_voice" and voice:
            name = voice_config.canonical_voice(voice)
            if not name:
                return {"error": (
                    f"unknown voice {voice!r} — available voices: "
                    f"{', '.join(voice_config.GEMINI_VOICES)}")}
            previous = voice_config.current_voice()
            if name == previous:
                return {"verb": "set_voice", "voice": name,
                        "note": "already the active voice"}
            try:
                voice_config.set_voice(name)
            except Exception as exc:
                return {"error": f"could not save the voice preference: {exc}"}
            return {"verb": "set_voice", "voice": name, "previous": previous,
                    "note": ("voice saved — a live voice session applies it "
                             "on reconnect")}
        return {"verb": action,
                "current_voice": voice_config.current_voice(),
                "voices": list(voice_config.GEMINI_VOICES)}

    VOCAB_DOC_KEY = "vocab/log"

    async def vocab_note(
        self, term: str, correct: str, note: str | None = None
    ) -> dict[str, Any]:
        """Retired surface name (tools-merge-memory) — kept as the direct
        call form of ada_remember kind='vocab'; the alias shim maps
        term/correct onto `text` for execute()-routed calls."""
        term, correct = (term or "").strip(), (correct or "").strip()
        if not term or not correct:
            raise ValueError("vocab_note requires term and correct")
        return await self._vocab_append(f"{term} → {correct}", note)

    async def _vocab_append(
        self, text: Any, note: Any = None
    ) -> dict[str, Any]:
        """Append a term-coaching entry to the current speaker's personal
        vocab log (vocab/log in their own personal bank — KK's notes land in
        personal-kk, Tony's in personal-tony). Not confirmation-gated:
        append-only, scoped to the caller's own bank."""
        if self.mddb is None:
            raise PermissionError(
                "vocab notes are unavailable on this instance")
        bank = memory_ops.persona_bank_for(self.banks, self._memory_identity())
        if bank is None:
            raise PermissionError(
                "vocab notes need a personal bank — this identity has none"
            )
        entry = str(text or "").strip()
        note_s = str(note).strip() if note else ""
        if not entry:
            raise ValueError("vocab memory requires text ('term → correction')")
        today = datetime.now(timezone.utc).date().isoformat()
        line = f"- {entry} — {today}" + (f" ({note_s})" if note_s else "")
        doc = await self.mddb.get_document(bank.mddb_collection, self.VOCAB_DOC_KEY)
        body = ((doc or {}).get("contentMd") or doc and doc.get("content_md") or "")
        if not body.strip():
            body = "# Vocabulary — terms I heard, gently corrected\n"
        if line not in body:
            body = body.rstrip("\n") + "\n" + line + "\n"
        meta = {
            "kind": ["vocab"], "subject": ["persona"],
            "status": ["active"], "scope": ["instance"],
            "last_verified": [today],
        }
        if doc is None:
            ok = await self.mddb.add_document(
                bank.mddb_collection, self.VOCAB_DOC_KEY, "en", body, meta)
        else:
            ok = await self.mddb.update_document(
                bank.mddb_collection, self.VOCAB_DOC_KEY, content_md=body, meta=meta)
        if not ok:
            return {"status": "error", "error": "mddb write failed"}
        return {"status": "noted", "bank": bank.name, "key": self.VOCAB_DOC_KEY,
                "entry": entry}

    def _persona_admin(self, caller: str | None) -> bool:
        """Full-access identities may manage other people's profiles.

        'admin' is the name the master ADA_API_KEY resolves to; otherwise a
        {full: true} entry in person_policies or control_policies grants it
        (same convention as actuation admin)."""
        if not caller:
            return False
        if caller == "admin":
            return True
        for policies in (self.banks.person_policies, self.banks.control_policies):
            if (policies.get(caller) or {}).get("full"):
                return True
        return False

    async def _resolve_persona_target(self, person: str, caller: str | None) -> str:
        """Resolve person ('KK' or 'person.kk') to an entity id and check
        the caller may touch that profile."""
        resolved = await self.context.ha_client.resolve_person(person)
        if resolved is None:
            try:
                known = ", ".join(
                    p["entity_id"] for p in await self.context.ha_client.persons()
                ) or "none"
            except Exception:
                known = "unavailable (Home Assistant unreachable)"
            raise ValueError(
                f"no Home Assistant person matches {person!r} (known: {known})"
            )
        target = resolved["entity_id"]
        if target == caller:
            return target
        # Self-targeting by name still counts as self when both identities
        # land in the same persona bank (e.g. key 'user-kk' -> person.kk).
        if caller:
            own = memory_ops.persona_bank_for(self.banks, caller)
            theirs = memory_ops.persona_bank_for(self.banks, target)
            if own is not None and own is theirs:
                return target
        if not self._persona_admin(caller):
            logger.warning(
                "denied persona manage of %s for identity %r", target, caller
            )
            raise PermissionError(
                "managing another person's profile requires a full-access "
                f"identity (caller: {caller or 'anonymous'})"
            )
        return target

    async def _persona_list(self) -> dict[str, Any]:
        """List HA person entities with their persona bank + custom knobs."""
        people = await self.context.ha_client.persons()
        out = []
        for p in people:
            entry = dict(p)
            if self.mddb is not None:
                bank = memory_ops.persona_bank_for(self.banks, p["entity_id"])
                entry["persona_bank"] = bank.name if bank else None
                persona = await memory_ops.get_persona(
                    self.mddb, self.banks, p["entity_id"]
                )
                entry["custom_knobs"] = persona["custom"]
            out.append(entry)
        return {"verb": "list", "persons": out}

    async def ada_forget(self, bank: str, key: str, reason: str | None = None) -> dict[str, Any]:
        """Retract a memory: status becomes retracted; the doc stays auditable."""
        return await memory_ops.forget(
            self.mddb, self.banks, bank, key, reason, session_id=self.session_id,
            person_entity=self._memory_identity(),
        )

    async def ada_outcome(
        self,
        bank: str,
        key: str,
        outcome: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Record how a remembered fact/check turned out (good/bad/…)."""
        return await memory_ops.record_outcome(
            self.mddb, self.banks, bank, key, outcome, note,
            session_id=self.session_id,
            person_entity=self._memory_identity(),
        )

    # -- Meta/ops umbrella (tools-merge-meta-voice, 2026-10-05): ada_outcome,
    #    ada_usage_summary, ada_mddb_health, ada_decision_check and
    #    ada_deep_research consolidated behind action=. outcome/usage/health
    #    run here; check/research dispatch provider-side on live sessions
    #    (their results arrive as injected text turns) and get an honest
    #    error on this path — pre-merge they were provider-only too.

    async def ada_ops(
        self,
        action: str,
        bank: str | None = None,
        key: str | None = None,
        outcome: str | None = None,
        note: str | None = None,
        source: str = "all",
        reset: bool = False,
        collection: str | None = None,
        product: str | None = None,
        url: str | None = None,
        mode: str | None = None,
        topic: str | None = None,
        depth: str | None = None,
    ) -> dict[str, Any]:
        """Meta/ops tools behind one action= param (the absorbed
        ada_outcome / ada_usage_summary / ada_mddb_health /
        ada_decision_check / ada_deep_research seats)."""
        action = str(action or "").strip().lower()
        if action == "outcome":
            if not bank or not key or not outcome:
                raise ValueError(
                    "action 'outcome' requires bank, key, and outcome")
            return await self.ada_outcome(
                bank=str(bank), key=str(key), outcome=str(outcome),
                note=note)
        if action == "usage":
            return await self.ada_usage_summary(
                source=str(source or "all"), reset=bool(reset))
        if action == "health":
            return await self._ops_mddb_health(collection)
        if action in ("check", "research"):
            return {"error": (
                f"ada_ops action='{action}' only runs inside a live voice "
                "session — its result is spoken back mid-conversation; "
                "it is not available on this call path")}
        raise ValueError(
            f"invalid ada_ops action {action!r}: expected "
            "outcome|usage|health|check|research")

    async def _ops_mddb_health(self, collection: str | None) -> dict[str, Any]:
        """The absorbed ada_mddb_health drop-in (tools.d): MDDB vector-stats
        report — total docs, missing vectors, per-collection lag."""
        if self.mddb is None:
            return {"ok": False, "error": "memory database is not configured"}
        collection = (collection or "").strip()
        resp = await self.mddb._client.get(f"{self.mddb.base_url}/vector-stats")
        resp.raise_for_status()
        stats = resp.json().get("collections") or {}
        rows = []
        total_docs = total_missing = 0
        for name, v in sorted(stats.items()):
            if collection and name != collection:
                continue
            total = int(v.get("total_documents") or 0)
            embedded = int(v.get("embedded_documents") or 0)
            missing = max(0, total - embedded)
            total_docs += total
            total_missing += missing
            if missing:
                rows.append(f"{name}: {missing} missing of {total}")
        summary = {
            "ok": True,
            "collections": (len(stats) if not collection
                            else (1 if collection in stats else 0)),
            "total_documents": total_docs,
            "missing_vectors": total_missing,
        }
        if collection and collection not in stats:
            return {"ok": False, "error": f"no collection named {collection!r}"}
        if rows:
            summary["lagging"] = rows[:8]
            if len(rows) > 8:
                summary["lagging_truncated"] = len(rows) - 8
        else:
            summary["note"] = "all collections fully embedded"
        return summary

    async def ada_enroll_speaker(
        self,
        name: str,
        ha_person: str | None = None,
        display_name: str | None = None,
        confirmed: bool = False,
        who: str = "speaker",
    ) -> dict[str, Any]:
        """Enroll the current speaker's voice from buffered audio.

        Uses the unrecognized-speaker voice accrued across the session
        (or the last ~15s of audio if they were already identified) —
        no separate recording needed. Call this when the user asks to
        enroll their voice or when Ada offers enrollment.
        """
        # who='guest' absorbed chaba's guest_register (tools-merge-
        # meta-voice): a plain name registration for later admin
        # promotion — no voice buffer or speaker gating involved.
        if str(who or "speaker").strip().lower() == "guest":
            return await self.guest_register(str(name))
        speaker_session = _CALLER_SPEAKER_SESSION.get() or self.speaker_session
        if not ha_person:
            # Auto-resolve 'Name' -> person.<slug> so the enrollment maps to
            # the speaker's HA person (memory banks + actuation ACL follow)
            # even when the model doesn't pass ha_person explicitly.
            try:
                resolved = await self.context.ha_client.resolve_person(str(name))
                if resolved:
                    ha_person = resolved["entity_id"]
            except Exception:
                slug = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
                if slug:
                    candidate = f"person.{slug}"
                    try:
                        state = await self.context.ha_client.get_state(candidate)
                        if state.get("entity_id") == candidate:
                            ha_person = candidate
                    except Exception:
                        pass
        # Secondary-turn carve-out: while a non-owner voice is identified,
        # only an enrollment targeting the OWNER is allowed — and only if
        # the buffered voice doesn't score as the secondary speaker
        # (otherwise e.g. KK could enroll her own voice under 'Tony').
        if self._is_secondary_turn():
            target = ha_person or f"person.{_slug(name)}"
            if target != self._owner():
                raise PermissionError(
                    "Only the session owner can enroll voices on this "
                    "session — propose it to them aloud and let them ask "
                    "in their own voice.")
            cur = self._current_speaker()
            if cur and not confirmed:
                try:
                    buf_name, _buf_score = speaker_session.identify_buffer()
                    buf_person = (
                        speaker_session._identifier.get_ha_person(buf_name)
                        if buf_name else None)
                except Exception:
                    buf_person = None
                if buf_person and buf_person == cur:
                    raise PermissionError(
                        f"the buffered voice still matches {buf_name}. "
                        "If this is the owner being MISIDENTIFIED (their "
                        "stale print matches another enrolled speaker), "
                        "say so aloud, ask them to confirm the fix "
                        "out loud, then retry with confirmed=true — that "
                        "overwrites the stale profile. Otherwise the "
                        "identified speaker must stop talking first.")
        if speaker_session is None:
            return {
                "error": "speaker identification is not active on this session "
                "(speaker ID may be disabled or not configured)"
            }
        # force replaces a stale/poisoned profile — only ever allowed for
        # the session OWNER's own enrollment, and only after the user
        # explicitly confirms the fix (confirmed=true)
        owner = self._owner()
        target_person = ha_person or f"person.{_slug(name)}"
        force = bool(confirmed) and owner is not None and \
            target_person == owner
        try:
            result = speaker_session.enroll_from_buffer(
                str(name),
                ha_person=ha_person or None,
                display_name=display_name or None,
                force=force,
            )
            out = {
                "status": "enrolled",
                "name": result.get("name"),
                "ha_person": result.get("ha_person"),
                "display_name": result.get("display_name"),
                "duration_s": result.get("duration_s"),
            }
            if force:
                out["note"] = ("profile was force-replaced — if another "
                               "speaker still gets matched to you, they "
                               "may need to re-enroll.")
        except ValueError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.warning("voice enrollment failed: %s", exc)
            return {"error": f"enrollment failed: {exc}"}

    # -- Chaba guest tools (CHABA_MEMORY=1 instances only) ------------------
    # File-backed public memory under ~/.local/share/chaba/. No MDDB, no
    # embeddings, no bank registry — guests write to guests/<name>.yml and
    # promoted users to users/<name>.yml (+ a private file for their own
    # namespace). Identity comes from the websocket session, not the model.

    def _require_chaba(self) -> None:
        if self.chaba is None:
            raise PermissionError("guest tools are only available on CHABA_MEMORY=1 instances")

    async def guest_remember(self, key: str, text: str) -> dict[str, Any]:
        """Save a public memory under this visitor's declared name."""
        self._require_chaba()
        return self.chaba.remember(self.session_id, key, text)

    async def guest_remember_private(self, key: str, text: str) -> dict[str, Any]:
        """Save a private note — only for admin-promoted users."""
        self._require_chaba()
        return self.chaba.remember_private(self.session_id, key, text)

    async def guest_recall(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Search public guest memories (and own private notes for users)."""
        self._require_chaba()
        return {"hits": self.chaba.recall(query, session_id=self.session_id, limit=limit)}

    async def guest_register(self, name: str) -> dict[str, Any]:
        """Retired surface name (tools-merge-meta-voice) — kept as the
        direct call form of ada_enroll_speaker who='guest'; the alias shim
        adds who='guest' for execute()-routed calls."""
        self._require_chaba()
        return self.chaba.register_pending(name, session_id=self.session_id)

    # -- Miniapp/CMS page tools: one MDDB document per page in CMS_COLLECTION --
    # Meta carries the page contract the miniapp shell renders: slug, title,
    # format (markdown/html/yaml/slides), and the last-updated timestamp.

    @staticmethod
    def _cms_slug(slug: str) -> str:
        s = (slug or "").strip().lower().replace(" ", "-")
        if not _SLUG_RE.match(s):
            raise ValueError(
                f"invalid page slug {slug!r}: use 1-64 chars of a-z, 0-9, '-' or '_'"
            )
        return s

    @staticmethod
    def _cms_page_summary(doc: dict[str, Any]) -> dict[str, Any]:
        meta = doc.get("meta") or {}

        def first(name: str) -> str | None:
            v = meta.get(name)
            if isinstance(v, list) and v:
                return str(v[0])
            return str(v) if isinstance(v, str) else None

        page = {
            "slug": first("slug") or doc.get("key"),
            "title": first("title") or doc.get("key"),
            "format": first("format") or "markdown",
            "updated": first("updated"),
        }
        # Provenance passthrough — generated pages carry these; the CMS
        # viewer uses generated_by to offer a Regenerate control. Report
        # fields too: Ada needs summary/updated/freshness BEFORE deciding
        # to drill deeper (the report-first ritual).
        for name in ("generated_by", "report_role", "parent",
                     "summary", "domain", "fresh_for", "confidence",
                     "supersedes", "timeline"):
            v = first(name)
            if v:
                page[name] = v
        links = meta.get("links")
        if isinstance(links, list) and links:
            page["links"] = [str(x) for x in links]
        children = meta.get("children")
        if isinstance(children, list) and children:
            page["children"] = [str(c) for c in children]
        return page

    # -- cms_read: the merged read side (tools-merge-cms) -------------------
    # Absorbs cms_list_pages/cms_get_page/cms_verify_page — the absorbed
    # methods below keep their names as the per-action implementations;
    # only the declared surface changed (aliases route old call sites).

    async def cms_read(
        self, action: str, key: str = "", slug: str = "",
        lang: str = "en", limit: int = 50,
    ) -> Any:
        """Read the miniapp CMS. action='list' returns every page's
        slug/title/format/updated; 'get' returns one page's full content
        by key (the page slug) + lang; 'verify' re-reads a page and checks
        its content parses for the declared format. Free reads — no
        confirmation."""
        action = (action or "").strip().lower()
        if action not in ("get", "list", "verify"):
            raise ValueError(
                f"invalid action {action!r}: expected get|list|verify")
        if action == "list":
            return await self.cms_list_pages(limit=limit)
        k = self._cms_slug(key or slug)
        if action == "get":
            return await self.cms_get_page(k, lang=lang)
        return await self.cms_verify_page(k)

    async def cms_list_pages(self, limit: int = 50) -> list[dict[str, Any]]:
        """List pages in the miniapp CMS collection, grouped by slug with
        the language variants present (lang is a per-doc field in MDDB)."""
        docs = await self.mddb.search_documents(
            CMS_COLLECTION, filter_meta={"kind": ["page"]}, limit=limit
        )
        by_slug: dict[str, dict[str, Any]] = {}
        for d in docs:
            page = self._cms_page_summary(d)
            slug = page["slug"]
            dlang = d.get("lang") or "en"
            if slug in by_slug:
                by_slug[slug]["langs"].add(dlang)
                by_slug[slug].setdefault("titles", {})[dlang] = page["title"]
                continue
            page["langs"] = {dlang}
            page["titles"] = {dlang: page["title"]}
            by_slug[slug] = page
        for p in by_slug.values():
            p["langs"] = sorted(p["langs"])
        return list(by_slug.values())

    async def cms_get_page(
        self, slug: str, lang: str = "en"
    ) -> dict[str, Any] | None:
        """Fetch one page's content by slug + language; falls back to 'en'
        when the requested variant doesn't exist."""
        key = self._cms_slug(slug)
        lang = (lang or "en").strip().lower()
        doc = await self.mddb.get_document(CMS_COLLECTION, key, lang)
        fallback = False
        if not doc and lang != "en":
            doc = await self.mddb.get_document(CMS_COLLECTION, key, "en")
            fallback = bool(doc)
        if not doc:
            return None
        page = self._cms_page_summary(doc)
        page["content"] = doc.get("contentMd") or doc.get("content") or ""
        page["lang"] = doc.get("lang") or "en"
        if fallback:
            page["fallback"] = True
        return page

    async def cms_publish_page(
        self,
        slug: str,
        title: str,
        content: str,
        format: str = "markdown",
        lang: str = "en",
        summary: str = "",
        domain: str = "",
        fresh_for: str = "",
        links: str = "",
        supersedes: str = "",
        confidence: str = "",
    ) -> dict[str, Any]:
        """Create or update a miniapp page. Upserts by (slug, lang) — 'en'
        and 'th' variants of the same slug coexist; the viewer toggles.
        summary/domain/fresh_for feed reports-index: a one-line brief Ada
        can answer from without cms_get_page, the grouping domain, and a
        staleness hint ('1h', '6h', '1d') the index flags when exceeded."""
        slug = self._cms_slug(slug)
        fmt = (format or "markdown").strip().lower()
        lang = (lang or "en").strip().lower()
        # The model tends to bake the language into the slug
        # (gold-report-th + Thai content stored as 'en') — split it.
        if lang == "en" and slug.endswith("-th"):
            slug = slug[:-3]
            lang = "th"
        if lang not in ("en", "th"):
            raise ValueError(
                f"invalid lang {lang!r}: expected 'en' or 'th'")
        if fmt not in CMS_FORMATS:
            raise ValueError(
                f"invalid format {format!r}: expected one of {sorted(CMS_FORMATS)}"
            )
        if not (title or "").strip():
            raise ValueError("title is required")
        now = datetime.now(timezone.utc)
        updated = now.isoformat(timespec="seconds")
        # Merge the existing doc's meta instead of replacing it — generated
        # pages carry provenance (generated_by/sources/parent/children) and
        # memory-schema fields a wholesale replace would silently strip.
        try:
            existing = await self.mddb.get_document(CMS_COLLECTION, slug, lang)
        except Exception:
            existing = None
        if not isinstance(existing, dict):
            existing = None
        meta = {
            k: (v if isinstance(v, list) else [v])
            for k, v in ((existing or {}).get("meta") or {}).items()
        }
        meta.setdefault("bank", ["cms"])
        meta.setdefault("scope", ["tony"])
        meta.setdefault("status", ["active"])
        meta.setdefault("source", ["api"])
        meta["written_by"] = ["cms_publish_page"]
        meta.setdefault("subject", [slug])
        meta.setdefault("attribute", ["page"])
        meta.setdefault("valid_from", [now.date().isoformat()])
        meta["last_verified"] = [now.date().isoformat()]
        meta.update({
            "kind": ["page"],
            "slug": [slug],
            "title": [title.strip()],
            "format": [fmt],
            "lang": [lang],
            "updated": [updated],
            "instance": [self._instance_id or "ada"],
        })
        # Report-structure fields — summary is the one-line brief the
        # reports-index shows so Ada answers without re-reading the page;
        # fresh_for is a staleness hint the index renders as stale/FRESH.
        if summary.strip():
            meta["summary"] = [summary.strip()[:240]]
        if domain.strip():
            meta["domain"] = [domain.strip().lower()]
        if fresh_for.strip():
            meta["fresh_for"] = [fresh_for.strip()]
        # Linking — comma-separated slugs this report derives from or
        # supersedes. links = parent/child/source references; supersedes
        # marks the older report this one replaces (staleness signal).
        if isinstance(links, str) and links.strip():
            meta["links"] = [x.strip() for x in links.split(",") if x.strip()]
        elif isinstance(links, list) and links:
            meta["links"] = [str(x).strip() for x in links if str(x).strip()]
        if supersedes.strip():
            meta["supersedes"] = [supersedes.strip()]
        if confidence.strip():
            meta["confidence"] = [confidence.strip()]
        # Timeline — append-only audit of what changed and when. Cap at 40
        # entries; visible via cms_get_page meta and the report pages.
        tl = [x for x in meta.get("timeline", []) if isinstance(x, str)]
        tl.append(f"{updated[:16]} published: {title.strip()[:80]}")
        meta["timeline"] = tl[-40:]
        # Post-merge contract check — the gate already rejects missing
        # model args, so gaps here mean an internal caller republished a
        # legacy page; warn rather than break the merge path.
        meta_check = validate_report_meta(meta)
        if not meta_check["ok"] or meta_check["warnings"]:
            logger.warning(
                "cms_publish_page %s meta_contract gaps: %s", slug, meta_check)
        result = await self.mddb.add_document(
            CMS_COLLECTION,
            slug,
            lang,
            content,
            meta=meta,
            durable=True, tool="cms_publish_page",
            session_id=self.session_id,
        )
        if write_outbox.is_queued(result):
            self._log_session_event(
                "write_queued", tool="cms_publish_page", key=slug)
            return {
                "status": "queued",
                "slug": slug,
                "note": ("mddb is unreachable — the page is parked in the "
                         "local write outbox and will publish automatically "
                         "within the retry window; the user will hear about "
                         "it if it ultimately fails. Do NOT describe it as "
                         "published yet."),
            }
        if result is None:
            return {
                "status": "error",
                "error": "mddb write failed",
                "slug": slug,
                "note": ("This is a storage failure, NOT a confirmation "
                         "problem — do not ask the user to re-confirm. "
                         "Report the failure plainly and suggest checking "
                         "the mddb service."),
            }
        # Give the model the REAL URLs — she has invented /cms/<slug> paths
        # on tony-dell before (404). view_url is the CMS viewer; cast_url adds
        # the api key so a vcast display can render it without a stored key.
        base = os.environ.get(
            "ADA_CMS_BASE", "https://idc01.taila0626a.ts.net/cms/")
        view_url = f"{base}#/{slug}"
        key = os.environ.get("ADA_API_KEY", "")
        cast_url = f"{base}?api_key={key}#/{slug}" if key else view_url
        # Refresh the reports index in the background — cheap page Ada reads
        # instead of re-querying per report.
        asyncio.create_task(self._cms_reports_index())
        out = {
            "status": "published",
            "slug": slug,
            "title": title.strip(),
            "format": fmt,
            "lang": lang,
            "updated": updated,
            "view_url": view_url,
            "cast_url": cast_url,
            "note": "To show this page on a display: cast_to_screen("
                    "action='nav', url=<cast_url>) — only after this result "
                    "shows status=published.",
        }
        if not meta_check["ok"] or meta_check["warnings"]:
            out["meta_contract"] = meta_check
        return out

    async def cms_delete_page(self, slug: str) -> dict[str, Any]:
        """Delete a miniapp page by slug."""
        slug = self._cms_slug(slug)
        result = await self.mddb.delete_document(
            CMS_COLLECTION, slug,
            durable=True, tool="cms_delete_page",
            session_id=self.session_id)
        if write_outbox.is_queued(result):
            self._log_session_event(
                "write_queued", tool="cms_delete_page", key=slug)
            return {"status": "queued", "slug": slug,
                    "note": ("mddb is unreachable — the delete is parked in "
                             "the local write outbox and will land within "
                             "the retry window.")}
        if result is None:
            return {"status": "not_found", "slug": slug}
        asyncio.create_task(self._cms_reports_index())
        return {"status": "deleted", "slug": slug}

    async def cms_note_update(
        self, slug: str, note: str, lang: str = "en", summary: str = ""
    ) -> dict[str, Any]:
        """Append a timeline note to an existing page — the lightweight
        'this report learned something new' path. Unlike cms_publish_page it
        merges content (adds a Timeline section entry) and never replaces,
        so it needs no confirmation. Optionally refreshes the page's
        one-line summary shown in reports-index."""
        slug = self._cms_slug(slug)
        lang = (lang or "en").strip().lower()
        if not (note or "").strip():
            raise ValueError("note is required")
        doc = await self.mddb.get_document(CMS_COLLECTION, slug, lang)
        if doc is None and lang != "en":
            doc = await self.mddb.get_document(CMS_COLLECTION, slug, "en")
        if not isinstance(doc, dict):
            return {"status": "not_found", "slug": slug,
                    "error": "no such page — use cms_publish_page to create it"}
        meta = {
            k: (v if isinstance(v, list) else [v])
            for k, v in (doc.get("meta") or {}).items()
        }
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = f"{now[:16]} {note.strip()[:200]}"
        tl = [x for x in meta.get("timeline", []) if isinstance(x, str)]
        tl.append(entry)
        meta["timeline"] = tl[-40:]
        meta["updated"] = [now]
        meta["written_by"] = ["cms_note_update"]
        if summary.strip():
            meta["summary"] = [summary.strip()[:240]]
        # Contract check is warn-only here — note is a merge-only append;
        # gaps mean the underlying page predates the contract.
        meta_check = validate_report_meta(meta)
        if not meta_check["ok"] or meta_check["warnings"]:
            logger.warning(
                "cms_note_update %s meta_contract gaps: %s", slug, meta_check)
        content = doc.get("contentMd") or ""
        marker = "## Timeline"
        line = f"- {entry}"
        if marker in content:
            content = content.rstrip() + "\n" + line + "\n"
        else:
            content = content.rstrip() + f"\n\n{marker}\n\n{line}\n"
        result = await self.mddb.add_document(
            CMS_COLLECTION, slug, doc.get("lang") or lang, content, meta=meta,
            durable=True, tool="cms_note_update",
            session_id=self.session_id)
        if write_outbox.is_queued(result):
            self._log_session_event(
                "write_queued", tool="cms_note_update", key=slug)
            return {"status": "queued", "slug": slug,
                    "note": ("mddb is unreachable — the note is parked in "
                             "the local write outbox and will land within "
                             "the retry window.")}
        if result is None:
            return {"status": "error", "error": "mddb write failed", "slug": slug}
        asyncio.create_task(self._cms_reports_index())
        # Diff so Ada can report what changed: note text + new summary
        # when refreshed. Times are the time-of-event stamps already in
        # the timeline entries.
        diff = {"note": entry}
        if summary.strip():
            diff["summary"] = summary.strip()[:240]
        out = {"status": "noted", "slug": slug,
               "timeline_entries": len(meta["timeline"]),
               "updated": now, "diff": diff}
        if not meta_check["ok"] or meta_check["warnings"]:
            out["meta_contract"] = meta_check
        return out

    # -- cms_edit: the merged edit side (tools-merge-cms) -------------------
    # Absorbs cms_note_update/cms_delete_page/cms_automation — per-action
    # dispatch onto the absorbed methods below; 'op' carries the automation
    # registry action for action='automate' (list|get|set|enable|disable|run).

    async def cms_edit(
        self,
        action: str,
        slug: str = "",
        note: str = "",
        summary: str = "",
        lang: str = "en",
        op: str = "",
        enabled: bool | None = None,
        interval_min: int | None = None,
        run_now: bool | None = None,
        max_items: int | None = None,
        since_hours: int | None = None,
        require: str | None = None,
        feeds: list | None = None,
        langs: list | None = None,
        parent: str | None = None,
        children: list | None = None,
    ) -> dict[str, Any]:
        """Edit-side CMS ops. action='note' appends a timeline note to an
        existing page (ungated); 'delete' removes the page (confirmed);
        'automate' drives the page's automation registry — op carries the
        registry action (list/get are free reads; set/enable/disable/run
        are confirmed writes)."""
        action = (action or "").strip().lower()
        if action == "note":
            return await self.cms_note_update(
                slug, note, lang=lang, summary=summary)
        if action == "delete":
            return await self.cms_delete_page(slug)
        if action == "automate":
            return await self.cms_automation(
                op, slug=slug, enabled=enabled, interval_min=interval_min,
                run_now=run_now, max_items=max_items,
                since_hours=since_hours, require=require, feeds=feeds,
                langs=langs, parent=parent, children=children)
        raise ValueError(
            f"invalid action {action!r}: expected note|delete|automate")

    # -- _cms_edit_sections: page/section ops (PWA edit drawer; not a
    #    declared tool — the model-facing cms_edit above carries the
    #    note/delete/automate actions) -------------------------------------
    # Ops: add (append/insert text), update (replace section or whole page),
    # improve/expand/consolidate (LLM transforms), split (section -> new
    # page), merge (other page's content folded in). Sections are addressed
    # by their '## heading' text — stable across edits, unlike line numbers.
    CMS_EDIT_OPS = ("add", "update", "improve", "expand", "consolidate",
                    "split", "merge")
    _CMS_LLM_OPS = ("improve", "expand", "consolidate")

    @staticmethod
    def _cms_sections(content: str) -> list[tuple[str, int, int]]:
        """[(heading, start, end)] — sections = '## ' blocks; 'preamble' is
        text before the first heading."""
        lines = content.split("\n")
        marks = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
        out = []
        if not marks:
            return [("preamble", 0, len(lines))]
        if marks[0] > 0:
            out.append(("preamble", 0, marks[0]))
        for j, m in enumerate(marks):
            end = marks[j + 1] if j + 1 < len(marks) else len(lines)
            out.append((lines[m][3:].strip(), m, end))
        return out

    async def _cms_llm(self, prompt: str) -> str:
        from google import genai
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        resp = await client.aio.models.generate_content(
            model=os.environ.get("ADA_CMS_EDIT_MODEL",
                                 "gemini-3.5-flash-lite"),
            contents=prompt)
        return (resp.text or "").strip()

    async def _cms_edit_sections(
        self, slug: str, op: str, text: str = "", section: str = "",
        instruction: str = "", lang: str = "en", target_slug: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        """Structured page edit. section='' means the whole page. LLM ops
        take 'instruction' (what to change); mechanical ops take 'text'
        (new content) or 'target_slug' (split/merge)."""
        slug = self._cms_slug(slug)
        op = (op or "").strip().lower()
        if op not in self.CMS_EDIT_OPS:
            raise ValueError(
                f"invalid op {op!r}: expected one of {self.CMS_EDIT_OPS}")
        doc = await self.mddb.get_document(CMS_COLLECTION, slug, lang)
        if not isinstance(doc, dict):
            return {"status": "not_found", "slug": slug}
        content = doc.get("contentMd") or ""
        sections = self._cms_sections(content)
        lines = content.split("\n")

        target = None
        if section.strip():
            want = section.strip().lower()
            for name, s, e in sections:
                if name.lower() == want:
                    target = (name, s, e)
                    break
            if target is None:
                return {"status": "error", "slug": slug,
                        "error": f"no section {section!r} — "
                                 f"sections: {[n for n, _, _ in sections]}"}

        new_content, created = None, None
        if op == "add":
            block = text.strip()
            if not block:
                raise ValueError("add requires text")
            if target:
                # insert before the next section starts (end of target's block)
                lines.insert(target[2], "\n" + block if block.startswith("#")
                             else block)
                new_content = "\n".join(lines)
            else:
                new_content = content.rstrip() + "\n\n" + block + "\n"
        elif op == "update":
            block = text.strip()
            if not block:
                raise ValueError("update requires text")
            if target:
                name, s, e = target
                new = lines[:s + 1] + block.split("\n") + lines[e:]
                new_content = "\n".join(new)
            else:
                new_content = block
        elif op in self._CMS_LLM_OPS:
            scope = ("\n\n".join(lines[target[1]:target[2]])
                     if target else content)
            if not scope.strip():
                return {"status": "error", "error": "nothing to transform"}
            instr = instruction.strip() or (
                {"improve": "tighten the writing, keep every fact",
                 "expand": "add depth and detail, keep every fact",
                 "consolidate": "merge overlapping/duplicate sections"}[op])
            out = await self._cms_llm(
                f"Rewrite this {'section' if target else 'page'} — {instr}.\n"
                "Return ONLY the new markdown, no commentary.\n\n" + scope)
            if not out:
                return {"status": "error", "error": "llm returned empty"}
            if target:
                name, s, e = target
                out_lines = out.split("\n")
                if out_lines and not out_lines[0].startswith("## "):
                    out_lines.insert(0, f"## {name}")
                new_content = "\n".join(lines[:s] + out_lines + lines[e:])
            else:
                new_content = out
        elif op == "split":
            if not target:
                raise ValueError("split requires a section")
            tslug = self._cms_slug(target_slug or "")
            name, s, e = target
            moved = "\n".join(lines[s:e])
            pub = await self.cms_publish_page(
                tslug, title.strip() or name, moved, lang=lang,
                links=slug)
            if pub.get("status") != "published":
                return {"status": "error", "error": "split publish failed",
                        "detail": pub}
            created = tslug
            new_content = "\n".join(
                lines[:s]
                + [f"## {name}", f"*Moved to [{name}](/#{tslug}).*"]
                + lines[e:])
        elif op == "merge":
            tslug = self._cms_slug(target_slug or "")
            other = await self.mddb.get_document(CMS_COLLECTION, tslug, lang)
            if not isinstance(other, dict):
                return {"status": "error", "error": f"no page {tslug!r}"}
            body = (other.get("contentMd") or "").strip()
            new_content = content.rstrip() + "\n\n" + body + "\n"
            # mark the absorbed page superseded
            ometa = {k: (v if isinstance(v, list) else [v])
                     for k, v in (other.get("meta") or {}).items()}
            ometa["status"] = ["superseded"]
            ometa["superseded_by"] = [slug]
            await self.mddb.add_document(
                CMS_COLLECTION, tslug, lang, body,
                meta=ometa)
        # persist via publish so meta/timeline/index all stay honest
        meta_title = title.strip() or next(
            iter((doc.get("meta") or {}).get("title") or []), slug)
        res = await self.cms_publish_page(
            slug, str(meta_title), new_content, lang=lang)
        res["op"] = op
        if target:
            res["section"] = target[0]
        if created:
            res["created"] = created
        return res

    async def _cms_reports_index(self) -> None:
        """Regenerate the reports-index page — one row per report/tagged
        page with slug, domain, one-line summary, updated, and a stale flag
        when 'updated' exceeds its fresh_for hint. Ada reads THIS to answer
        'what reports exist / what changed' without per-page cms_get_page."""
        try:
            docs = await self.mddb.search_documents(
                CMS_COLLECTION, limit=200)
        except Exception as exc:
            logger.warning("reports-index list failed: %s", exc)
            return
        now = datetime.now(timezone.utc)
        def stale(meta: dict) -> bool:
            ff = (meta.get("fresh_for") or [""])[0]
            upd = (meta.get("updated") or [""])[0]
            secs = fresh_for_seconds(ff)
            if secs is None or not upd:
                return False
            try:
                dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
                return (now - dt).total_seconds() > secs
            except Exception:
                return False
        rows = []
        for d in docs:
            meta = d.get("meta") or {}
            kind = (meta.get("kind") or [""])[0]
            if kind not in ("report", "page"):
                continue
            slug = (meta.get("slug") or [d.get("key") or "?"])[0]
            if slug == "reports-index":
                continue
            rows.append({
                "slug": slug,
                "title": (meta.get("title") or [slug])[0],
                "domain": (meta.get("domain") or ["-"])[0],
                "summary": (meta.get("summary") or [""])[0],
                "updated": (meta.get("updated") or ["-"])[0][:16],
                "fresh": (meta.get("fresh_for") or ["-"])[0],
                "stale": stale(meta),
            })
        rows.sort(key=lambda r: r["updated"], reverse=True)
        lines = [f"# Reports index — {now:%Y-%m-%d %H:%M}Z\n",
                 "Brief summaries of every report page — read the linked page",
                 "only when the summary isn't enough.\n",
                 "| slug | domain | updated | fresh | summary |",
                 "|---|---|---|---|---|"]
        for r in rows[:60]:
            flag = " ⚠STALE" if r["stale"] else ""
            summ = (r["summary"] or r["title"])[:80]
            lines.append(f"| {r['slug']} | {r['domain']} | {r['updated']}"
                         f"{flag} | {r['fresh']} | {summ} |")
        md = "\n".join(lines)
        await self.mddb.add_document(
            CMS_COLLECTION, "reports-index", "en", md,
            meta={"kind": ["page"], "slug": ["reports-index"],
                  "title": [f"Reports index — {now:%Y-%m-%d %H:%M}Z"],
                  "format": ["markdown"], "domain": ["meta"],
                  "summary": ["Auto-generated index of report pages — "
                              "slug, domain, staleness, one-line brief."],
                  "fresh_for": ["6h"], "confidence": ["high"],
                  "updated": [now.isoformat(timespec="seconds")],
                  "timeline": [f"{now.isoformat(timespec='seconds')[:16]} "
                               "index regenerated"],
                  "instance": [self._instance_id or "ada"],
                  "written_by": ["_cms_reports_index"]})

    # -- CMS automation registry (ada-cms-automation) -----------------------
    # One doc per page slug, contentMd = JSON of the page's switches/knobs
    # and the worker's write-back state. The flood-news worker merges this
    # over its seed config, so Ada flips switches by editing the doc here.

    @staticmethod
    def _cms_automation_cfg(doc: dict[str, Any] | None) -> dict[str, Any]:
        if not doc:
            return {}
        try:
            cfg = json.loads(doc.get("contentMd") or "{}")
        except (TypeError, ValueError):
            return {}
        return cfg if isinstance(cfg, dict) else {}

    async def _cms_automation_doc(self, slug: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        doc = await self.mddb.get_document(CMS_AUTOMATION_COLLECTION, slug, "en")
        return doc, self._cms_automation_cfg(doc)

    async def _cms_automation_save(self, slug: str, cfg: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        await self.mddb.add_document(
            CMS_AUTOMATION_COLLECTION,
            slug,
            "en",
            json.dumps(cfg, ensure_ascii=False, indent=2),
            meta={
                "kind": ["automation-config"], "bank": ["cms"],
                "scope": ["tony"], "status": ["active"], "source": ["api"],
                "written_by": ["ada:cms_automation"], "subject": [slug],
                "attribute": ["automation"], "slug": [slug],
                "title": [f"CMS automation: {slug}"], "format": ["json"],
                "lang": ["en"], "updated": [now.isoformat(timespec="seconds")],
                "last_verified": [now.date().isoformat()],
            },
        )

    @staticmethod
    def _cms_automation_state(cfg: dict[str, Any], slug: str) -> dict[str, Any]:
        return {
            "slug": slug,
            "enabled": cfg.get("enabled", True),
            "interval_min": cfg.get("interval_min", 0),
            "run_now": cfg.get("run_now", False),
            "last_run": cfg.get("last_run"),
            "last_status": cfg.get("last_status"),
            "last_count": cfg.get("last_count"),
            "last_error": cfg.get("last_error"),
            "last_duration_s": cfg.get("last_duration_s"),
        }

    def _cms_automation_knobs(
        self, slug: str, *, enabled=None, interval_min=None, run_now=None,
        max_items=None, since_hours=None, require=None, feeds=None,
        langs=None, parent=None, children=None,
    ) -> dict[str, Any]:
        """Validate and collect the writable knobs. Raises ValueError on
        bad input; returns only the keys the caller actually passed."""
        out: dict[str, Any] = {}
        if enabled is not None:
            out["enabled"] = bool(enabled)
        if run_now is not None:
            out["run_now"] = bool(run_now)
        if interval_min is not None:
            try:
                v = int(interval_min)
            except (TypeError, ValueError):
                raise ValueError("interval_min must be an integer")
            if not 0 <= v <= 7 * 24 * 60:
                raise ValueError("interval_min must be 0-10080 (max 7 days)")
            out["interval_min"] = v
        if max_items is not None:
            try:
                v = int(max_items)
            except (TypeError, ValueError):
                raise ValueError("max_items must be an integer")
            if not 1 <= v <= 50:
                raise ValueError("max_items must be 1-50")
            out["max_items"] = v
        if since_hours is not None:
            try:
                v = int(since_hours)
            except (TypeError, ValueError):
                raise ValueError("since_hours must be an integer")
            if not 1 <= v <= 720:
                raise ValueError("since_hours must be 1-720 (max 30 days)")
            out["since_hours"] = v
        if require is not None:
            require = str(require).strip()
            if require:
                if len(require) > 200:
                    raise ValueError("require regex too long (max 200 chars)")
                try:
                    re.compile(require, re.IGNORECASE)
                except re.error as exc:
                    raise ValueError(f"invalid require regex: {exc}")
            out["require"] = require
        if feeds is not None:
            if not isinstance(feeds, list):
                raise ValueError(
                    "feeds must be a list of [name, url] pairs")
            norm = []
            for f in feeds:
                if (not isinstance(f, (list, tuple)) or len(f) != 2):
                    raise ValueError("each feed must be a [name, url] pair")
                fname, url = str(f[0]).strip().lower(), str(f[1]).strip()
                if not _SLUG_RE.match(fname):
                    raise ValueError(f"invalid feed name {fname!r}")
                if not re.match(r"https?://", url):
                    raise ValueError(f"feed url must be http(s): {url!r}")
                norm.append([fname, url])
            if not norm:
                raise ValueError("feeds list cannot be empty")
            out["feeds"] = norm
        if langs is not None:
            if not isinstance(langs, list) or not langs:
                raise ValueError("langs must be a non-empty list")
            bad = [l for l in langs if str(l) not in ("en", "th")]
            if bad:
                raise ValueError(
                    f"invalid langs {bad!r}: supported are 'en' and 'th'")
            out["langs"] = [str(l) for l in langs]
        if parent is not None:
            out["parent"] = self._cms_slug(parent) if str(parent).strip() else None
        if children is not None:
            if not isinstance(children, list):
                raise ValueError("children must be a list of page slugs")
            out["children"] = [self._cms_slug(str(c)) for c in children]
        return out

    async def cms_automation(
        self,
        action: str,
        slug: str = "",
        enabled: bool | None = None,
        interval_min: int | None = None,
        run_now: bool | None = None,
        max_items: int | None = None,
        since_hours: int | None = None,
        require: str | None = None,
        feeds: list | None = None,
        langs: list | None = None,
        parent: str | None = None,
        children: list | None = None,
    ) -> dict[str, Any]:
        """Inspect and adjust a miniapp page's automation switches and knobs.

        Each generated page has a registry doc in ada-cms-automation that the
        scheduled news worker honors: 'enabled' pauses updates, 'interval_min'
        throttles them, 'run_now' queues a one-shot regeneration, 'feeds' and
        'require' control what is fetched, 'parent'/'children' link reports
        into a hierarchy. Actions: list (all pages), get (one page),
        set/enable/disable/run (writes — confirmation-gated).
        """
        action = (action or "").strip().lower()
        if action == "list":
            docs = await self.mddb.search_documents(
                CMS_AUTOMATION_COLLECTION, limit=200)
            pages = []
            for d in docs or []:
                slug = d.get("key") or ""
                pages.append(
                    self._cms_automation_state(
                        self._cms_automation_cfg(d), slug))
            pages.sort(key=lambda p: p["slug"])
            return {"pages": pages, "count": len(pages)}

        key = self._cms_slug(slug)
        doc, cfg = await self._cms_automation_doc(key)

        if action == "get":
            if not doc:
                return {"status": "not_found", "slug": key,
                        "note": "no automation config yet — the worker "
                                "creates one on its first run"}
            return {"status": "ok", "config": cfg,
                    **self._cms_automation_state(cfg, key)}

        # Write actions: merge validated knobs into the stored config.
        knobs = self._cms_automation_knobs(
            key, enabled=enabled, interval_min=interval_min,
            run_now=run_now, max_items=max_items, since_hours=since_hours,
            require=require, feeds=feeds, langs=langs, parent=parent,
            children=children)
        if action == "enable":
            knobs["enabled"] = True
        elif action == "disable":
            knobs["enabled"] = False
        elif action == "run":
            knobs["run_now"] = True
        elif action != "set":
            raise ValueError(
                f"invalid action {action!r}: expected list/get/set/"
                "enable/disable/run")
        if not knobs:
            raise ValueError(f"action {action!r}: nothing to change")
        cfg.update(knobs)
        await self._cms_automation_save(key, cfg)
        out = {"status": "updated", "slug": key, "changes": knobs}
        if action == "run":
            out["queued"] = True
            out["note"] = ("regeneration queued — the worker picks it up on "
                           "its next pass and clears run_now when done")
        return out

    async def cms_verify_page(self, slug: str) -> dict[str, Any]:
        """Re-read a page and check its content parses for its declared format.

        The assistant can't see the rendered miniapp — this is the feedback
        loop after publish: returns a structural summary of what the viewer
        renders, or the parse error to fix.
        """
        key = self._cms_slug(slug)
        doc = await self.mddb.get_document(CMS_COLLECTION, key, "en")
        if not isinstance(doc, dict):
            return {"ok": False, "status": "not_found", "slug": slug}
        page = self._cms_page_summary(doc)
        content = doc.get("contentMd") or doc.get("content") or ""
        fmt = page["format"]
        report: dict[str, Any] = {
            "ok": True,
            "slug": page["slug"],
            "title": page["title"],
            "format": fmt,
            "chars": len(content),
            # Report meta contract (ssot.apps.ada-cms-reports.yml) — ok
            # stays about content parse; meta_contract reports the field
            # check Ada can act on (republish with the missing fields).
            "meta_contract": validate_report_meta(doc.get("meta") or {}),
        }
        if not content.strip():
            report.update(ok=False, error="page content is empty")
            return report
        if fmt == "yaml":
            try:
                import yaml
                data = yaml.safe_load(content)
            except ImportError:
                report["summary"] = {"note": "pyyaml not installed — parse check skipped"}
                return report
            except Exception as exc:
                report.update(
                    ok=False,
                    error=f"yaml parse error: {str(exc).splitlines()[0]}",
                )
                return report
            if not isinstance(data, dict):
                report.update(
                    ok=False,
                    error="yaml page must be a mapping (title/sections/items)",
                )
                return report
            sections = data.get("sections")
            report["summary"] = {
                "title": data.get("title"),
                "subtitle": data.get("subtitle"),
                "section_count": len(sections) if isinstance(sections, list) else 0,
                "sections": [
                    {
                        "label": (s or {}).get("label") or (s or {}).get("title"),
                        "items": len((s or {}).get("items") or []),
                    }
                    for s in sections
                ] if isinstance(sections, list) else [],
            }
        elif fmt == "slides":
            slides = [
                s for s in re.split(r"^---+\s*$", content, flags=re.M) if s.strip()
            ]
            report["summary"] = {"slide_count": len(slides)}
        elif fmt == "html":
            title = re.search(r"<title[^>]*>(.*?)</title>", content, re.I | re.S)
            report["summary"] = {
                "title": title.group(1).strip() if title else None,
                "has_doctype": content.lstrip().lower().startswith("<!doctype"),
                "script_tags": len(re.findall(r"<script\b", content, re.I)),
            }
        else:  # markdown — validate fenced rich blocks too
            headings = [
                ln.strip() for ln in content.splitlines() if ln.lstrip().startswith("#")
            ]
            report["summary"] = {"headings": headings[:20]}
            blocks = re.findall(
                r"```(chart3?|mermaid|media)\s*\n(.*?)```", content, re.S
            )
            block_report = []
            for kind, body in blocks:
                entry: dict[str, Any] = {"type": kind}
                if kind == "mermaid":
                    entry["lines"] = len(body.splitlines())
                else:
                    try:
                        import yaml
                        yaml.safe_load(body)
                        entry["yaml_ok"] = True
                    except Exception as exc:
                        entry["yaml_ok"] = False
                        entry["error"] = str(exc).splitlines()[0]
                        report["ok"] = False
                block_report.append(entry)
            if block_report:
                report["summary"]["blocks"] = block_report
        return report

    # -- YouTube -> TV casting (yt-live shim on tony-dell) --

    @staticmethod
    def _yt_api(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        import urllib.request
        base = os.environ.get("YT_LIVE_API", "http://tony-dell:8791")
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            base + path, data=data,
            headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    async def yt(
        self,
        action: str = "status",
        query: str = "",
        url: str = "",
        language: str = "th",
    ) -> dict[str, Any]:
        """YouTube surface — tools-merge-yt (2026-10-05) consolidated
        yt_cast / yt_cast_status / yt_cast_stop / yt_transcript into one
        action= tool. Ungated: cast/stop actuate the TV but the old names
        were never confirm-gated; status/transcript are reads."""
        action = (action or "status").strip().lower()
        if action == "cast":
            return await self.yt_cast(query=query or url, language=language)
        if action == "status":
            return await self.yt_cast_status()
        if action == "stop":
            return await self.yt_cast_stop()
        if action == "transcript":
            return await self.yt_transcript(url=url or query,
                                            language=language)
        raise ValueError(
            f"invalid action {action!r}: expected cast|status|stop|transcript")

    async def yt_cast(self, query: str, language: str = "th") -> dict[str, Any]:
        """Cast a YouTube video to the living-room TV with translated
        subtitles — or a direct media file URL (.mp4/.m4v/.webm/.mkv/.mp3/
        .m4a/.m3u8), which plays instantly without transcoding (e.g. the
        dubbed demos under https://tony-dell.taila0626a.ts.net/apps/yt-live/).
        `query` is a YouTube URL, media file URL, or a search phrase — prefer
        the video title plus channel name for accuracy. TV ONLY — if the user
        names a numbered screen, use cast_to_screen(action='play') instead;
        vcast displays auto-embed YouTube URLs."""
        import asyncio
        return await asyncio.to_thread(
            self._yt_api, "/cast", {"q": query, "lang": language})

    async def yt_cast_status(self) -> dict[str, Any]:
        """Progress of the current YouTube cast (transcode state, segments)."""
        import asyncio
        return await asyncio.to_thread(self._yt_api, "/status")

    async def yt_cast_stop(self) -> dict[str, Any]:
        """Stop the currently casting YouTube video on the TV."""
        import asyncio
        return await asyncio.to_thread(self._yt_api, "/stop", {})

    # -- YouTube transcript (yt-dlp on mn01 — Thai news sites block scrapers,
    #    YouTube auto-captions are the open lane) --

    # -- CCTV peek: single frame via go2rtc on tony-dell, saved to the HA
    #    /local/ static dir so the TV/vcast browsers can load it without auth --

    _CCTV_CAMS = {
        # go2rtc stream -> HA camera / friendly label (tony-dell :1984)
        "coffee corner": "ip_cam_65_hd", "coffee": "ip_cam_65_hd",
        "c201": "xiaomi_c201_hd", "xiaomi c201": "xiaomi_c201_hd",
        "c100": "xiaomi_c100_hd", "xiaomi c100": "xiaomi_c100_hd",
        "ip_cam_65": "ip_cam_65_hd", "ip_cam_65_hd": "ip_cam_65_hd",
        "ip_cam_65_low": "ip_cam_65_low",
        "xiaomi_c201": "xiaomi_c201_hd", "xiaomi_c201_hd": "xiaomi_c201_hd",
        "xiaomi_c100": "xiaomi_c100_hd", "xiaomi_c100_hd": "xiaomi_c100_hd",
        "xiaomi_c201_sd": "xiaomi_c201", "xiaomi_c100_sd": "xiaomi_c100",
    }
    _YT_CAMS = {
        # YouTube live "cameras" — a single frame is grabbed server-side
        # by ~/.local/bin/yt-frame.sh on ADA_CCTV_SSH (yt-dlp + ffmpeg),
        # published as a relay asset, shown as a plain image. Easier and
        # more reliable than an iframe for feeds like safari cams.
        "safari": "https://www.youtube.com/watch?v=ydYDqZQpim8",
        "africam": "https://www.youtube.com/watch?v=ydYDqZQpim8",
        "namibia": "https://www.youtube.com/watch?v=ydYDqZQpim8",
        "namib": "https://www.youtube.com/watch?v=ydYDqZQpim8",
        "namib desert": "https://www.youtube.com/watch?v=ydYDqZQpim8",
        "watering hole": "https://www.youtube.com/watch?v=ydYDqZQpim8",
    }

    @staticmethod
    def _vms_publish(camera: str) -> dict[str, Any]:
        """VMS-channel path: pull one frame from the XMEye shim (mn01:8377)
        and publish it as a relay asset so vcast pages get a SAME-ORIGIN
        URL — the canvas stays clean for vcast_snapshot verification.
        Returns {"ok", "url"|"error"}."""
        import base64, time
        vms = os.environ.get("ADA_VMS_SNAP_URL", "").rstrip("/")
        if not vms:
            return {"ok": False, "error": "VMS snapshot service not configured"}
        try:
            import urllib.parse
            url = f"{vms}/snap?ch={urllib.parse.quote(camera)}"
            # P2P streams are flaky — a dead pane now 503s; retry once.
            png = b""
            last_exc: Exception | None = None
            for _try in range(2):
                try:
                    with urllib.request.urlopen(
                            urllib.request.Request(url), timeout=75) as r:
                        png = r.read()
                    if len(png) >= 500:
                        break
                except Exception as exc:
                    last_exc = exc
                    png = b""
                    time.sleep(2)
            if last_exc is not None and not png:
                raise last_exc
            if len(png) < 500:
                return {"ok": False, "error": f"no frame from {camera!r} (camera may be offline)"}
            slug = "".join(c if c.isalnum() else "-"
                           for c in camera.lower()).strip("-")
            token = f"cam:{slug}-{int(time.time())}"
            ToolRunner._vcast_api("/frame", {
                "screen": 0, "token": token,
                "data": "data:image/png;base64," + base64.b64encode(png).decode(),
                "state": "asset"})
            # URL the DISPLAY fetches — must be the public same-origin https
            # route (vcast pages sit under tony-dell.../apps/), never the
            # local VCAST_API base (http cross-origin -> canvas taint).
            pub = os.environ.get(
                "VCAST_PUBLIC_API",
                "https://tony-dell.taila0626a.ts.net/api/input-bridge")
            return {"ok": True, "url": f"{pub}/frame?screen=0&token={token}",
                    "camera": camera}
        except Exception as exc:
            return {"ok": False, "error": f"VMS snapshot failed: {exc}"}

    @staticmethod
    def _yt_publish(camera: str, yt_url: str) -> dict[str, Any]:
        """YouTube-live path: one frame via yt-frame.sh on ADA_CCTV_SSH,
        published as a relay asset (same-origin /frame?token=) so the
        vcast canvas stays clean. Returns {"ok", "url"|"error"}."""
        import base64, subprocess, time
        host = os.environ.get("ADA_CCTV_SSH", "tony-dell-m2m")
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 host, f"~/.local/bin/yt-frame.sh '{yt_url}' /dev/stdout"],
                capture_output=True, timeout=75)
            jpg = proc.stdout or b""
            if len(jpg) < 500:
                return {"ok": False,
                        "error": "no frame from youtube "
                                 f"(stream offline? {proc.stderr.decode()[-160:]})"}
            slug = "".join(c if c.isalnum() else "-"
                           for c in camera.lower()).strip("-")
            token = f"cam:{slug}-{int(time.time())}"
            ToolRunner._vcast_api("/frame", {
                "screen": 0, "token": token,
                "data": "data:image/jpeg;base64," + base64.b64encode(jpg).decode(),
                "state": "asset"})
            pub = os.environ.get(
                "VCAST_PUBLIC_API",
                "https://tony-dell.taila0626a.ts.net/api/input-bridge")
            return {"ok": True, "url": f"{pub}/frame?screen=0&token={token}",
                    "camera": camera}
        except Exception as exc:
            return {"ok": False, "error": f"youtube frame failed: {exc}"}

    @staticmethod
    def _cctv_grab(camera: str) -> dict[str, Any]:
        """Fetch one JPEG via go2rtc on tony-dell into the HA /local/ dir.
        Falls back to the VMS shim for estate cameras (front road, pool,
        tennis, etc). Returns {"ok", "url"|"error"}."""
        import subprocess
        import time
        src = ToolRunner._CCTV_CAMS.get(camera.strip().lower())
        if not src:
            yt = ToolRunner._YT_CAMS.get(camera.strip().lower())
            if yt:
                return ToolRunner._yt_publish(camera, yt)
            # not a go2rtc home cam — try the VMS estate channel set
            return ToolRunner._vms_publish(camera)
        # Try the preferred stream, then fall back through SD/base variants —
        # _hd streams die when a cam degrades while the base stream survives
        # (c100/ip65 returned 200+0B on _hd while the plain names were fine).
        variants = [src]
        if src.endswith("_hd"):
            variants += [src[:-3], src[:-3] + "_sd"]
        name = f"snap-{src}-{int(time.time())}.jpg"
        host = os.environ.get("ADA_CCTV_SSH", "tony-dell-m2m")
        tried = []
        ok = False
        for v in variants:
            cmd = (
                f"mkdir -p ~/.config/home-assistant/www/cam && "
                f"curl -sf -m 20 -o ~/.config/home-assistant/www/cam/{name} "
                f"--size-limit 500 "
                f"'http://127.0.0.1:1984/api/frame.jpeg?src={v}' "
                f"&& [ -s ~/.config/home-assistant/www/cam/{name} ] && echo ok"
            )
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, cmd],
                capture_output=True, text=True, timeout=45)
            tried.append(v)
            if proc.stdout.strip() == "ok":
                src = v
                ok = True
                break
        if not ok:
            return {"ok": False,
                    "error": f"no frame from {src} (camera may be offline; tried {tried})"}
        base = os.environ.get(
            "ADA_CCTV_PUBLIC_BASE",
            "https://tony-dell.taila0626a.ts.net:8123/local/cam/")
        return {"ok": True, "url": base + name, "camera": src}

    async def ada_camera_snapshot(
            self, view: str = "", source: str = "auto",
            mode: str = "live", screen: int = 0, target: str = "",
            query: str = "", lat: float | None = None,
            lon: float | None = None, heading: float | None = None,
            channel: str = "", camera: str = "") -> dict[str, Any]:
        """One still frame from a camera, optionally pushed to a display.

        Canonical for the camera merge (tools-merge-camera): absorbs
        cctv_snapshot (snap + show on TV/screen) and traffic_camera
        (public Thailand traffic cams). source: 'auto' (default — traffic
        only when traffic-style args arrive), 'vms' (property CCTV +
        go2rtc home cams), 'traffic' (Longdo/iTIC). mode='cached' serves
        the camwall's last-good frame instead of a live pull.

        Runner path (tool_runner.execute / REST) — the frame can't attach
        to a live turn from here, so the result carries a fetchable url.
        The provider's dispatch attaches the image itself."""
        import asyncio
        src = str(source or "auto").lower()
        if src == "traffic" or (src == "auto" and (
                str(query or "").strip() or lat is not None
                or lon is not None or heading is not None)):
            return await asyncio.to_thread(
                self._traffic_snapshot, query or view, lat, lon, heading)
        if src not in ("auto", "vms"):
            return {"ok": False,
                    "error": f"unknown source {source!r} — use auto|vms|traffic"}
        want = str(view or channel or camera or "").strip()
        if not want:
            return {"ok": False,
                    "error": "view is required — a camera name "
                             "('swimming pool', 'c201', 'coffee corner')"}
        if str(mode or "").lower() == "cached":
            url, note = await asyncio.to_thread(self._camwall_fallback, want)
            if not url:
                return {"ok": False,
                        "error": note or f"no stored frame for {want!r}"}
            shot = {"ok": True, "url": url, "camera": want, "stored": True}
        else:
            shot = await asyncio.to_thread(self._cctv_grab, want)
        if not shot.get("ok"):
            return shot
        url = shot["url"]
        t = (target or "").strip().lower()
        n = int(screen or 0)
        if n or t == "screen" or t.startswith("vcast"):
            n = n or 1
            await self._check_screen_owner(n, self._memory_identity())
            out = await asyncio.to_thread(
                self._vcast_api, "/pub",
                {"screen": n, "msg": {"type": "image", "url": url}})
            out.update({"url": url, "camera": shot["camera"], "screen": n})
            if (n in self._tv_cfg()["screens"]
                    and out.get("delivered", 1)):
                tv = await self._tv_input_verify(url)
                out.update(tv)
                if tv.get("tv_note"):
                    out["note"] = tv["tv_note"]
            return out
        if t == "tv":
            out = await self.tv_action(cmd="nav", text=url)
            if isinstance(out, dict):
                out.update({"url": url, "camera": shot["camera"]})
            return out
        return {"ok": True, "url": url, "camera": shot["camera"],
                "output": f"Still frame from camera '{shot['camera']}' "
                          f"at {url}"}

    @staticmethod
    def _traffic_snapshot(query: str, lat: float | None,
                          lon: float | None,
                          heading: float | None) -> dict[str, Any]:
        """traffic_camera absorb (runner path): Longdo/iTIC search, snap
        the best match, publish a same-origin relay asset. Same flow as
        the provider's _traffic_camera minus the frame attachment."""
        from backend import traffic_camera as tc
        query = str(query or "").strip()
        if not query and lat is None:
            return {"ok": False,
                    "error": "pass a query (area/road) or lat+lon "
                             "(+optional heading)"}
        try:
            cams = tc.find_cams(query, lat, lon, heading)
        except Exception as exc:
            return {"ok": False, "error": f"camera feed unavailable: {exc}"}
        if not cams:
            return {"ok": False,
                    "error": f"no traffic camera matched "
                             f"{query or 'that position'}."}
        if cams[0].get("suspended"):
            return {"ok": False,
                    "error": cams[0]["title"] +
                             " — the feed currently has live frames for "
                             "Bangkok and Nonthaburi cams only."}
        dead = []
        for cand in cams[:4]:
            try:
                res = tc.snap(cand, 10)
            except Exception:
                res = None
            if res and res[0]:
                jpeg, _mime = res
                slug = re.sub(r"[^a-z0-9]+", "-",
                              (cand.get("camid") or "cam").lower())
                out = {"ok": True, "camid": cand["camid"],
                       "title": cand["title"], "matches": len(cams),
                       "output": f"Traffic camera '{cand['title']}'"}
                if cand.get("dist_km") is not None:
                    out["dist_km"] = cand["dist_km"]
                cast_url = tc.publish_relay(jpeg, slug)
                if cast_url:
                    out["cast_url"] = cast_url
                return out
            dead.append(cand["title"][:60])
        return {"ok": False,
                "error": f"{len(dead)} matched camera(s) returned no "
                         f"usable frame ({', '.join(dead)})."}

    # -- async chat push (LINE / Telegram) --

    async def chat_send(self, channel: str = "line", text: str = "",
                        image_url: str = "", camera: str = "",
                        to: str = "", photo: str = "", doc: str = "",
                        key: str = "", op: str = "",
                        session_id: str = "", screen: int = 0,
                        show: bool = True) -> dict[str, Any]:
        """Queue an outbound LINE/Telegram message in the background and
        return immediately. camera names a VMS channel (snapped on the
        shim); image_url is any fetchable image (camwall thumb, cast_url).
        Completion arrives as a system note via /api/notify.

        tools-merge-tasks-status absorbed the photo-picker pair and the
        doc-upload card actions: photo='pick' starts a Google Photos
        picker and delivers the picker_uri to the channel (was
        photos_pick), photo='picked' polls a picker session, casts the
        first pick to a screen (show=true) and sends it to the channel
        (was photos_picked, takes session_id/screen/show); doc='show' /
        'process' / 'card' covers sys_show_uploaded_document /
        process_document_upload / doc_upload_card_action — key= names a
        held /api/documents/intake key (default: newest), op= carries a
        card button ('archive'/'print' re-dispatch through execute() so
        the ada_doc_* confirm gates still apply)."""
        photo = str(photo or "").strip().lower()
        doc = str(doc or "").strip().lower()
        if doc.startswith("doc/"):
            # A bare intake key in the doc slot means "show that one".
            key = key or doc
            doc = "show"
        if photo:
            return await self._chat_send_photo(
                photo, channel=channel, text=text, to=to,
                session_id=str(session_id or ""), screen=int(screen or 0),
                show=bool(show))
        if doc:
            return await self._chat_send_doc(
                doc, channel=channel, text=text, to=to,
                key=str(key or ""), op=str(op or ""))
        return await self._chat_send_queue(
            channel=channel, text=text, image_url=image_url,
            camera=camera, to=to)

    async def _chat_send_queue(self, channel: str, text: str,
                               image_url: str, camera: str,
                               to: str) -> dict[str, Any]:
        import asyncio
        import secrets
        job_id = "chat-" + secrets.token_hex(4)
        asyncio.create_task(self._chat_send_run(
            job_id, channel=channel, text=text, image_url=image_url,
            camera=camera, to=to))
        return {"status": "queued", "job_id": job_id, "channel": channel,
                "note": "Running in the background. Tell the user the "
                        "message is being sent — a system note will report "
                        "success or failure when it finishes."}

    async def _chat_send_photo(self, action: str, *, channel: str,
                               text: str, to: str, session_id: str,
                               screen: int, show: bool) -> dict[str, Any]:
        """photo= sub-flows — absorbed photos_pick / photos_picked keep
        their picker semantics; chat_send additionally delivers results
        to the named channel."""
        if action == "pick":
            out = await self.photos_pick()
            uri = str(out.get("picker_uri") or "")
            if uri and str(channel or "").strip():
                send_text = (str(text).strip() + " " if str(
                    text or "").strip() else "") + uri
                out["send"] = await self._chat_send_queue(
                    channel=channel, text=send_text,
                    image_url="", camera="", to=to)
            return out
        if action == "picked":
            if not session_id:
                raise ValueError(
                    "session_id is required for photo='picked'")
            out = await self.photos_picked(
                session_id, screen=int(screen or 0), show=bool(show))
            items = out.get("items") or []
            base = str(items[0].get("baseUrl") or "") if items else ""
            if out.get("picked") and base and str(channel or "").strip():
                mime = str(items[0].get("mimeType") or "")
                url = base + ("=dv" if mime.startswith("video/")
                              else "=w2048")
                out["send"] = await self._chat_send_queue(
                    channel=channel, text=text, image_url=url,
                    camera="", to=to)
            return out
        raise ValueError(
            f"invalid photo action {action!r}: expected pick|picked")

    async def _chat_send_doc(self, action: str, *, channel: str,
                             text: str, to: str, key: str,
                             op: str) -> dict[str, Any]:
        """doc= sub-flows — the doc-upload card actions absorbed from the
        chaba side: 'show' posts the held upload's summary to the
        channel, 'process' returns the held intake assessment, 'card'
        takes op=<button> where archive/print re-dispatch through
        execute() so the ada_doc_* confirmation gates still apply."""
        from backend import document_check
        engine = document_check.engine()
        ref = str(key or "").strip()
        held_key, held = ref, (engine.held(ref) if ref else None)
        if held is None and not ref:
            latest = engine.latest()
            held_key, held = latest if latest else ("", None)
        op_l = str(op or "").strip().lower()
        if action == "card" and op_l == "archive":
            if held is None:
                raise RuntimeError(
                    "no held document to act on — upload it again")
            stem = re.sub(r"[^a-z0-9]+", "-", str(
                (held.meta or {}).get("filename") or
                "document").lower()).strip("-") or "document"
            return await self.execute("ada_doc_archive", {
                "slug": stem[:40], "intake_key": held_key})
        if action == "card" and op_l == "print":
            raise RuntimeError(
                "print needs an archived doc slug — archive the held "
                "intake first (op='archive'), then ada_doc_print")
        if action not in ("show", "process", "card"):
            raise ValueError(
                f"invalid doc action {action!r}: expected "
                "show|process|card")
        if held is None:
            raise RuntimeError(
                f"no held document{f' for key {ref!r}' if ref else ''}"
                " — intake results live in RAM only, upload again")
        meta = held.meta or {}
        summary = (
            f"Document '{meta.get('filename') or held_key}' "
            f"({meta.get('doc_type') or 'document'}) held as {held_key} "
            f"— print-ready PDF and preview at "
            f"/api/documents/{held_key}/pdf|preview")
        out = {"doc": held_key, "doc_action": action,
               "doc_type": meta.get("doc_type"),
               "filename": meta.get("filename"),
               "preview_url": f"/api/documents/{held_key}/preview",
               "pdf_url": f"/api/documents/{held_key}/pdf"}
        if op_l:
            out["op"] = op_l
        if str(channel or "").strip():
            out["send"] = await self._chat_send_queue(
                channel=channel, text=text or summary,
                image_url="", camera="", to=to)
        return out

    async def _chat_send_run(self, job_id: str, channel: str, text: str,
                             image_url: str, camera: str,
                             to: str) -> None:
        """Background worker: resolve the image (VMS snap or direct URL),
        POST to the relay /send endpoints, then surface the result to the
        live session via /api/notify."""
        import urllib.parse
        import urllib.request
        summary_bits: list[str] = []
        ok_any = False
        url = (image_url or "").strip()
        cam = (camera or "").strip()
        if cam and not url:
            vms = os.environ.get("ADA_VMS_SNAP_URL", "").rstrip("/")
            if vms:
                url = f"{vms}/snap?ch=" + urllib.parse.quote(cam)
        if cam and url and "snap?ch=" in url:
            # Warm the serial shim once — the relay would otherwise fetch
            # the raw endpoint and a 503 means a broken/blocked delivery.
            try:
                with urllib.request.urlopen(url, timeout=180) as r:
                    if r.status != 200:
                        summary_bits.append(
                            f"camera '{cam}' snap failed ({r.status})")
                        url = ""
            except Exception as exc:
                summary_bits.append(f"camera '{cam}' snap failed ({exc})")
                url = ""
        if not url and cam:
            # Live snap dead → fall back to the camwall puller's last-good
            # frame (data/<zone>/<cam>.jpg + manifest ts/ok per cam).
            url, fnote = await asyncio.to_thread(self._camwall_fallback, cam)
            if url:
                summary_bits.append(
                    f"camera '{cam}' live snap down — sending {fnote}")
            elif fnote:
                summary_bits.append(f"camera '{cam}': {fnote}")
        relays = [c for c in (channel or "line").lower().split(",")
                  if c.strip()]
        if "both" in relays or "all" in relays:
            relays = ["line", "telegram"]
        for svc in relays:
            svc = svc.strip()
            if svc == "line":
                ep = os.environ.get("ADA_LINE_SEND_URL",
                                    "http://127.0.0.1:8912/send")
            elif svc in ("telegram", "tg"):
                ep = os.environ.get("ADA_TG_SEND_URL",
                                    "http://127.0.0.1:8911/send")
            else:
                summary_bits.append(f"unknown channel '{svc}'")
                continue
            payload = {"text": text, "image_url": url,
                       "caption": text or cam, "to": to,
                       "chat_id": to}
            try:
                req = urllib.request.Request(
                    ep, data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=150) as r:
                    res = json.load(r)
                if res.get("ok"):
                    ok_any = True
                    summary_bits.append(f"{svc}: delivered")
                else:
                    summary_bits.append(
                        f"{svc}: failed ({res.get('error', '?')})")
            except Exception as exc:
                summary_bits.append(f"{svc}: failed ({exc})")
        note = (f"[job {job_id}] chat_send "
                f"{'DONE' if ok_any else 'FAILED'} — "
                + "; ".join(summary_bits))
        try:
            key = os.environ.get("ADA_API_KEY", "")
            req = urllib.request.Request(
                os.environ.get("ADA_NOTIFY_URL",
                               "http://127.0.0.1:8002/api/notify"),
                data=json.dumps({"text": note, "urgent": "0"}).encode(),
                headers={"Content-Type": "application/json",
                         "x-api-key": key})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:
            logger.warning("chat_send notify failed: %s", exc)

    def _camwall_fallback(self, cam: str) -> tuple[str, str]:
        """Resolve `cam` to the camwall puller's cached frame URL.

        Returns (url, note) — url empty when no manifest matches. The puller
        writes data/<zone>/<key>.jpg + manifest-<zone>.json per zone on
        tony-dell; the manifest marks each cam ok/err + ts so we prefer the
        freshest working frame over a stale one."""
        import difflib
        import urllib.parse
        import urllib.request
        # puller edges serve camwall data on :8380; Ada runs on idc01 so
        # loopback is the shortest path — env or VCAST host as fallback.
        cbase = os.environ.get("ADA_CAMWALL_BASE", "").rstrip("/")
        if not cbase:
            pub = os.environ.get(
                "VCAST_PUBLIC_API",
                "https://tony-dell.taila0626a.ts.net/api/input-bridge")
            parts = urllib.parse.urlsplit(pub)
            cbase = f"{parts.scheme}://{parts.netloc}/apps/camwall"
        try:
            wall = self._vcast_api("/camwall")
            zones = list((wall.get("zones") or {}).keys())
        except Exception:
            zones = []
        if not zones:
            from vms_camera import VMS_CAMWALL_ZONES
            zones = list(VMS_CAMWALL_ZONES)
        want = cam.strip().lower()
        best: tuple[float, str] | None = None  # (ts, url)
        for zone in zones:
            try:
                with urllib.request.urlopen(
                        f"{cbase}/data/{zone}/manifest-{zone}.json",
                        timeout=8) as r:
                    man = json.load(r)
            except Exception:
                continue
            for c in man.get("cams") or []:
                label = str(c.get("label") or c.get("key") or "").lower()
                key = str(c.get("key") or "")
                if want in label or label in want or \
                        difflib.SequenceMatcher(None, want, label).ratio() > 0.6:
                    url = f"{cbase}/data/{zone}/{key}.jpg"
                    ts = float(c.get("ts") or 0)
                    # a cam currently erroring still has its last frame —
                    # prefer ok cams, else newest ts wins
                    score = ts + (1e12 if c.get("ok") else 0)
                    if best is None or score > best[0]:
                        best = (score, url)
        if best:
            return best[1], "last cached frame"
        return "", "no camwall cached frame either"

    async def yt_transcript(self, url: str, language: str = "th") -> dict[str, Any]:
        """Fetch a YouTube video's auto-captions as plain text. `url` is a
        YouTube URL or video ID. The extraction runs on the transcript host
        (mn01) via `yt-transcript.sh`. Returns title, language and up to ~6k
        chars of transcript text — enough to summarize for a spoken report.
        Thai news sites block scrapers; this is the news-source fallback."""
        import asyncio
        import subprocess
        host = os.environ.get("ADA_YT_TRANSCRIPT_HOST", "mn01")
        script = os.environ.get(
            "ADA_YT_TRANSCRIPT_BIN", "~/.local/bin/yt-transcript.sh")
        proc = await asyncio.to_thread(
            subprocess.run,
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             host, script, url, language],
            capture_output=True, text=True, timeout=90)
        out = (proc.stdout or "").strip()
        if out.startswith("NO_CAPTIONS"):
            return {"ok": False, "error": "no captions available",
                    "title": out[11:].strip() or url}
        lines = out.splitlines()
        title = next((l[7:] for l in lines if l.startswith("TITLE: ")), url)
        lang = next((l[6:] for l in lines if l.startswith("LANG: ")), language)
        text = "\n".join(
            l for l in lines
            if not l.startswith(("TITLE:", "LANG:"))).strip()
        if not text:
            return {"ok": False, "error": "empty transcript", "title": title}
        return {"ok": True, "title": title, "language": lang,
                "transcript": text, "chars": len(text)}

    # -- vcast virtual displays (input-bridge relay on tony-dell :3010) --

    @staticmethod
    def _vcast_api(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        import urllib.request
        base = os.environ.get(
            "VCAST_API", "https://tony-dell.taila0626a.ts.net/api/input-bridge")
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            base + path, data=data,
            headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    async def cctv_wall(self, action: str, zone: str, screen: int = 0,
                        pane: int | None = None,
                        settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start/stop the periodic-thumbnail camera wall on a vcast screen.
        Enables the zone in the relay's /camwall state (the puller on
        tony-dell refreshes thumbs into /apps/camwall/data/<zone>/) and casts
        the wall page. Zones: zone-a, noble-park, tony-house, vms-noble-club,
        vms-noble-a, rama9 (demo traffic wall), traffic (DOH Bangkok),
        burapha (Bangna–Burapha expressway), chonburi (Chonburi corridor).
        Walls keep warm thumbs even while disabled, so casting an area is
        instant — the puller refreshes in the background.
        settings (optional) tunes the zone in the relay: {"interval": s,
        "jpeg_q": 1-8, "thumb_w": px, "cams_skip": [key], "cams_extra":
        [{label,kind,url}], "effects": ["timestamp","grid",
        "yolo:person,car@0.35"]}. yolo overlays detections and appends a
        rolling detections-<zone>.jsonl while the effect stays on."""
        import asyncio
        action = (action or "start").strip().lower()
        zone = (zone or "").strip().lower().replace(" ", "-")
        valid = {"zone-a", "noble-park", "tony-house",
                 "vms-noble-club", "vms-noble-a", "rama9",
                 "traffic", "burapha", "chonburi"}
        if zone not in valid:
            return {"error": f"unknown zone {zone!r} — valid: {sorted(valid)}"}
        if action == "settings":
            if not settings:
                return {"error": "settings action requires a settings object"}
            # the model sometimes double-wraps settings —
            # {"settings": {...}} — flatten before relay (2026-09-30: a
            # nested write silently failed to clear the yolo effect)
            if isinstance(settings, dict) and isinstance(
                    settings.get("settings"), dict):
                inner = settings.pop("settings")
                inner.update(settings)
                settings = inner
            out = await asyncio.to_thread(
                self._vcast_api, "/camwall",
                {"zone": zone, "settings": settings})
            if isinstance(out, dict):
                out["applied"] = settings
            return out
        if action == "status":
            # read the zone manifest (public static file on tony-dell) —
            # roster, per-cam freshness, yolo counts, wall health
            import urllib.request
            man_url = (os.environ.get(
                           "ADA_CAMWALL_BASE",
                           "https://tony-dell.taila0626a.ts.net/apps/camwall/")
                       + f"data/{zone}/manifest-{zone}.json")
            try:
                man = await asyncio.to_thread(
                    lambda: json.load(urllib.request.urlopen(
                        urllib.request.Request(man_url), timeout=15)))
            except Exception as exc:
                return {"ok": False, "zone": zone,
                        "error": f"manifest fetch failed: {exc}"}
            now = time.time()
            cams = [{
                "label": c.get("label"), "ok": bool(c.get("ok")),
                "age_s": int(now - c["ts"]) if c.get("ts") else None,
                "det": c.get("det"), "err": c.get("err"),
            } for c in man.get("cams", [])]
            out = {"ok": True, "zone": zone,
                   "live": sum(1 for c in cams if c["ok"]),
                   "total": len(cams), "cams": cams,
                   "wall_url": man_url.rsplit("/data/", 1)[0]
                               + f"/?zone={zone}"}
            # surface the relay's zone settings (interval, effects, …) so the
            # model sees what's tunable and reports the current config instead
            # of guessing — 2026-10-01: wall_ops turn failed with 'the tool
            # doesn't support intervals' because settings were invisible
            try:
                st = await asyncio.to_thread(self._vcast_api, "/camwall")
                zs = (st.get("zones") or {}).get(zone) or {}
                if zs:
                    out["enabled"] = zs.get("enabled")
                    out["settings"] = zs.get("settings") or {}
                    out["settings_hint"] = (
                        "change with action='settings', settings={...} "
                        "(interval s, jpeg_q 1-8, thumb_w px, cams_skip, "
                        "cams_extra, effects)")
            except Exception:
                pass
            return out
        if action == "stop":
            await asyncio.to_thread(
                self._vcast_api, "/camwall", {"zone": zone, "enabled": False})
            if screen:
                await asyncio.to_thread(
                    self._vcast_api, "/pub",
                    {"screen": int(screen), "msg": {"type": "stop"}})
                try:
                    await asyncio.to_thread(self._vcast_api, "/capture",
                                            {"screen": int(screen),
                                             "active": False})
                except Exception:
                    pass
            return {"ok": True, "zone": zone, "stopped": True}
        n = int(screen or 1)
        payload = {"zone": zone, "enabled": True, "screen": n}
        if settings:
            payload["settings"] = settings
        if n:
            await self._check_screen_owner(n, self._memory_identity())
        busy = await self._screen_busy(n, pane) if n else None
        await asyncio.to_thread(self._vcast_api, "/camwall", payload)
        if pane is None:
            try:
                await asyncio.to_thread(self._vcast_api, "/capture", {
                    "screen": n, "source": "camwall", "ch": zone,
                    "active": True, "by": "ada"})
            except Exception:
                pass
        # root-relative: displays may load vcast from LAN (no tailnet) —
        # a hardcoded tailnet URL iframes to nothing there (black screen).
        # Every tony-dell origin serves /apps/camwall (Caddy -> pull edges).
        url = f"/apps/camwall/?zone={zone}"
        # cycle/zones ride as URL params on the page — page-side display
        # behavior, not relay state, so they don't touch /camwall.
        cyc = (settings or {}).get("cycle_s")
        if cyc:
            url += f"&cycle={int(cyc)}"
        zlist = (settings or {}).get("zones")
        if zlist:
            url += f"&zones={','.join(zlist)}&zone_s={int((settings or {}).get('zone_s') or 45)}"
        if (settings or {}).get("random"):
            url += "&random=1"
        nav_msg: dict[str, Any] = {"type": "nav", "url": url}
        if pane is not None:
            nav_msg["pane"] = int(pane)
        out = await asyncio.to_thread(
            self._vcast_api, "/pub", {"screen": n, "msg": nav_msg})
        # wall health from the manifest — Ada warns immediately when the
        # zone is dead instead of the user discovering a blank wall
        try:
            st = await self.cctv_wall("status", zone)
            out["live_cams"] = st.get("live")
            out["total_cams"] = st.get("total")
            if st.get("ok") is False or (st.get("total") and not st.get("live")):
                out["note_extra"] = (
                    "ALL cameras in this wall are currently down — "
                    "warn the user the wall will show errors until the "
                    "source recovers.")
            elif st.get("live") is not None and st.get("live") < st.get("total"):
                out["note_extra"] = (
                    f"only {st['live']}/{st['total']} cameras are live — "
                    "tell the user which are down (see 'cams' in a status "
                    "call if they ask).")
        except Exception:
            pass
        out.update({
            "zone": zone, "screen": n, "url": url,
            "note": ("thumbs refresh in the background (VMS cams ~60s, "
                     "house ~30s) — the wall fills in within a minute."),
        })
        if busy:
            out["replaced"] = busy
            out["note"] += (f" It interrupted {busy['desc']} — "
                            "acknowledge that to the user.")
        if settings:
            out["applied"] = settings
        # TV-hosted screen: 'delivered' only means the relay acked — if
        # the TV's foreground app is another input the wall is invisible.
        # Verify via HA and push the wall URL at the TV browser on
        # mismatch (the proven workaround) instead of claiming success.
        if n in self._tv_cfg()["screens"]:
            tv = await self._tv_input_verify(url)
            out.update(tv)
            if tv.get("tv_note"):
                out["note"] += " " + tv["tv_note"]
        return out

    async def vcast_list(self) -> dict[str, Any]:
        """List registered vcast virtual displays (screen number, device,
        online/offline, current state), plus the relay's ground truth:
        active camera-capture leases and enabled cam-wall zones."""
        import asyncio
        data = await asyncio.to_thread(self._vcast_api, "/displays")
        out: dict[str, Any] = {
            "screens": [
                {
                    "screen": s["screen"],
                    "name": s["name"],
                    "device": s.get("label") or s["name"],
                    "online": s.get("connected", False),
                    "state": s.get("state") or "idle",
                    "detail": s.get("state_detail") or "",
                    "panes": s.get("panes"),
                }
                for s in data.get("screens", [])
            ],
            "pending": len(data.get("pending", [])),
        }
        try:
            caps = await asyncio.to_thread(self._vcast_api, "/capture")
            out["active_captures"] = caps.get("captures") or {}
        except Exception:
            pass
        try:
            wall = await asyncio.to_thread(self._vcast_api, "/camwall")
            zones = wall.get("zones") or {}
            out["camwall_zones"] = {
                z: v for z, v in zones.items() if v.get("enabled")}
            # Ground-truth check: the zone registry says a wall is on a
            # screen, but the screen's own state report is authoritative —
            # a user nav/reconnect can leave them diverged (2026-09-29:
            # wall claimed on screen 1 while the user saw a single cam).
            mismatches = []
            for z, v in out["camwall_zones"].items():
                scr = v.get("screen")
                s = next((x for x in out["screens"]
                          if x.get("screen") == scr), None)
                if s and s.get("online") and "camwall" not in (
                        s.get("detail") or ""):
                    mismatches.append(
                        f"zone '{z}' registered on screen {scr} but the "
                        f"screen reports '{s.get('state')}: "
                        f"{s.get('detail') or 'no detail'}' — the wall is "
                        "NOT actually showing; the zone flag is stale. Do "
                        "NOT tell the user it is up — restart it with "
                        "cctv_wall if they want it.")
            if mismatches:
                out["state_mismatch"] = mismatches
        except Exception:
            pass
        # TV input visibility: screens rendered by the TV's browser are
        # only visible while the TV's foreground app IS the browser —
        # surface it so Ada doesn't claim a wall/cast is showing while
        # the TV sits on the STB input (2026-10-04 incident).
        try:
            tvcfg = self._tv_cfg()
            for s in out.get("screens") or []:
                if s.get("screen") in tvcfg["screens"]:
                    s["on_tv"] = True
            ti = await self._tv_input_state()
            if ti.get("on_cast_app") is not None:
                out["tv_input"] = ti
                if ti["on_cast_app"] is False:
                    out["tv_input"]["warning"] = (
                        f"the TV is on '{ti.get('app') or ti.get('state')}' — "
                        "casts to TV-hosted screens are NOT visible right "
                        "now; say so or push the URL via tv_action nav.")
        except Exception:
            pass
        return out

    async def vcast_snapshot(self, screen: int) -> dict[str, Any]:
        """vcast_snapshot (runner path): ask the display to capture its
        own frame — /pub {type:snap-request,token} then poll
        /frame?screen&token until the JPEG lands. GEV iframes can't be
        read by the vcast page, so the same call also fires the GEV
        remote capture_frame command at that screen — that command is
        retired INTO this tool (tools-merge-camera), not a separate
        surface. The provider's live path attaches the frame to the
        turn; here the /frame URL is the result."""
        import asyncio
        import urllib.request
        try:
            n = int(screen)
        except (TypeError, ValueError):
            return {"ok": False,
                    "error": "screen number required — call "
                             "cast_to_screen(action='list') to see "
                             "registered displays."}
        base = os.environ.get(
            "VCAST_API",
            "https://tony-dell.taila0626a.ts.net/api/input-bridge")
        token = f"snap-{int(time.time() * 1000)}-runner"
        try:
            out = await asyncio.to_thread(
                self._vcast_api, "/pub",
                {"screen": n,
                 "msg": {"type": "snap-request", "token": token}})
        except Exception as exc:
            return {"ok": False, "error": f"snap-request failed: {exc}"}
        if not out.get("delivered", 0):
            return {"ok": False,
                    "error": f"screen {n} is not connected — "
                             "check cast_to_screen(action='list') for "
                             "online displays."}

        def _post(url: str, payload: dict):
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            return urllib.request.urlopen(req, timeout=10)

        def _get(url: str):
            return urllib.request.urlopen(url, timeout=10)

        # Fire the retired capture_frame command at this screen's GEV
        # remotes; whichever path lands the frame first wins the poll.
        try:
            await asyncio.to_thread(
                _post,
                os.environ.get(
                    "GEV_CMD_URL",
                    "https://tony-dell.taila0626a.ts.net"
                    "/apps/gev-cmd/command"),
                {"name": "capture_frame", "args": {"token": token},
                 "screen": n, "wait": 0})
        except Exception:
            pass  # bridge down or no GEV client — snap-request may land
        last_err = None
        for _ in range(15):
            try:
                r = await asyncio.to_thread(
                    _get, f"{base}/frame?screen={n}&token={token}")
                if r.headers.get("Content-Type", "").startswith("image/"):
                    pub = os.environ.get("VCAST_PUBLIC_API", base)
                    return {"ok": True, "screen": n,
                            "url": f"{pub}/frame?screen={n}&token={token}",
                            "output": f"Still frame captured from vcast "
                                      f"screen {n}."}
                body = json.loads(r.read() or b"{}")
                if body.get("error") == "simulated":
                    return {"ok": False,
                            "error": f"screen {n} is a headless/simulated "
                                     "display — no real pixels to capture "
                                     f"(state "
                                     f"'{body.get('state') or 'unknown'}')."}
                if body.get("error") and body["error"] != "image-load-failed":
                    last_err = body
                elif body.get("error"):
                    return {"ok": False,
                            "error": f"screen {n} could not capture: "
                                     f"{body['error']} (state="
                                     f"{body.get('state') or 'unknown'})."}
            except Exception as exc:
                last_err = {"error": str(exc)}
            await asyncio.sleep(0.8)
        return {"ok": False,
                "error": f"screen {n} did not return a frame in time"
                         + (f" ({last_err.get('error')})"
                            if last_err else "")}

    async def vcast_gesture(self, screen: int | None = None,
                            mode: str = "off") -> dict[str, Any]:
        """Toggle gesture control on a vcast display — publishes a
        {type:gesture, mode} command into the screen's room. The display
        acks via its reported state (gesture:<mode> on success, an
        error state if the camera or mode is unavailable)."""
        import asyncio
        if screen is None:
            return {"error": "screen required — call "
                             "cast_to_screen(action='list')"}
        mode = str(mode or "off").lower()
        if mode not in ("off", "room", "hand"):
            return {"error": "mode must be off|room|hand"}
        out = await asyncio.to_thread(
            self._vcast_api, "/pub",
            {"screen": screen, "msg": {"type": "gesture", "mode": mode}})
        if not out.get("ok"):
            return {"ok": False, "screen": screen, "error": out.get("error")
                    or "publish failed"}
        res = {"ok": True, "screen": screen, "mode": mode,
               "delivered": out.get("delivered", 0)}
        if not res["delivered"]:
            res["warning"] = ("screen connected but nothing delivered — "
                              "check cast_to_screen(action='list')")
        # give the page a beat to report its new state, then echo it back
        await asyncio.sleep(1.5)
        try:
            data = await asyncio.to_thread(self._vcast_api, "/displays")
            s = next((x for x in data.get("screens", [])
                      if x.get("screen") == screen), None)
            if s:
                res["screen_state"] = s.get("state")
                res["screen_detail"] = s.get("state_detail")
        except Exception:
            pass
        return res

    _TOURS_PATH = Path(__file__).resolve().parent / "gev_tours.json"

    # Per-command arg whitelist (gev-command-whitelist card) — mirrors
    # the GEV tool schema in chaba stacks/tony-dell/gev-gemini/tools.json
    # (extracted from GEV_REALTIME_TOOLS in gods-eye-view/vite.config.js).
    # args: {key: kind-spec} is the allowed key set; required keys must be
    # present; requires_one lists alternative groups where one group must
    # be fully present. Commands absent from the map pass through — the
    # whitelist only short-circuits calls the client is guaranteed to
    # refuse, it never narrows a command's real surface.
    _GEV_ARG_SCHEMAS: dict[str, dict[str, Any]] = {
        "fly_to_location": {
            "args": {"locationId": "enum[austin|sf|nyc|tokyo|london|paris|dubai|dc]", "query": "str", "latitude": "num", "longitude": "num", "viewMode": "enum[close|overview]", "rangeM": "num", "waitForArrival": "bool"},
            "required": [],
            "requires_one": [["locationId"], ["query"], ["latitude", "longitude"]],
        },
        "select_nearest_aircraft": {
            "args": {"layerId": "enum[flights|military]", "locationId": "enum[austin|sf|nyc|tokyo|london|paris|dubai|dc]", "locationQuery": "str", "latitude": "num", "longitude": "num"},
            "required": ["layerId"],
        },
        "adjust_camera_zoom": {
            "args": {"direction": "enum[in|out]", "amount": "enum[little|medium|lot]"},
            "required": ["direction", "amount"],
        },
        "zoom_to_globe": {
            "args": {},
            "required": [],
        },
        "set_layer_visibility": {
            "args": {"layerId": "enum[flights|military|earthquakes|satellites|rocket-launches|traffic|cctv|radio|bikeshare|ais-live-vessels|local-datacenters|local-dams|telegeography-submarine-cables|local-firms|local-flood-inundation-high|local-flood-inundation-medium|local-flood-inundation-low]", "enabled": "bool"},
            "required": ["layerId", "enabled"],
        },
        "show_data_layers_menu": {
            "args": {"layerId": "enum[flights|military|earthquakes|satellites|traffic|cctv|radio|bikeshare|ais-live-vessels|local-datacenters|local-dams|telegeography-submarine-cables|local-firms|local-flood-inundation-high|local-flood-inundation-medium|local-flood-inundation-low]"},
            "required": [],
        },
        "set_panel_open": {
            "args": {"panelId": "enum[data-panel|location-bar|control-panel|cctv-panel|radio-panel|scene-panel|pp-toggles|global-context-panel]", "open": "bool"},
            "required": ["panelId", "open"],
        },
        "set_context_mode": {
            "args": {"mode": "enum[off|contacts|flights|space-missions|missions]"},
            "required": ["mode"],
        },
        "control_cockpit": {
            "args": {"action": "enum[enter|exit|previous|next|prev|status]", "targetLayer": "enum[flights|military|ais-live-vessels|military-installations]", "aircraftClass": "str"},
            "required": ["action"],
        },
        "set_visual_style": {
            "args": {"style": "enum[normal|retro|surveillance|thermal|anime|noir|snow]"},
            "required": ["style"],
        },
        "get_entity_context": {
            "args": {"scope": "enum[auto|selected|in_view]", "layerId": "enum[local-datacenters|local-dams|telegeography-submarine-cables|local-firms]", "limit": "num"},
            "required": [],
        },
        "get_current_view_state": {
            "args": {},
            "required": [],
        },
        "set_hud": {
            "args": {"visible": "enum[on|off|auto]", "layout": "enum[tactical|operator|minimal]"},
            "required": [],
        },
        "set_detection": {
            "args": {"enabled": "bool", "mode": "enum[sparse|balanced|dense]", "densityPct": "num", "allocationStrategy": "enum[elastic|weighted]"},
            "required": [],
        },
        "set_map_stack": {
            "args": {"stack": "enum[photoreal|bing-aerial|bing-labels|esri-imagery|osm]"},
            "required": ["stack"],
        },
        "set_post_processing": {
            "args": {"bloom": "obj", "sharpen": "obj"},
            "required": [],
        },
        "control_scene": {
            "args": {"action": "enum[list|play|stop|next|status]", "sceneId": "str"},
            "required": ["action"],
        },
        "control_cctv": {
            "args": {"action": "enum[enable|disable|select|next|prev|nearest|focus|coverage|viewshed|adjust|projection|autohop]", "cameraQuery": "str", "enabled": "bool"},
            "required": ["action"],
        },
        "control_radio": {
            "args": {"action": "enum[enable|disable|play|resume|pause|stop|next|previous|volume|select|status]", "volumePct": "num", "category": "enum[all|news|talk|weather|public-safety|aviation-marine|traffic-transit|music]", "locationId": "enum[austin|sf|nyc|tokyo|london|paris|dubai|dc]", "locationQuery": "str", "latitude": "num", "longitude": "num", "country": "str", "stationQuery": "str"},
            "required": ["action"],
        },
        "track_entity": {
            "args": {"query": "str", "layerId": "str"},
            "required": ["query"],
        },
        "stop_tracking": {
            "args": {},
            "required": [],
        },
        "frame_overhead": {
            "args": {"target": "enum[flights|military|satellites|vessels]", "radiusKm": "num"},
            "required": ["target"],
        },
        "annotate_map": {
            "args": {"annotations": "list", "flyTo": "bool", "persist": "bool"},
            "required": ["annotations"],
        },
        "clear_annotations": {
            "args": {},
            "required": [],
        },
        "move_camera": {
            "args": {"motion": "enum[orbit|pan|tilt|rotate|zoom|fly|stop]", "direction": "enum[left|right|up|down|in|out|forward|back]", "speed": "enum[slow|normal|fast]", "mode": "enum[once|continuous]", "amount": "enum[little|medium|lot]"},
            "required": ["motion"],
        },
        "fly_route": {
            "args": {"label": "str", "speed": "enum[slow|normal|fast]"},
            "required": [],
        },
        "analyst_query": {
            "args": {"layers": "list", "scope": "obj", "filters": "list", "sortBy": "str", "sortDir": "enum[asc|desc]", "limit": "num", "followUp": "bool"},
            "required": [],
        },
        "next_iss_pass": {
            "args": {"latitude": "num", "longitude": "num", "minElevationDeg": "num"},
            "required": [],
        },
    }

    def _gev_args_error(self, name: str,
                        args: Any) -> dict[str, Any] | None:
        """Local arg check for known GEV commands — rejects unknown or
        missing keys with the expected schema instead of spending a ws
        relay round-trip on a call the client is guaranteed to refuse
        (the {name,args}-in-args wrap bug class, 0bb8e9b). Commands
        without a map entry pass through untouched."""
        schema = (self._GEV_ARG_SCHEMAS.get(name)
                  if isinstance(name, str) else None)
        if schema is None:
            return None
        problems = []
        if args is None:
            args = {}
        if not isinstance(args, dict):
            problems.append(f"args must be an object, got {type(args).__name__}")
        else:
            unknown = sorted(k for k in args if k not in schema["args"])
            if unknown:
                problems.append(f"unknown args {unknown}")
            missing = [k for k in schema["required"] if k not in args]
            if missing:
                problems.append(f"missing required args {missing}")
            groups = schema.get("requires_one") or []
            if groups and not any(
                    all(k in args for k in g) for g in groups):
                problems.append(
                    "needs one of " + " | ".join(
                        "+".join(g) for g in groups))
        if not problems:
            return None
        required = schema["required"]
        expected = ", ".join(
            f"{k}{'*' if k in required else ''}: {v}"
            for k, v in schema["args"].items())
        return {"ok": False,
                "error": (f"gev_command '{name}' rejected before relay: "
                          f"{'; '.join(problems)} — expected args "
                          f"schema: {{{expected}}}"),
                "expected": schema}

    async def gev_command(self, name: str | None = None,
                          args: dict[str, Any] | None = None,
                          screen: int | None = None,
                          pane: int | None = None,
                          wait: float = 3.0,
                          tour: str | None = None) -> dict[str, Any]:
        """Send a command to God's Eye View clients — forwards a
        function_call frame through the gev-gemini bridge to connected GEV
        browsers (including casted ones). screen=N targets that display;
        pane=N narrows to that split-screen pane; wait (seconds,
        0=fire-and-forget) collects the clients' tool_response so queries
        like get_current_view_state can answer. tools-merge-gev
        (2026-10-05): also handles what used to be gev_tour — pass
        tour='<id or alias>' for a tour's executable card; a bare call
        (no name) lists tours."""
        import asyncio
        import urllib.request
        if tour is not None or not name:
            return self._gev_tour_card(tour)
        # The model intermittently wraps the call envelope inside args:
        #   args={'args': {'query': '...'}, 'name': 'fly_to_location'}
        # — GEV then sees no query/coords and errors cryptically
        # ("needs a locationId, query, or latitude/longitude"). Unwrap once
        # when args looks like a nested call envelope.
        if (isinstance(args, dict) and "name" in args
                and isinstance(args.get("args"), dict)):
            args = args["args"]
        # Arg whitelist (gev-command-whitelist): known commands get their
        # args checked against the GEV schema locally — a guaranteed-refuse
        # call returns the expected schema instead of a relay round-trip.
        err = self._gev_args_error(name, args)
        if err is not None:
            return err
        base = os.environ.get(
            "GEV_CMD_URL",
            "https://tony-dell.taila0626a.ts.net/apps/gev-cmd/command")
        payload = json.dumps({
            "name": name, "args": args or {},
            "screen": screen,
            "pane": pane,
            "wait": min(float(wait or 0), 10.0),
        }).encode()
        def _post():
            req = urllib.request.Request(
                base, data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        try:
            out = await asyncio.to_thread(_post)
        except Exception as e:
            return {"ok": False, "error": f"gev command relay: {e}"}
        if not out.get("delivered"):
            return {"ok": False, "error":
                    "no GEV clients connected — cast /apps/gev/ first"}
        # get_current_view_state returns ~5k tokens of layer/detection/hud
        # config per call — it lands in session history and inflates every
        # subsequent turn. Slim to what Ada actually narrates.
        for resp in out.get("responses") or []:
            r = resp.get("response")
            if not isinstance(r, dict) or "layers" not in r:
                continue
            r["layers"] = [
                {k: l.get(k) for k in ("id", "name", "count", "error")}
                for l in r.get("layers") or [] if l.get("enabled")
            ]
            for dead in ("controls", "detection", "scenePlayback",
                         "celestalRing", "bloom", "sharpen"):
                r.pop(dead, None)
        # The all-failed -> top-level-error lift (0bb8e9b) is generalized
        # at the execute() boundary — normalize_tool_result surfaces a
        # responses[] where every client reported ok:false as a top-level
        # failure for every tool, not just this one.
        return out

    def _gev_tour_card(self, tour: str | None) -> dict[str, Any]:
        """Named GEV flyover tours — the absorbed gev_tour body
        (tools-merge-gev). tour=None lists them; an id/alias returns the
        executable card. A tour card is NOT self-running: drive each stop
        with gev_command/vcast_say/cast_to_screen per the card's per_stop
        recipe."""
        try:
            data = json.loads(self._TOURS_PATH.read_text())
        except Exception as exc:
            return {"error": f"tour registry unreadable: {exc}"}
        tours = data.get("tours") or {}
        if not tour:
            return {"tours": {k: {"title": v.get("title"),
                                  "stops": len(v.get("stops") or []),
                                  "aliases": v.get("aliases")}
                              for k, v in tours.items()},
                    "hint": "gev_command(tour='<id or alias>') returns the "
                            "stop list to execute"}
        q = tour.strip().lower()
        hit = next((k for k, v in tours.items()
                    if q == k or q in (v.get("aliases") or [])
                    or q in str(v.get("title") or "").lower()), None)
        if not hit:
            return {"error": f"no tour matches '{tour}'",
                    "tours": sorted(tours)}
        t = dict(tours[hit])
        t["id"] = hit
        t["per_stop"] = (
            "For each stop: gev_command fly_to_location "
            "{latitude:<lat>, longitude:<lon>} → annotate_map with "
            "COORDINATES {annotations:[{type:'pin', latitude:<lat>, "
            "longitude:<lon>, label:<label>}]} — place-name resolution "
            "is unreliable, never annotate by 'target' name → "
            "cast_to_screen(action='say', text=...) narration from 'say'. If the stop has a "
            "frame_url, also cast_to_screen(action='image', url=frame_url, "
            "pane=1) to pin the nearest CCTV camera next to the map. "
            "If the tour has route_points instead of stops: annotate_map "
            "{type:'route', points:[...]}, then fly_route {speed:'fast'}.")
        return {"output": t}

    @staticmethod
    def _cast_screens_cfg() -> dict[str, Any]:
        """vcast screen-ownership registry (screen -> person.*|'shared',
        speaker aliases -> person). Missing file = no gating."""
        import json as _json
        try:
            with open(os.path.expanduser(
                    os.environ.get("ADA_CAST_SCREENS",
                                   "~/.local/share/ada-pi/cast-screens.json"))) as f:
                return _json.load(f)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _private_screen_denial(what: str, owner: str, person: str) -> PermissionError:
        """Owner-lock denial text. Must tell the model that the ACL lives in
        cast-screens.json — a memory note (ada_remember) can never change it,
        so 'record it as public then retry' loops forever."""
        who = owner.replace("person.", "")
        lock = ("owner-locks live in cast-screens.json; memory notes cannot "
                f"change them — {who} flips the entry to \"shared\" there "
                "if it should be public")
        if person == "anonymous":
            return PermissionError(
                f"denied: {what} is {who}'s private screen — speaker "
                f"unidentified. If the speaker is {who}, retry once "
                f"speaker-ID resolves them; {lock}")
        return PermissionError(
            f"denied: {what} is {who}'s private screen "
            f"(speaker={person.replace('person.', '')}) — {lock}")

    async def _check_screen_owner(self, screen: int, ident: str | None) -> None:
        """vcast screens are owner-locked: a speaker may only cast to their
        own screen unless the screen is 'shared'."""
        cfg = self._cast_screens_cfg()
        if not cfg:
            return  # no registry — don't gate
        owner = (cfg.get("screens") or {}).get(str(int(screen)), "shared")
        s = (ident or "").strip()
        person = (s if s.startswith("person.")
                  else (cfg.get("aliases") or {}).get(s, "anonymous"))
        if owner == "shared" or owner == person:
            return
        raise self._private_screen_denial(f"screen {screen}", owner, person)

    def _check_tv_source_owner(self, target: str, ident: str | None) -> None:
        """Gate personal desktop streams in tv_action nav targets
        (screenlive:* / workspace:* / screen:* = the tony-dell seat,
        tony-omen:* = Tony's desktop). HA's rest_command swallows the
        controller's 403, so deny here before the call ever leaves.
        cast-browser still enforces as the authoritative backstop."""
        cfg = self._cast_screens_cfg()
        if not cfg:
            return
        t = (target or "").lower()
        source = None
        if re.match(r"^(screenlive|screen:|workspace)", t):
            source = "seat"
        elif t.startswith("tony-omen:"):
            source = "omen"
        if not source:
            return  # plain URL / named page — no personal screen involved
        owner = (cfg.get("sources") or {}).get(source, "shared")
        s = (ident or "").strip()
        person = (s if s.startswith("person.")
                  else (cfg.get("aliases") or {}).get(s, "anonymous"))
        if owner == "shared" or owner == person:
            return
        raise self._private_screen_denial(source, owner, person)

    _TV_DEFAULT_ENTITY = "media_player.tony_tv"
    _TV_DEFAULT_OK_APPS = ("browser", "com.webos.app.browser")

    def _tv_cfg(self) -> dict[str, Any]:
        """TV input-visibility config. A vcast screen rendered in the
        TV's webOS browser is only VISIBLE when the TV's foreground app
        is the browser — a delivered cast on another input looks like a
        no-op to the viewer (2026-10-04: cctv_wall 'delivered' while the
        TV sat on the True STB app for 3min). Registry 'tv' block in
        cast-screens.json: {"entity": "media_player.tony_tv",
        "screens": [5], "ok_apps": ["browser"]} — ok_apps match the
        media_player's app_id/source as substrings (case-insensitive).
        Env overrides: ADA_TV_ENTITY, ADA_TV_SCREENS (csv),
        ADA_TV_OK_APPS (csv)."""
        raw = self._cast_screens_cfg()
        tv = raw.get("tv") if isinstance(raw.get("tv"), dict) else {}
        entity = (os.environ.get("ADA_TV_ENTITY") or tv.get("entity")
                  or self._TV_DEFAULT_ENTITY)
        env_screens = os.environ.get("ADA_TV_SCREENS")
        if env_screens is not None:
            screens = {int(s) for s in env_screens.split(",")
                       if s.strip().isdigit()}
        else:
            screens = {int(s) for s in (tv.get("screens") or [])
                       if str(s).strip().isdigit()}
        env_apps = os.environ.get("ADA_TV_OK_APPS")
        if env_apps is not None:
            ok_apps = [a.strip().lower() for a in env_apps.split(",")
                       if a.strip()]
        else:
            ok_apps = [str(a).lower() for a in
                       (tv.get("ok_apps") or self._TV_DEFAULT_OK_APPS)]
        return {"entity": entity, "screens": screens, "ok_apps": ok_apps}

    async def _tv_input_state(self) -> dict[str, Any]:
        """Read the TV's foreground app via its HA media_player entity.
        Returns {entity, state, app, on_cast_app} — on_cast_app is True
        when the TV shows the cast surface (browser), False on a foreign
        input/app or when the TV is off, and None when HA cannot say
        (unreachable, entity missing, no app attribute). Never raises."""
        cfg = self._tv_cfg()
        out: dict[str, Any] = {"entity": cfg["entity"],
                               "on_cast_app": None}
        try:
            st = await self.context.ha_client.get_state(cfg["entity"])
        except Exception:
            return out
        if not isinstance(st, dict):
            return out
        out["state"] = str(st.get("state") or "unknown")
        attrs = st.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        app = str(attrs.get("app_id") or attrs.get("app_name")
                  or attrs.get("source") or "")
        out["app"] = app
        if out["state"] in ("off", "standby", "unavailable", "unknown"):
            out["on_cast_app"] = False
        elif app:
            out["on_cast_app"] = any(
                pat in app.lower() for pat in cfg["ok_apps"])
        return out

    def _tv_abs_url(self, url: str) -> str:
        """Root-relative vcast paths can't be pushed at the TV browser —
        resolve them against the public relay origin (same base the
        camwall fallback uses; every tony-dell origin serves /apps)."""
        url = str(url or "")
        if url.startswith(("http://", "https://")) or not url:
            return url
        import urllib.parse
        parts = urllib.parse.urlsplit(os.environ.get(
            "VCAST_PUBLIC_API",
            "https://tony-dell.taila0626a.ts.net/api/input-bridge"))
        return f"{parts.scheme}://{parts.netloc}{url}"

    async def _tv_input_verify(self, url: str) -> dict[str, Any]:
        """Post-cast truth check for a TV-hosted screen. Reads the TV's
        foreground app via HA; a foreign app means the delivered cast is
        invisible. Remediation is the proven 2026-10-04 workaround: push
        the URL straight at the TV browser (tv_action nav), which
        foregrounds the browser app and shows the page. The result block
        always reports what actually happened — a still-wrong input is a
        visible failure, not a silent 'ok'."""
        first = await self._tv_input_state()
        if first.get("on_cast_app") is not False:
            return {"tv_input": first}
        full_url = self._tv_abs_url(url)
        rem: dict[str, Any] = {"action": "tv_browser_nav",
                               "url": full_url}
        try:
            nav = await self.tv_action(cmd="nav", text=full_url)
            rem["ok"] = not (isinstance(nav, dict)
                             and nav.get("ok") is False)
        except Exception as exc:
            rem["ok"] = False
            rem["error"] = str(exc)
            nav = None
        # The nav's own post-check (runner.tv_action appends tv_input on
        # cmd=nav) doubles as the re-read; fall back to a direct read.
        after = nav.get("tv_input") if isinstance(nav, dict) else None
        if not isinstance(after, dict) or after.get("on_cast_app") is None:
            after = await self._tv_input_state()
        if isinstance(after, dict) and after.get("app"):
            rem["after_app"] = after["app"]
        visible = after.get("on_cast_app")
        if visible is True:
            note = ("the TV was on a different input — the URL was pushed "
                    "to the TV browser and is now showing; tell the user "
                    "the TV input was switched to it.")
        elif visible is False:
            note = ("WARNING: the TV is still on "
                    f"{after.get('app') or after.get('state')} — the cast "
                    "is NOT visible. Do not claim it is showing; tell the "
                    "user plainly and suggest switching the TV "
                    "input/source manually.")
        else:
            note = ("the TV was on a different input — pushed the URL to "
                    "the TV browser, but the TV's input state could not "
                    "be re-verified; do not claim it is showing until "
                    "checked.")
        return {"tv_input_mismatch": True, "tv_input": first,
                "tv_remediation": rem, "tv_input_after": after,
                "tv_note": note}

    async def _screen_busy(self, screen: int,
                           pane: int | None = None) -> dict[str, Any] | None:
        """Is a screen running a flow a new cast would interrupt? Returns
        {'kind','desc'} for active capture leases, enabled camwall zones
        and playing media; None for idle/nav/image/speak states.
        pane=N targets a sub-pane: exempt when the screen is actually
        split (panes>1 in the registry) — it fills a cell, not the whole
        display. On a non-split screen pane 0 is the whole screen."""
        import asyncio
        if pane is not None:
            try:
                disp = await asyncio.to_thread(self._vcast_api, "/displays")
                n = next((s.get("panes") or 1
                          for s in disp.get("screens", [])
                          if str(s.get("screen") or "") == str(screen)), 1)
                if n > 1:
                    return None
            except Exception:
                pass
        try:
            caps = await asyncio.to_thread(self._vcast_api, "/capture")
            for k, cap in (caps.get("captures") or {}).items():
                if str(k) == str(screen) and cap.get("active"):
                    # A capture lease outlives its display: the wall page
                    # clears it on nav, but a user cast or reconnect leaves
                    # it active — false would_interrupt forces a manual
                    # reset (2026-09-30 transcript). Trust the screen's own
                    # reported state: if it's connected and NOT showing a
                    # capture, the lease is stale — release it.
                    try:
                        disp = await asyncio.to_thread(
                            self._vcast_api, "/displays")
                        scr = next(
                            (s for s in disp.get("screens", [])
                             if str(s.get("screen") or "") == str(screen)),
                            {})
                    except Exception:
                        scr = {}
                    st = str(scr.get("state") or "")
                    det = str(scr.get("state_detail") or "")
                    if (scr.get("connected")
                            and "camwall" not in st + det
                            and "uplink" not in st + det
                            and "capture" not in st + det):
                        try:
                            await asyncio.to_thread(
                                self._vcast_api, "/capture",
                                {"screen": int(k), "active": False})
                        except Exception:
                            pass
                        break
                    return {"kind": "capture",
                            "desc": f"a camera capture/uplink "
                                    f"({cap.get('source') or 'cam'}) is "
                                    "live on it"}
        except Exception:
            pass
        try:
            wall = await asyncio.to_thread(self._vcast_api, "/camwall")
            zones = {z for z, v in (wall.get("zones") or {}).items()
                     if v.get("enabled")
                     and str(v.get("screen") or "") == str(screen)}
            if zones:
                # A zone's screen binding goes stale constantly: the puller
                # keeps refreshing thumbs long after the display moved on.
                # Trust the screen's own reported state over the flag —
                # only treat the wall as "busy" if the display still shows
                # it (or the display didn't answer at all).
                try:
                    disp = await asyncio.to_thread(
                        self._vcast_api, "/displays")
                    scr = next(
                        (s for s in disp.get("screens", [])
                         if str(s.get("screen") or "") == str(screen)), {})
                except Exception:
                    scr = {}
                detail = str(scr.get("state_detail") or "")
                live_wall = ("camwall" in detail
                             or any(z in detail for z in zones))
                if scr.get("state") is None or live_wall:
                    z = sorted(zones)[0]
                    return {"kind": "camwall", "zone": z,
                            "desc": f"the '{z}' camera wall is "
                                    "refreshing on it"}
                # stale flag — clear it so the next check is clean
                for z in zones:
                    try:
                        await asyncio.to_thread(
                            self._vcast_api, "/camwall",
                            {"zone": z, "screen": None})
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            disp = await asyncio.to_thread(self._vcast_api, "/displays")
            st = next((s.get("state") for s in disp.get("screens", [])
                       if str(s.get("screen") or "") == str(screen)), None)
            if st == "playing":
                return {"kind": "playing",
                        "desc": "a stream/video is playing on it"}
        except Exception:
            pass
        return None

    async def cast_to_screen(self, screen: int | None = None,
                             action: str = "nav",
                             url: str = "", pane: int | None = None,
                             panes: int | None = None,
                             mode: str | None = None,
                             interval: int | None = None,
                             text: str = "",
                             shortcut: str = "",
                             confirmed: bool = False) -> dict[str, Any]:
        """Cast to a numbered vcast virtual display (NOT the TV).
        action: nav|play|image|audio|stop|layout|zoom|unzoom|uplink|
        uplink-stop, plus the merged vcast_* seats (tools-merge-display):
        'cast' (auto-route by content), 'say' (was vcast_say — narrate on
        the display), 'list' (was vcast_list — enumerate displays),
        'status' (was vcast_status — one screen's live state) and
        'shortcut' (was vcast_shortcut — nav a named /apps/<name>/ app).
        url required for nav/play/image/audio/cast; text for say.

        action='image' is for still frames (JPEG/PNG) — optional
        interval=N re-fetches the image every N seconds (good for
        traffic-cam stills that refresh server-side). 'play' is ONLY for
        real video streams (mp4/HLS) — a still image sent to play renders
        a black video pane, so this tool auto-routes image content to
        'image'.

        Content routing: web pages -> 'nav'; video files/streams (mp4,
        m3u8) and YouTube/Vimeo watch URLs -> 'play' (the display
        auto-rewrites them to embed players — pass the URL as-is, do NOT
        use yt action='cast', which is the TV only); still images -> 'image';
        audio-only -> 'audio'.

        Split-screen: action='layout' + panes=2..5 splits the screen into
        that many sub-panes; subsequent casts take pane=0..N-1 (0 is
        left/top). action='zoom' + pane=N makes one pane fullscreen,
        'unzoom' (or zoom pane=-1) returns to the grid. A stop with pane=N
        clears just that pane; stop alone resets the whole screen to
        single-pane idle."""
        import asyncio
        action = str(action or "nav").lower()
        # Merged vcast_* seats — the reads run without a screen and before
        # the owner check (listing displays was never owner-locked); 'say'
        # delegates to the absorbed vcast_say body, which owner-checks the
        # screen itself.
        if action == "list":
            return await self.vcast_list()
        if action == "status":
            return await self._vcast_display_status(screen)
        if screen is None:
            return {"ok": False, "delivered": 0,
                    "error": "screen number required — "
                             "cast_to_screen(action='list') shows the "
                             "registered displays."}
        screen = int(screen)
        if action == "say":
            return await self.vcast_say(screen, text)
        if action == "shortcut":
            url = self._cast_shortcut_url(url or shortcut)
            if not url:
                return {"ok": False, "delivered": 0, "error": (
                    "action='shortcut' needs an app name ('gev', "
                    "'camwall', …) or a URL/path in url")}
            action = "nav"
        await self._check_screen_owner(screen, self._memory_identity())
        # Interrupt gate: replacing content on a busy screen (camera
        # capture, camwall zone, playing stream) needs the user's yes —
        # Ada must say what's running and get consent before clobbering.
        # layout alone is exempt: it reshapes the screen but keeps the
        # content panes it can carry (a capture lease isn't clobbered by
        # regridding). zoom/unzoom/stop-of-pane are user-directed UI ops.
        busy = None
        # 'cast' is checked pre-routing too — it always lands on a
        # content action, and an unprobed 'cast' onto a busy screen must
        # confirm just like a nav/play would.
        if action in {"nav", "play", "image", "audio", "uplink", "cast"}:
            busy = await self._screen_busy(screen, pane)
            if busy and confirmed is not True:
                return {"ok": False, "delivered": 0,
                        "would_interrupt": busy,
                        "needs_confirm": (
                            f"screen {screen} is busy: {busy['desc']} "
                            "Tell the user what is running, ask whether to "
                            "replace it, then call again with "
                            "confirmed=true only after they say yes. "
                            "If the user instead says to reset/clear/stop "
                            "the screen, use cast_to_screen(action='stop') "
                            "first — that releases it without confirm.")}
        probe: dict[str, Any] = {}
        auto_image = False
        cast_routed = False
        if action == "layout":
            msg: dict[str, Any] = {"type": "layout",
                                   "panes": int(panes or 1)}
            if mode:
                msg["mode"] = str(mode)
        elif action in {"zoom", "unzoom"}:
            msg = {"type": "zoom",
                   "pane": -1 if action == "unzoom" else int(pane or 0)}
        elif action in {"stop", "uplink", "uplink-stop"}:
            msg = {
                "type": "uplink-start" if action == "uplink" else action}
        else:
            if action == "cast":
                # Generic cast: pick the pane type from what the URL
                # actually serves — a still image in <video> renders
                # black, a page in an image pane never loads.
                if not url:
                    raise ValueError("url is required for action='cast'")
                if str(url).startswith(("http://", "https://")):
                    try:
                        probe = await asyncio.to_thread(
                            self._frame_check, str(url))
                    except Exception:
                        probe = {}
                ctype = str(probe.get("content_type") or "").lower()
                if ctype.startswith("image/"):
                    action = "image"
                elif ctype.startswith("audio/"):
                    action = "audio"
                elif (ctype.startswith("video/") or "mpegurl" in ctype
                      or re.search(
                          r"(youtube\.com|youtu\.be|youtube-nocookie\.com"
                          r"|vimeo\.com)", str(url))
                      or re.search(
                          r"\.(m3u8|mp4|webm|mov|m4v)(\?|#|$)",
                          str(url), re.I)):
                    action = "play"
                else:
                    action = "nav"
                cast_routed = True
            if action not in {"nav", "play", "image", "audio"}:
                raise ValueError(
                    f"unknown action {action!r} (cast|nav|play|image|audio|stop|layout|zoom|unzoom|uplink|uplink-stop|say|list|status|shortcut)")
            if not url:
                raise ValueError("url is required for " + action)
            # Pre-flight for nav/play: probe the target BEFORE pubbing —
            # a dead URL or a still image sent to <video> both render as a
            # black pane that looks identical to "it didn't work"
            # (2026-10-01: Ada cast a JPEG snapshot with action='play' and
            # an invented URL that didn't even resolve; screen 1 just
            # stayed on the previous camera with no visible error).
            if (action in {"nav", "play"} and not probe
                    and str(url).startswith(("http://", "https://"))):
                try:
                    probe = await asyncio.to_thread(
                        self._frame_check, str(url))
                except Exception:
                    probe = {}
            if probe.get("dead"):
                return {
                    "ok": False, "delivered": 0,
                    "error": (f"URL is unreachable ({probe['dead']}) — not "
                              "casting. Never invent a URL: re-fetch a "
                              "current one from the source tool (e.g. "
                              "ada_camera_snapshot returns cast_url) and "
                              "retry "
                              "with that exact value.")}
            if (action == "play"
                    and str(probe.get("content_type") or "").lower()
                    .startswith("image/")):
                # Still image in a <video> element never decodes —
                # auto-route to the image pane so the cast works instead
                # of producing a black video-vw0 pane.
                action = "image"
                auto_image = True
            msg = {"type": action, "url": url}
            if interval is not None and action == "image":
                msg["interval"] = int(interval)
        if pane is not None and action not in {"layout", "unzoom"}:
            msg["pane"] = int(pane)
        # Snapshot what the screen is showing BEFORE a full-frame nav/play/
        # image replaces it — a camwall nav'd over a GEV page silently kills
        # the map session (2026-10-02 ZA tour: cctv_wall to screen 5 evicted
        # the tour with no warning; Ada should have used a PiP pane).
        displaced: str | None = None
        if action in {"nav", "play", "image", "audio"} and pane is None:
            try:
                disp = await asyncio.to_thread(self._vcast_api, "/displays")
                scr = next(
                    (s for s in disp.get("screens", [])
                     if str(s.get("screen") or "") == str(screen)), None)
                if scr and scr.get("connected"):
                    pd = str(scr.get("state_detail") or "")
                    if scr.get("state") == "nav" and pd and "gev" in pd:
                        displaced = ("GEV session — the map/annotations on "
                                     "this screen are gone; for a camera "
                                     "alongside the map use a split layout "
                                     "and cast_to_screen(image, pane=N)")
                    elif scr.get("state") == "nav" and pd:
                        displaced = f"the '{pd[:60]}' page"
            except Exception:
                pass
        warn = probe.get("warn")
        out = await asyncio.to_thread(
            self._vcast_api, "/pub", {"screen": screen, "msg": msg})
        if warn:
            out["frame_warn"] = warn
        if auto_image:
            out["action_fixed"] = (
                "url serves a still image — cast as 'image', not 'play' "
                "(a video element cannot decode it and shows black)")
        if cast_routed:
            out["action_routed"] = (
                f"action='cast' resolved to '{action}' from the URL's "
                "content type")
        # capture lease bookkeeping — the relay's /capture state is ground
        # truth for the ask-before-stopping contract; the display also POSTs
        # on uplink-start, but this covers display-offline cases
        if action in {"uplink", "uplink-stop", "stop"}:
            try:
                await asyncio.to_thread(self._vcast_api, "/capture", {
                    "screen": screen,
                    "active": action == "uplink",
                    "source": "cam",
                    "by": "ada",
                })
            except Exception:
                pass
        try:
            caps = await asyncio.to_thread(self._vcast_api, "/capture")
            out["active_captures"] = caps.get("captures") or {}
        except Exception:
            pass
        if busy:
            out["replaced"] = busy
            out["note"] = ("this cast interrupted something that was "
                           "running — acknowledge it to the user "
                           f"({busy['desc']}).")
        if displaced:
            out["displaced"] = displaced
        # Post-cast ground truth: pub only means the relay accepted the
        # frame — the display may still be showing the old page (iframe
        # refused, browser didn't navigate, stale client).  Re-read the
        # screen's own report so Ada claims only what the display claims.
        try:
            await asyncio.sleep(0.8)  # let the display ack state
            disp = await asyncio.to_thread(self._vcast_api, "/displays")
            scr = next(
                (s for s in disp.get("screens", [])
                 if str(s.get("screen") or "") == str(screen)), None)
            if scr is not None:
                detail = str(scr.get("state_detail") or "")
                out["screen_state"] = scr.get("state")
                out["screen_detail"] = detail
                if action in {"nav", "play", "image", "audio"} and url:
                    host = str(url).split("/")[2] if "//" in str(url) else str(url)
                    if url not in detail and host not in detail:
                        out["verify_warn"] = (
                            f"screen {screen} still reports "
                            f"'{detail[:80] or scr.get('state')}' — the new "
                            "page may not have loaded; do not claim it "
                            "changed.")
                # Dead render detection: 'video-vw0' means the video
                # element decoded nothing (a still image cast with
                # action='play' lands here — black pane); the image pane
                # reports 'image-load-failed' outright. Do not claim the
                # cast worked when the pane is dead.
                if (scr.get("state") == "image-load-failed"
                        or "video-vw0" in detail):
                    out["render_warn"] = (
                        f"screen {screen} reports no decodable content "
                        f"('{detail[:80] or scr.get('state')}') — it is "
                        "likely showing black or the previous page. If the "
                        "source is a still image, re-cast with "
                        "action='image'; if the URL is dead, fetch a fresh "
                        "one from the source tool. Do NOT tell the user it "
                        "changed.")
        except Exception:
            pass
        # TV-hosted screen: the display page can ack the nav while the TV
        # is on another input — verify and remediate via the TV browser
        # rather than claiming the cast is visible.
        if (action in {"nav", "play", "image", "audio"} and url
                and screen in self._tv_cfg()["screens"]
                and out.get("delivered", 1)):
            tv = await self._tv_input_verify(str(url))
            out.update(tv)
            if tv.get("tv_note"):
                out["note"] = (str(out.get("note") or "") + " "
                               + tv["tv_note"]).strip()
        return out

    async def _vcast_display_status(
            self, screen: int | None = None) -> dict[str, Any]:
        """cast_to_screen(action='status') — the seat for the absorbed
        vcast_status census name: one screen's live state (what it shows,
        panes, its capture lease and camwall zone). No screen -> the same
        full report as action='list'."""
        data = await self.vcast_list()
        if screen is None:
            return data
        n = int(screen)
        for s in data.get("screens") or []:
            if s.get("screen") == n:
                out = dict(s)
                out["ok"] = True
                caps = data.get("active_captures") or {}
                if str(n) in caps:
                    out["capture"] = caps[str(n)]
                zones = [z for z, v in (data.get("camwall_zones") or {})
                         .items() if v.get("screen") == n]
                if zones:
                    out["camwall_zones"] = zones
                if data.get("state_mismatch"):
                    out["state_mismatch"] = data["state_mismatch"]
                return out
        return {"ok": False,
                "error": f"screen {n} is not a registered display",
                "screens": [s.get("screen")
                            for s in data.get("screens") or []]}

    def _cast_shortcut_url(self, target: str) -> str | None:
        """cast_to_screen(action='shortcut') — resolve a short app name
        to its same-origin /apps/<name>/ page (the display resolves it
        against whatever origin served it — LAN or tailnet). Full URLs
        and / paths pass through unchanged."""
        t = str(target or "").strip()
        if not t:
            return None
        if t.startswith(("http://", "https://", "/")):
            return t
        slug = re.sub(r"[^a-z0-9_-]+", "", t.lower())
        return f"/apps/{slug}/" if slug else None

    @staticmethod
    def _frame_check(url: str) -> dict[str, Any]:
        """HEAD the nav/play target. Returns {warn, content_type, dead}:
        'warn' for XFO/CSP frame-ancestors blocks (or a dead YouTube id —
        oembed 404 caught a 'Video unavailable' cast 2026-09-29),
        'content_type' lets the caller reroute still images off 'play',
        'dead' marks a URL that cannot be fetched at all (DNS/connrefused
        — an invented or stale URL renders as a silent black pane)."""
        import urllib.request
        import urllib.error
        import urllib.parse
        out: dict[str, Any] = {}
        if re.search(r"(youtube\.com|youtu\.be|youtube-nocookie\.com)", url):
            try:
                oe = urllib.request.urlopen(
                    "https://www.youtube.com/oembed?format=json&url="
                    + urllib.parse.quote(url, safe=""), timeout=6)
                if oe.status == 200:
                    return out
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    out["warn"] = (
                        f"{url} is not a playable video (oembed 404 — "
                        "dead/removed/unlisted). Do NOT cast it; find "
                        "another video id first.")
                    return out
            except Exception:
                return out  # inconclusive — let the display try
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "ada-vcast/1.0"})
        try:
            resp = urllib.request.urlopen(req, timeout=6)
        except urllib.error.HTTPError as exc:
            resp = exc  # still carries headers
        except urllib.error.URLError as exc:
            # DNS failure / connrefused mean the display will fetch the
            # same dead URL — hard-fail instead of casting a black pane.
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, ConnectionRefusedError)) \
                    or "gaierror" in type(reason).__name__ \
                    or "timed out" in str(reason).lower() \
                    or "Name or service" in str(reason) \
                    or "refused" in str(reason).lower():
                out["dead"] = f"{type(reason).__name__}: {reason}"
                return out
            return out  # other transport errors: let the display try
        except Exception:
            return out
        hdrs = resp.headers
        ctype = (hdrs.get("Content-Type") or "").split(";")[0].strip()
        if ctype:
            out["content_type"] = ctype
        xfo = (hdrs.get("X-Frame-Options") or "").upper()
        if xfo.startswith(("DENY", "SAMEORIGIN")):
            out["warn"] = (
                f"{url} forbids iframe embedding "
                f"(X-Frame-Options: {xfo}) — the screen will show "
                "blank. Pick a different source or snapshot the feed "
                "instead of nav-ing the page.")
            return out
        csp = hdrs.get("Content-Security-Policy") or ""
        m = re.search(r"frame-ancestors\s+([^;]+)", csp, re.I)
        if m and "'*'" not in m.group(1) and "https:" not in m.group(1):
            out["warn"] = (
                f"{url} restricts framing via CSP frame-ancestors "
                f"({m.group(1).strip()}) — the screen may show blank; "
                "prefer a different source.")
        return out

    async def vcast_say(self, screen: int, text: str) -> dict[str, Any]:
        """Speak a short narration line on a vcast display. Web Speech
        synthesis is tried first; when the text is non-Latin (Thai) and the
        display lacks a matching voice it falls back to a backend-synthesized
        Gemini-TTS WAV fetched from the relay — same voice as Ada herself.
        If the display hasn't been tapped for audio yet, it shows a toast
        and reports speak-blocked instead of speaking."""
        import asyncio
        screen = int(screen)
        # Scrub markup before it reaches a synthesizer — CMS/memory content
        # carries HTML entities and markdown remnants the TTS would speak
        # verbatim ("&nbsp;ชัดเจน", "][พาสเจอร์ไรซ์]" — transcript
        # 519088cb6d). Truncate first so sanitize also drops a cut-off
        # partial entity at the tail.
        text = speech_sanitize.sanitize_speech(str(text or "").strip()[:300])
        if not text.strip():
            raise ValueError("text required")
        await self._check_screen_owner(screen, self._memory_identity())
        msg: dict[str, Any] = {"type": "speak", "text": text}
        # Backend TTS only for text the average lab browser can't voice —
        # Thai (or any non-Latin) reliably falls back to an English voice
        # otherwise (2026-10-02: every vcast_say on screen 5 came out
        # English despite th-TH lang tag — no Thai voice installed).
        if re.search(r"[\u0E00-\u0E7F\u0100-\u024F\u0370-\u03FF"
                     r"\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]", text):
            audio_url = await self._vcast_tts(text)
            if audio_url:
                msg["audio"] = audio_url
        return await asyncio.to_thread(
            self._vcast_api, "/pub", {"screen": screen, "msg": msg})

    async def _vcast_tts(self, text: str) -> str | None:
        """Synthesize `text` with Gemini TTS (same key + voice as the live
        session) and publish the WAV to the relay's /frame store — returns
        the public same-origin URL a vcast display can <audio>-fetch, or
        None on any failure (caller falls back to client-side TTS)."""
        import base64, io, wave, urllib.request
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY")
        if not api_key:
            return None
        try:
            from backend import voice_config
            model = os.environ.get(
                "VCAST_TTS_MODEL", "gemini-2.5-flash-preview-tts")
            def _synth() -> bytes | None:
                # plain REST — the google-genai sync client dies inside
                # to_thread with 'client has been closed'
                payload = json.dumps({
                    "contents": [{"parts": [{"text": text}]}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {"voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": voice_config.current_voice()}}},
                    }}).encode()
                req = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/"
                    f"models/{model}:generateContent?key={api_key}",
                    data=payload,
                    headers={"Content-Type": "application/json"})
                resp = json.load(urllib.request.urlopen(req, timeout=30))
                for part in (resp.get("candidates", [{}])[0]
                             .get("content", {}).get("parts") or []):
                    data = (part.get("inlineData") or {}).get("data")
                    if data:
                        return base64.b64decode(data)  # 24kHz s16le PCM
                return None
            pcm = await asyncio.to_thread(_synth)
            if not pcm:
                return None
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(pcm)
            token = f"say:{secrets.token_hex(4)}"
            self._vcast_api("/frame", {
                "screen": 0, "token": token,
                "data": "data:audio/wav;base64,"
                        + base64.b64encode(buf.getvalue()).decode(),
                "state": "asset"})
            pub = os.environ.get(
                "VCAST_PUBLIC_API",
                "https://tony-dell.taila0626a.ts.net/api/input-bridge")
            return f"{pub}/frame?screen=0&token={token}"
        except Exception:
            logger.exception("vcast tts failed")
            return None
