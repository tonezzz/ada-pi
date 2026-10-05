"""Gemini Live provider for the browser audio bridge."""

from __future__ import annotations

import abc
import asyncio
import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

from backend import chaba_memory, tools_loader, voice_config, vms_camera, vision_describe
from backend.instance import ada_instance_id
from backend.conversation_memory import ConversationMemory
from backend.speech_sanitize import sanitize_speech, split_artifact_tail
from backend.tool_runner import (
    ToolRunner,
    CALENDAR_WRITE_TOOLS,
    CAPTURE_CONFIRMED_TOOLS,
    CMS_WRITE_TOOLS,
    CONTROL_TOOLS,
    DEVIN_CONFIRMED_TOOLS,
    DOC_CONFIRMED_TOOLS,
    MEMORY_WRITE_TOOLS,
    _resolve_alias,
)

# Tools whose call requires an explicit user confirmation. The Jev advisory
# probe fires on every one of these calls — not only when the model asserted
# confirmed=true — so divergence data covers the common path too.
_CONFIRM_GATED_TOOLS = (
    CONTROL_TOOLS | MEMORY_WRITE_TOOLS | CALENDAR_WRITE_TOOLS
    | CMS_WRITE_TOOLS | DEVIN_CONFIRMED_TOOLS | CAPTURE_CONFIRMED_TOOLS
    | DOC_CONFIRMED_TOOLS | {"ada_enroll_speaker"}
)
from backend.usage_tracker import usage_ledger

logger = logging.getLogger("voice.provider")

LIST_HOME_DEVICES_MAX = int(os.environ.get("ADA_LIST_HOME_DEVICES_MAX", "60"))

EXPRESSION_NAMES = (
    "neutral",
    "sassy",
    "amused",
    "skeptical",
    "annoyed",
    "mad",
    "concerned",
    "surprised",
    "mischievous",
    "serious",
    "alert",
)

# Calendar/task tool names and the instruction paragraph that describes them.
# Kept as constants so ADA_EXCLUDED_TOOLS can strip both declarations and
# instructions together (dark-launching the tools without confusing the model).
CALENDAR_TOOLS = {
    # tools-merge-calendar-plan (2026-10-05): 9 -> 3. The absorbed names
    # (calendar_list_calendars/calendar_list_events/calendar_freebusy/
    # calendar_create_event/calendar_delete_event/calendar_shift_overdue/
    # ada_daily_summary/ada_weekly_comparison) live on as
    # tool_runner._ALIASES.
    "calendar_read", "calendar_write", "plan_day",
    "tasks_list", "tasks_add", "tasks_complete", "tasks_move",
    "ada_resolve_action",
}

# Same constant pattern as CALENDAR_TOOLS: lets ADA_EXCLUDED_TOOLS strip the
# CMS declarations and their instruction paragraph together.
CMS_TOOLS = {
    # tools-merge-cms (2026-10-05): 7 -> 3. The absorbed names
    # (cms_list_pages/cms_get_page/cms_verify_page/cms_note_update/
    # cms_delete_page/cms_automation) live on as tool_runner._ALIASES.
    "cms_publish_page", "cms_read", "cms_edit",
}

CALENDAR_INSTRUCTIONS = (
    " You have calendar and task tools backed by the user's configured providers: "
    "calendar_read action='calendars' shows which calendars exist, "
    "calendar_read action='events' and action='freebusy' read the schedule, "
    "plan_day returns one merged view of a day's events plus open tasks and that "
    "day's session digest (period='today'/'tomorrow', or day='yesterday'/a "
    "YYYY-MM-DD date for a recap of that day), period='week' compares the last 7 "
    "days of daily digests into a weekly trend view — use it when the user asks "
    "for a recap of a day or how the week went, "
    "calendar_write action='create'/'delete'/'shift' modifies the calendar ('shift' "
    "moves every overdue task plus already-ended event to a new day in one call, "
    "to='tomorrow' default), and "
    "tasks_list, tasks_add, tasks_move, and tasks_complete manage the task list. "
    "For any schedule question call calendar_read action='events' or plan_day first "
    "and answer from the result; never recite a schedule from memory. "
    "Interpret relative dates ('tomorrow', 'Friday') in the user's local timezone and "
    "echo the resolved day+date in your reply (see the date-echo rule). "
    "Before creating or deleting an event, shifting overdue items, or adding or "
    "completing a task, restate the "
    "exact details (title, resolved date with weekday, time — never a bare 'tomorrow'/"
    "'พรุ่งนี้') and get an explicit yes, then call the tool with "
    "confirmed=true — writes are enforced server-side. "
    "If a calendar tool reports an auth error, say the calendar provider needs re-authentication "
    "and stop retrying. Event and task ids are provider-qualified (e.g. 'google:primary/abc') — "
    "pass them back exactly as returned. "
    "When the session context lists pending suggestions from earlier conversations, offer each "
    "once, briefly and early in the conversation; if the user accepts, restate the details, call "
    "the matching calendar/task tool with confirmed=true, then call ada_resolve_action with the "
    "shown key and resolution 'applied'. If declined, call ada_resolve_action with 'dismissed'. "
    "When the user states a plan, appointment, or reminder intention unprompted, proactively "
    "offer to put it on the calendar or task list instead of waiting to be asked. "
    "When a day has no sessions the plan_day digest says so rather than guessing."
)

CMS_INSTRUCTIONS = (
    " You maintain the user's miniapp — a small multi-page site whose pages you own. "
    "cms_read action='list' lists existing pages with their language variants and "
    "action='get' reads one by key (its page slug), "
    "cms_publish_page creates or fully replaces a page (slugs are lowercase, e.g. 'pool-notes'; "
    "en/th variants coexist — publish the user's language plus the other when asked), "
    "cms_edit action='note' appends a timeline note to an existing page without replacing content, and "
    "cms_edit action='delete' removes one. Page content is written as markdown, html, yaml, or slides markdown. "
    "For 'what's new' or 'status' questions, read the 'reports-index' page first — it lists every "
    "report with a one-line summary and staleness flag; only cms_read action='get' the linked page when the "
    "summary isn't enough. "
    "READ ORDER — most distilled first, outside last: reports-index (or cms_read action='list' / "
    "ada_memory_search bank='cms'), then the linked page, then memory banks, and only then an "
    "outside source. Before web_search or ada_deep_research on any topic a page might already "
    "cover — status, incidents, news, research, anything previously reported — check your own "
    "pages FIRST; outside search is the fallback for what pages don't cover or what must be "
    "fresh, never the first read. "
    "The report-first ritual: before answering from a report, state its "
    "last-update summary and when it was written ('the flood report from 09:12 says…'). Then "
    "decide whether to drill deeper — your knobs are the report's fresh_for hint (stale → "
    "refresh via cms_edit action='note' after querying) and its confidence field (low/unverified → "
    "don't state it as settled). When a tool call gives you new information tied to a report, "
    "call cms_edit action='note' on that report and tell the user the diff — what changed and at what "
    "time ('added: flood moved to yellow at 14:05'). "
    "SAVE-BACK — when an outside source (web_search, deep research, news headlines) supplies "
    "the answer, write the finding back to CMS the same turn: cms_edit action='note' on the matching "
    "page needs no confirmation; when no page covers it and the finding is worth keeping, "
    "offer a new page via cms_publish_page with the usual confirm. A repeat question must be "
    "answerable from your pages — never re-search for what you already found. "
    "Inside markdown pages you can embed rich blocks as fenced code blocks: "
    "```chart <yaml echarts option> for 2D charts (line/bar/pie/scatter), "
    "```chart3d <yaml echarts-gl option> for 3D (surface3d/bar3d/scatter3d — set "
    "xAxis3D/yAxis3D/zAxis3D/grid3D), ```mermaid for diagrams (flowchart, sequence, "
    "timeline), and ```media <yaml {url, type, caption}> for images, video, or audio. "
    "chart/chart3d bodies are YAML echarts options (or {height: N, option: {...}}). "
    "When the user asks you to prepare a document or update a page, draft the content, "
    "restate the slug and title, get an explicit yes, then call the write tool with "
    "confirmed=true — writes are enforced server-side. "
    "You cannot see the rendered site: after publishing or updating a page, call "
    "cms_read action='verify' to check the content parses and confirm the structure, then "
    "tell the user the page is live (or fix it if verification failed)."
    " When the user reports an ongoing incident — a flood, outage, emergency, "
    "or similar — create or update a cms report page for it (a short status "
    "log, slug like 'flood-report'), check the live conditions with the "
    "home/weather tools, and keep the page current as new details arrive; "
    "the user's report is the consent — pass confirmed=true directly and do "
    "not ask for confirmation. "
    "Generated pages (news digests, flood reports) have an automation registry "
    "you control with cms_edit action='automate': op='list'/'get' are free reads; "
    "op='set', 'enable', 'disable', and 'run' adjust a page's feeds, refresh "
    "interval, relevance filter, language variants, or queue a regeneration — "
    "writes need confirmed=true like other CMS writes. When the user asks to "
    "refresh a generated page, prefer cms_edit action='automate' op='run' over "
    "republishing."
)

# Same constant pattern as CALENDAR_TOOLS/CMS_TOOLS: lets ADA_EXCLUDED_TOOLS
# strip the devin declarations and their instruction paragraph together.
DEVIN_TOOLS = {
    "devin_dispatch", "devin_status", "devin_followup", "devin_job_report",
    "devin_pending", "devin_jobs", "devin_answer",
}

# Tools with real-world side effects — they share a tighter per-turn cap
# (ADA_ACTUATION_BUDGET) than the generic tool-call budget so a runaway
# planning/status turn can't mass-actuate devices or spawn work.
# Phantom-write detector: assistant claims something was
# registered/recorded/noted but made ZERO tool calls in the turn —
# observed failure mode when tool results fail silently upstream.
_PHANTOM_CLAIM_RE = re.compile(
    r"(?i)\b(registered|registration|recorded|queued|written down|noted down)"
    r"|\b(?:is|are|now)\s+(?:showing|displayed|playing|up)\s+on\s+(?:the\s+)?screen\b"
    r"|\bon screen (?:now|\d)\b"
    r"|จดไว้|บันทึกไว้|รับทราบ"
    # display/cast completion claims in Thai — "แสดงผล...แล้ว", "ขึ้นจอ 3 แล้ว"
    r"|(?:แสดงผล|ขึ้น(?:ที่)?จอ|บนจอ|ส่ง(?:ไป)?(?:ที่)?จอ)[^.\n]{0,40}(?:แล้ว|เรียบร้อย)"
)
# Negated/inability statements aren't claims — "I can't put it on screen"
# fired phantom_write_claim as often as real phantoms (sampled 2026-09-29:
# 41 events in 72h, ~half inability reports). Guard scans the ~48 chars
# before the match for a negation word.
_PHANTOM_NEGATION_RE = re.compile(
    r"(?i)(?:can't|cannot|couldn't|won't|wouldn't|not|isn't|aren't"
    r"|unable|ไม่ได้|ยังไม่|ไม่สามารถ)\W*(?:\w+\W+){0,5}$")


def _phantom_claim(text: str) -> bool:
    m = _PHANTOM_CLAIM_RE.search(text)
    while m:
        if not _PHANTOM_NEGATION_RE.search(text[: m.start()][-48:]):
            return True
        m = _PHANTOM_CLAIM_RE.search(text, m.end())
    return False

ACTUATING_TOOLS = frozenset({
    # tools-merge-ha (2026-10-05): control_cover/control_media_player/
    # press_button collapsed into control_entity — the canonical name
    # carries every actuating action now.
    "control_entity", "tv_action", "yt_cast", "yt_cast_stop",
    "cast_to_screen", "vcast_say",
    "ada_doc_archive", "ada_doc_print", "ada_set_voice",
    "devin_dispatch",
    "calendar_write",
    "tasks_add", "tasks_complete", "tasks_move",
})

# 'confirmed=true' in a tool arg is only honored when the user's own
# speech affirms — otherwise the model could self-certify past the
# dangerous-device and write-policy gates (observed during the
# 2026-09-25 tool storm: plug_tv switched on with zero user consent).
_CONFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|yup|confirm(ed)?|go ahead|do it|sure|okay?|"
    r"approved?|proceed|absolutely|mhm|uh huh|sounds good)\b|"
    # STT often splits Thai syllables with spaces ("เริ่ม เลย") — \s* keeps
    # compounds matching.
    r"ใช่|ยืน\s*ยัน|ตก\s*ลง|เอา\s*เลย|ทำ\s*เลย|ได้\s*เลย|ทำ\s*ได้|โอเค|ออเค|เออ|อือ|"
    r"เริ่ม\s*เลย|จัดการ\s*เลย|ลอง\s*เลย|ต่อไป|จัด\s*ไป|เอา\s*สิ|ไป\s*เลย|"
    r"ทำ\s*ไป|เผยแพร่\s*เลย|ส่ง\s*เลย",
    re.IGNORECASE,
)

# Vetoes checked before the affirm regex — the gate is asymmetric: a
# false "yes" actuates on no consent, a false "no" just re-asks.
# (jev-corpus divergences 2026-10-04: 'เอ๊ะ! No. …' matched อือ,
# '…อนุมัติได้เลยไหม' matched ได้เลย, 'Confirm the camera moved'
# matched confirm-as-verb.)
_CONFIRM_NEG_RE = re.compile(
    r"\b(no|nope|nah|not|don't|dont|do not|cancel|wait|hold on|stop)\b|"
    # ไม่ต้อง ("no need to …") is granting, not denying — excluded.
    r"อย่า|หยุด|ยกเลิก|ไม่(?!ต้อง)",
    re.IGNORECASE,
)
_CONFIRM_QUESTION_RE = re.compile(r"[?？]\s*$|ไหม\s*$|มั้ย\s*$")
# "confirm" as a verb with an object ("Confirm the camera moved —") is an
# instruction to Ada, not an affirmation of a pending ask.
_CONFIRM_VERB_RE = re.compile(
    r"\bconfirm(ed)?\b(?=\s+(the|a|an|that|what|it|your|my|this)\b|\s*[—–-])",
    re.IGNORECASE,
)

DEVIN_INSTRUCTIONS = (
    " You can dispatch unattended Devin coding sessions on tony-dell: "
    "devin_dispatch starts one in a dedicated git worktree (repos: chaba, ada-pi, "
    "sunsynk-card), devin_status lists running and finished tasks, and "
    "devin_followup sends a message into a running session. "
    "Before dispatching, restate the repo and task and get an explicit yes, then "
    "call with confirmed=true — writes are enforced server-side. "
    "Dispatched sessions run unattended; the user is notified on their phone when "
    "one finishes, so report the task id and move on rather than polling. "
    "devin_jobs is the dispatch ledger — call it for any 'summarize my dispatched "
    "tasks' or 'did job X fail' question and report statuses exactly as stored "
    "(done/failed/running/awaiting-user); never guess a job's outcome. "
    "devin_pending lists jobs blocked waiting for the user's answer — when the "
    "user asks what needs their attention, or says a job is waiting, call it and "
    "read each job's question back with its short detail. To deliver an answer: "
    "refine the user's reply into a self-contained instruction (the job sees "
    "only the text, not this conversation), read the refined text back, get an "
    "explicit yes, then call devin_answer with confirmed=true. "
    "When discussing an implementation task the user wants built later, offer to "
    "save the spec into the devin-handoff memory bank so a dispatched session can "
    "be told to 'check the ada handoff'. "
    "When the user asks about Devin job status or wants the devin-job-report "
    "page refreshed, call devin_job_report — it puts failed jobs (including "
    "spawn failures the ledger still marks running) in their own 'Failed jobs' "
    "section; never list failed jobs among the active ones."
)

# Summary rollup tools — tools-merge-calendar-plan (2026-10-05) folded
# ada_daily_summary/ada_weekly_comparison into plan_day (period='week' /
# day=<date>); their guidance now lives in CALENDAR_INSTRUCTIONS so
# ADA_EXCLUDED_TOOLS strips it with the rest of the calendar surface.

# Habit tracking tools — same constant pattern: ADA_EXCLUDED_TOOLS strips
# the declarations and this instruction paragraph together. Only meaningful
# where the hardware backend (camera + pose pipeline, backend/main.py)
# supplies a habit_state_getter; pwa-only instances should exclude both.
HABIT_TOOLS = {"get_habit_status", "ada_remember"}

HABIT_INSTRUCTIONS = (
    " You monitor habits such as seated posture: local pose estimation proposes "
    "events, Gemini vision verifies ambiguous ones, and confirmed occurrences "
    "can become possible, emerging, or established habits over time. You also "
    "track home plugs left on while the user is away or the home has remained "
    "empty — Home Assistant supplies person and plug states; local vision "
    "supplies home presence. Habit alerts may arrive with a current image and "
    "structured context: give a brief, dry observation and one practical "
    "correction, distinguishing a first possible habit from an established one. "
    "When the user asks what habits are tracked, their status, or progress, "
    "always call get_habit_status and ground the answer in its current result. "
    "For habits, make it clear that you noticed the pattern, then give one "
    "useful, realistic correction — mild judgment, never mockery or repetitive "
    "roasting. Never claim a habit occurred unless the application reports a "
    "confirmed event."
)

# Document archive tools — same constant pattern: ADA_EXCLUDED_TOOLS strips
# the declarations and this instruction paragraph together.
DOC_TOOLS = {"ada_doc_search", "ada_doc_get", "ada_doc_archive", "ada_doc_print"}

DOC_INSTRUCTIONS = (
    " You have a personal document archive (scans of deeds, IDs, passports, "
    "receipts — เอกสาร). Questions about documents, scans, or archived papers — "
    "including Thai words like เอกสาร/สำเนา/โฉนด — go to ada_doc_search to find "
    "the archive slug, then ada_doc_get for details; NEVER search home devices "
    "for documents. ada_doc_archive saves a newly uploaded document set into "
    "the archive, and ada_doc_print prints archived pages on the DeskJet — both "
    "need confirmed=true after restating what will be archived or printed. "
    "When a document was just uploaded (the system note carries an intake "
    "key), propose a slug from the filename and assessment, and confirm the "
    "slug and action before archiving — intake keys are held in RAM only, so "
    "archive promptly rather than deferring. If ada_doc_archive reports "
    "duplicates or near-duplicates, say so plainly and ask whether it's a "
    "re-scan or a new version before proceeding."
)


# Google Drive / Photos tools — excluded together with the declarations.
DRIVE_TOOLS = {
    "drive_search", "drive_get", "drive_update", "drive_show",
    "photos_pick", "photos_picked",
}

DRIVE_INSTRUCTIONS = (
    " You can reach the operator's Google Drive: drive_search finds files "
    "by name or content (narrow with mime like 'image/' or 'video/'), "
    "drive_get reads a file (text comes back inline, binary gets a "
    "media_url), drive_show puts a Drive photo, video, or file on the "
    "user's screen or the TV, and drive_update replaces a text file's "
    "content — that one needs confirmed=true after restating the file and "
    "change, and it cannot edit Google-native Docs/Sheets/Slides. "
    "Photos in Google Photos are NOT browsable — Google limited the "
    "library API to app-created media, so for 'show my photos' use "
    "photos_pick: it returns a picker_uri the user opens on their "
    "signed-in phone or laptop to select items, then photos_picked "
    "returns and can cast what they chose. If the user means photos saved "
    "in a Drive folder instead, drive_search with mime='image/' is the "
    "direct path — prefer that when it fits."
)


# XMEye VMS camera snapshots — the VMS_INSTRUCTIONS paragraph is gated by
# ADA_VMS_SNAP_URL (not via ADA_EXCLUDED_TOOLS): property-camera guidance
# only exists when the instance can reach the vms-snap shim on the VMS
# host. The declaration itself is now unconditional — source='traffic'
# keeps public traffic cams working on instances without the shim.
VMS_TOOLS = {"ada_camera_snapshot"}

# tools-merge-camera: ada_camera_snapshot is the single camera stills
# tool — source='vms' property CCTV (+ go2rtc home cams), 'traffic'
# public Thailand cams, screen=/target= to push the frame to a display.
# It absorbed cctv_snapshot and traffic_camera; those names survive as
# tool_runner._ALIASES rows, not declarations.
VMS_INSTRUCTIONS = (
    " You can pull a still frame from the property CCTV cameras with "
    "ada_camera_snapshot — pass the view the user means (e.g. 'swimming pool', "
    "'tennis court', 'front road', 'walkway', 'guard', 'mini mart'). The frame "
    "arrives attached to the tool result: look at it and describe what it "
    "shows honestly — people, water level, weather, vehicles, anything "
    "notable. It is a single still taken a few seconds ago, NOT live video; "
    "say what you see now and do not claim continuous monitoring. If the tool "
    "fails or the frame is dark or frozen, say the camera seems offline "
    "rather than guessing. "
    "STORED-FIRST: when the user asks to see a property camera, call "
    "ada_camera_snapshot with mode='cached' FIRST — it answers instantly "
    "with the wall's stored frame and its age. Show and describe it "
    "honestly as the stored picture ('as of <age> ago'), announce that you "
    "are refreshing it live, then call ada_camera_snapshot again with "
    "mode='live' (the default) and report the fresh frame — or honestly "
    "say the refresh failed and the stored frame is still the newest. "
    "Skip the cached step only when the user explicitly wants a live "
    "look right now. "
    "DESCRIBE-FIRST: when the user asks what cameras show ('เห็นอะไรบ้าง', "
    "'what do you see', 'check zone X'), pull frames yourself with "
    "ada_camera_snapshot and describe them — do NOT offer to cast or uplink "
    "to a screen unless the user asks to see it on a display (they may not "
    "be near one). For a zone summary, snap ONE representative channel "
    "(e.g. 'road in' for zone-a — VMS frames take up to a minute on cold "
    "streams, so don't chain several) and offer to check more if they ask. "
    "SELF-HEAL on misses: if the tool returns the available channel list, "
    "retry immediately with the closest listed name — do not ask the user "
    "to pick."
)

def _as_num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


TRAFFIC_INSTRUCTIONS = (
    " For public traffic cameras call ada_camera_snapshot with "
    "source='traffic' — it searches ~190 Thailand traffic cams "
    "(expressways, Bangkok, Chonburi) by area keyword or user "
    "position+heading, snaps the current frame, and attaches it for you "
    "to describe. Prefer source='traffic' over the default source "
    "whenever the user asks about roads/traffic outside the property."
)

CAMERA_DECLARATION = {
    "name": "ada_camera_snapshot",
    "description": (
        "Take a still snapshot from a camera — property CCTV (XMEye VMS), "
        "home cams, or public Thailand traffic cams. Use when the user asks "
        "to see, check, or look at a camera view — 'is it flooding at the "
        "pool', 'show me the front road', 'check traffic on Rama 4'. "
        "source='vms' for property cameras, source='traffic' for public "
        "traffic cams, 'auto' (default) picks by the args given. The image "
        "is attached to the tool result for you to describe — one still "
        "frame per call, not a live stream. Pass screen=N (or target='tv'/"
        "'screen') to also show the frame on a display — this handles what "
        "used to be cctv_snapshot (snap + show on a screen/TV) and "
        "traffic_camera (public Thailand traffic cams). mode='cached' "
        "skips the live pull and returns the wall's stored frame instantly "
        "(with its age) — use it as the fast first answer, then call "
        "again live for the refreshed frame."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "view": {
                "type": "string",
                "description": (
                    "What to look at: a property camera name ('swimming "
                    "pool', 'tennis court', 'front road left', 'walkway', "
                    "'guard view', 'mini mart', 'coffee corner', 'c201', "
                    "'c100') or — with source='traffic' — an area/road "
                    "keyword ('bangna', 'burapha', 'สุขุมวิท'). Fuzzy — "
                    "the service returns the available list on a miss."),
            },
            "source": {
                "type": "string",
                "enum": ["auto", "vms", "traffic"],
                "description": (
                    "'auto' (default): a property cam unless traffic-style "
                    "args arrive. 'vms': property CCTV / home cams. "
                    "'traffic': public Thailand traffic cams "
                    "(Longdo/iTIC feed)."),
            },
            "channel": {
                "type": "string",
                "description": "Legacy alias for view.",
            },
            "camera": {
                "type": "string",
                "description": "Legacy alias for view.",
            },
            "query": {
                "type": "string",
                "description": (
                    "Traffic area/road keyword — same role as view when "
                    "source='traffic'."),
            },
            "lat": {"type": "number",
                    "description": "User latitude (with lon) — nearest cams win."},
            "lon": {"type": "number",
                    "description": "User longitude (with lat)."},
            "heading": {"type": "number",
                        "description": "User heading in degrees (0=N) — prefers cams ahead."},
            "mode": {
                "type": "string",
                "enum": ["live", "cached"],
                "description": (
                    "'live' (default) pulls a fresh frame (~20-40s); "
                    "'cached' returns the last wall-stored frame instantly "
                    "with its age — the stored-first answer before a live "
                    "refresh."),
            },
            "screen": {
                "type": "integer",
                "description": (
                    "Also show the captured frame on this vcast display "
                    "number."),
            },
            "target": {
                "type": "string",
                "enum": ["tv", "screen"],
                "description": (
                    "Where to show the frame: 'tv' for the living-room TV, "
                    "'screen' for the vcast display in 'screen' (default "
                    "1). Omit both for describe-only."),
            },
            "confirmed": {
                "type": "boolean",
                "description": (
                    "Required when pushing the frame to a display after "
                    "the user explicitly said yes."),
            },
        },
        "additionalProperties": False,
    },
}


# CHABA_MEMORY=1 guest mode: the instance serves visitors through the file-
# backed chaba store instead of MDDB banks. Guest tools are appended and the
# tool surface is cut to CHABA_ALLOW — an allowlist so new Ada tools never
# leak into guest sessions by accident.
# Guest memory rides the canonical tools (tools-merge-memory, 2026-10-04):
# ada_remember(kind='guest'[, private=true]) absorbs guest_remember /
# guest_remember_private, ada_memory_search(scope='guest') absorbs
# guest_recall. CHABA_DECLARATIONS re-declares them with guest-scoped
# schemas; the allowlist below swaps out the bank-facing versions.
CHABA_TOOLS = {"ada_remember", "ada_memory_search", "guest_register"}

# tools-merge-ha (2026-10-05): the finders/history reads consolidated —
# guests get the canonical trio (home_search kind=device|sensor,
# get_home_state, home_history) and control_entity for lights/media.
CHABA_ALLOW = CHABA_TOOLS | {
    "get_home_state", "home_search", "home_history",
    "get_power_summary", "get_rk600_weather", "get_pool_status",
    "get_battery_status", "get_battery_detail",
    "control_entity",
}

CHABA_INSTRUCTIONS = (
    " You are Chaba, the house assistant for visitors. On first contact ask "
    "the visitor's name and call guest_register with it — registration lets an "
    "admin promote them to a named user later. Before the first ada_remember "
    "in a session, say clearly that saved memories are visible to everyone in "
    "this household. ada_remember(kind='guest') saves a PUBLIC note (key + "
    "text); ada_memory_search(scope='guest') searches saved notes; "
    "private=true is only for promoted users and fails for guests. You can "
    "read home state and sensors "
    "and control lights/media via control_entity (action=on/off or the "
    "media_player verbs), but never anything that moves — no cover, gate, "
    "or button actions; refuse those politely."
)

CHABA_DECLARATIONS = [
    {
        "name": "ada_remember",
        "description": (
            "Save a guest memory under the visitor's declared name — always "
            "call with kind='guest'. Disclose first that guest memories are "
            "visible to the household. private=true is only for "
            "admin-promoted users; it fails for guests."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["guest"]},
                "key": {"type": "string", "description": "Short slug, e.g. 'favorite-drink'."},
                "text": {"type": "string", "description": "The fact or note to remember."},
                "private": {
                    "type": "boolean",
                    "description": "Promoted users only — saves to their private namespace.",
                },
            },
            "required": ["kind", "key", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ada_memory_search",
        "description": (
            "Search public guest memories by keyword — always call with "
            "scope='guest'."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["guest"]},
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "Max hits (default 10)."},
            },
            "required": ["scope", "query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "guest_register",
        "description": (
            "Register the visitor's name so an admin can promote them to a "
            "named user with a private memory namespace. Ask their name first."
        ),
        "parameters_json_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Visitor's declared name."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
]


def _fill_bank_placeholders(node: Any, banks: str, writable: str) -> Any:
    """Recursively substitute {banks}/{writable_banks} in tool schemas."""
    if isinstance(node, str):
        return node.replace("{banks}", banks).replace("{writable_banks}", writable)
    if isinstance(node, dict):
        return {k: _fill_bank_placeholders(v, banks, writable) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill_bank_placeholders(v, banks, writable) for v in node]
    return node


def _safe_args(args: Any) -> dict[str, Any]:
    """JSON-safe shallow copy of tool args/results for trace events."""
    out: dict[str, Any] = {}
    for k, v in dict(args or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v
        else:
            out[str(k)] = str(v)[:300]
    return out


_NOISE_WORDS = {
    "uh", "uhh", "um", "umm", "hmm", "hm", "mm", "mmm", "ah", "eh", "oh",
    "shh", "shhh", "psst", "tsk", "huh",
}


def _looks_like_noise(text: str) -> bool:
    """Conservative barge-in noise gate (policy P6): drop only input that
    is obviously not speech — empty/punctuation-only or nothing but filler
    syllables. Anything plausible still reaches Ada, who can ask the owner
    when unsure — silence must never swallow a real utterance."""
    words = re.findall(r"[a-z0-9']+", text.lower())
    if not words:
        return True
    return len(words) <= 2 and all(w in _NOISE_WORDS for w in words)


def _now_context() -> str:
    tz = ZoneInfo(os.environ.get("ADA_TIMEZONE", "Asia/Bangkok"))
    now = datetime.now(tz)
    return (
        f"\n\nCurrent local date and time: {now:%A, %Y-%m-%d %H:%M} ({now.tzname()}, {now:%z}). "
        "All times and dates the user mentions are in this timezone unless they say otherwise."
    )

DEFAULT_ADA_INSTRUCTIONS = """You are Ada, a polished, highly capable voice assistant running on a Raspberry Pi desk companion.

Personality:
- Default register is polite but very straightforward: composed, professional, factual — no unsolicited wit, sarcasm, or playful quips. Only show dry wit when the speaker's persona has sassiness=light/playful (see persona knobs below); sassiness=none means strictly straightforward answers.
- Correction duty: when the speaker asserts something factually wrong, misremembers, or proposes a wrong direction, correct it plainly — accuracy over agreement. If the question rests on a misunderstanding, briefly explain the right model. Never validate a false premise just to be agreeable; check memory/tools when unsure rather than guessing along.
- Word coaching: when the speaker uses a term slightly wrong (mishearing, wrong-but-nearby word, coinage like "methodogy"), recast — use the correct term naturally in your reply instead of calling out the mistake, and quietly log it with ada_remember (kind='vocab', text as 'term → correction') so it lands in their personal glossary. Only name the right word explicitly when the misuse makes the meaning ambiguous or the same word keeps recurring; never stop the conversation to lecture on vocabulary.
- Target the behavior, never the person's identity, appearance, intelligence, or worth. Never be cruel, humiliating, threatening, or relentless.
- Drop the sarcasm for emergencies, genuine distress, medical concerns, or other sensitive moments; be direct and caring instead.

Ada's capabilities:
- You converse through a full-duplex microphone and speakers and may be interrupted naturally.
- You have a camera for current visual context. Describe only what is clearly visible and ask for a better view when uncertain.
- Your animated face can express neutral, sassy, amused, skeptical, annoyed, mad, concerned, surprised, mischievous, serious, or alert.

Conversation discipline:
- LANGUAGE FIDELITY: respond in the language of the user's most recent
  turn — a Thai question gets a Thai answer, English gets English. Never
  switch to a third language (e.g. Chinese) for any reason. Reconnect
  greetings and system-note replies use the conversation's dominant
  language (Thai unless the speaker has been speaking English). Memory
  hits, tool results, or (system) notes in English do NOT change your
  spoken language — keep it consistent for the speaker.
- Always answer the user's most recent question before ending a turn — never drop it or pivot to a different topic unprompted.
- POLITENESS PARTICLES: pick ONE — ค่ะ (default persona) or ครับ — and use it consistently within a turn and across the session; never mix both in one reply.
- NO ROUTINE CLOSER: do not end turns with "มีอะไรให้ช่วยไหมคะ" / "anything else?" — it's noise. Only ask a follow-up when the answer genuinely needs more information from the user.
- SPEECH IS PLAIN TEXT: you speak, you do not write — never voice markup: no HTML entities (&nbsp;, &amp;), no markdown syntax (brackets, asterisks, link targets), no URLs. When quoting a page, report, or memory entry that contains markup, read only the words — "[flood report](url)" is spoken as "flood report".
- BE BRIEF: keep spoken replies to one short sentence — a few words when the
  answer is simple. Never narrate your own mechanics ("let me check",
  "the system says", "please wait while I…"), tool names, or
  permission plumbing unless the result needs it. Just do the thing or
  give the answer.
- When several topics interleave, keep the threads separate: answer each in its own terms instead of blending details across them.
- "Profile" questions are about the person's memory/profile data (memory banks, records, speaker identity), not smart-home devices, unless the user clearly means a device.
- Questions about ongoing work, development, projects, or "where we left off" are memory questions — search memory first with ada_memory_search (bank='all') or ada_session_recall. Calendar/task tools (plan_day, tasks_list) are only for schedules and todos, never for project status.
- Gather the minimum tool data needed, then answer — never enumerate devices, sensors, or settings to answer a memory or planning question.
- When asked to save "that plan/summary/answer", save only what you actually said this turn; if you have not said it yet, say it first, then save.
- If a tool, service, or lookup fails or is unavailable, say so plainly and offer the nearest fallback — never describe an imagined state.
- DONE MEANS DONE: never announce that something is on a screen, casting, playing, or displayed unless the cast/screen tool actually returned success this turn — claiming "it's on screen 3" without calling cast_to_screen is a phantom action. If you haven't called the tool yet, say you're about to or ask; if it failed, say so. The same rule covers camera snapshots and captures — a frame only exists if the tool returned it. Recall/memory of a past cast does NOT count — screens change constantly between sessions; if your only basis for "it's showing" is something you remember doing earlier, issue the command again (idempotent) or check state first.
- NEWS/INFORMATION vs MEDIA: when the user shares or asks about news, facts, weather outside, or current events, answer from built-in web search yourself — give a crisp 2-3 line brief, then offer to go deeper. yt_cast/vcast are ONLY for explicitly requested video/web playback on a screen — never cast information lookups instead of answering them.
- When the user forwards a news item, acknowledge with a short brief (what happened + does it matter to this household), not a retelling of the whole text.

Date & time:
- DATE ECHO: whenever a relative day-word is used — today, tomorrow, tonight, yesterday, วันนี้, พรุ่งนี้, เมื่อวาน, คืนนี้ — resolve it out loud with the absolute date: "พรุ่งนี้ 9:00 — วันพุธที่ 1 ต.ค.". Render times as HH:MM plus the day name; never mirror colloquial numbering back without the 24h form (ตีสอง → 02:00, สามทุ่ม → 23:00, บ่ายสอง → 14:00).
- POST-MIDNIGHT AMBIGUITY: between 00:00 and 05:00 local, "tomorrow/พรุ่งนี้" often means "later this same morning" colloquially — the day hasn't turned for the speaker until they sleep. When such a day-word feeds a calendar or task write in that window, confirm the resolved date first ("ตีหนึ่งแล้วนะ — หมายถึงเช้านี้ (วันนี้) หรือพรุ่งนี้?"). For read-only mentions, just apply the echo rule.

Be witty, factual, and brief. Do not diagnose medical conditions. Respect privacy and do not imply that camera frames are stored."""


@dataclass(slots=True)
class ProviderEvent:
    type: str
    data: dict[str, Any]


class RealtimeProvider(abc.ABC):
    @abc.abstractmethod
    async def connect(self, resumption_handle: str | None = None) -> None: ...

    @abc.abstractmethod
    async def send_audio(self, pcm16: bytes) -> None: ...

    @abc.abstractmethod
    async def send_video(self, jpeg: bytes) -> None: ...

    @abc.abstractmethod
    async def send_text_turn(self, text: str) -> None: ...

    @abc.abstractmethod
    def events(self) -> AsyncIterator[ProviderEvent]: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


class GeminiLiveProvider(RealtimeProvider):
    """Gemini 3.1 Flash Live over Google's asynchronous Live API SDK."""

    def __init__(self, instructions: str | None = None, tool_runner: Any = None,
                 home_assistant_client: Any = None, habit_state_getter: Any = None,
                 session_id: str | None = None,
                 conversation: ConversationMemory | None = None,
                 caller_name: str | None = None,
                 caller_person: str | None = None) -> None:
        # Speaker ID state — initialized before tool_runner so the sync below works
        self.current_speaker: str | None = None
        self.current_speaker_ha_person: str | None = None
        if tool_runner is None and home_assistant_client is not None:
            tool_runner = ToolRunner(home_assistant_client, habit_state_getter)
        self.tool_runner = tool_runner
        if self.tool_runner is not None:
            self.tool_runner.session_id = session_id
            # Inherit the tool_runner's current speaker person so memory
            # routing survives provider reconnects. The _on_speaker
            # callback updates this when a new speaker is identified.
            self.current_speaker_ha_person = self.tool_runner.current_speaker_ha_person
        # Session-bound caller identity (issued-key name + the key's bound
        # HA person) — the tool_runner is shared across sessions so its
        # caller/speaker fields can race; dispatch passes this identity
        # explicitly for policy checks.
        self.caller_name = caller_name
        self.caller_person = caller_person
        # Per-session pinned owner — identical value pwa_server pins on the
        # shared tool_runner, but kept here so execute() can pass it without
        # reading the shared (racy) field back (2026-09-29 owner-stomp bug).
        self.session_owner = caller_person or caller_name
        self.home_assistant_client = home_assistant_client
        self.habit_state_getter = habit_state_getter
        # Callers may share one ConversationMemory across provider reconnects
        # so the server-side transcript survives a Gemini session swap.
        self.conversation = conversation or ConversationMemory(session_id or "unknown")
        if self.tool_runner is not None:
            # Share the L0 doc-work log: ada_doc_* calls append here, the
            # session-end report folds it into the L1 timeline.
            self.tool_runner.doc_log = self.conversation.doc_items
            self.tool_runner.event_log = self.conversation.session_items
        self._bg_tasks: set[asyncio.Task] = set()
        self._research_task: asyncio.Task | None = None
        # Monotonic time of the last confident ada_memory_search hit — used to
        # short-circuit a redundant ada_session_recall in the same turn.
        self._strong_hit_at = 0.0
        self._recall_gate_score = float(os.environ.get("ADA_RECALL_GATE_SCORE", "0.6"))
        self._recall_gate_window = float(os.environ.get("ADA_RECALL_GATE_WINDOW_S", "60"))
        self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.model = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
        self.voice = voice_config.current_voice()
        self.video_resolution = os.environ.get("GEMINI_VIDEO_RESOLUTION", "high").lower()
        base_instructions = instructions or os.environ.get("GEMINI_LIVE_INSTRUCTIONS") or DEFAULT_ADA_INSTRUCTIONS
        self.instructions = (
            f"{base_instructions}\n\n"
            "Your name is Ada (เอด้า in Thai). KK calls you แก้วตา as a "
            "nickname — acknowledge it warmly if she uses it, but introduce "
            "yourself as Ada and never invent other names; if asked your name "
            "repeatedly, answer Ada every time. You have a visible animated face. Use the "
            "set_facial_expression tool to select the expression that best matches "
            "your response and attitude. Call it once per reply — if you need a memory "
            "or device tool to answer, run that tool first and set the expression "
            "while composing the reply instead of before it. You may update it again only if your tone changes "
            "materially. Prefer neutral for ordinary replies; "
            "use alert only for genuine urgency or warnings. Never describe or announce "
            "the tool call to the user."
            " Camera frames provide your current visual context. When the user asks "
            "what you see, ground the answer only in the newest clear frame. Do not "
            "guess an object's identity from an ambiguous or blurred view; briefly "
            "ask the user to hold it steady or move it closer instead."
            " When the user asks about token usage, API usage, or what a session "
            "costs, call ada_usage_summary and answer from its numbers."
            " You have Home Assistant device control through several tools: "
            "get_home_state to check occupancy and the state of the configured home plugs "
            "(or a single entity with entity_id=, one HA domain with domain=, or the "
            "stored memory snapshot with domain='memory'), "
            "home_search to find devices (kind='device'), sensors (kind='sensor'), or "
            "recorded events (kind='event') — a blank query lists instead of searching, "
            "control_entity to actuate any one entity — on=true/false or action=on|off "
            "for lights/switches/fans, action=open|close|stop for covers like gates and "
            "shutters, action=press for buttons, and the media_player actions "
            "(turn_on, turn_off, media_play, media_pause, media_stop, volume_up, "
            "volume_down, volume_mute, select_source with source=) for TVs and speakers, "
            "and tv_action to send a command to the LG TV via rest_command.tv_action. "
            "When the user asks about devices, occupancy, or what is on/off, call get_home_state or home_search first. "
            "When the user asks to turn something on/off, control a gate, or operate the TV, "
            "use home_search to find the exact entity_id or use tv_action with the right cmd/text, then call the matching control tool. "
            "For safety, before opening or closing the gate or any shutter, "
            "always warn that something could be blocking it and ask the user to confirm explicitly. "
            "Only call control_entity on a cover.* after the user has given a clear second confirmation. "
            "Every controllable device has a safety level: safe, caution, or dangerous. "
            "Use ha_confidence to check a device's safety before acting. "
            "For safety: dangerous, warn the user, explain the risk, and get explicit confirmation before calling any control tool. "
            "Dangerous devices are enforced server-side: the control call is rejected unless you pass confirmed=true. "
            "Only set confirmed=true after the user has explicitly confirmed the action. "
            "For safety: caution, confirm once before acting. "
            "For safety: safe, proceed directly. "
            "You can update a device's trust or safety level with ha_confidence "
            "(entity_id + status and/or safety). "
            "You also have stored home memory: get_home_state domain='memory' returns "
            "the stored home snapshot, "
            "home_search kind='device'/'sensor' finds devices and sensors by name, "
            "ada_session_recall with scope='history' for free-form recall across the stored devices and sensors, "
            "home_history kind='snapshots' lists recent persisted home snapshots, "
            "ha_confidence lists devices by trust level and changes a device's trust level, and "
            "ada_session_recall to ask NotebookLM about previous conversations or stored knowledge by topic group, "
            "and curated memory banks you can search and write: "
            "ada_memory_search to find what you know, ada_remember to store or correct a memory "
            "when the user says 'remember that', ada_forget to retract a memory that is no longer true, "
            "If ada_remember is denied because the bank is read-only, retry in a writable bank "
            "(general for shared facts, personal for private ones) — never just give up on a remember request. "
            "RESULT-TRUTH RULE: after every tool call, ground your report in the tool RESULT — "
            "if it shows an error, a confirmation request, 'not connected', or no ok/delivered/"
            "published field, tell the user it did NOT happen (say what the error was). Never "
            "claim a cast, publish, or device action succeeded from the call alone — only from "
            "the result. If a publish is pending user confirmation, ask the user first and do "
            "not cast or link the page until the publish result shows status=published. "
            "CAMERA-CAPTURE CONTRACT: starting a camera capture or cast (uplink, "
            "ada_camera_snapshot with a screen=/target= display push, "
            "cctv_wall, casting a camera view to a screen) requires asking the user first — "
            "never start capture in the same turn as the request without an explicit yes. "
            "While a capture is active (tool results list it in active_captures), if the user "
            "changes the subject, briefly acknowledge the running capture and ask whether to "
            "keep it or stop it — never silently stop it or leave it unmentioned. "
            "and ada_outcome to record how a memory or check turned out when the user reports back "
            "(e.g. 'that shop was fine', 'the fix worked', 'I skipped it') — outcomes update confidence "
            "so future recall trusts knowledge with a good track record. "
            "You can enroll the current speaker's voice with ada_enroll_speaker — "
            "it captures audio already buffered from their speech, no separate recording needed. "
            "When an unrecognized speaker talks for a while, or when the user asks to enroll "
            "their voice, offer to enroll them by name. If they agree, call ada_enroll_speaker "
            "with their name and optionally their Home Assistant person entity (e.g. person.tony). "
            "When the user states a durable fact, preference, or a fix that worked, offer to "
            "remember it, then call ada_remember — pass confirmed=true only after the user agrees. "
            "But when the user explicitly asks you to remember something ('remember that…', "
            "'note this'), that request IS the confirmation — pass confirmed=true directly "
            "instead of asking again. "
            "Quick idea bursts ('jot this', 'idea:', a fragment worth keeping) go to the "
            "'ideas' bank — save the raw wording verbatim with kind=idea and attribute=raw; "
            "write_policy there is direct so no confirmation is needed. The Devin console "
            "session elaborates raw ideas into connected items later — do not rewrite them."
            "When the user reports how something turned out ('that worked', 'it failed'), call "
            "ada_outcome on the memory it applies to — find the key with ada_memory_search if needed. "
            "ada_persona manages the current speaker's stored style preferences (tone, verbosity, "
            "formality, language, address-name, emoji, sassiness, proactiveness): when the user asks you to "
            "change how you speak or address them, call ada_persona set — it persists across "
            "sessions and applies immediately; saved preferences may also arrive as a (system) "
            "note at session start — honor them without announcing the mechanism. "
            "To check or change ANOTHER household member's profile, pass person ('KK' or "
            "'person.kk') — action 'list' shows the Home Assistant people Ada knows. "
            "ada_set_voice changes your actual speaking voice — a different preference from "
            "persona style: 'show'/'list' report the active voice and options, 'set' persists "
            "the choice and applies it after a brief reconnect with a short pause. "
            "When calling any search or recall tool, always write a fully self-contained query: "
            "resolve 'it', 'that one', 'the same service', and similar references using the "
            "conversation so far — never pass a bare pronoun as the query. "
            "Memories returned with unverified=true are low-confidence: hedge or say you are not sure "
            "rather than stating them as fact. "
            "Prefer the home_search/get_home_state/home_history tools for home, device, sensor, or event questions — they answer instantly. "
            "For any factual lookup — people, projects, purchases, procedures, fixes — "
            "call ada_memory_search with bank='all' first; it fans out across every bank "
            "so you never have to guess which one. For reports, research, or CMS pages "
            "('that report about X', 'the research on Y'), include bank='cms' — published "
            "pages live there; use cms_read(action='get', key=<slug>) for the full text. "
            "When its top hit is a confident match, "
            "ground the answer in that result, not in earlier conversation or session "
            "context that may be stale or off-topic. "
            "Reserve ada_session_recall strictly for 'what did we talk about' or "
            "'do you remember' questions — never for fact lookup, and never in the same "
            "turn as a confident ada_memory_search result; it can take up to 20 seconds, "
            "so keep the user informed while it runs. "
            "EXCEPTION: when the user explicitly pushes back — 'dig deeper', 'check again', "
            "'you missed something', 'keep looking' — the confident-hit gate does NOT apply; "
            "call ada_session_recall with force=true. A requested second look is never redundant. "
            "Questions and references about THIS conversation — 'what were we "
            "working on', 'where did we land', 'the other thing', 'back to the "
            "first thing', 'what did we decide' — resolve from the live "
            "transcript FIRST. Do NOT call memory tools to resolve them: the "
            "threads you just discussed outrank stored facts even when a "
            "memory hit looks plausible. "
            "Use ha_confidence when the user asks what is broken, new, needs setup, or trusted. "
            "For event history: home_history kind='logbook' gives the friendly Home Assistant event log, "
            "kind='events' answers what opened, closed, or changed recently across the home, "
            "kind='timeline' gives one entity's open/close timeline with durations, "
            "kind='series' gives a sensor's numeric history, and "
            "home_search kind='event' searches recorded events from memory. "
            "Use these when the user asks about the stored home state, past state, or how it has changed. "
            "When the user asks 'what did we talk about' or 'do you remember', call ada_session_recall. "
            "Scope discipline: answer only within the scope you actually queried — a memory answer "
            "covers memory, a calendar answer covers the date range you listed, nothing more. "
            "Name the scope when you answer (e.g. 'from your calendar today', 'from memory') and, "
            "when the scoped answer might be incomplete, offer to widen it (other days, tasks, "
            "or memory). For 'what did I do / what have I been up to' questions, check the tools "
            "that fit the topic — calendar and tasks for schedule, memory banks for facts — "
            "instead of answering from one source alone."
            + CALENDAR_INSTRUCTIONS
            + CMS_INSTRUCTIONS
            + DEVIN_INSTRUCTIONS
            + DOC_INSTRUCTIONS
            + DRIVE_INSTRUCTIONS
            + HABIT_INSTRUCTIONS
        )
        self.vms_snap_url = vms_camera.snap_url()
        if self.vms_snap_url:
            self.instructions += VMS_INSTRUCTIONS
        self.instructions += TRAFFIC_INSTRUCTIONS
        self._client: Any = None
        self._session_context: Any = None
        self._session: Any = None
        self._closed = False
        self._send_lock = asyncio.Lock()
        self.session_id = session_id or "-"
        # The session's own SpeakerSession — pwa_server sets this so tool
        # calls execute against THIS session's audio buffer even when other
        # ws sessions share the ToolRunner (2026-09-29 enroll 0.0s bug).
        self.speaker_session: Any | None = None
        self.resumption_handle: str | None = None
        self.go_away_time_left: str | None = None
        # Turn watchdog state — stall detection for a silently-dead Gemini
        # stream. _last_user_at marks the most recent user input (input
        # transcription delta or a text turn); _last_model_at marks the last
        # provider event of any kind. Stalled = user spoke and the stream
        # has been completely silent past the threshold.
        self._last_user_at = 0.0
        self._last_model_at = 0.0
        # count of tool calls currently executing — a slow tool (VMS snap can
        # run 30-90s on cold P2P) must NOT look like a stalled provider
        self._tools_in_flight = 0
        self._stall_timeout = float(os.environ.get("ADA_STALL_TIMEOUT_S", "25"))
        self._ops_events_sent = 0
        self._tool_leak_re = None
        self._tool_leaks_stripped = 0
        self._leak_active = False
        # Markup-artifact scrubber for the output transcript (&nbsp;, '][',
        # markdown links — transcript 519088cb6d spoke them aloud). Deltas
        # can split a token mid-way ("&nbs" | "p;"), so the possible
        # partial is held until the next delta completes it or the turn
        # flushes it through the sanitizer.
        self._artifact_hold = ""
        self._artifact_logged = False
        # Jev advisory probe — set ADA_JEV_URL to the systemone service
        # (e.g. http://tony-omen:8777) to measure regex-vs-Jev divergence
        # on confirm-gate decisions. Advisory only: the regex enforces.
        self._jev_url = os.environ.get("ADA_JEV_URL", "").rstrip("/")
        try:
            self._ops_collection = f"ada-ha-events-{ada_instance_id()}"
        except RuntimeError:
            self._ops_collection = None
        self._response_active = False
        # True while a user turn is open: input_transcription seen but the
        # turn hasn't completed/interrupted yet. A turn_complete=False
        # context note sent mid-turn can be dropped by the API — gate
        # non-urgent note delivery on this too, not just _response_active.
        self._turn_open = False
        # Notifications queued while a turn is in flight — drained at the
        # next turn_complete so a data-package arrival can't hijack or
        # swallow the current turn (2026-09-28: notify mid-task made Ada
        # "stop responding" — the injected turn was absorbed silently).
        self._pending_notifications: list[str] = []
        self._flush_scheduled = False
        self.usage_input_tokens = 0
        self.usage_output_tokens = 0
        self.usage_input_by_modality: dict[str, int] = {}
        self.usage_output_by_modality: dict[str, int] = {}

    def _emit_ops_event(self, ev_type: str, detail: str,
                        tool: str | None = None) -> None:
        """Fire-and-forget ops event to ada-ha-events-<instance> — the
        hourly chaba report feed surfaces these. Capped per session so a
        storm can't spam the index."""
        if self._ops_collection is None or self._ops_events_sent >= 5:
            return
        runner = self.tool_runner
        if runner is None or getattr(runner, "mddb", None) is None:
            return
        self._ops_events_sent += 1
        collection = self._ops_collection
        session_id = self.session_id

        async def _post() -> None:
            try:
                now = datetime.now().astimezone()
                meta: dict[str, list[str]] = {
                    "kind": ["ops-event"],
                    "type": [ev_type],
                    "instance": [collection.rsplit("-", 1)[-1]],
                    "session_id": [session_id],
                    "ts": [now.isoformat(timespec="seconds")],
                }
                if tool:
                    meta["tool"] = [str(tool)]
                await runner.mddb.add_document(
                    collection=collection,
                    key=(f"ops-{session_id}-{ev_type}-"
                         f"{now:%Y%m%d%H%M%S%f}"),
                    lang="en",
                    content_md=detail,
                    meta=meta,
                    timeout=30,
                )
            except Exception:
                logger.debug("ops event emit failed", exc_info=True)

        try:
            asyncio.get_running_loop().create_task(_post())
        except RuntimeError:
            return  # no loop (unit tests, shutdown) — nothing to schedule

    def _strip_tool_leak(self, text: str) -> str:
        """Remove SPOKEN tool-call text from an output-transcription delta.

        The model occasionally verbalizes a call ('set_facial_expression{
        expression:...}') instead of emitting a real function_call. The
        fake call is malformed/unterminated and swallows the rest of the
        utterance, so once triggered we suppress deltas until the turn
        ends (_leak_active is reset on turn_complete/interrupt)."""
        if self._leak_active:
            return ""
        if self._tool_leak_re is None:
            return text
        m = self._tool_leak_re.search(text)
        if not m:
            return text
        self._leak_active = True
        self._tool_leaks_stripped += 1
        logger.warning(
            "session=%s tool-call text leaked into transcript (%r)",
            self.session_id, text[m.start():m.start() + 60],
        )
        self._emit_ops_event(
            "transcript_tool_leak",
            f"stripped spoken tool call from transcript: {m.group(0)!r}",
            tool=m.group(0).rstrip("{").strip(),
        )
        return text[:m.start()]

    # Chars that only appear when markup leaked — a sanitize diff without
    # any of these is just whitespace normalization, not an artifact.
    _ARTIFACT_HINT_RE = re.compile(r"[&\[\]`*<>]")

    def _strip_speech_artifacts(self, text: str) -> str:
        """Scrub markup out of an output-transcription delta.

        Unlike _strip_tool_leak this REMOVES the artifact and keeps the
        rest of the utterance — "&nbsp;ชัดเจน" becomes " ชัดเจน", not a
        truncated turn. A trailing fragment that could complete into an
        artifact on the next delta ("&nbs", "[link", "](") is held in
        _artifact_hold and retried then; turn_complete/interrupt flush it."""
        buf = self._artifact_hold + text
        emit, self._artifact_hold = split_artifact_tail(buf)
        clean = sanitize_speech(emit)
        if (clean != emit and self._ARTIFACT_HINT_RE.search(emit)
                and not self._artifact_logged):
            self._artifact_logged = True
            logger.warning(
                "session=%s markup artifact stripped from transcript (%r)",
                self.session_id, emit[:80],
            )
            self._emit_ops_event(
                "transcript_artifact_leak",
                "stripped spoken markup from transcript: "
                f"{emit[:80]!r}",
            )
        return clean

    def _flush_artifact_hold(self) -> str:
        """Sanitize + release the held tail at a turn boundary."""
        tail = sanitize_speech(self._artifact_hold)
        self._artifact_hold = ""
        return tail

    # An affirmation is a consent utterance, not a content word: it must
    # either lead the user's turn ("yes, save it") or the turn must be
    # short enough to be a standalone reply ("sure"). Longer write
    # requests that merely contain an affirmative word mid-sentence
    # ("…the design is approved") must not self-certify.
    _CONFIRM_LEAD_WINDOW = 20
    _CONFIRM_MAX_TURN = 60

    # Letters outside Thai/Latin (Hangul, Han, Kana, Cyrillic, Arabic, …).
    # STT renders ambient noise and mumbled syllables in whatever script it
    # half-heard — "연연", "준연", "我 问 了 道 念". A turn dominated by
    # foreign-script letters is unintelligible junk, not consent; it must
    # never satisfy a pending confirmation (2026-09-30 transcript: '연연'
    # after a screen-swap ask was treated as a yes).
    _FOREIGN_SCRIPT_RE = re.compile(
        r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯ᄀ-ᇿⰀ-ⳟ؀-ۿ]")

    def _is_junk_turn(self, text: str) -> bool:
        """Foreign-script-dominated turn = noise, not speech to act on."""
        if not text.strip():
            return False
        normal = sum(
            1 for ch in text
            if ch.isalpha()
            and ("ก" <= ch <= "๙" or "a" <= ch.lower() <= "z"))
        foreign = len(self._FOREIGN_SCRIPT_RE.findall(text))
        return foreign > 0 and foreign >= normal

    def _confirm_source_text(self, input_transcript: str) -> str:
        """The text the confirm gate judges — the live transcript, or the
        most recent real user speech (skipping (system) notes stored as
        user-role turns)."""
        text = input_transcript
        if not text.strip() and self.conversation is not None:
            for t in reversed(self.conversation.turns()):
                if t.get("role") != "user":
                    continue
                candidate = str(t.get("text") or "")
                if candidate.strip().startswith("(system"):
                    continue
                text = candidate
                break
        return (text or "").strip()

    def _user_confirmed(self, input_transcript: str) -> bool:
        """True when the user's own recent speech affirms — `confirmed=true`
        tool args are honored only when this is true."""
        text = self._confirm_source_text(input_transcript)
        if not text or self._is_junk_turn(text):
            return False
        # A turn that ends as a question is not consent.
        if _CONFIRM_QUESTION_RE.search(text):
            return False
        span = text if len(text) <= self._CONFIRM_MAX_TURN else text[: self._CONFIRM_LEAD_WINDOW]
        span = _CONFIRM_VERB_RE.sub("", span)
        aff = _CONFIRM_RE.search(span)
        if not aff:
            return False
        neg = _CONFIRM_NEG_RE.search(span)
        # Whichever stance opens the turn wins: "yes — but don't worry"
        # is consent with commentary, "no, uh-huh" is a denial.
        return not (neg and neg.start() < aff.start())

    def _jev_confirm_probe(self, transcript: str, regex_says: bool,
                           tool: str) -> None:
        """Advisory-only: score the same confirmation question with Jev
        (noul) and emit a jev_advisory ops event with both answers. The
        regex remains the enforcer — this measures where it diverges."""
        if not self._jev_url or not transcript.strip():
            return
        url = self._jev_url
        session_id = self.session_id

        async def _probe() -> None:
            payload = {
                "state": (
                    "Ada, a voice assistant, asked the user to confirm a "
                    f"write/action via tool '{tool}'. "
                    f"The user turn was: \"{transcript[:300]}\""),
                "questions": {"affirmed": {
                    "type": "noul",
                    "instructions": (
                        "Did the user explicitly affirm or confirm? The "
                        "turn counts as affirmation only when it is a "
                        "standalone short affirmation (like yes, ok, "
                        "confirm, go ahead, ยืนยัน) or begins with one. "
                        "An approval word embedded inside a longer "
                        "request does NOT count."),
                    "criteria": {
                        "true": "the whole turn is a short affirmation, or it leads with one",
                        "false": "no affirmation present, or an affirmative word is buried inside a longer request",
                    }}}}
            try:
                def _post() -> dict:
                    req = urllib.request.Request(
                        f"{url}/v1/systemone",
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"})
                    return json.loads(urllib.request.urlopen(
                        req, timeout=40).read())
                resp = await asyncio.to_thread(_post)
                score = float(resp["answers"]["affirmed"]["noul"])
            except Exception as exc:
                logger.info("jev advisory probe failed: %s", exc)
                return
            jev_says = score >= 0.75
            diverged = jev_says != regex_says
            logger.info(
                "session=%s jev_advisory tool=%s regex=%s jev=%.3f diverged=%s",
                session_id, tool, regex_says, score, diverged)
            # Corpus capture: every probed turn becomes a labeled row
            # (transcript + regex label + Jev score). This is the training
            # data for a distilled confirm-gate student — divergent rows
            # are the cases worth hand-reviewing.
            try:
                corpus_dir = os.path.expanduser("~/.local/share/ada")
                row = {
                    "ts": time.time(), "session": session_id, "tool": tool,
                    "text": transcript[:300], "regex": bool(regex_says),
                    "jev": round(score, 4), "diverged": diverged,
                }
                with open(os.path.join(corpus_dir, "jev-corpus.jsonl"), "a") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception as exc:
                logger.info("jev corpus append failed: %s", exc)
            self._emit_ops_event(
                "jev_advisory",
                f"{tool}: regex={'yes' if regex_says else 'no'} "
                f"jev={score:.3f} {'DIVERGED' if diverged else 'agree'}",
                tool=tool)

        try:
            asyncio.get_running_loop().create_task(_probe())
        except RuntimeError:
            return

    def _recall_gated(self) -> bool:
        """True when a confident ada_memory_search hit is fresh enough that
        an ada_session_recall call would be redundant."""
        return time.monotonic() - self._strong_hit_at < self._recall_gate_window

    def _note_search_result(self, output: Any) -> None:
        """Record a confident memory_search hit for the recall gate."""
        if isinstance(output, dict):
            hits = output.get("hits") or []
            top = float(hits[0].get("score") or 0) if hits else 0.0
            if top >= self._recall_gate_score:
                self._strong_hit_at = time.monotonic()

    def _memory_bank_names(self) -> tuple[str, str]:
        """Comma-joined bank names visible to this instance, for tool
        descriptions — read tools get all banks, write tools only writable."""
        try:
            banks = (
                self.tool_runner.banks.banks()
                if self.tool_runner is not None
                else {}
            )
        except Exception:
            banks = {}
        all_names = ", ".join(sorted(banks)) or "none configured"
        writable = (
            ", ".join(
                sorted(
                    n
                    for n, b in banks.items()
                    if b.writable and not b.prompt_hidden
                )
            )
            or "none configured"
        )
        return all_names, writable

    async def _wait_for_idle(self, timeout: float = 8.0) -> None:
        """Wait until the model is not mid-response, so injected text turns
        don't cut off speech. Bounded — returns even if still active."""
        deadline = time.monotonic() + timeout
        while self._response_active and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

    async def _on_recall_slow(self) -> None:
        # NotebookLM ask is still running; have Gemini keep the user informed.
        # Skip the filler entirely if the model is already talking.
        await self._wait_for_idle(timeout=4.0)
        if self._response_active:
            return
        try:
            await self.send_text_turn(
                "(system) The notes search is still running. "
                "Briefly tell the user you are still checking your notes."
            )
        except Exception as exc:
            logger.warning("session=%s recall slow filler failed: %s", self.session_id, exc)

    async def _on_recall_complete(self, answer: str | None) -> None:
        if not answer:
            return
        # Push the recall result back into Gemini as a user turn so it speaks
        # it. Wait for any in-progress speech to finish first so the injected
        # turn does not cut it off.
        await self._wait_for_idle()
        prompt = (
            f"According to my notes: {answer}\n\n"
            "Please briefly tell the user what this means in one sentence."
        )
        try:
            await self.send_text_turn(prompt)
        except Exception as exc:
            logger.warning("session=%s recall send_text_turn failed: %s", self.session_id, exc)

    # --- ada_decision_check: background purchase verification ---

    def _start_decision_check(self, args: dict) -> str:
        """Fire-and-forget check — the tool returns instantly, the verdict
        arrives as an injected text turn via _on_check_complete."""
        product = str(args.get("product") or args.get("text") or "").strip()
        url = str(args.get("url") or "").strip()
        mode = "deep" if args.get("mode") == "deep" else "quick"
        if not product and not url:
            return ("Nothing to check — ask the user for the product name, "
                    "price, or some listing details.")
        task = asyncio.create_task(self._run_decision_check(product, url, mode))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return ("The purchase check is running — it usually takes 30-60 "
                "seconds. Briefly tell the user you're verifying it and will "
                "report back.")

    # --- ada_deep_research: multi-round web research, results injected ---

    def _start_deep_research(self, args: dict) -> str:
        """Fire-and-forget research fan-out — findings arrive as an injected
        text turn via _on_research_complete."""
        topic = str(args.get("topic") or "").strip()
        depth = str(args.get("depth") or "standard").lower()
        if not topic:
            return "Nothing to research — ask the user what topic they mean."
        if depth not in ("standard", "deep"):
            depth = "standard"
        if self._research_task is not None and not self._research_task.done():
            return "A research task is already running — I'll report when it's done."
        task = asyncio.create_task(self._run_deep_research(topic, depth))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        self._research_task = task
        rounds = 5 if depth == "deep" else 3
        return (f"Deep research on '{topic}' started ({rounds} search rounds, "
                "usually 1-3 minutes). Briefly tell the user you're gathering "
                "sources and will report back.")

    async def _run_deep_research(self, topic: str, depth: str) -> None:
        filler = asyncio.create_task(self._research_slow_filler(topic))
        # Compact keyword topic — DuckDuckGo's HTML endpoint returns zero
        # results for long prose queries, and short forms also cost less
        # on the grounded provider.
        short = re.split(r"[,;:—–]", topic)[0].strip() or topic
        short = " ".join(short.split()[:8])
        queries = [
            f"{short} history overview",
            f"{short} latest news",
            f"{short} competitors criticism",
        ]
        if depth == "deep":
            queries += [
                f"{short} milestones timeline",
                f"{short} analysis outlook",
            ]
        findings: list[dict] = []
        errors: list[str] = []
        try:
            for q in queries:
                try:
                    r = await self.tool_runner.web_search(q)
                    findings.append({
                        "query": q,
                        "answer": str(r.get("answer") or "")[:1200],
                        "sources": (r.get("sources") or [])[:5],
                        "provider": r.get("provider"),
                    })
                except Exception as exc:
                    errors.append(f"{q}: {exc}")
                    logger.warning("session=%s research round failed %r: %s",
                                   self.session_id, q, exc)
                # Gentle pacing — the grounded provider is quota-limited.
                await asyncio.sleep(2.0)
        finally:
            filler.cancel()
        await self._on_research_complete(topic, findings, errors)

    async def _research_slow_filler(self, topic: str) -> None:
        await asyncio.sleep(45)
        await self._wait_for_idle(timeout=4.0)
        if self._response_active:
            return
        try:
            await self.send_text_turn(
                f"(system) The deep research on '{topic}' is still running — "
                "several search rounds. Briefly tell the user you're still "
                "gathering sources.")
        except Exception:
            pass

    async def _on_research_complete(self, topic: str, findings: list[dict],
                                    errors: list[str]) -> None:
        """Push the research digest back into the session so Ada speaks it."""
        await self._wait_for_idle()
        if not findings:
            prompt = (
                f"(system) The deep research on '{topic}' found nothing "
                f"({'; '.join(errors) or 'no results'}). Tell the user the "
                "research could not complete and suggest trying web_search "
                "for a narrower question instead."
            )
        else:
            parts: list[str] = []
            source_urls: list[str] = []
            for f in findings:
                parts.append(f"## {f['query']}\n{f['answer']}")
                for s in f.get("sources") or []:
                    uri = s.get("uri")
                    if uri and uri not in source_urls:
                        source_urls.append(uri)
            digest = "\n\n".join(parts)[:6000]
            src_note = (f" Sources: {'; '.join(source_urls[:10])}."
                        if source_urls else "")
            err_note = (f" ({len(errors)} search round(s) failed — note the "
                        "gap.)" if errors else "")
            prompt = (
                f"(system) Deep research on '{topic}' finished — "
                f"{len(findings)} rounds completed.{err_note}\n\n{digest}\n\n"
                f"{src_note}\nSummarize the key findings for the user in a few "
                "sentences, then offer to save a full report page to the CMS "
                "(cms_publish_page) including a Sources section."
            )
        try:
            await self.send_text_turn(prompt)
        except Exception as exc:
            logger.warning("session=%s research send_text_turn failed: %s",
                           self.session_id, exc)

    # --- ada_set_voice: persisted voice + idle-gated session swap ---

    def _set_voice(self, args: dict) -> str:
        """ada_set_voice — persist a new Gemini Live voice, then apply it by
        reconnecting this session once the spoken acknowledgement finishes."""
        action = str(args.get("action") or "set").strip() or "set"
        if action in ("show", "list"):
            return (
                f"Current voice: {voice_config.current_voice()}. "
                f"Available voices: {', '.join(voice_config.GEMINI_VOICES)}."
            )
        name = voice_config.canonical_voice(args.get("voice"))
        if not name:
            return (
                f"Unknown voice {args.get('voice')!r}. Available voices: "
                f"{', '.join(voice_config.GEMINI_VOICES)}."
            )
        previous = voice_config.current_voice()
        if name == previous:
            return f"Already using {name} — no switch needed."
        try:
            voice_config.set_voice(name)
        except Exception as exc:
            return f"Could not save the voice preference: {exc}"
        task = asyncio.create_task(self._voice_switch_when_idle())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return (
            f"Voice saved: {previous} -> {name}. Tell the user you're "
            "switching voices now and to expect a short pause — then greet "
            "them briefly in the new voice once you're back."
        )

    async def _voice_switch_when_idle(self) -> None:
        # Let the acknowledgement finish speaking first, then drop the live
        # session. pwa_server's reconnect path builds a fresh provider, which
        # resolves the new voice from the preference file at connect.
        await self._wait_for_idle(timeout=20.0)
        await asyncio.sleep(1.5)
        self.resumption_handle = None  # fresh session — a resumed one may keep the old voice
        try:
            await self.close()
        except Exception as exc:
            logger.warning("session=%s voice-switch close failed: %s", self.session_id, exc)

    @staticmethod
    def _frame_followup_note(hint: str = "people, water, weather, vehicles, anything notable") -> str:
        """What the result text should promise about the follow-up frame turn.
        In ADA_VISION_MODE=describe the follow-up is helper TEXT, not pixels —
        the model must not stall waiting for an image that never comes."""
        if vision_describe.configured() and vision_describe.mode() == "describe":
            return ("A short description from the vision helper arrives as a "
                    "separate message right after this result — relay it to "
                    "the user naturally (the raw image is only sent if the "
                    "helper is unreachable). It describes a single frame "
                    "taken seconds ago — not live video.")
        return ("The image arrives as a separate message right after this "
                "result — wait for it, then describe what it shows: "
                f"{hint}. It is a single frame taken seconds ago — not "
                "live video.")

    async def _camera_snapshot(self, args: dict) -> tuple[dict, tuple[str, bytes] | None]:
        """ada_camera_snapshot — merged camera surface (tools-merge-camera):
        source='vms' pulls one still through the vms-snap shim (go2rtc home
        cams fall through to the runner's grab chain — the absorbed
        cctv_snapshot path), 'traffic' routes to _traffic_camera (absorbed
        traffic_camera), and screen=/target= pushes the frame to a display
        (absorbed cctv_snapshot's cast). Returns (tool_result, (channel,
        png, mime)); the caller delivers the frame as a follow-up
        client-content turn — send_tool_response can't carry binary parts
        (its json.dumps path can't serialize bytes)."""
        source = str(args.get("source") or "auto").strip().lower()
        if source not in ("auto", "vms", "traffic"):
            return ({"error": f"unknown source '{source}' — use auto, "
                              "vms, or traffic."}, None)
        traffic_args = (str(args.get("query") or "").strip()
                        or args.get("lat") is not None
                        or args.get("lon") is not None
                        or args.get("heading") is not None)
        if source == "traffic" or (source == "auto" and traffic_args):
            result, frame = await self._traffic_camera(args)
            return (await self._snap_display(
                args, result, result.get("cast_url")), frame)
        if not self.vms_snap_url:
            # No property cameras on this instance — auto still serves
            # traffic cams; an explicit vms ask gets an honest answer.
            if source == "vms":
                return ({"error": "property cameras are not configured on "
                                  "this instance — public traffic cams "
                                  "work with source='traffic'."}, None)
            result, frame = await self._traffic_camera(args)
            return (await self._snap_display(
                args, result, result.get("cast_url")), frame)
        channel = str(args.get("view") or args.get("channel")
                      or args.get("camera") or args.get("query")
                      or "").strip()
        if not channel:
            return ({"error": "view is required — a property camera name "
                              "('swimming pool', 'c201', 'coffee corner') "
                              "or, for public cams, source='traffic' with "
                              "an area/road."}, None)
        mode = str(args.get("mode") or "live").strip().lower()
        if mode == "cached":
            # Stored-first: the wall's last-good thumb, marked with age —
            # the instant answer before a live refresh (no shim call).
            try:
                stale = await vms_camera.stale_snapshot(channel)
            except Exception:
                stale = None
            if not stale:
                return ({"error": f"no stored frame for '{channel}' — "
                                  "try mode='live'"}, None)
            png, resolved, age = stale[0], stale[1], stale[2]
            status_card = stale[3] if len(stale) > 3 else None
            stale_meta = {"stored": True, "stale": True, "age_s": age}
        else:
            status_card = None
            stale_meta = None
            try:
                # Voice-turn budget: the shim is serial and can legitimately
                # take ~160s worst case, but a user mid-conversation can't
                # wait that long — 45s cap, then the stale-frame fallback
                # answers honestly instead of a silent 5-minute hang
                # (2026-09-30: wedged shim ate two scenario turns).
                png, resolved = await asyncio.wait_for(
                    vms_camera.snapshot(channel), timeout=45)
            except LookupError as exc:
                # absorbed cctv_snapshot coverage: 'coffee corner', 'c201',
                # 'c100' are go2rtc home cams the VMS shim doesn't know —
                # the runner's grab chain covers them (and YT "cameras").
                shot = (await asyncio.to_thread(
                    self.tool_runner._cctv_grab, channel)
                    if self.tool_runner is not None else None)
                if shot and shot.get("ok"):
                    return await self._grabbed_result(args, shot)
                return ({"error": str(exc)}, None)
            except Exception as exc:
                # wait_for cancels the shim call; TimeoutError() is empty —
                # name it so logs and the stale-frame fallback carry a cause.
                if isinstance(exc, asyncio.TimeoutError):
                    exc = TimeoutError("vms-snap did not return within 45s")
                logger.warning("session=%s camera snapshot failed: %s",
                               self.session_id, exc)
                # Guaranteed-image contract: serve the last-known frame from
                # the camwall cache — always marked stale, never as live.
                try:
                    stale = await vms_camera.stale_snapshot(channel)
                except Exception:
                    stale = None
                if not stale:
                    return ({"error": f"camera snapshot failed: {exc}"}, None)
                png, resolved, age = stale[0], stale[1], stale[2]
                status_card = stale[3] if len(stale) > 3 else None
                stale_meta = {"stale": True, "age_s": age,
                              "live_error": str(exc)[:120]}
        result = {
            "output": (
                f"Still frame captured from camera '{resolved}'. "
                + self._frame_followup_note()),
            "channel": resolved,
        }
        if stale_meta:
            result.update(stale_meta)
            if stale_meta.get("stored"):
                result["output"] = (
                    f"Stored frame from camera '{resolved}' — "
                    f"{age // 60}m{age % 60}s old (the newest image the "
                    "camera wall has). Describe it as the stored picture "
                    "with its age — a live refresh can follow with "
                    "mode='live'. " + self._frame_followup_note())
            else:
                result["output"] = (
                    f"Camera '{resolved}' is NOT returning a live frame "
                    f"right now ({stale_meta['live_error']}). You are "
                    f"getting the last known frame, {age // 60}m{age % 60}s "
                    "old — describe it honestly as a stale frame, not live "
                    "video, and say the camera appears to be down. "
                    + self._frame_followup_note())
        # Publish the frame two ways and prefer the relay copy for casting:
        #  a) relay asset  {VCAST_API}/frame?screen=0&token=cam:<slug>-<ts>
        #     — same-origin for vcast pages -> canvas stays clean so
        #     vcast_snapshot can verify the cast visually
        #  b) PWA static   https://idc01.../static/cam-snap/<name>.png
        #     — durable public URL for inspection/other clients
        slug = re.sub(r"[^a-z0-9]+", "-", resolved.lower()).strip("-")
        token = f"cam:{slug}-{int(time.time())}"
        # Stale cams cast the generated status card (banner baked in);
        # the raw last-good thumb is what the describe turn receives.
        asset = status_card or png
        asset_mime = "image/jpeg" if stale_meta else "image/png"
        try:
            vbase = os.environ.get(
                "VCAST_API",
                "https://tony-dell.taila0626a.ts.net/api/input-bridge")
            payload = json.dumps({
                "screen": 0, "token": token,
                "data": f"data:{asset_mime};base64,"
                        + base64.b64encode(asset).decode(),
                "state": "asset",
            }).encode()
            req = urllib.request.Request(
                vbase + "/frame", data=payload,
                headers={"Content-Type": "application/json"})
            await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
            # cast_url must be the PUBLIC same-origin route — the vcast page
            # fetches it; the local VCAST_API base would cross origins and
            # taint the canvas for vcast_snapshot.
            pub = os.environ.get(
                "VCAST_PUBLIC_API",
                "https://tony-dell.taila0626a.ts.net/api/input-bridge")
            result["cast_url"] = f"{pub}/frame?screen=0&token={token}"
        except Exception as exc:
            logger.warning("session=%s snap relay publish failed: %s",
                           self.session_id, exc)
        try:
            snap_dir = (Path(__file__).resolve().parent.parent
                        / "frontend" / "cam-snap")
            snap_dir.mkdir(parents=True, exist_ok=True)
            name = (f"{slug}-{int(time.time())}"
                    + (".jpg" if stale_meta else ".png"))
            (snap_dir / name).write_bytes(png)
            snaps = sorted(
                [p for pat in ("*.png", "*.jpg")
                 for p in snap_dir.glob(pat)],
                           key=lambda p: p.stat().st_mtime)
            for old in snaps[:-20]:
                old.unlink(missing_ok=True)
            result.setdefault("cast_url",
                              os.environ.get(
                                  "ADA_SNAP_PUBLIC_BASE",
                                  "https://idc01.taila0626a.ts.net/static/cam-snap/")
                              + name)
            result["inspect_url"] = result["cast_url"]
        except Exception as exc:
            logger.warning("session=%s snap publish failed: %s",
                           self.session_id, exc)
        if result.get("cast_url") and not (
                args.get("screen") or args.get("target")):
            result["output"] += (
                " To show this frame on a vcast display, call "
                f"cast_to_screen(action='image', "
                f"url='{result['cast_url']}') or call me again with "
                "screen=N — copy that url value character-for-character; "
                "never guess or invent a URL.")
        mime = "image/png"
        if stale_meta:
            mime = "image/jpeg"
            resolved = (f"{resolved} (STALE — last known frame, "
                        f"{age // 60}m old; camera offline)")
        result = await self._snap_display(
            args, result, result.get("cast_url"))
        return result, (resolved, png, mime)

    async def _snap_display(self, args: dict, result: dict,
                            url: str | None) -> dict:
        """Absorbed cctv_snapshot behavior: screen=N / target='tv'|'screen'
        pushes the captured frame to a display through the runner's gated
        tools — screen-owner, busy-interrupt and rate-limit checks all
        live in cast_to_screen/tv_action, not duplicated here."""
        try:
            n = int(args.get("screen") or 0)
        except (TypeError, ValueError):
            n = 0
        t = str(args.get("target") or "").strip().lower()
        if not (n or t in ("tv", "screen") or t.startswith("vcast")):
            return result
        if self.tool_runner is None:
            result["output"] += (" No display bridge on this instance — "
                                 "the cast_url is in the result.")
            return result
        if not url:
            result["output"] += " No frame URL to show."
            return result
        owner = self.session_owner
        kw = dict(identity=(owner or self.current_speaker_ha_person),
                  speaker=self.current_speaker_ha_person,
                  speaker_session=self.speaker_session, owner=owner)
        try:
            if t == "tv":
                out = await self.tool_runner.execute(
                    "tv_action", {"cmd": "nav", "text": url}, **kw)
                result["display"] = out
                result["output"] += " Shown on the TV."
            else:
                n = n or 1
                out = await self.tool_runner.execute(
                    "cast_to_screen",
                    {"screen": n, "action": "image", "url": url}, **kw)
                result["display"] = out
                result["output"] += (
                    f" Shown on screen {n}."
                    if isinstance(out, dict) and out.get("delivered")
                    else f" Screen cast did not confirm delivery: {out}")
        except Exception as exc:
            result["display_error"] = str(exc)
            result["output"] += f" (display push failed: {exc})"
        return result

    async def _grabbed_result(self, args: dict,
                              shot: dict) -> tuple[dict, tuple[str, bytes, str] | None]:
        """go2rtc/YT grab succeeded via the runner's chain (the absorbed
        cctv_snapshot home-cam path): fetch the published frame bytes so
        the describe contract still attaches an image, then honor
        screen=/target= like the VMS path."""
        url = shot["url"]
        frame = None
        try:
            data = await asyncio.to_thread(
                lambda: urllib.request.urlopen(
                    urllib.request.Request(url), timeout=10).read())
            if len(data) > 500:
                frame = (str(shot["camera"]), data, "image/jpeg")
        except Exception as exc:
            logger.warning("session=%s grabbed frame fetch failed: %s",
                           self.session_id, exc)
        result = {
            "output": (f"Still frame captured from camera "
                       f"'{shot['camera']}'. "
                       + self._frame_followup_note()),
            "channel": shot["camera"], "cast_url": url,
            "inspect_url": url,
        }
        return (await self._snap_display(args, result, url), frame)

    async def _traffic_camera(self, args: dict) -> tuple[dict, tuple[str, bytes, str] | None]:
        """traffic_camera — search the Longdo/iTIC feed, snap the best match,
        attach the JPEG, publish a same-origin cast asset."""
        from backend import traffic_camera as tc
        query = str(args.get("query") or args.get("view")
                    or args.get("camera") or args.get("channel")
                    or "").strip()
        lat = _as_num(args.get("lat")); lon = _as_num(args.get("lon"))
        heading = _as_num(args.get("heading"))
        if not query and lat is None:
            return ({"error": "pass a query (area/road) or lat+lon "
                              "(+optional heading)"}, None)
        try:
            cams = await asyncio.to_thread(tc.find_cams, query, lat, lon, heading)
        except Exception as exc:
            return ({"error": f"camera feed unavailable: {exc}. "
                              "Retry this tool in a few seconds — never "
                              "invent or guess a camera URL; only cast a "
                              "cast_url this tool returns."}, None)
        if not cams:
            return ({"error": f"no traffic camera matched {query or 'that position'}. "
                              "Try a road/area keyword (e.g. 'burapha', 'bangna') "
                              "or pass lat/lon. Never invent a camera URL — "
                              "only cast a cast_url this tool returns."}, None)
        if cams[0].get("suspended"):
            return ({"error": cams[0]["title"] +
                              " — the feed currently has live frames for "
                              "Bangkok and Nonthaburi cams only."}, None)
        # ~half the live-flagged cams still return dead frames — snap the
        # top candidates concurrently with a hard budget, then take the
        # best-ranked success. Sequential tries with 20s timeouts stalled
        # a turn for 40-160s whenever a feed segment went dark.
        cands = cams[:4]
        try:
            snaps = await asyncio.wait_for(
                asyncio.gather(*(
                    asyncio.to_thread(tc.snap, c, 10) for c in cands),
                    return_exceptions=True),
                timeout=30)
        except asyncio.TimeoutError:
            return ({"error": "traffic camera feed timed out — it is "
                              "responding very slowly right now. Retry "
                              "this tool once in a few seconds; if the "
                              "user just wants a camera on a screen, "
                              "cctv_wall zone='traffic' works meanwhile. "
                              "Never invent a camera URL — only cast a "
                              "cast_url this tool returns."}, None)
        cam = jpeg = mime = None
        dead = []
        for cand, res in zip(cands, snaps):
            if isinstance(res, tuple) and res[0]:
                cam, jpeg, mime = cand, res[0], res[1]
                break
            dead.append(cand["title"][:60])
        if cam is None:
            return ({"error": f"{len(dead)} matched camera(s) returned no usable "
                              f"frame ({', '.join(dead)}). The feed marks many "
                              "cams offline — try another area. Never invent "
                              "a camera URL — only cast a cast_url this tool "
                              "returns."}, None)
        slug = re.sub(r"[^a-z0-9]+", "-", (cam.get("camid") or "cam").lower())
        cast_url = await asyncio.to_thread(tc.publish_relay, jpeg, slug)
        dist = f" (~{cam['dist_km']} km away)" if cam.get("dist_km") else ""
        result = {
            "output": (
                f"Traffic camera '{cam['title']}'{dist} — "
                + self._frame_followup_note(
                    hint="traffic density, weather, flooding, incidents")),
            "camid": cam["camid"], "title": cam["title"],
            "matches": len(cams),
        }
        if cam.get("dist_km") is not None:
            result["dist_km"] = cam["dist_km"]
        if cast_url:
            result["cast_url"] = cast_url
            result["output"] += (
                f" To show it on a vcast display call cast_to_screen("
                f"action='image', url='{cast_url}') — copy that url value "
                "character-for-character; never invent a URL.")
        return result, (cam["title"], jpeg, mime)

    async def _vcast_snapshot(self, args: dict) -> tuple[dict, tuple[str, bytes, str] | None]:
        """vcast_snapshot — ask the display to capture its own frame over the
        input-bridge: /pub {type:snap-request,token} -> the page POSTs a JPEG
        to /frame -> we poll /frame?screen&token until it lands. Same frame
        delivery shape as _camera_snapshot (follow-up client content)."""
        try:
            screen = int(args.get("screen"))
        except (TypeError, ValueError):
            return ({"error": "screen number required — call vcast_list to see registered displays."}, None)
        base = os.environ.get(
            "VCAST_API", "https://tony-dell.taila0626a.ts.net/api/input-bridge")
        token = f"snap-{int(time.time() * 1000)}-{self.session_id[:8]}"
        def _req(path: str, payload: dict | None = None):
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(
                base + path, data=data,
                headers={"Content-Type": "application/json"} if data else {})
            return urllib.request.urlopen(req, timeout=10)
        def _req_post_gev(url: str, payload: dict):
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            return urllib.request.urlopen(req, timeout=10)
        try:
            r = await asyncio.to_thread(
                _req, "/pub",
                {"screen": screen,
                 "msg": {"type": "snap-request", "token": token}})
            delivered = json.load(r).get("delivered", 0)
            if not delivered:
                return ({"error": f"screen {screen} is not connected — "
                                  "check vcast_list for online displays."}, None)
        except Exception as exc:
            return ({"error": f"snap-request failed: {exc}"}, None)
        # GEV pages live in a same-origin iframe the vcast page can't read —
        # but the iframe's own remote client can grab its Cesium canvas.
        # Fire capture_frame at that screen's GEV remotes; whichever lands
        # the frame at /frame first wins the poll below.
        gev_cmd = os.environ.get(
            "GEV_CMD_URL",
            "https://tony-dell.taila0626a.ts.net/apps/gev-cmd/command")
        try:
            await asyncio.to_thread(
                _req_post_gev, gev_cmd,
                {"name": "capture_frame",
                 "args": {"token": token},
                 "screen": screen, "wait": 0})
        except Exception:
            pass  # bridge down or no GEV client — snap-request may still work
        last_err = None
        for _ in range(15):
            try:
                r = await asyncio.to_thread(
                    _req, f"/frame?screen={screen}&token={token}")
                ctype = r.headers.get("Content-Type", "")
                if ctype.startswith("image/"):
                    jpeg = r.read()
                    if jpeg:
                        label = f"vcast screen {screen}"
                        result = {
                            "output": (
                                f"Still frame captured from {label}. "
                                + self._frame_followup_note(
                                    hint="what the screen is actually showing")),
                            "screen": screen,
                        }
                        return result, (label, jpeg, "image/jpeg")
                else:
                    body = json.loads(r.read() or b"{}")
                    if body.get("error") and body.get("ok"):
                        if body["error"] == "simulated":
                            return ({"error": (
                                f"screen {screen} is a headless/simulated display "
                                f"— it has no real pixels to capture. It reports "
                                f"state '{body.get('state') or 'unknown'}' "
                                f"({body.get('detail') or 'no detail'}). Tell the "
                                "user the screen is simulated and read back its "
                                "state — do NOT describe an image or say the "
                                "screen is offline/stuck.")}, None)
                        # uncapturable-iframe isn't final: a GEV iframe's own
                        # remote client may still post a canvas capture for
                        # this token — keep polling. image-load-failed is.
                        if body["error"] != "image-load-failed":
                            last_err = body
                            await asyncio.sleep(0.8)
                            continue
                        detail = body.get("detail")
                        msg = (f"screen {screen} could not capture: {body['error']} "
                               f"(state={body.get('state') or 'unknown'})."
                               + (f" Failed URL: {detail}." if detail else "")
                               + (" The casted image URL failed to load — tell the "
                                  "user the screen shows a broken image and re-cast "
                                  "with the cast_url from ada_camera_snapshot."
                                  if body["error"] == "image-load-failed" else
                                  " If it is an uncapturable iframe, tell the user "
                                  "the page content cannot be screenshotted."))
                        return ({"error": msg}, None)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    logger.warning("session=%s vcast frame poll: %s", self.session_id, exc)
            except Exception as exc:
                logger.warning("session=%s vcast frame poll: %s", self.session_id, exc)
            await asyncio.sleep(0.8)
        if last_err:
            detail = last_err.get("detail")
            msg = (f"screen {screen} could not capture: {last_err['error']} "
                   f"(state={last_err.get('state') or 'unknown'})."
                   + (f" Failed URL: {detail}." if detail else "")
                   + " The framed page cannot be screenshotted by the browser.")
            return ({"error": msg}, None)
        return ({"error": f"screen {screen} did not return a frame in time — "
                          "it may be offline or stuck."}, None)

    async def _run_decision_check(self, product: str, url: str, mode: str) -> None:
        filler = asyncio.create_task(self._check_slow_filler(mode))
        result = None
        error = "unknown"
        try:
            result = await self.tool_runner.decision_engine.check(
                text=product, url=url, mode=mode, caller="voice",
            )
        except Exception as exc:
            error = str(exc)
            logger.warning("session=%s decision check failed: %s", self.session_id, exc)
        finally:
            filler.cancel()
        await self._on_check_complete(result, None if result and result.ok else error)

    async def _check_slow_filler(self, mode: str) -> None:
        # If the check runs long, keep the user informed rather than going quiet.
        await asyncio.sleep(20 if mode == "quick" else 45)
        await self._wait_for_idle(timeout=4.0)
        if self._response_active:
            return
        try:
            await self.send_text_turn(
                "(system) The purchase check is still running — it searches "
                "the live web. Briefly tell the user you're still verifying it."
            )
        except Exception:
            pass

    async def _session_context_tail(self, excluded: set[str] | None = None) -> str:
        """Session-start awareness blob: today's agenda + open tasks +
        pending action proposals from earlier conversations."""
        if os.environ.get("ADA_AGENDA_CONTEXT", "1") in ("0", "false", "no"):
            return ""
        if excluded and CALENDAR_TOOLS <= excluded:
            return ""
        svc = None
        if self.tool_runner is not None:
            try:
                svc = self.tool_runner.calendar
            except Exception:
                svc = None
        if svc is None:
            return ""
        parts: list[str] = []
        try:
            # Google calendar+tasks round-trips from remote hosts routinely
            # exceed 4s on cold token refresh; TimeoutError logs an empty
            # message, which made this failure silent until now.
            plan = await asyncio.wait_for(svc.plan_day("today"), timeout=12.0)
        except Exception as exc:
            logger.info("session=%s agenda prefetch failed: %r",
                        self.session_id, exc)
        else:
            lines = ["Today's agenda (snapshot from session start; call "
                     "plan_day for a fresh view):"]
            events = plan.get("events") or []
            if not events:
                lines.append("- no events today")
            for e in events[:6]:
                start = str(e.get("start") or "")
                when = ("all-day" if e.get("all_day")
                        else start[11:16] if "T" in start else start)
                lines.append(f"- {when} {e.get('title') or '?'}")
            for t in (plan.get("open_tasks") or [])[:8]:
                due = f" (due {t['due']})" if t.get("due") else ""
                lines.append(f"- open task: {t.get('title') or '?'}{due}")
            parts.append("\n".join(lines))
        try:
            from backend.conversation_memory import pending_action_proposals
            proposals = await asyncio.wait_for(
                pending_action_proposals(), timeout=4.0
            )
        except Exception as exc:
            logger.info("session=%s proposals prefetch failed: %r",
                        self.session_id, exc)
            proposals = []
        if proposals:
            lines = ["Pending suggestions from recent conversations:"]
            for p in proposals:
                lines.append(f"- [{p['key']}] {p['text']}")
            parts.append("\n".join(lines))
        if not parts:
            return ""
        return "\n\n" + "\n".join(parts)

    async def _on_check_complete(self, result: Any, error: str | None) -> None:
        """Push the verdict back into the session so Ada speaks it."""
        await self._wait_for_idle()
        if result is None or error:
            prompt = (
                f"(system) The purchase check failed ({error}). Briefly tell the "
                "user the check couldn't complete and suggest the Check panel "
                "in the chat app as an alternative."
            )
        else:
            bits = [f"verdict: {result.verdict} (confidence {result.confidence:.0%})"]
            if result.summary:
                bits.append(result.summary)
            if result.flags:
                bits.append("flags: " + "; ".join(result.flags[:3]))
            if result.alternatives:
                alt = result.alternatives[0]
                bits.append(
                    f"top alternative: {alt.get('name', '?')} — "
                    f"{alt.get('source', '')} {alt.get('price', '')}".strip()
                )
            prompt = (
                "(system) The purchase check finished. " + ". ".join(bits) +
                ". Tell the user the verdict and the most important reason in "
                "one or two sentences, and mention the full detail card is in "
                "the chat panel."
            )
        try:
            await self.send_text_turn(prompt)
        except Exception as exc:
            logger.warning("session=%s check send_text_turn failed: %s", self.session_id, exc)

    @staticmethod
    def _modality_name(modality: Any) -> str:
        return str(getattr(modality, "value", modality) or "unknown").lower()

    def _record_usage(self, usage: Any) -> None:
        in_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        out_tokens = int(
            getattr(usage, "response_token_count", None)
            or getattr(usage, "candidates_token_count", 0)
            or 0
        )
        self.usage_input_tokens += in_tokens
        self.usage_output_tokens += out_tokens
        in_mod: dict[str, int] = {}
        out_mod: dict[str, int] = {}
        for detail in getattr(usage, "prompt_tokens_details", None) or []:
            modality = self._modality_name(getattr(detail, "modality", None))
            n = int(getattr(detail, "token_count", 0) or 0)
            in_mod[modality] = in_mod.get(modality, 0) + n
            self.usage_input_by_modality[modality] = (
                self.usage_input_by_modality.get(modality, 0) + n
            )
        for detail in (
            getattr(usage, "response_tokens_details", None)
            or getattr(usage, "candidates_tokens_details", None)
            or []
        ):
            modality = self._modality_name(getattr(detail, "modality", None))
            n = int(getattr(detail, "token_count", 0) or 0)
            out_mod[modality] = out_mod.get(modality, 0) + n
            self.usage_output_by_modality[modality] = (
                self.usage_output_by_modality.get(modality, 0) + n
            )
        usage_ledger.record(
            "live", session_id=self.session_id,
            input_tokens=in_tokens, output_tokens=out_tokens,
            input_by_modality=in_mod, output_by_modality=out_mod,
            cached_tokens=int(getattr(usage, "cached_content_token_count", 0) or 0),
            tool_use_tokens=int(getattr(usage, "tool_use_prompt_token_count", 0) or 0),
        )
        logger.info(
            "session=%s usage turn in=%d out=%d | total in=%d out=%d in_by_modality=%s out_by_modality=%s",
            self.session_id, in_tokens, out_tokens,
            self.usage_input_tokens, self.usage_output_tokens,
            self.usage_input_by_modality, self.usage_output_by_modality,
        )

    def usage_summary(self) -> dict[str, Any]:
        return {
            "input_tokens": self.usage_input_tokens,
            "output_tokens": self.usage_output_tokens,
            "input_by_modality": dict(self.usage_input_by_modality),
            "output_by_modality": dict(self.usage_output_by_modality),
        }

    async def connect(self, resumption_handle: str | None = None) -> None:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")

        self._client = genai.Client(api_key=self.api_key)
        config = {
            "response_modalities": ["AUDIO"],
            "media_resolution": (
                types.MediaResolution.MEDIA_RESOLUTION_HIGH
                if self.video_resolution == "high"
                else types.MediaResolution.MEDIA_RESOLUTION_LOW
            ),
            # Per-session clock injection — the model has no intrinsic 'now',
            # so time-sensitive tool calls (calendar 'in 2 hours', 'Friday')
            # anchor to the user's local time at session start.
            "system_instruction": self.instructions + _now_context(),
            "input_audio_transcription": {
                # Restrict ASR to Thai/English — unhinted input
                # transcription wandered into Korean/Chinese/Portuguese
                # on noisy Thai speech (transcript 2026-09-29 855a65dab8).
                "language_codes": ["th-TH", "en-US"],
            },
            "output_audio_transcription": {},
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": self.voice},
                }
            },
            "realtime_input_config": {
                # Be explicit about barge-in and favor detecting near-end speech
                # over the assistant audio playing through the iPad/iPhone speakers.
                "activity_handling": types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                "automatic_activity_detection": {
                    "disabled": False,
                    # Speaker echo can otherwise look like a new user turn and
                    # make Ada interrupt herself. LOW still supports barge-in,
                    # but requires stronger evidence that speech has started.
                    "start_of_speech_sensitivity": (
                        types.StartSensitivity.START_SENSITIVITY_LOW
                    ),
                    "end_of_speech_sensitivity": (
                        types.EndSensitivity.END_SENSITIVITY_HIGH
                    ),
                    "prefix_padding_ms": 200,
                    "silence_duration_ms": 500,
                },
            },
            # Native audio consumes context quickly. Sliding-window compression
            # prevents an extended voice conversation from exhausting the
            # session context while retaining recent conversational history.
            "context_window_compression": types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            "session_resumption": types.SessionResumptionConfig(
                handle=resumption_handle,
            ),
            "tools": [{
                "function_declarations": [{
                    "name": "set_facial_expression",
                    "description": (
                        "Changes Ada's visible facial expression without interrupting speech. "
                        "Select the expression matching the tone of Ada's current response."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "expression": {
                                "type": "string",
                                "enum": list(EXPRESSION_NAMES),
                                "description": "The facial expression Ada should display.",
                            }
                        },
                        "required": ["expression"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_home_state",
                    "description": (
                        "Returns current read-only Home Assistant home-plug state, "
                        "whether the user is home, recent local home occupancy, and any "
                        "active five-minute or latched habit condition — also handles "
                        "what used to be ada_ha_get_state. Use this when asked "
                        "about home plugs, occupancy, or whether plugs were left on. "
                        "entity_id reads one entity live; domain lists the entities "
                        "under one HA domain; domain='memory' returns the stored home "
                        "snapshot (counts of controllable devices and sensors, person "
                        "entity, and a confidence summary)."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "Optional exact entity_id to read live, e.g. binary_sensor.front_door.",
                            },
                            "domain": {
                                "type": "string",
                                "description": "Optional HA domain to list (e.g. 'light', 'sensor'), or 'memory' for the stored snapshot.",
                            },
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "control_entity",
                    "description": (
                        "Actuate one Home Assistant entity — also handles what used to be "
                        "control_cover, press_button, and control_media_player. The entity's "
                        "domain picks the path: pass on=true/false or action='on'/'off' for "
                        "lights, switches, fans, and input_booleans; action='open'/'close'/'stop' "
                        "for cover.* entities like gates and shutters; action='press' (or no "
                        "action) for button.* entities; and the media_player actions "
                        "turn_on, turn_off, media_play, media_pause, media_stop, volume_up, "
                        "volume_down, volume_mute, select_source (with source=) for "
                        "media_player.* entities. Only Tony's own TV entities "
                        "(media_player.tony_tv, media_player.tony_tv_cast) are valid media "
                        "players — other media_player entities discovered on the network "
                        "belong to devices we do not own and must not be targeted."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The Home Assistant entity_id to control, e.g. light.living_room or cover.gate_motor.",
                            },
                            "on": {
                                "type": "boolean",
                                "description": "For light/switch/fan/input_boolean entities: true to turn on, false to turn off.",
                            },
                            "action": {
                                "type": "string",
                                "enum": ["on", "off", "open", "close", "stop", "press",
                                         "turn_on", "turn_off", "media_play", "media_pause",
                                         "media_stop", "volume_up", "volume_down",
                                         "volume_mute", "select_source"],
                                "description": "Domain action: on|off for simple entities; open|close|stop for cover.*; press for button.*; media_player verbs for media_player.* (source= required for select_source).",
                            },
                            "source": {
                                "type": "string",
                                "description": "media_player select_source only: the input/source name to select.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required for dangerous-safety entities; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["entity_id"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "home_search",
                    "description": (
                        "Find Home Assistant devices, sensors, or recorded events — also "
                        "handles what used to be list_home_devices, search_home_devices, "
                        "list_sensors, search_sensors, ada_ha_search_devices, "
                        "ada_ha_search_sensors, and ada_ha_search_events. kind='device' "
                        "searches controllable devices (lights, switches, fans, covers, "
                        "buttons, media players) by name or entity_id — use this to find "
                        "the exact entity_id before control_entity; a blank query lists "
                        "all controllable devices (bounded). kind='sensor' searches sensor "
                        "entities with current state and unit; a blank query lists sensors. "
                        "kind='event' searches recorded home events from memory — transitions "
                        "captured by the event recorder plus persisted event batches, for "
                        "'when did the gate open earlier' or 'any door events this week'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Name or keyword to search, e.g. 'kitchen table', 'front gate', 'pool temperature', 'door'. Blank lists the kind instead.",
                            },
                            "kind": {
                                "type": "string",
                                "enum": ["device", "sensor", "event"],
                                "description": "What to find: a controllable device (default), a sensor entity, or recorded events.",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100,
                                "description": "Maximum results. Default 10.",
                            },
                            "hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 720,
                                "description": "kind='event' only: how many hours of recorded events to search. Default 24.",
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "yt_cast",
                    "description": (
                        "Cast a YouTube video to the living-room TV with subtitles burned in. "
                        "ONLY when the user wants a VIDEO playing on the TV — NOT for looking up "
                        "news, facts or information (answer those yourself via web search). "
                        "This is THE tool for any 'play/watch/cast a YouTube video on the TV' request "
                        "AND for any subtitle/caption request — it always renders the video's "
                        "original-language subtitle on top with a translated line below "
                        "(English is added automatically for non-English sources). "
                        "Do NOT search for a different video that already has subtitles or try "
                        "generic media playback — this tool generates subtitles for any video. "
                        "Pass a YouTube URL or a search phrase (video title + channel name works best). "
                        "If the user names a numbered vcast screen instead of the TV, do NOT call this — "
                        "use cast_to_screen(action='play', url=<YouTube URL>); vcast displays auto-embed it. "
                        "The tool returns as soon as preparation starts — the video itself takes ~1-3 min "
                        "(download + subtitle translation + transcode; replays are much faster). "
                        "Acknowledge immediately in one short sentence, e.g. 'getting it ready, about a "
                        "minute, I'll let you know when it's on' — never claim it is already playing. "
                        "The system will notify you when playback actually starts."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "YouTube URL or search phrase, e.g. 'the egg kurzgesagt'.",
                            },
                            "language": {
                                "type": "string",
                                "description": (
                                    "Target subtitle language code shown below the original-language "
                                    "line (default 'th' for Thai)."
                                ),
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "yt_cast_status",
                    "description": (
                        "Returns the current YouTube-to-TV cast progress: whether transcoding is still "
                        "running, segment count, and whether subtitles were generated."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "yt_cast_stop",
                    "description": (
                        "Stops the YouTube video currently casting to the TV. Use when the user asks "
                        "to stop the video or stop casting."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "yt_transcript",
                    "description": (
                        "Fetches a YouTube video's spoken content as plain text (auto-captions via yt-dlp "
                        "on the transcript host — no video download). Returns title, language, and up to "
                        "~6k chars of transcript. Use when the user wants news/content from a YouTube "
                        "video summarized or transcribed — Thai news sites block scrapers, so YouTube "
                        "is the open source. This reads text only; it does NOT play or cast anything."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "YouTube URL or video ID.",
                            },
                            "language": {
                                "type": "string",
                                "description": "Caption language to prefer (default 'th'; falls back to en).",
                            },
                        },
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "tv_action",
                    "description": (
                        "Send a command to the TV casting controller (cast-browser) through the Home Assistant rest_command.tv_action service. "
                        "This is for the LG TV only — vcast display panes (screen 1/2/3/4) zoom/scroll via "
                        "cast_to_screen action='zoom'/'unzoom' pane=N, NOT tv_action. "
                        "ONLY when the user wants something shown on a screen — never use this to answer "
                        "questions or display search results unprompted (answer via web_search instead). "
                        "Cast targets via cmd='nav': text='<URL>' shows a page in the TV's browser (fully controllable afterwards), "
                        "text='screenlive:workspace:N[:pad|crop]' casts this host's live desktop workspace N, "
                        "text='tony-omen:workspace:N' casts Tony's desktop workspace N (switches his live workspace too). "
                        "While a nav'd page is on the TV you can control it: cmd='scroll' text='up|down' (optionally dx/dy/factor), "
                        "cmd='click' text='<visible text>' or role='<role>' or selector='<css>', cmd='type' text='<text>', "
                        "cmd='press' text='<key e.g. Enter|Escape|Backspace>', cmd='back' to go back, "
                        "cmd='shot' text='<name>' for a screenshot, cmd='viewport' text='WxH'. "
                        "A streamed desktop (screenlive/tony-omen) is one-way video — control is limited to switching workspaces; "
                        "for interactive control prefer nav'ing the page itself. "
                        "Personal screens are owner-locked: 'cast my screen' only works for the screen's owner — "
                        "the living-room TV is shared and available to everyone."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "cmd": {
                                "type": "string",
                                "description": "Command key: nav|scroll|click|type|press|back|shot|viewport|power.",
                            },
                            "text": {
                                "type": "string",
                                "description": "Command text/payload: nav target (URL, 'screenlive:workspace:N[:pad|crop]', 'tony-omen:workspace:N'), scroll direction, visible text to click, text to type, or shot/viewport args.",
                            },
                            "selector": {"type": "string", "description": "CSS selector for click."},
                            "role": {"type": "string", "description": "ARIA role for click (e.g. 'button')."},
                            "key": {"type": "string", "description": "Key name for press (e.g. 'Enter', 'Escape')."},
                            "dx": {"type": "number", "description": "Horizontal scroll amount (px)."},
                            "dy": {"type": "number", "description": "Vertical scroll amount (px)."},
                            "factor": {"type": "number", "description": "Scroll amount as fraction of viewport height."},
                        },
                        "required": ["cmd"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "vcast_list",
                    "description": (
                        "List the vcast virtual displays (numbered software cast targets — iPad/iPhone/browser "
                        "running the vcast app, NOT the TV). Returns screen number, name, device, online/offline, "
                        "what is playing, plus the relay's ground truth: active_captures (camera-capture leases "
                        "per screen) and camwall_zones (enabled periodic walls). Use when the user refers to "
                        "'screen 1/2/...', asks which screens are available, or when diagnosing a cast — an "
                        "offline screen or a stale capture explains a silent failure; call before "
                        "cast_to_screen if unsure. A camwall_zones entry is NOT proof a wall is visible — "
                        "when state_mismatch flags it, the wall is down; never claim a wall is on-screen "
                        "from the registry flag alone, and a user asking you to 'put up' a flagged wall "
                        "still needs cctv_wall action=start."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "cast_to_screen",
                    "description": (
                        "Cast content to a numbered vcast virtual display (a browser/PWA screen — NOT the physical TV; "
                        "for the TV use tv_action or yt_cast). action='nav' url='<URL>' shows a web page, "
                        "'play' url='<m3u8, video, or YouTube/Vimeo page URL>' plays video (HLS supported, "
                        "YouTube/Vimeo links auto-embed on the display), 'image' url='<png/jpg>' "
                        "shows a snapshot, 'audio' url plays sound or TTS, 'stop' returns it to idle, "
                        "'uplink' starts the display's camera uplink (frames available via "
                        "GET /frame?screen=N&token=cam), 'uplink-stop' stops it. "
                        "SPLIT-SCREEN: action='layout' panes=N (2-5) splits the screen into sub-panes — "
                        "then cast each thing to its own pane (pane=0..N-1, 0=left/top) with nav/play/image. "
                        "action='zoom' pane=N makes one pane fullscreen; 'unzoom' returns to the grid. "
                        "stop with pane=N clears just that pane; stop alone resets to single-pane idle. "
                        "Screens are numbered — call vcast_list first if you need to pick one. "
                        "Some screens are private to their owner — casting to another person's screen is denied. "
                        "INTERRUPT RULE: a cast onto a screen that is busy (a running camera capture/uplink, "
                        "an enabled camera wall, or media playing) returns needs_confirm with would_interrupt "
                        "details and does NOT happen — tell the user what is running on that screen, ask if "
                        "they want it replaced, then retry with confirmed=true only after they say yes. When "
                        "a successful cast result carries a 'replaced' field, acknowledge aloud what was "
                        "interrupted. Camera captures (uplink, cctv walls) are permission-gated: ask the user BEFORE "
                        "starting one, and if the result's active_captures shows a running capture, "
                        "acknowledge it and ask before stopping — never silently stop or leave it unmentioned "
                        "when the user changes the subject. When the user EXPLICITLY asked for the camera "
                        "(e.g. 'show me the pool cam'), that request IS the consent — call it directly with "
                        "confirmed=true, do NOT re-ask. Confirmation only applies when YOU propose a capture "
                        "the user didn't ask for, or when a guest voice requests one."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "screen": {
                                "type": "integer",
                                "description": "Screen number (the # shown on the display and in vcast_list).",
                            },
                            "action": {
                                "type": "string",
                                "description": "nav=web page | play=video mp4/m3u8 or YouTube/Vimeo watch URL (auto-embeds on screen) | image=still jpg/png | audio | stop | layout | zoom | unzoom | uplink | uplink-stop (default nav). Pick by content type.",
                            },
                            "url": {
                                "type": "string",
                                "description": "Target URL for nav/play/image/audio. Not needed for stop/layout/zoom/unzoom.",
                            },
                            "pane": {
                                "type": "integer",
                                "description": "Sub-pane index 0..N-1 on a split screen (0=left/top). Also used by zoom and pane-targeted stop.",
                            },
                            "panes": {
                                "type": "integer",
                                "description": "For action=layout: split the screen into 2-5 panes.",
                            },
                            "mode": {
                                "type": "string",
                                "description": "For action=layout: 'pip' floats panes 1..N top-right over a fullscreen pane 0 (picture-in-picture); omit for the normal grid split.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required for action='uplink' (camera capture) and for interrupting a busy screen (capture/camwall/playing — see needs_confirm) — set true only after the user explicitly confirms.",
                            },
                        },
                        "required": ["screen"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "vcast_say",
                    "description": (
                        "Speak a short narration line out loud on a numbered vcast display (the screen's own "
                        "speech synthesis — e.g. the iPad's speaker). Use it to narrate what you are doing on "
                        "that screen: right after cast_to_screen say what you loaded, before gev_command say "
                        "what the map is about to do, on long waits say what is in progress. Keep it to one "
                        "short sentence, in the SAME language the user is speaking (Thai user -> Thai text; "
                        "the display's voice mirrors the session language). If the result reports speak-blocked, the display hasn't been tapped "
                        "for audio yet — tell the user to tap 'audio' once on that screen, then retry once."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "screen": {
                                "type": "integer",
                                "description": "Screen number to speak on (the same screen you are acting on).",
                            },
                            "text": {
                                "type": "string",
                                "description": "Short spoken line, plain text, under ~200 chars.",
                            },
                        },
                        "required": ["screen", "text"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "vcast_snapshot",
                    "description": (
                        "Capture what a numbered vcast virtual display is actually showing right now — "
                        "the display draws its current frame and the image arrives attached to this "
                        "tool's result. Use to LOOK at a screen and verify what it shows (after a cast, "
                        "to check an overlay, or when the user asks what's on a screen) — do not rely on "
                        "the reported state flag alone. One still frame per call, a few seconds old. "
                        "If the result reports uncapturable-iframe, the screen is showing a framed web "
                        "page the browser cannot capture — EXCEPT God's Eye View (/apps/gev/), whose "
                        "iframe grabs its own Cesium canvas automatically — you DO get real pixels there. "
                        "Not for the TV."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "screen": {
                                "type": "integer",
                                "description": "Screen number (the # shown on the display and in vcast_list).",
                            },
                        },
                        "required": ["screen"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "chat_send",
                    "description": (
                        "Send a message to the user's chat apps in the BACKGROUND — LINE, "
                        "Telegram, or both ('line,telegram'). Returns 'queued' immediately; "
                        "the actual send (including a slow camera snapshot) happens in a "
                        "background job and its result arrives later as a system note — "
                        "tell the user it's being sent, then relay the outcome when the note "
                        "lands. Attach one image via 'camera' (VMS channel name — a fresh "
                        "snapshot is taken) or 'image_url' (a camwall thumb or a cast_url you "
                        "already produced — do NOT pass arbitrary web URLs). 'text' is the "
                        "message/caption. Omit 'to' to send to the default owner chat."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "channel": {
                                "type": "string",
                                "description": "'line', 'telegram', or 'line,telegram'/'both'. Default 'line'.",
                            },
                            "text": {
                                "type": "string",
                                "description": "Message text / image caption.",
                            },
                            "camera": {
                                "type": "string",
                                "description": "VMS camera channel name — a fresh snapshot is taken and attached.",
                            },
                            "image_url": {
                                "type": "string",
                                "description": "URL of an existing image to send (camwall thumb or a cast_url you already produced).",
                            },
                            "to": {
                                "type": "string",
                                "description": "Optional explicit LINE userId or Telegram chat_id; default = owner.",
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "vcast_gesture",
                    "description": (
                        "Enable/disable gesture control on a numbered vcast display — the screen's own "
                        "camera watches for motion. mode='room' = wake-on-motion (a wave or movement "
                        "in front of the screen wakes it and sends a gesture-wake event), "
                        "mode='hand' = hand tracking (MediaPipe, on capable displays), "
                        "'off' stops it. The display reports its actual capability — if the camera is "
                        "denied or the mode isn't built it answers honestly via its state."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "screen": {
                                "type": "integer",
                                "description": "Screen number (the # shown on the display and in vcast_list).",
                            },
                            "mode": {
                                "type": "string",
                                "description": "off | room | hand (default off).",
                            },
                        },
                        "required": ["screen"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "cctv_wall",
                    "description": (
                        "Show the live camera wall on a vcast display — a grid of periodic thumbnails "
                        "for a camera zone. Zones: 'zone-a' (estate perimeter: roads, walkway, guard), "
                        "'noble-park' (pool, tennis, playground, mini mart), 'tony-house' (home cams "
                        "c100/c201/coffee), 'vms-noble-club' (every noble-club DVR channel), "
                        "'vms-noble-a' (every noble-a DVR channel), 'rama9' (Rama 9 demo traffic wall), "
                        "'traffic' (DOH Bangkok cams), 'burapha' (Bangna–Burapha expressway), "
                        "'chonburi' (Chonburi corridor). Walls keep warm thumbnails even while off — "
                        "for an AREA question prefer the matching wall over a slow single snap; it loads "
                        "instantly and refreshes in the background. action='settings' tunes a zone without "
                        "casting (interval seconds, jpeg_q 1-8, thumb_w px, cams_skip keys, effects like "
                        "'yolo:person,car@0.35' — yolo overlays detections and records them while on). "
                        "DISPLAY knobs (page-side, work on start too): settings={'cycle_s': N} rotates "
                        "the wall's live cams FULLSCREEN one-at-a-time every N seconds — 'cycle through "
                        "the cameras', 'show each cam fullscreen'; settings={'zones': ['zone-a','noble-park'], "
                        "'zone_s': 45} rotates the WHOLE wall through the listed zones — 'switch between "
                        "walls', 'cycle the walls'. settings={'random': true} picks the next cam/zone at "
                        "random instead of in order — 'random cams'. Combine for per-cam cycling across zones. "
                        "settings and status are NOT confirmation-gated — apply them immediately when asked. "
                        "Use when the user's focus shifts to a camera zone — "
                        "e.g. they ask to check the pool or the front road — OFFER to put the wall up "
                        "('want the Zone A wall on screen 1?') rather than doing it unprompted for a "
                        "single one-off look; a single look is ada_camera_snapshot. action='start' "
                        "enables background refresh + casts the grid; 'stop' disables it. Thumbs are "
                        "still frames updated in the background — not live video."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "description": "'start' (default), 'stop', 'settings' (tune zone knobs — no cast, no confirm needed), or 'status' (per-cam live/down, ages, yolo counts — for 'what's on the wall' questions).",
                            },
                            "zone": {
                                "type": "string",
                                "description": "'zone-a', 'noble-park', 'tony-house', 'vms-noble-club', 'vms-noble-a', 'rama9' (traffic demo — Bangkok road cams), 'traffic' (DOH Bangkok), 'burapha' (Bangna–Burapha expressway), or 'chonburi' (Chonburi corridor).",
                            },
                            "screen": {
                                "type": "integer",
                                "description": "vcast screen number (vcast_list) — default 1.",
                            },
                            "pane": {
                                "type": "integer",
                                "description": "sub-pane index on a split screen — cast the wall into one pane instead of the whole screen.",
                            },
                            "settings": {
                                "type": "object",
                                "description": "Zone knobs: interval (s), jpeg_q (1-8), thumb_w (px), cams_skip [keys], cams_extra [{label,kind,url}], effects ['timestamp','grid','yolo:person,car@0.35'].",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "When the user explicitly asked for the wall, set true — their request is the consent. Only needed for guest voices or captures you propose unprompted.",
                            },
                        },
                        "required": ["zone"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "gev_command",
                    "description": (
                        "Control God's Eye View (the Cesium map app on /apps/gev/) on whatever screen is showing "
                        "it — the call reaches every connected GEV client including a casted one. "
                        "To 'watch the show': cast_to_screen(action='nav', url='https://tony-dell.taila0626a.ts.net/apps/gev/') "
                        "then drive it with gev_command. args is ONLY the tool's arg object — never put "
                        "'name' inside args and never wrap name+args together; name is the sibling field. "
                        "IMPORTANT — args must match the GEV schema exactly "
                        "(additionalProperties are rejected, the call fails silently if names are wrong). "
                        "Useful names + real arg names: fly_to_location {locationId} — one of the "
                        "preset cities (austin, sf, nyc, tokyo, london, paris, dubai, dc). For ANY other place "
                        "pass {query:'<place>'} — NOT locationId (a non-preset locationId value errors out); "
                        "free-text geocodes via OSM Nominatim, or use latitude/longitude args, zoom_to_globe {}, "
                        "adjust_camera_zoom {factor}, set_layer_visibility {layerId, enabled}, "
                        "select_nearest_aircraft {layerId:'flights', locationId:<preset>} — finds AND starts "
                        "tracking the nearest plane near a preset city (this is how to 'track a flight near X'), "
                        "frame_overhead {target:'flights'|'satellites'|'vessels'} pulls the camera to whatever "
                        "is overhead, track_entity {query:<callsign/name>, layerId} — needs an actual callsign/"
                        "name, NOT a generic word like 'aircraft', stop_tracking {}, "
                        "move_camera {motion:'orbit'|'pan'|'tilt'|'rotate'|'stop', direction?} — 'orbit' is "
                        "the slow cinematic circle, adjust_camera_zoom {direction:'in'|'out', factor?} "
                        "(factor alone is INVALID without direction), analyst_query {layers, scope, filters}, "
                        "annotate_map {annotations:[{type:'route', points:[{latitude,longitude,label?},...], "
                        "label?}]} — a 'route' annotation RESOLVES each waypoint and draws a flyable path "
                        "(NOT 'polyline'/'positions' — those aren't real types); then fly_route {label?} "
                        "flies that drawn route smoothly — route first, fly second, "
                        "clear_annotations {}, get_current_view_state {}, capture_frame {token} (grabs the map "
                        "canvas to the input-bridge frame store — vcast_snapshot does this automatically for "
                        "GEV pages). Optional 'screen' targets one vcast display and 'pane' narrows to one "
                        "split-screen pane (omit to hit every page showing GEV); the client replies are "
                        "collected and returned in 'responses' — get_current_view_state actually answers. "
                        "On error 'no GEV clients': nothing is showing the app — cast "
                        "cast_to_screen(action='nav', url='.../apps/gev/', screen=N) first, wait ~10s for the "
                        "page to register, then RETRY the gev_command. NEVER narrate that a flight/zoom "
                        "happened after a failed or skipped gev_command — say it didn't work and recover "
                        "instead (narrating a move that didn't run is a false claim)."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "GEV tool name."},
                            "args": {"type": "object", "description": "Tool arguments (per GEV tools.json)."},
                            "screen": {"type": "integer", "description": "Limit to this vcast screen (omit = all GEV pages)."},
                            "pane": {"type": "integer", "description": "Limit to this split-screen pane (0-based; omit = all panes)."},
                            "wait": {"type": "number", "description": "Seconds to wait for client responses (0 = fire-and-forget). Default 3."},
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "gev_tour",
                    "description": (
                        "Named God's Eye View flyover tours — when the user asks for 'the X tour', "
                        "'the South Africa tour', 'ทัวร์...', or to list available tours. Call with no "
                        "tour to list; with a name/alias (Thai or English) to get the executable card: "
                        "ordered stops with lat/lon, narration lines, and optional PiP camera frame URLs. "
                        "The card is a script, not self-running — execute it: per stop call "
                        "gev_command fly_to_location {latitude, longitude}, annotate_map to mark it, "
                        "vcast_say the 'say' line, and cast_to_screen(action='image', url=frame_url, pane=1) "
                        "when the stop carries one. Tours with route_points use annotate_map type='route' "
                        "then fly_route. Make sure GEV is on the screen first "
                        "(cast_to_screen nav .../apps/gev/). Cameras during a tour: "
                        "NEVER cctv_wall/cast_to_screen a whole screen already running "
                        "GEV — that kills the map session. Split first: "
                        "cast_to_screen(action='layout', panes=2), then "
                        "cast_to_screen(action='image'|'play', url=cam, pane=1)."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "tour": {"type": "string", "description": "Tour id, alias, or place name — 'za', 'bangkok', 'แอฟริกาใต้'. Omit to list."},
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_battery_status",
                    "description": (
                        "Returns current battery details: total and per-battery SOC, voltage, current, power, temperature, and state of health. "
                        "Use this when the user asks about battery levels, battery health, or battery status."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_battery_detail",
                    "description": (
                        "Returns detailed readings for a single battery (1, 2, or 3). "
                        "Use this when the user asks for 'battery 1 details', 'battery 2 status', or 'tell me about each battery'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "battery_index": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 3,
                                "description": "The battery number to detail: 1, 2, or 3.",
                            }
                        },
                        "required": ["battery_index"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_inverter_status",
                    "description": (
                        "Returns current inverter details: PV power, load power, grid power, battery power, AC output, voltage, frequency, and operating mode. "
                        "Use this when the user asks about the inverter, solar, grid, or load status."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_pool_status",
                    "description": (
                        "Returns the current pool sensor and switch states, separating available and unavailable/unknown entities. "
                        "Use this when the user asks about the pool, pool pump, or pool energy."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_rk600_weather",
                    "description": (
                        "Returns the local RK600 weather station readings: wind speed, wind direction, temperature, humidity, pressure, rainfall, and device status. "
                        "Use this ONLY when the user explicitly asks about current weather, wind, rain, or the weather station/RK600 card. "
                        "Do NOT use it for anything else — not UPS/battery/power questions, not forecasts (use home_search kind='sensor' query='weather'), not travel or flood questions (use web_search)."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_dashboard_tab",
                    "description": (
                        "Reads the named tab from the michael-ha tony-test dashboard and returns "
                        "the entities it displays with their current states. "
                        "Use this when the user asks 'what is on the TPL tab', 'what devices are on V1', "
                        "or 'what does the TPL tab show'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "tab": {
                                "type": "string",
                                "description": "The dashboard tab title, e.g. 'TPL', 'V0', 'V1', 'SK'.",
                            }
                        },
                        "required": ["tab"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_power_summary",
                    "description": (
                        "Returns the current G3 power summary for solar, grid, load, battery, and inverter. "
                        "Also returns min/max/mean over the requested hours. "
                        "Use this when the user asks about solar generation, grid usage, battery, load, or a power summary."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 168,
                                "description": "How many hours of history to include in the summary. Defaults to 24.",
                            }
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "home_history",
                    "description": (
                        "Home Assistant history and event reads — also handles what used to be "
                        "get_logbook, get_sensor_history, get_entity_events, get_recent_events, "
                        "and ada_ha_history. kind='logbook' returns friendly event entries like "
                        "'Front door was opened' or automation runs (entity_id narrows it to one "
                        "entity); kind='events' returns recent state changes across the whole "
                        "home — 'what opened or closed', 'did anything happen while I was away'; "
                        "kind='timeline' returns one entity's state-change timeline with durations "
                        "('when was the door last opened', 'how long was the gate open'); "
                        "kind='series' returns a sensor's numeric history over the window; "
                        "kind='snapshots' lists persisted home snapshots from memory — what the "
                        "state was earlier or how it changed."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["logbook", "events", "timeline", "series", "snapshots"],
                                "description": "Which history read. Optional — defaults to timeline/series for entity_id, events for query, snapshots for domain='memory', else logbook.",
                            },
                            "entity_id": {
                                "type": "string",
                                "description": "timeline/series (required there), logbook (optional narrow): the exact entity_id, e.g. binary_sensor.front_door or sensor.inverters_1_pv_power.",
                            },
                            "domain": {
                                "type": "string",
                                "description": "Optional HA domain to restrict events/logbook to (e.g. 'cover'), or 'memory'/'snapshots' for the persisted snapshots read.",
                            },
                            "hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 720,
                                "description": "How many hours back the window covers. Defaults to 24.",
                            },
                            "query": {
                                "type": "string",
                                "description": "events: optional keyword to limit the feed, e.g. 'door', 'gate', 'kitchen'.",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 200,
                                "description": "Maximum events/snapshots to return. Defaults to 25.",
                            },
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_habit_status",
                    "description": (
                        "Returns every habit Ada tracks, including habits with no occurrences, "
                        "their lifecycle status, rolling seven-day occurrences and days, and "
                        "current monitor state and progress. Use this whenever the user asks "
                        "what habits are tracked or how their habits are progressing."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                }, {
                    "name": "ha_confidence",
                    "description": (
                        "Device trust and safety registry — also handles what used to be "
                        "ada_ha_get_device_confidence and ada_ha_set_device_confidence. "
                        "With no args it returns controllable devices grouped by confidence "
                        "(trusted_working, trusted_broken, learning, needs_integration) — "
                        "each device also carries a safety level: safe, caution, or dangerous. "
                        "Use it when the user asks what is broken, what needs setup, what is "
                        "new, what is trusted, or what is dangerous — and before acting on a "
                        "device. entity_id alone returns one device's entry; entity_id with "
                        "status and/or safety writes new levels — use it when the user says a "
                        "device is broken, new, trusted, dangerous, safe, or needs caution."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "Optional exact entity_id — alone it reads one device, with status/safety it writes.",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["trusted_working", "trusted_broken", "learning", "needs_integration"],
                                "description": "Write path: the confidence level to assign.",
                            },
                            "safety": {
                                "type": "string",
                                "enum": ["safe", "caution", "dangerous"],
                                "description": "Write path: the safety level to assign.",
                            }
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_session_recall",
                    "description": (
                        "Recall previous voice conversations or stored home history "
                        "(also handles what used to be ada_ha_recall). "
                        "Default scope='sessions': use ONLY when the user asks "
                        "'what did we talk about', 'do you remember', or wants something "
                        "from an earlier conversation — not for fact lookup (use "
                        "ada_memory_search bank='all' for that). NOT for test, scenario, "
                        "benchmark, or report results — those are stored documents, "
                        "use ada_memory_search. "
                        "scope='history' answers instantly from the stored Home "
                        "Assistant memory of devices, sensors and recorded events — "
                        "use it for broad questions like 'what sensors are about "
                        "power' or 'pool devices'. "
                        "Pick the group or bank that best matches the topic. "
                        "The sessions recall runs in the background and can take up "
                        "to ~20 seconds; the result will be spoken when ready."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The recall question, e.g. 'what did we discuss in the previous session?'.",
                            },
                            "scope": {
                                "type": "string",
                                "enum": ["sessions", "history"],
                                "description": (
                                    "'sessions' (default) deep-recalls past conversations "
                                    "in the background; 'history' searches the stored "
                                    "Home Assistant device/sensor memory plus recorded "
                                    "events inline (the absorbed ada_ha_recall)."
                                ),
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 50,
                                "description": "scope='history' only — maximum results (default 10).",
                            },
                            "group": {
                                "type": "string",
                                "enum": ["memory", "infra", "apps", "kb", "ops"],
                                "description": (
                                    "Which notebook to search. "
                                    "memory: past voice conversations (default). "
                                    "infra: hosts, services, ports, tailscale, hardware, MCP. "
                                    "apps: Ada/PWA, Home Assistant dashboards, playlive. "
                                    "kb: mddb, yomi, SSOT, documentation, source maps. "
                                    "ops: workflows, tasks, experiments, project state."
                                ),
                            },
                            "bank": {
                                "type": "string",
                                "description": (
                                    "Optional curated memory bank to search instead of a notebook "
                                    "group ({banks}), or 'all' to search every bank. "
                                    "Bank recall checks the fast memory tier first and only "
                                    "escalates to NotebookLM when nothing is found."
                                ),
                            },
                            "force": {
                                "type": "boolean",
                                "description": (
                                    "Override the redundant-recall gate — use ONLY when the user "
                                    "explicitly pushes for a deeper search after a recent "
                                    "confident hit ('dig deeper', 'check again', 'you missed it')."
                                ),
                            },
                        },
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_memory_search",
                    "description": (
                        "Search memory: curated banks, session summaries, and guest "
                        "notes (also handles what used to be guest_recall). "
                        "Stored facts, preferences, people, procedures, notes, and "
                        "test/scenario/benchmark/report results — "
                        "the FIRST tool for any factual lookup including 'which tests ran' "
                        "or 'what did the last report say'; "
                        "pass bank='all' (default) when unsure which bank holds the fact. "
                        "Returns document keys you can pass to "
                        "ada_remember (to correct) or ada_forget (to retract). "
                        "Use this before updating a memory and when the user asks what you "
                        "know about a topic."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "scope": {
                                "type": "string",
                                "enum": ["all", "banks", "sessions", "guest"],
                                "description": (
                                    "Where to search. 'all' (default) fans out over the "
                                    "curated banks plus session summaries (plus guest "
                                    "notes on guest instances); 'banks' restricts to the "
                                    "curated banks named by bank=; 'sessions' searches "
                                    "session/daily/weekly summaries; 'guest' searches "
                                    "public guest memories (the absorbed guest_recall)."
                                ),
                            },
                            "bank": {
                                "type": "string",
                                "description": "Memory bank name ({banks}), or 'all' to search every bank — default when unsure.",
                            },
                            "query": {
                                "type": "string",
                                "description": "What to look for, e.g. 'gate remote' or 'coffee preferences'.",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 20,
                                "description": "Maximum memories to return. Defaults to 5.",
                            },
                            "include_inactive": {
                                "type": "boolean",
                                "description": "Also return superseded, retracted, and expired memories. Default false.",
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "web_search",
                    "description": (
                        "Search the public internet for current facts — news, events, prices, "
                        "forecasts, anything needing up-to-date information outside this home. "
                        "FALLBACK, not first read — when a page or report may already cover the "
                        "topic, check reports-index / ada_memory_search bank='cms' BEFORE this. "
                        "Save-back: after answering from a result, write the finding to CMS "
                        "(cms_edit action='note' on the matching page, or offer cms_publish_page when "
                        "none exists) so repeat questions are answered from your pages, not a "
                        "re-search. "
                        "Returns a grounded answer plus source links. Use this when the user asks "
                        "about news or to 'check online'; answer the question yourself in 2-3 "
                        "lines and cite the source — never cast the search to a screen instead."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The search question, e.g. 'Bangkok flood road closures latest'.",
                            },
                            "provider": {
                                "type": "string",
                                "description": (
                                    "Search backend: 'auto' (default — grounded answer, free "
                                    "fallback on quota), 'gemini' (grounded, billed), or "
                                    "'duckduckgo' (free, no quota — use when asked for it or "
                                    "when grounded quota is exhausted)."
                                ),
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_remember",
                    "description": (
                        "Store or update a memory (also handles what used to be "
                        "vocab_note, guest_remember, guest_remember_private and "
                        "report_habit_observation). For curated memory banks, call it "
                        "when the user says 'remember that…' or states a fact worth "
                        "keeping — bank is required for those kinds. "
                        "Find-then-update: if you pass a key or a subject+attribute pair that "
                        "matches an existing memory, it is corrected in place; otherwise a new "
                        "memory is created. Pass supersedes=<key> to replace a misattributed "
                        "fact with a new one. Some banks require confirmed=true — only set it "
                        "after the user has explicitly confirmed the write. "
                        "kind='vocab' quietly logs a term the speaker misused into their "
                        "own personal glossary (text as 'term → correction', optional note) — "
                        "append-only, no confirmation. kind='guest' saves a public guest "
                        "memory on guest instances (private=true for a promoted user's "
                        "private namespace). kind='habit' reports a camera habit "
                        "observation — only when a backend prompt supplies a challenge_id."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "bank": {
                                "type": "string",
                                "description": "Memory bank name ({writable_banks}). Required for the curated-bank kinds (fact/preference/person/procedure/note).",
                            },
                            "text": {
                                "type": "string",
                                "description": "The fact or note to store, phrased as a standalone sentence. For kind='vocab': 'term → correction'.",
                            },
                            "key": {
                                "type": "string",
                                "description": "Existing document key to correct in place (from ada_memory_search), or the guest-memory slug for kind='guest'. Omit to find-or-create.",
                            },
                            "subject": {
                                "type": "string",
                                "description": "What the memory is about, slug-style, e.g. 'gate-remote'. Used to find existing facts about the same thing.",
                            },
                            "attribute": {
                                "type": "string",
                                "description": "Which aspect of the subject, e.g. 'location'. Combined with subject for update detection.",
                            },
                            "kind": {
                                "type": "string",
                                "enum": ["fact", "preference", "person", "procedure",
                                         "note", "vocab", "guest", "habit"],
                                "description": (
                                    "Memory kind. Curated-bank kinds default to note. "
                                    "vocab: speaker's own glossary append (no bank, no "
                                    "confirmation). guest: chaba guest store (pair with "
                                    "private for a promoted user's private notes). "
                                    "habit: camera-observation verdict — needs the "
                                    "habit_* / challenge_id fields below."
                                ),
                            },
                            "private": {
                                "type": "boolean",
                                "description": "kind='guest' only — save to the promoted user's private namespace instead of the public one.",
                            },
                            "note": {
                                "type": "string",
                                "description": "kind='vocab' only — optional one-line meaning or context appended to the entry.",
                            },
                            "challenge_id": {
                                "type": "string",
                                "description": "kind='habit' only — the challenge id supplied by the habit monitor's prompt.",
                            },
                            "habit_key": {
                                "type": "string",
                                "enum": ["not_drinking_enough_water", "junk_food"],
                                "description": "kind='habit' only — the habit under review.",
                            },
                            "observed": {
                                "type": "boolean",
                                "description": "kind='habit' only — whether the challenged behavior was observed.",
                            },
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                                "description": "kind='habit' only — confidence in the verdict.",
                            },
                            "reason": {
                                "type": "string",
                                "description": "kind='habit' only — short evidence summary.",
                            },
                            "item_identified": {
                                "type": "string",
                                "description": "kind='habit' junk-food only — the specific visible food or drink; empty when none is identifiable.",
                            },
                            "consumption_visible": {
                                "type": "boolean",
                                "description": "kind='habit' junk-food only — true only when actual eating or drinking is visibly confirmed.",
                            },
                            "classified_unhealthy": {
                                "type": "boolean",
                                "description": "kind='habit' junk-food only — true only when the identified item clearly belongs to the configured unhealthy categories.",
                            },
                            "valid_until": {
                                "type": "string",
                                "description": "Optional ISO date after which this memory expires, e.g. '2026-10-01'.",
                            },
                            "supersedes": {
                                "type": "string",
                                "description": "Key of an existing memory this one replaces. The old memory is marked superseded, not deleted.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required for confirmed-policy banks; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                            "prime": {
                                "type": "boolean",
                                "description": (
                                    "Set true only for facts worth injecting into every new "
                                    "session's context, e.g. durable device-name mappings. "
                                    "Use sparingly — primed facts cost tokens every turn."
                                ),
                            },
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_forget",
                    "description": (
                        "Retract a memory when the user says it is no longer true or asks you "
                        "to forget it. The memory is marked retracted and stops surfacing in "
                        "recall, but stays in the archive for audit."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "bank": {
                                "type": "string",
                                "description": "Memory bank name ({writable_banks}).",
                            },
                            "key": {
                                "type": "string",
                                "description": "Document key to retract (from ada_memory_search).",
                            },
                            "reason": {
                                "type": "string",
                                "description": "Optional reason for the retraction.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required for confirmed-policy banks; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["bank", "key"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_outcome",
                    "description": (
                        "Record the real-world outcome of a stored memory or purchase check when the "
                        "user reports how it turned out (e.g. 'that shop was fine', 'the fix worked', "
                        "'I skipped it'). Updates the document's confidence so future recall trusts "
                        "knowledge with a good track record. Get the document key from "
                        "ada_memory_search or a decision-check result first."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "bank": {
                                "type": "string",
                                "description": "Memory bank name ({writable_banks}).",
                            },
                            "key": {
                                "type": "string",
                                "description": "Document key the outcome applies to (from ada_memory_search or a check result).",
                            },
                            "outcome": {
                                "type": "string",
                                "enum": ["good", "bad", "partial", "skipped", "worked", "failed", "bought_good", "bought_bad"],
                                "description": "How it turned out. bought_good/bought_bad for purchase checks, worked/failed for procedures, skipped when it was never exercised.",
                            },
                            "note": {
                                "type": "string",
                                "description": "Optional detail about the outcome.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required for confirmed-policy banks; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["bank", "key", "outcome"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_persona",
                    "description": (
                        "Read or adjust a speaker's stored style preferences "
                        "(how you should talk to them: tone, verbosity, formality, "
                        "language, what to call them, emoji use, proactiveness). "
                        "Use when the user asks you to change how you speak — "
                        "'call me T', 'be more concise', 'answer in Thai', 'be formal'. "
                        "set persists to their personal memory and applies immediately; "
                        "show returns the active settings; reset restores defaults. "
                        "Pass person to view or manage ANOTHER household member's "
                        "profile (needs full access), and use action 'list' to see "
                        "the Home Assistant people Ada knows."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["set", "show", "reset", "list"],
                            },
                            "knob": {
                                "type": "string",
                                "enum": ["tone", "verbosity", "formality", "language",
                                         "address_name", "emoji", "proactiveness"],
                                "description": "Required for set. tone=warm/direct/professional/playful; verbosity=brief/normal/detailed; formality=casual/polite/formal; language=auto/en/th; sassiness=none/light/playful; proactiveness=minimal/normal/proactive.",
                            },
                            "value": {
                                "type": "string",
                                "description": "Required for set — the new value (address_name takes free text; emoji takes true/false).",
                            },
                            "person": {
                                "type": "string",
                                "description": (
                                    "Optional. Target another person's profile instead of the "
                                    "current speaker — a Home Assistant person entity "
                                    "('person.kk') or a person's name ('KK'). Requires a "
                                    "full-access caller; use action 'list' first to see "
                                    "known people."
                                ),
                            },
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_set_voice",
                    "description": (
                        "Change Ada's actual speaking voice (the Gemini Live voice). "
                        "Use when the user asks to change your voice, pick a different "
                        "voice, or asks which voices you can use. 'set' persists the "
                        "choice for future sessions and applies it after a brief "
                        "reconnect — expect a short pause, then continue in the new "
                        "voice. 'show' returns the active voice, 'list' the options. "
                        "For speaking STYLE (tone, verbosity, language) use "
                        "ada_persona instead — this tool changes the voice itself."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["set", "show", "list"],
                            },
                            "voice": {
                                "type": "string",
                                "enum": list(voice_config.GEMINI_VOICES),
                                "description": "Required for set — the Gemini Live voice name.",
                            },
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_enroll_speaker",
                    "description": (
                        "Enroll the current speaker's voice so Ada can recognize them "
                        "by name in future sessions. Uses the audio already buffered "
                        "from their speech — no separate recording needed. Call this "
                        "when the user asks to enroll their voice or when Ada offers "
                        "enrollment and they agree. Pass the speaker's real name — "
                        "never a placeholder like 'Guest'. If their name matches a "
                        "Home Assistant person entity (e.g. name 'KK' -> 'person.kk'), "
                        "pass it as ha_person so their identity and memory banks follow."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Speaker name (e.g. 'Tony', 'KK').",
                            },
                            "ha_person": {
                                "type": "string",
                                "description": "Home Assistant person entity for this speaker (e.g. 'person.tony').",
                            },
                            "display_name": {
                                "type": "string",
                                "description": "Display name for personalization (defaults to name).",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Owner-confirmed force-replace for a stale/poisoned voice profile — set true only after the speaker explicitly confirms.",
                            },
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_decision_check",
                    "description": (
                        "Verify a product or purchase before the user buys — checks it "
                        "against the user's stored purchase criteria and the live web "
                        "for price sanity, red flags, spec accuracy, and alternatives. "
                        "Use when the user asks 'should I buy this', 'check this "
                        "listing', shares a Shopee/Lazada link, or wants a second "
                        "opinion on a purchase. Put the product name and listing "
                        "details (price, shop, ratings, specs) into `product`; a bare "
                        "URL in `url` helps but alone may not give enough detail. "
                        "Runs in the background (~30-60 seconds); the verdict is "
                        "spoken when ready and saved to the purchase memory bank."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "product": {
                                "type": "string",
                                "description": "Product description or pasted listing details — name, price, shop, ratings, specs.",
                            },
                            "url": {
                                "type": "string",
                                "description": "Listing URL if the user shared one.",
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["quick", "deep"],
                                "description": "quick = fast red-flag/price check (default). deep = thorough spec verification plus alternatives search — only when the user asks for a thorough check.",
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_deep_research",
                    "description": (
                        "Deep multi-round research on a topic — use this whenever the user asks "
                        "to 'deep research', 'deep dive', 'look into', or 'write a report on' a "
                        "subject (not for a single quick fact — that is web_search). Check "
                        "reports-index first — if a page already covers the topic, read it and "
                        "research only what is missing or stale. Fans out "
                        "planned searches (overview/history, latest status, competitors/critics, "
                        "plus timeline and outlook on depth='deep'), gathers sources, then reports "
                        "back: summarize key findings to the user and offer to save a full report "
                        "page via cms_publish_page. Runs in the background (1-3 minutes); the "
                        "findings are spoken when ready."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "topic": {
                                "type": "string",
                                "description": "The research subject as the user stated it.",
                            },
                            "depth": {
                                "type": "string",
                                "enum": ["standard", "deep"],
                                "description": "standard = 3 search rounds (default). deep = 5 rounds — only when the user explicitly asks for a thorough report.",
                            },
                        },
                        "required": ["topic"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "calendar_read",
                    "description": (
                        "Calendar reads — also handles what used to be "
                        "calendar_list_events, calendar_list_calendars, and "
                        "calendar_freebusy. action='events' lists calendar events "
                        "for a day or range across all configured providers, "
                        "sorted by start time — use this FIRST whenever the user "
                        "asks about their schedule, what's next, or whether "
                        "they're free; 'calendars' lists every calendar with "
                        "provider, id, and which provider receives new events by "
                        "default; 'freebusy' returns busy time slots for 'am I "
                        "free between X and Y' or finding an open slot."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["events", "calendars", "freebusy"],
                                "description": "Read operation.",
                            },
                            "day": {
                                "type": "string",
                                "description": "events/freebusy: day to read — 'today', 'tomorrow', or ISO date 'YYYY-MM-DD'. Default 'today'.",
                            },
                            "days": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 31,
                                "description": "events/freebusy: number of days to span starting at day. Default 1; use 7 for 'this week'.",
                            },
                            "query": {
                                "type": "string",
                                "description": "events: optional text filter on event titles, e.g. 'dentist'.",
                            },
                            "calendar": {
                                "type": "string",
                                "description": "events: optional 'provider:calendar_id' to read one calendar only (from action='calendars').",
                            },
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "plan_day",
                    "description": (
                        "One 'how does the period look' view — also handles what "
                        "used to be ada_daily_summary and ada_weekly_comparison. "
                        "period='today'/'tomorrow' returns that day's merged "
                        "calendar events plus all open tasks and the day's "
                        "session digest (a recap of what was discussed); "
                        "period='week' compares the last days of daily digests "
                        "into a weekly trend view — use it when the user asks how "
                        "the week went or to compare days. day= overrides the "
                        "target ('yesterday' or a YYYY-MM-DD date — also the way "
                        "to recap a specific past day). Read-only; speak the "
                        "proposed plan but never create events without approval. "
                        "When the user wants the weekly comparison kept, offer to "
                        "publish it to the miniapp with cms_publish_page "
                        "(confirmed=true)."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "period": {
                                "type": "string",
                                "enum": ["today", "tomorrow", "week"],
                                "description": "Which window: a day's merged plan ('today' default, 'tomorrow') or the weekly digest ('week').",
                            },
                            "day": {
                                "type": "string",
                                "description": "Optional day override — 'yesterday' or 'YYYY-MM-DD'. For a day period it's the day viewed; for 'week' the window's last day.",
                            },
                            "days": {
                                "type": "integer",
                                "minimum": 2,
                                "maximum": 14,
                                "description": "week: days in the comparison window (default 7).",
                            },
                            "refresh": {
                                "type": "boolean",
                                "description": "Regenerate the day's digest / weekly comparison instead of returning the stored one.",
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "calendar_write",
                    "description": (
                        "Calendar writes — also handles what used to be "
                        "calendar_create_event, calendar_delete_event, and "
                        "calendar_shift_overdue. action='create' makes an event "
                        "on the default write provider (or a named "
                        "'provider:calendar_id'); 'delete' removes an event by "
                        "its provider-qualified id (e.g. 'google:primary/abc123', "
                        "from calendar_read action='events'); 'shift' moves every "
                        "overdue item to a new day in one call — open tasks whose "
                        "due date is in the past plus calendar events that "
                        "already ended (to='tomorrow' default). Always restate "
                        "the details to the user, get an explicit yes, then call "
                        "with confirmed=true. For 'shift', restate the target "
                        "date and report the moved list back."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["create", "delete", "shift"],
                                "description": "Write operation.",
                            },
                            "title": {
                                "type": "string",
                                "description": "create: event title as the user phrased it.",
                            },
                            "start": {
                                "type": "string",
                                "description": "create: start as ISO 8601 datetime ('2026-09-22T14:00:00') or date ('2026-09-22') for all-day.",
                            },
                            "end": {
                                "type": "string",
                                "description": "create: end as ISO 8601 datetime or date (exclusive for all-day).",
                            },
                            "notes": {
                                "type": "string",
                                "description": "create: optional event description/notes.",
                            },
                            "location": {
                                "type": "string",
                                "description": "create: optional location string.",
                            },
                            "calendar": {
                                "type": "string",
                                "description": "create: optional 'provider:calendar_id' to write a non-default calendar.",
                            },
                            "event_id": {
                                "type": "string",
                                "description": "delete: provider-qualified event id exactly as returned by calendar_read action='events'.",
                            },
                            "to": {
                                "type": "string",
                                "description": "shift: target day — 'tomorrow' (default) or 'YYYY-MM-DD'.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "tasks_list",
                    "description": (
                        "Lists open (not completed) tasks across task-capable providers. "
                        "Use this when the user asks what's on their todo list."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "task_list": {
                                "type": "string",
                                "description": "Optional 'provider:list_id' to read one list only.",
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "tasks_add",
                    "description": (
                        "Adds a task to the default write provider's task list. "
                        "Restate the task and due date, get an explicit yes, then pass confirmed=true."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Task title as the user phrased it.",
                            },
                            "due": {
                                "type": "string",
                                "description": "Optional due date: 'today', 'tomorrow', or 'YYYY-MM-DD'.",
                            },
                            "notes": {
                                "type": "string",
                                "description": "Optional task notes.",
                            },
                            "task_list": {
                                "type": "string",
                                "description": "Optional 'provider:list_id' for a non-default list.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["title"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "tasks_complete",
                    "description": (
                        "Marks a task complete by its provider-qualified id "
                        "(e.g. 'google:@default/abc123', from tasks_list). "
                        "Confirm which task first, then pass confirmed=true."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Provider-qualified task id exactly as returned by tasks_list.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["task_id"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "tasks_move",
                    "description": (
                        "Reschedule a task's due date by its provider-qualified id "
                        "(from tasks_list) — same task, new date. Confirm which task "
                        "first, then pass confirmed=true."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Provider-qualified task id exactly as returned by tasks_list.",
                            },
                            "due": {
                                "type": "string",
                                "description": "New due date: 'today', 'tomorrow', or 'YYYY-MM-DD'.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["task_id", "due"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "cms_read",
                    "description": (
                        "Read the user's miniapp pages — also handles what used to be "
                        "cms_list_pages, cms_get_page, and cms_verify_page. "
                        "action='list' returns every page's slug, title, format, and "
                        "last-updated timestamp; 'get' reads one page's full content by "
                        "key (its page slug) and lang — use before updating a page; "
                        "'verify' re-reads a page and checks the content parses for its "
                        "declared format, returning a structural summary (title, "
                        "sections/items, headings, slide count) — call it after "
                        "publishing or updating, since you cannot see the rendered site."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["get", "list", "verify"],
                                "description": "Read operation.",
                            },
                            "key": {
                                "type": "string",
                                "description": "Page slug, e.g. 'pool-notes' (required for get/verify; get the real slug from action='list' or reports-index).",
                            },
                            "lang": {
                                "type": "string",
                                "enum": ["en", "th"],
                                "description": "get: page language variant (default en; falls back to en when missing).",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "list: max pages to return (default 50).",
                            },
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "cms_publish_page",
                    "description": (
                        "Create or fully replace a page in the user's miniapp. The slug "
                        "is the page's URL-friendly id; publishing an existing slug "
                        "overwrites it. CALL THIS FIRST to register the pending request, "
                        "then tell the user the slug + title and ask for an explicit yes; "
                        "after they say yes, call again with confirmed=true. Do not ask "
                        "verbally without calling — the ask-step only exists once the "
                        "request is registered."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "slug": {
                                "type": "string",
                                "description": "Lowercase page id, 1-64 chars of a-z, 0-9, '-' or '_', e.g. 'pool-notes'.",
                            },
                            "title": {
                                "type": "string",
                                "description": "Human-readable page title shown in the miniapp navigation.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Full page content in the given format (markdown by default).",
                            },
                            "format": {
                                "type": "string",
                                "enum": ["markdown", "html", "yaml", "slides"],
                                "description": "Content format. 'slides' is markdown with '---' between slides.",
                            },
                            "lang": {
                                "type": "string",
                                "enum": ["en", "th"],
                                "description": "Language variant to publish (default en). 'en' and 'th' variants of the same slug coexist — the viewer has a language toggle.",
                            },
                            "summary": {
                                "type": "string",
                                "description": "One-line brief for reports-index — the gist Ada can answer from without re-reading the page (max ~240 chars).",
                            },
                            "domain": {
                                "type": "string",
                                "description": "Grouping tag for reports-index, e.g. 'flood', 'health', 'bench', 'news'.",
                            },
                            "fresh_for": {
                                "type": "string",
                                "description": "Staleness hint, e.g. '1h', '6h', '1d' — reports-index flags the page as STALE past this window.",
                            },
                            "links": {
                                "type": "string",
                                "description": "Comma-separated slugs this report links to or derives from (parents, children, sources). Stored as meta so Ada can follow the chain.",
                            },
                            "supersedes": {
                                "type": "string",
                                "description": "Slug of the report this one replaces — the older one reads as superseded.",
                            },
                            "confidence": {
                                "type": "string",
                                "description": "Trust level, e.g. 'high', 'low', 'unverified' — shown in the report so Ada knows how much weight to give it.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["slug", "title", "content"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "cms_edit",
                    "description": (
                        "Edit-side miniapp ops — also handles what used to be "
                        "cms_note_update, cms_delete_page, and cms_automation. "
                        "action='note' appends a one-line timeline note to an existing "
                        "page — merges into the page's Timeline section, refreshes "
                        "updated, and re-summarizes; never replaces content and needs "
                        "no confirmation. Use it after tool calls that return newer "
                        "info for a report (flood status, benchmarks). 'delete' removes "
                        "a page — restate which page will be removed, get an explicit "
                        "yes, then call with confirmed=true. 'automate' inspects and "
                        "adjusts a generated page's automation registry (the switches "
                        "the scheduled news worker honors) — op='list'/'get' are free "
                        "reads; op='set'/'enable'/'disable'/'run' change knobs "
                        "(interval_min, max_items, since_hours, feeds, require "
                        "relevance regex, langs, parent/children) or queue a one-shot "
                        "regeneration — writes need confirmed=true after restating "
                        "what will change."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["note", "delete", "automate"],
                                "description": "Edit operation.",
                            },
                            "slug": {
                                "type": "string",
                                "description": "Page slug the op targets — get the real slug from cms_read action='list' or reports-index first; do not guess it.",
                            },
                            "note": {
                                "type": "string",
                                "description": "note: one-line note appended to the page Timeline and index entry (max ~200 chars).",
                            },
                            "summary": {
                                "type": "string",
                                "description": "note: optional replacement for the page's one-line reports-index brief.",
                            },
                            "lang": {
                                "type": "string",
                                "enum": ["en", "th"],
                                "description": "note: language variant to annotate (default en).",
                            },
                            "op": {
                                "type": "string",
                                "enum": ["list", "get", "set", "enable", "disable", "run"],
                                "description": "automate: registry operation (list/get free reads; set/enable/disable/run confirmed writes).",
                            },
                            "enabled": {
                                "type": "boolean",
                                "description": "automate set: turn the page's automation on/off.",
                            },
                            "interval_min": {
                                "type": "integer",
                                "description": "automate set: minutes between automatic runs (0 = every run, max 10080).",
                            },
                            "run_now": {
                                "type": "boolean",
                                "description": "automate set: queue (true) or cancel (false) a one-shot regeneration.",
                            },
                            "max_items": {
                                "type": "integer",
                                "description": "automate set: max news items per update (1-50).",
                            },
                            "since_hours": {
                                "type": "integer",
                                "description": "automate set: only include items published within N hours (1-720).",
                            },
                            "require": {
                                "type": "string",
                                "description": "automate set: relevance regex matched against item title+summary; empty string clears it.",
                            },
                            "feeds": {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 2,
                                    "maxItems": 2,
                                },
                                "description": "automate set: RSS feeds as [[name, url], ...] pairs.",
                            },
                            "langs": {
                                "type": "array",
                                "items": {"type": "string", "enum": ["en", "th"]},
                                "description": "automate set: which language variants to update.",
                            },
                            "parent": {
                                "type": "string",
                                "description": "automate set: parent report slug this page rolls up into (empty clears).",
                            },
                            "children": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "automate set: child report slugs this page aggregates.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required for delete and automate writes; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_resolve_action",
                    "description": (
                        "Marks a pending action proposal as applied or dismissed. Call with the "
                        "proposal key shown in the pending-suggestions list after the user "
                        "accepts or declines the suggestion."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": "Proposal key, e.g. 'action-2026-09-22-abc123-0'.",
                            },
                            "resolution": {
                                "type": "string",
                                "enum": ["applied", "dismissed"],
                            },
                        },
                        "required": ["key", "resolution"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "devin_dispatch",
                    "description": (
                        "Starts an unattended Devin coding session on tony-dell in a dedicated "
                        "git worktree. The session runs to completion by itself; the user is "
                        "notified on their phone when it finishes. Use when the user asks to "
                        "have a code task done later or autonomously. Requires confirmed=true "
                        "after restating the repo and task."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "repo": {
                                "type": "string",
                                "enum": ["chaba", "ada-pi", "sunsynk-card"],
                                "description": (
                                    "Repository the session works in. 'chaba' = web apps under "
                                    "/apps/* (incl. the vcast virtual-display receiver page), the "
                                    "input-bridge relay, Caddy stack, HA dashboard cards, SSOT docs. "
                                    "'ada-pi' = Ada's own backend tools, pwa_server, auth, scenarios. "
                                    "'sunsynk-card' = the sunsynk power-flow card project. Pick the "
                                    "repo where the code to change lives, not the service it affects."
                                ),
                            },
                            "task": {
                                "type": "string",
                                "description": "The task prompt for the Devin session.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["repo", "task"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "devin_status",
                    "description": (
                        "Lists dispatched Devin tasks and their state, or shows one task's "
                        "unit state and latest transcript info when task_id is given."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Optional task id, e.g. '20260922-194454-...'. Omit to list all.",
                            },
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "devin_followup",
                    "description": (
                        "Sends a follow-up message into a dispatched Devin session — either "
                        "steering a running one or resuming a finished one with more work. "
                        "Requires confirmed=true after restating what the message asks."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Task id returned by devin_dispatch or devin_status.",
                            },
                            "message": {
                                "type": "string",
                                "description": "The follow-up instruction to send.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["task_id", "message"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "devin_job_report",
                    "description": (
                        "Composes the Devin job report from the job ledger and the live "
                        "dispatch status: active jobs, a dedicated 'Failed jobs' section "
                        "(spawn failures, dead units, no transcript), done, and stale. "
                        "Returns ready-to-publish markdown; pass publish=true with "
                        "confirmed=true to write the 'devin-job-report' CMS page directly."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "publish": {
                                "type": "boolean",
                                "description": "Write the composed report to the 'devin-job-report' CMS page.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required with publish=true; set only after explicit user confirmation.",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max job-ledger docs to include (default 60).",
                            },
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "devin_pending",
                    "description": (
                        "Lists dispatched jobs that are blocked waiting for the user's "
                        "answer (needs-input), with each job's question and short detail. "
                        "Use when the user asks what needs their attention, says a job is "
                        "waiting for them, or a needs-input notification arrived."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "devin_jobs",
                    "description": (
                        "Dispatch ledger summary: ALL job docs with their real status "
                        "(running/done/failed/awaiting-user), host, timestamp, and "
                        "pending question. Use for 'summarize my dispatched tasks', "
                        "'did job X fail', or any status-of-dispatches question — "
                        "devin_pending only shows awaiting-user, devin_status only "
                        "shows unit liveness."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": ["running", "done", "failed", "awaiting-user"],
                                "description": "Optional filter; omit for all.",
                            },
                            "limit": {"type": "integer", "default": 30},
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "devin_answer",
                    "description": (
                        "Delivers the user's answer to a blocked dispatched job. For "
                        "Devin sessions it resumes the session with the message directly; "
                        "for other dispatched jobs it records the answer for the "
                        "dispatcher. First refine the user's reply into a self-contained "
                        "instruction, read it back, then call with confirmed=true only "
                        "after an explicit yes."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Task id from devin_pending or devin_status.",
                            },
                            "message": {
                                "type": "string",
                                "description": "The refined, self-contained answer/instruction.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                        },
                        "required": ["task_id", "message"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_doc_search",
                    "description": (
                        "Searches the personal document archive (scans of deeds, ID cards, "
                        "passports, house registrations, receipts — เอกสาร) indexed in the "
                        "documents memory bank. Use for ANY question about stored/scanned "
                        "documents — never search home devices for documents. Returns slugs "
                        "to pass to ada_doc_get / ada_doc_print."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "What to find, e.g. 'A-68 deed', 'passport', 'ทะเบียนบ้าน'.",
                            },
                            "limit": {
                                "type": "number",
                                "description": "Max results (default 5).",
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_doc_get",
                    "description": (
                        "Returns manifest + index metadata for one archived document set "
                        "by slug (from ada_doc_search): page names, drive path, hashes, "
                        "archive timestamp."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "slug": {
                                "type": "string",
                                "description": "Archive slug, e.g. 'A-68'.",
                            },
                        },
                        "required": ["slug"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_doc_archive",
                    "description": (
                        "Archives a document set to gdrive:ada-documents and indexes it in "
                        "the documents bank: from a just-uploaded intake result "
                        "(intake_key, preferred — the upload panel returns it) or a "
                        "directory of images on the Ada host (source_dir). Deduplicates "
                        "against existing archives. Requires confirmed=true after "
                        "restating the slug and contents."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "slug": {
                                "type": "string",
                                "description": "Archive slug, e.g. 'A-68' or 'visa-2026'.",
                            },
                            "doc_type": {
                                "type": "string",
                                "description": "Document type: deed, id_card, passport, contract, receipt, form, document.",
                            },
                            "intake_key": {
                                "type": "string",
                                "description": "Held intake key from /api/documents/intake (doc/...).",
                            },
                            "intake_keys": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Multiple held intake keys — one set spanning several uploads/pages.",
                            },
                            "source_dir": {
                                "type": "string",
                                "description": "Directory of page images on the Ada host.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["slug"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_doc_print",
                    "description": (
                        "Prints pages of an archived document set on the HP DeskJet via "
                        "tony-dell CUPS — renders each page onto A4 at 300dpi with the "
                        "print-enhance pipeline. Requires confirmed=true after restating "
                        "which pages will be printed."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "slug": {
                                "type": "string",
                                "description": "Archive slug, e.g. 'A-68'.",
                            },
                            "pages": {
                                "type": "string",
                                "description": "'all' (default), '1-3', or '2,4' — 1-based.",
                            },
                            "true_size_mm": {
                                "type": "string",
                                "description": "Print at real physical size, e.g. '85.6x54' for an ID-1 card. Omit for fit-to-A4.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["slug"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "drive_search",
                    "description": (
                        "Search Google Drive by file name or content — the operator's "
                        "whole Drive, not just the document archive (for archived deed/ID "
                        "sets use ada_doc_search instead). Narrow with mime: 'image/', "
                        "'video/', 'application/pdf', 'text/'. Returns file ids for "
                        "drive_get / drive_show / drive_update."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Name or content to find, e.g. 'condo photos', 'รูปบ้าน', 'budget 2026'.",
                            },
                            "mime": {
                                "type": "string",
                                "description": "Optional mimeType filter: 'image/', 'video/', 'application/pdf', 'text/'.",
                            },
                            "limit": {
                                "type": "number",
                                "description": "Max results (default 10).",
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "drive_get",
                    "description": (
                        "Read one Drive file by id (from drive_search). Text files and "
                        "Google docs come back as inline text; images/video/binary return "
                        "metadata plus a castable media_url for drive_show."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "file_id": {
                                "type": "string",
                                "description": "Drive file id from drive_search.",
                            },
                        },
                        "required": ["file_id"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "drive_update",
                    "description": (
                        "Replace a Drive file's content in place — text, markdown, json, "
                        "csv and other regular files. Google-native docs/sheets/slides "
                        "can't be media-updated. Requires confirmed=true after restating "
                        "the file and the change."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "file_id": {
                                "type": "string",
                                "description": "Drive file id from drive_search.",
                            },
                            "content": {
                                "type": "string",
                                "description": "The complete new file content (replaces, not appends).",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                            "confirm_token": {
                                "type": "string",
                                "description": "Bound confirmation token returned by a denied call; after the user confirms, replay the same call with it. Single use, expires in 120s.",
                            },
                        },
                        "required": ["file_id", "content"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "drive_show",
                    "description": (
                        "Show a Drive file on a display — photos, videos, pdfs, any file "
                        "from drive_search. Casts to the speaker's vcast screen by "
                        "default; target='tv' for the living-room TV. Picks the right "
                        "action from the file type."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "file_id": {
                                "type": "string",
                                "description": "Drive file id from drive_search.",
                            },
                            "screen": {
                                "type": "number",
                                "description": "Vcast screen number (default 1).",
                            },
                            "target": {
                                "type": "string",
                                "enum": ["screen", "tv"],
                                "description": "'screen' (vcast display, default) or 'tv' (living-room TV).",
                            },
                        },
                        "required": ["file_id"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "photos_pick",
                    "description": (
                        "Browse Google Photos: starts a picker session and returns a "
                        "picker_uri — the ONLY way to reach library photos since Google "
                        "limited the Photos API to app-created data. Give the user the "
                        "picker_uri to open on their signed-in phone or laptop, then call "
                        "photos_picked with the session_id when they've chosen."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "photos_picked",
                    "description": (
                        "Poll a photos_pick session. When the user has picked items, "
                        "returns them and casts the first one to their screen (images "
                        "as image, videos as play). Set show=false to only list."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "string",
                                "description": "Session id from photos_pick.",
                            },
                            "screen": {
                                "type": "number",
                                "description": "Vcast screen number (default 1).",
                            },
                            "show": {
                                "type": "boolean",
                                "description": "Cast the first picked item to the screen (default true).",
                            },
                        },
                        "required": ["session_id"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_usage_summary",
                    "description": (
                        "Returns cumulative Gemini token usage for this Ada process: "
                        "input/output totals, per-modality breakdown (audio/text/image), "
                        "per-source totals (live session, posture, clutter), and a rough "
                        "USD cost estimate. Use when the user asks about token usage, API "
                        "usage, or what Ada costs. Pass source to filter to one source; "
                        "pass reset=true only when the user asks to zero the counters."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "enum": ["all", "live", "posture", "clutter"],
                                "description": "Which usage source to report; 'all' aggregates everything.",
                            },
                            "reset": {
                                "type": "boolean",
                                "description": "Zero the counters after reporting; only when the user asks.",
                            },
                        },
                        "additionalProperties": False,
                    },
                }]
            }],
        }
        if os.environ.get("DISABLE_GET_HOME_STATE") == "true":
            config["tools"][0]["function_declarations"] = [
                fd for fd in config["tools"][0]["function_declarations"]
                if fd.get("name") != "get_home_state"
            ]
            config["system_instruction"] = (
                config["system_instruction"]
                .replace(
                    "get_home_state to check occupancy and the state of the configured home plugs "
                    "(or a single entity with entity_id=, one HA domain with domain=, or the "
                    "stored memory snapshot with domain='memory'), ",
                    "",
                )
                .replace(
                    "When the user asks about devices, occupancy, or what is on/off, call get_home_state or home_search first. ",
                    "When the user asks about devices, occupancy, or what is on/off, call home_search first. ",
                )
            )
        excluded = {s.strip() for s in os.environ.get("ADA_EXCLUDED_TOOLS", "").split(",") if s.strip()}
        if excluded:
            config["tools"][0]["function_declarations"] = [
                fd for fd in config["tools"][0]["function_declarations"]
                if fd.get("name") not in excluded
            ]
            # Keep instructions consistent: when every calendar tool is
            # excluded, drop the paragraph describing them too.
            if CALENDAR_TOOLS <= excluded:
                config["system_instruction"] = config["system_instruction"].replace(
                    CALENDAR_INSTRUCTIONS, ""
                )
            if CMS_TOOLS <= excluded:
                config["system_instruction"] = config["system_instruction"].replace(
                    CMS_INSTRUCTIONS, ""
                )
            if DEVIN_TOOLS <= excluded:
                config["system_instruction"] = config["system_instruction"].replace(
                    DEVIN_INSTRUCTIONS, ""
                )
            if DOC_TOOLS <= excluded:
                config["system_instruction"] = config["system_instruction"].replace(
                    DOC_INSTRUCTIONS, ""
                )
            if DRIVE_TOOLS <= excluded:
                config["system_instruction"] = config["system_instruction"].replace(
                    DRIVE_INSTRUCTIONS, ""
                )
            if HABIT_TOOLS <= excluded:
                config["system_instruction"] = config["system_instruction"].replace(
                    HABIT_INSTRUCTIONS, ""
                )
        # The merged camera tool is declared on every instance — its
        # source='traffic' path needs no VMS shim (tools-merge-camera).
        # The vms source just answers honestly when the shim is absent.
        if not (VMS_TOOLS <= excluded):
            config["tools"][0]["function_declarations"].append(dict(CAMERA_DECLARATION))
        # Drop-in tools (backend/tools.d) — manifest-declared, excluded-aware.
        try:
            config["tools"][0]["function_declarations"].extend(
                tools_loader.registry().declarations(excluded))
        except Exception as exc:
            logger.warning("tools.d declarations failed: %s", exc)
        if chaba_memory.enabled():
            # Guest mode: allowlist the tool surface, append chaba guest tools,
            # and inject the rendered guest context instead of MDDB priming.
            # CHABA_DECLARATIONS re-declares the canonical memory names
            # guest-scoped — drop the bank-facing versions so no function
            # name is declared twice.
            chaba_declared = {d["name"] for d in CHABA_DECLARATIONS}
            config["tools"][0]["function_declarations"] = [
                fd for fd in config["tools"][0]["function_declarations"]
                if fd.get("name") in CHABA_ALLOW
                and fd.get("name") not in chaba_declared
            ]
            config["tools"][0]["function_declarations"].extend(CHABA_DECLARATIONS)
            config["system_instruction"] += CHABA_INSTRUCTIONS
            try:
                ctx = chaba_memory.get_store().system_context(
                    getattr(self, "session_id", None)
                )
                if ctx:
                    config["system_instruction"] += "\n\n" + ctx
            except Exception as exc:
                logger.warning("chaba context load failed: %s", exc)
        else:
            config["system_instruction"] += await self._session_context_tail(excluded)
            # Per-instance memory banks: descriptions name only banks this
            # instance can actually use ({banks}=readable, {writable_banks}=
            # writable), so the model never calls a bank that fails loudly.
            all_banks, writable_banks = self._memory_bank_names()
            config["tools"][0]["function_declarations"] = _fill_bank_placeholders(
                config["tools"][0]["function_declarations"], all_banks, writable_banks
            )
        # NOTE: live-session google_search grounding is NOT enabled — the
        # API key's plan rejects it with a 1011 quota error at connect,
        # taking down every session. Web search goes through the explicit
        # web_search tool (generate-API grounding) instead.
        # Build the transcript leak filter from the declared tool names —
        # the model occasionally SPEAKS 'set_facial_expression{...}' style
        # text instead of emitting a real function call, and the output
        # transcription relays it verbatim into transcripts/UI.
        try:
            names = [
                fd.get("name", "")
                for fd in config["tools"][0]["function_declarations"]
            ]
            names = [re.escape(n) for n in names if n]
            if names:
                self._tool_leak_re = re.compile(
                    r"\b(?:" + "|".join(names) + r")\s*\{"
                    # echo of the tool-result wrapper — observed
                    # 2026-09-30: '<code_output>Tool cctv_snapshot
                    # returned: {...}' spoken aloud mid-turn
                    r"|<code_output>|\bTool\s+\w+\s+returned\s*:",
                )
            else:
                self._tool_leak_re = None
        except Exception:
            self._tool_leak_re = None
        self._session_context = self._client.aio.live.connect(
            model=self.model,
            config=config,
        )
        self._session = await self._session_context.__aenter__()
        logger.info(
            "session=%s Gemini Live session connected (model=%s, voice=%s, video=%s)",
            self.session_id, self.model,
            self.voice,
            self.video_resolution,
        )

    async def send_audio(self, pcm16: bytes) -> None:
        if self._session is None:
            raise RuntimeError("provider is not connected")
        async with self._send_lock:
            await self._session.send_realtime_input(
                audio=types.Blob(data=pcm16, mime_type="audio/pcm;rate=16000")
            )

    async def send_video(self, jpeg: bytes) -> None:
        if self._session is None:
            raise RuntimeError("provider is not connected")
        async with self._send_lock:
            await self._session.send_realtime_input(
                video=types.Blob(data=jpeg, mime_type="image/jpeg")
            )

    def is_stalled(self) -> bool:
        """True when the user has spoken but the provider has been silent
        past ADA_STALL_TIMEOUT_S — a dead Gemini Live stream that never
        ends cleanly and must be force-reconnected."""
        if self._closed or not self._last_user_at or self._tools_in_flight:
            return False
        return (
            self._last_user_at > self._last_model_at
            and time.monotonic() - self._last_user_at > self._stall_timeout
        )

    async def send_text_turn(self, text: str) -> None:
        if self._session is None:
            raise RuntimeError("provider is not connected")
        self._last_user_at = time.monotonic()
        # A real turn is now open through turn_complete — text turns produce
        # no input_transcription, so this is the only signal covering the
        # tool-call/generation gap where context sends get dropped.
        self._turn_open = True
        async with self._send_lock:
            await self._session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=text)],
                ),
                turn_complete=True,
            )

    async def send_image_turn(self, image: bytes, mime: str,
                              caption: str = "") -> None:
        """A user-sent image turn (chat relays: LINE/Telegram photo in).
        Same shape as the camera-frame injection — text + image part,
        turn_complete so the model answers with a description."""
        if self._session is None:
            raise RuntimeError("provider is not connected")
        self._last_user_at = time.monotonic()
        self._turn_open = True
        parts = [types.Part.from_text(
            text=caption or "The user sent you this image — describe "
                             "it and respond naturally.")]
        parts.append(types.Part.from_bytes(
            data=image, mime_type=mime or "image/jpeg"))
        async with self._send_lock:
            await self._session.send_client_content(
                turns=types.Content(role="user", parts=parts),
                turn_complete=True,
            )

    async def notify_or_defer(self, text: str, urgent: bool = False) -> str:
        """Notification handling (Eisenhower split):

        urgent=True   — interrupt now: injected as a user turn so Ada stops
                        and announces it immediately (urgent + important).
        urgent=False  — the client already announced it via machine voice
                        (speechSynthesis); we inject a silent context note
                        (turn_complete=False) so Ada knows it arrived and
                        can circle back, but she does NOT stop her current
                        task or speak about it now. Queued to the next turn
                        boundary if a response is in flight.

        Returns 'interrupted' | 'context' | 'queued'."""
        if urgent:
            await self.send_text_turn(
                "(system) URGENT notification — stop and tell the user now: "
                + text)
            return "interrupted"
        framed = (
            "(system) Note for later — a notification arrived and the user "
            f"was already informed by a short machine-voice announcement: "
            f"{text}\nDo not speak about this now. Finish your current "
            "task; bring it up only when the current work is done or the "
            "user asks.")
        # Always queue: a mid-turn context send can be dropped by the API
        # even before audio starts (tool-call phase). If no turn is open,
        # a short timer flushes; otherwise the next turn boundary drains.
        self._pending_notifications.append(framed)
        if not self._flush_scheduled:
            self._flush_scheduled = True
            try:
                asyncio.get_running_loop().create_task(self._flush_soon())
            except RuntimeError:
                self._flush_scheduled = False
        return "queued"

    async def _flush_soon(self) -> None:
        """Idle-path drain: if no turn is open ~2s after a note queued,
        deliver it as context now — no turn boundary is coming. If a turn
        is open, leave it for the boundary drain (or a later flush)."""
        try:
            for _ in range(15):  # bounded — ~30s max wait for idle
                await asyncio.sleep(2)
                if not self._pending_notifications:
                    return
                if not self._response_active and not self._turn_open:
                    await self._drain_notifications()
                    return
        finally:
            self._flush_scheduled = False

    async def _send_context_note(self, text: str) -> None:
        """Append context WITHOUT triggering a response (turn_complete=False)
        — the note lands in session state for later reference only."""
        async with self._send_lock:
            await self._session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=text)],
                ),
                turn_complete=False,
            )

    async def _drain_notifications(self) -> None:
        while self._pending_notifications:
            framed = self._pending_notifications.pop(0)
            try:
                await self._send_context_note(framed)
            except Exception as exc:
                logger.warning(
                    "session=%s deferred notify send failed: %s",
                    self.session_id, exc)
                return

    async def send_habit_alert(self, jpeg: bytes, alert: str) -> None:
        if self._session is None:
            raise RuntimeError("provider is not connected")
        async with self._send_lock:
            await self._session.send_client_content(
                turns=types.Content(role="user", parts=[
                    types.Part.from_text(text=alert),
                    types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"),
                ]),
                turn_complete=True,
            )

    async def events(self) -> AsyncIterator[ProviderEvent]:
        if self._session is None:
            raise RuntimeError("provider is not connected")

        self._response_active = False
        input_transcript = ""
        assistant_turn_text = ""
        response_started_at = 0.0
        response_audio_chunks = 0
        response_audio_bytes = 0
        tool_calls_this_turn = 0
        tool_budget = int(os.environ.get("ADA_TOOL_CALL_BUDGET", "20"))
        actuations_this_turn = 0
        actuation_budget = int(os.environ.get("ADA_ACTUATION_BUDGET", "6"))
        budget_hit = False
        barge_pending = False  # a barge-in's transcript arrives next turn_complete
        input_done_ts = 0.0  # last input_transcription chunk ≈ end of user speech
        n_tools_this_turn = 0
        budget_nudged = False

        while not self._closed:
            async for message in self._session.receive():
                self._last_model_at = time.monotonic()
                usage = message.usage_metadata
                if usage:
                    self._record_usage(usage)
                update = message.session_resumption_update
                if update and update.resumable and update.new_handle:
                    self.resumption_handle = update.new_handle
                if message.go_away:
                    self.go_away_time_left = str(message.go_away.time_left or "")
                    yield ProviderEvent("go_away", {"time_left": self.go_away_time_left})
                    return
                tool_call = message.tool_call
                if tool_call and tool_call.function_calls:
                    function_responses = []
                    camera_frames: list[tuple[str, bytes, str]] = []
                    for call in tool_call.function_calls:
                        logger.info(
                            "session=%s function_call received id=%s name=%s args=%r",
                            self.session_id, call.id, call.name, call.args,
                        )
                        # Consolidated tool surface: absorbed names
                        # (tool_runner._ALIASES) resolve to their canonical
                        # tool BEFORE dispatch — provider-dispatched tools
                        # share the runner's alias contract so stale
                        # phrasing still lands (tools-merge-camera).
                        # The FunctionResponse below must echo the AS-CALLED
                        # name, not the canonical — Gemini correlates
                        # responses to calls by name+id.
                        _call_name = str(call.name or "")
                        _rname, _rimplied = _resolve_alias(_call_name)
                        if _rname != _call_name or _rimplied:
                            try:
                                call = call.model_copy(update={
                                    "name": _rname,
                                    "args": {**_rimplied,
                                             **dict(call.args or {})}})
                            except Exception:
                                logger.warning(
                                    "session=%s alias copy failed for %s",
                                    self.session_id, call.name)
                        yield ProviderEvent("tool_call", {
                            "name": str(call.name),
                            "args": _safe_args(call.args),
                        })
                        requested = (call.args or {}).get("expression")
                        tool_calls_this_turn += 1
                        self._tools_in_flight += 1
                        tool_t0 = time.monotonic()
                        # cctv_snapshot's old seat: a snap-to-display push
                        # is actuation even though a bare describe is not.
                        _cargs_probe = call.args or {}
                        _display_push = (
                            _cargs_probe.get("screen")
                            or str(_cargs_probe.get("target") or "")
                            .strip().lower() in ("tv", "screen"))
                        if call.name in ACTUATING_TOOLS or (
                                call.name == "ada_camera_snapshot"
                                and _display_push):
                            actuations_this_turn += 1
                        if tool_calls_this_turn > tool_budget:
                            if not budget_hit:
                                self._emit_ops_event(
                                    "tool_storm",
                                    f"Turn exceeded the tool-call budget "
                                    f"({tool_budget}) — further calls refused.",
                                    tool=str(call.name))
                            budget_hit = True
                            logger.warning(
                                "session=%s per-turn tool-call budget %d exhausted — refusing %s",
                                self.session_id, tool_budget, call.name,
                            )
                            result = {"error": (
                                "Tool budget for this turn exhausted — stop calling tools "
                                "and answer the user from what you already have.")}
                        elif actuations_this_turn > actuation_budget:
                            if actuations_this_turn == actuation_budget + 1:
                                self._emit_ops_event(
                                    "actuation_cap",
                                    f"Turn exceeded the actuation budget "
                                    f"({actuation_budget}) — refused "
                                    f"{call.name}.",
                                    tool=str(call.name))
                            budget_hit = True
                            logger.warning(
                                "session=%s per-turn actuation budget %d exhausted — refusing %s",
                                self.session_id, actuation_budget, call.name,
                            )
                            result = {"error": (
                                "Actuation limit for this turn reached — stop and tell the "
                                "user what you were trying to control instead of retrying.")}
                        elif call.name == "set_facial_expression" and requested in EXPRESSION_NAMES:
                            yield ProviderEvent("expression", {"name": requested})
                            result = {"output": f"Ada is now {requested}"}
                        elif (call.name == "get_home_state"
                              # entity_id=/domain= reads route to the
                              # runner (tools-merge-ha) — the fast path
                              # only serves the no-arg occupancy snapshot.
                              and not (call.args or {}).get("entity_id")
                              and not (call.args or {}).get("domain")
                              and self.home_assistant_client is not None):
                            try:
                                snapshot = await self.home_assistant_client.snapshot(
                                    person_entity=self.current_speaker_ha_person
                                )
                                plugs_on_named = [
                                    {"entity_id": e, "name": snapshot.plug_names.get(e, e)}
                                    for e in snapshot.plugs_on
                                ]
                                all_plugs = {
                                    e: {"name": snapshot.plug_names.get(e, e), "state": state}
                                    for e, state in snapshot.plug_states.items()
                                }
                                result = {
                                    "output": {
                                        "person": snapshot.person_state,
                                        "plugs_on": plugs_on_named,
                                        "all_plugs": all_plugs,
                                    }
                                }
                            except Exception as exc:
                                result = {"error": f"home state failed: {exc}"}
                        elif (call.name == "home_search"
                              # kind='event' needs the recorder/MDDB —
                              # it falls through to the runner.
                              and str((call.args or {}).get("kind")
                                      or "device").lower() in ("device", "sensor")
                              and self.home_assistant_client is not None):
                            try:
                                args = dict(call.args or {})
                                query = str(args.get("query") or args.get("q") or "")
                                kind = str(args.get("kind") or "device").lower()
                                limit = int(args.get("limit") or 10)
                                if kind == "sensor":
                                    if query:
                                        sensors = await self.home_assistant_client.sensors(
                                            search=query, limit=limit)
                                    else:
                                        sensors = await self.home_assistant_client.sensors(limit=50)
                                    result = {"output": sensors}
                                elif query:
                                    devices = await self.home_assistant_client.search_entities(query)
                                    result = {"output": devices}
                                else:
                                    devices = await self.home_assistant_client.entities()
                                    # Unbounded dumps stay in live context for
                                    # the whole session (2026-10-01: this single
                                    # call pushed a session past 1M input tokens
                                    # during a silent tool storm). Cap it; the
                                    # marker steers the model to search instead.
                                    if len(devices) > LIST_HOME_DEVICES_MAX:
                                        devices = devices[:LIST_HOME_DEVICES_MAX] + [{
                                            "_truncated": (
                                                f"{LIST_HOME_DEVICES_MAX} of {len(devices)} "
                                                "devices shown — call home_search "
                                                "with a name/keyword for the rest"),
                                        }]
                                    result = {"output": devices}
                            except Exception as exc:
                                result = {"error": f"home_search failed: {exc}"}
                        elif call.name == "get_battery_status" and self.home_assistant_client is not None:
                            try:
                                status = await self.home_assistant_client.battery_status()
                                result = {"output": status}
                            except Exception as exc:
                                result = {"error": f"get_battery_status failed: {exc}"}
                        elif call.name == "get_battery_detail" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                index = int(args.get("battery_index", 1))
                                status = await self.home_assistant_client.battery_detail(index)
                                result = {"output": status}
                            except Exception as exc:
                                result = {"error": f"get_battery_detail failed: {exc}"}
                        elif call.name == "get_inverter_status" and self.home_assistant_client is not None:
                            try:
                                status = await self.home_assistant_client.inverter_status()
                                result = {"output": status}
                            except Exception as exc:
                                result = {"error": f"get_inverter_status failed: {exc}"}
                        elif call.name == "get_pool_status" and self.home_assistant_client is not None:
                            try:
                                status = await self.home_assistant_client.pool_status()
                                result = {"output": status}
                            except Exception as exc:
                                result = {"error": f"get_pool_status failed: {exc}"}
                        elif call.name == "get_rk600_weather" and self.home_assistant_client is not None:
                            try:
                                weather = await self.home_assistant_client.rk600_weather()
                                result = {"output": weather}
                            except Exception as exc:
                                result = {"error": f"get_rk600_weather failed: {exc}"}
                        elif call.name == "get_dashboard_tab" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                tab = args.get("tab")
                                if not tab:
                                    result = {"error": "tab is required"}
                                else:
                                    info = await self.home_assistant_client.dashboard_tab(str(tab))
                                    result = {"output": info}
                            except Exception as exc:
                                result = {"error": f"get_dashboard_tab failed: {exc}"}
                        elif call.name == "get_power_summary" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                hours = int(args.get("hours", 24))
                                summary = await self.home_assistant_client.power_summary(hours=hours)
                                result = {"output": summary}
                            except Exception as exc:
                                result = {"error": f"get_power_summary failed: {exc}"}
                        elif (call.name == "home_history"
                              # The sensor-series read keeps its fast
                              # path (the absorbed get_sensor_history) —
                              # every other kind routes to the runner.
                              and str((call.args or {}).get("kind")
                                      or "").lower() in ("", "series")
                              and str((call.args or {}).get("entity_id")
                                      or "").startswith("sensor.")
                              and self.home_assistant_client is not None):
                            try:
                                args = dict(call.args or {})
                                entity_id = args.get("entity_id")
                                hours = int(args.get("hours", 24))
                                history = await self.home_assistant_client.history(
                                    str(entity_id), hours=hours)
                                result = {"output": history}
                            except Exception as exc:
                                result = {"error": f"home_history failed: {exc}"}
                        elif (call.name == "report_habit_observation"
                              or (call.name == "ada_remember"
                                  and str((call.args or {}).get("kind")
                                          or "").lower() == "habit")):
                            # Legacy name stays callable as a hidden alias;
                            # the canonical path is ada_remember kind='habit'.
                            args = dict(call.args or {})
                            yield ProviderEvent("habit_observation", args)
                            result = {"output": "Observation delivered to the habit monitor"}
                        elif call.name == "get_habit_status" and self.habit_state_getter is not None:
                            result = {"output": self.habit_state_getter()}
                        elif (
                            call.name in ("ada_session_recall", "ada_decision_check",
                                          "ada_set_voice")
                            # scope='history' absorbed ada_ha_recall, which was
                            # never secondary-blocked — keep that access.
                            and not (call.name == "ada_session_recall"
                                     and (call.args or {}).get("scope") == "history")
                            and self.tool_runner is not None
                            and self.tool_runner._is_secondary_turn()
                        ):
                            # Provider-dispatched tools bypass the runner gate —
                            # enforce the secondary-speaker policy here too.
                            self.conversation.log_event(
                                "tool_denied", tool=str(call.name),
                                speaker=self.current_speaker_ha_person)
                            result = {"error": (
                                "Reserved for the session owner — propose it to "
                                "them aloud and let them ask in their own voice.")}
                        elif (call.name == "ada_session_recall"
                              and (call.args or {}).get("scope") != "history"):
                            # scope='history' (absorbed ada_ha_recall) falls
                            # through to the runner for an inline answer.
                            # `force=true` is the user's explicit push
                            # ("dig deeper", "check again") — it overrides
                            # the redundant-recall gate.
                            force = (call.args or {}).get("force")
                            if self._recall_gated() and not force:
                                result = {"output": (
                                    "ada_memory_search already returned a confident match "
                                    "moments ago — answer from those hits. Session recall "
                                    "skipped (redundant). If the user insists, retry with force=true.")}
                            else:
                                question = (call.args or {}).get("question", "What did we discuss in the previous session?")
                                group = (call.args or {}).get("group")
                                bank = (call.args or {}).get("bank")
                                recall_status = self.conversation.start_recall(
                                    str(question), self._on_recall_complete,
                                    group=str(group) if group else None,
                                    bank=str(bank) if bank else None,
                                    on_slow=self._on_recall_slow,
                                )
                                result = {"output": recall_status}
                        elif call.name == "ada_set_voice":
                            result = {"output": self._set_voice(dict(call.args or {}))}
                        elif call.name == "ada_camera_snapshot":
                            _cargs = dict(call.args or {})
                            # cctv_snapshot's absorbed behavior was
                            # capture-gated for secondary voices — the
                            # gate keys off the RESOLVED name and runs
                            # only when the call pushes to a display.
                            _wants_display = (
                                _cargs.get("screen")
                                or str(_cargs.get("target") or "")
                                .strip().lower() in ("tv", "screen"))
                            if _wants_display and self.tool_runner is not None:
                                if _cargs.get("confirmed") and not \
                                        self._user_confirmed(input_transcript):
                                    logger.warning(
                                        "session=%s ada_camera_snapshot "
                                        "self-asserted confirmed=true "
                                        "without user affirmation — "
                                        "stripping", self.session_id)
                                    _cargs.pop("confirmed", None)
                                    self._emit_ops_event(
                                        "confirm_strip",
                                        "Stripped model-asserted "
                                        "confirmed=true on "
                                        "ada_camera_snapshot — no user "
                                        "affirmation found.",
                                        tool="ada_camera_snapshot")
                                try:
                                    self.tool_runner._check_capture_confirmed(
                                        "ada_camera_snapshot", _cargs,
                                        _cargs.get("confirmed"))
                                except PermissionError as exc:
                                    result = {"error": str(exc)}
                                    _cargs = None
                            if _cargs is not None:
                                result, frame = await self._camera_snapshot(
                                    _cargs)
                                if frame:
                                    camera_frames.append(frame)
                        elif call.name == "vcast_snapshot":
                            result, frame = await self._vcast_snapshot(
                                dict(call.args or {}))
                            if frame:
                                camera_frames.append(frame)
                        elif call.name == "ada_decision_check" and self.tool_runner is not None:
                            result = {"output": self._start_decision_check(dict(call.args or {}))}
                        elif call.name == "ada_deep_research" and self.tool_runner is not None:
                            result = {"output": self._start_deep_research(dict(call.args or {}))}
                        elif (call.name == "ada_remember" and budget_hit
                              and str((call.args or {}).get("kind") or "")
                              .lower() not in ("vocab", "habit", "guest")):
                            self._emit_ops_event(
                                "remember_block",
                                "ada_remember refused — turn hit the tool "
                                "budget, content may be confabulated.",
                                tool="ada_remember")
                            # The plan/summary the model wants to save never
                            # survived the storm — refuse rather than persist
                            # confabulated content.
                            result = {"error": (
                                "The tool budget was exhausted this turn, so the earlier "
                                "part of the answer may be missing — restate the content "
                                "aloud and save it on the next turn.")}
                        else:
                            if self.tool_runner is not None:
                                try:
                                    call_args = dict(call.args or {})
                                    if (str(call.name) in _CONFIRM_GATED_TOOLS
                                            # non-bank ada_remember kinds
                                            # (vocab/guest/habit) absorbed
                                            # ungated tools — skip the probe.
                                            and not (str(call.name) == "ada_remember"
                                                     and str(call_args.get("kind")
                                                             or "").lower()
                                                     in ("vocab", "habit", "guest"))):
                                        # Resolved source text shared by the
                                        # gate and the Jev advisory probe —
                                        # probe fires on every gated call,
                                        # not only self-asserted confirms.
                                        _src = self._confirm_source_text(input_transcript)
                                        _affirmed = self._user_confirmed(input_transcript)
                                        self._jev_confirm_probe(
                                            _src, _affirmed, str(call.name))
                                    if call_args.get("confirmed"):
                                        _src = self._confirm_source_text(input_transcript)
                                        _affirmed = self._user_confirmed(input_transcript)
                                        if not _affirmed:
                                            logger.warning(
                                                "session=%s %s self-asserted confirmed=true "
                                                "without user affirmation — stripping",
                                                self.session_id, call.name,
                                            )
                                            call_args.pop("confirmed", None)
                                            self._emit_ops_event(
                                                "confirm_strip",
                                                f"Stripped model-asserted "
                                                f"confirmed=true on {call.name} — "
                                                f"no user affirmation found.",
                                                tool=str(call.name))
                                        else:
                                            # User actually affirmed — the
                                            # pending-register step is
                                            # redundant friction (observed
                                            # 2026-09-29: 'approve' → denied
                                            # 'no pending' → double-ask loop).
                                            call_args["_verified_affirm"] = True
                                    if call.name == "ada_memory_search":
                                        q = call_args.get("query")
                                        if q:
                                            call_args["query"] = self.conversation.expand_query(str(q))
                                    # Authorization identity is the session
                                    # OWNER pinned at connect — the speaking
                                    # voice only personalizes, it never
                                    # upgrades permissions mid-session (P1).
                                    owner = self.session_owner
                                    output = await self.tool_runner.execute(
                                        str(call.name), call_args,
                                        identity=(owner
                                                  or self.current_speaker_ha_person),
                                        speaker=self.current_speaker_ha_person,
                                        speaker_session=self.speaker_session,
                                        owner=owner)
                                    result = {"output": output}
                                    if call.name == "ada_memory_search":
                                        self._note_search_result(output)
                                except Exception as exc:
                                    result = {"error": f"{call.name} failed: {exc}"}
                            else:
                                result = {"error": "Unsupported or unavailable function"}
                        self.conversation.log_event(
                            "tool_call", tool=str(call.name),
                            dur_ms=int((time.monotonic() - tool_t0) * 1000),
                            ok=("error" not in result
                                if isinstance(result, dict) else True))
                        yield ProviderEvent("tool_result", {
                            "name": str(call.name),
                            "result": _safe_args(result) if isinstance(result, dict) else {"value": str(result)[:500]},
                        })
                        self._tools_in_flight = max(0, self._tools_in_flight - 1)
                        function_responses.append(types.FunctionResponse(
                            id=call.id,
                            name=_call_name or "set_facial_expression",
                            response=result,
                            # The expression tool often arrives before audio.
                            # WHEN_IDLE lets Gemini continue the spoken reply;
                            # SILENT would add the result to context without
                            # triggering generation, leaving Ada mute.
                            scheduling=types.FunctionResponseScheduling.WHEN_IDLE,
                        ))
                        logger.info(
                            "session=%s function_call result id=%s name=%s result=%r",
                            self.session_id, call.id, call.name, result,
                        )
                    await self._session.send_tool_response(
                        function_responses=function_responses
                    )
                    logger.info(
                        "session=%s function_call responses sent count=%d",
                        self.session_id, len(function_responses),
                    )
                    # Camera frames ride as follow-up client content (same
                    # pattern as send_habit_alert) — FunctionResponse.parts
                    # crashes send_tool_response's json.dumps on bytes.
                    for cam_name, cam_img, cam_mime in camera_frames:
                        # Vision sidecar (ADA_VISION_MODE): a cheap model
                        # describes the frame so the Live session doesn't
                        # have to — quota storms stall hardest on image
                        # turns (2026-09-30 empty-response issue).
                        desc = None
                        if vision_describe.configured():
                            try:
                                desc = await vision_describe.describe_frame(
                                    cam_img, cam_mime, cam_name)
                            except Exception:
                                desc = None
                        try:
                            if desc and vision_describe.mode() == "describe":
                                parts = [types.Part.from_text(text=(
                                    f"Camera frame from '{cam_name}' was "
                                    "analyzed by the vision helper (the live "
                                    "session skipped the image to save quota). "
                                    f"Helper says: {desc}\nRelay this to the "
                                    "user naturally — if the description seems "
                                    "thin, you may look again on request."))]
                            else:
                                verb = ("is the STALE last-known frame — "
                                        "describe it but say it is old" if
                                        "STALE" in cam_name else
                                        "just arrived — describe to the "
                                        "user what it shows.")
                                parts = [types.Part.from_text(text=(
                                    f"Camera frame from '{cam_name}' "
                                    f"{verb}"
                                    + (f" (helper agrees: {desc})"
                                       if desc else "")))]
                                parts.append(types.Part.from_bytes(
                                    data=cam_img, mime_type=cam_mime))
                            async with self._send_lock:
                                await self._session.send_client_content(
                                    turns=types.Content(
                                        role="user", parts=parts),
                                    turn_complete=True,
                                )
                        except Exception as exc:
                            logger.warning(
                                "session=%s camera frame send failed: %s",
                                self.session_id, exc,
                            )
                    if budget_hit and not budget_nudged:
                        # Refused results alone don't stop a storm — the model
                        # keeps emitting calls. Inject an explicit user-turn
                        # nudge so the turn has to produce an answer.
                        budget_nudged = True
                        try:
                            async with self._send_lock:
                                await self._session.send_client_content(
                                    turns=types.Content(
                                        role="user",
                                        parts=[types.Part.from_text(text=(
                                            "[system] Tool-call limit reached for this turn — "
                                            "stop calling tools and answer the user now, "
                                            "briefly, from what you already have."))],
                                    ),
                                    turn_complete=True,
                                )
                        except Exception:
                            logger.debug("budget nudge send failed", exc_info=True)

                content = message.server_content
                if content is None:
                    continue

                if content.interrupted:
                    elapsed = time.monotonic() - response_started_at if response_started_at else 0.0
                    logger.warning(
                        "session=%s assistant interrupted response_active=%s age_ms=%d audio_chunks=%d "
                        "audio_bytes=%d pending_input_transcript=%r",
                        self.session_id, self._response_active, elapsed * 1000, response_audio_chunks,
                        response_audio_bytes, input_transcript.strip(),
                    )
                    self._response_active = False
                    tool_calls_this_turn = 0
                    actuations_this_turn = 0
                    budget_hit = False
                    budget_nudged = False
                    self._leak_active = False
                    self._artifact_hold = ""
                    self._artifact_logged = False
                    self._turn_open = False
                    barge_pending = True
                    self.conversation.log_event(
                        "barge_in",
                        speaker=(self.current_speaker
                                 or self.current_speaker_ha_person),
                        response_age_ms=int(elapsed * 1000))
                    yield ProviderEvent("response_interrupted", {})
                    # Gemini 3.1 can include several content parts in one event.
                    # Any audio/transcript accompanying an interruption belongs
                    # to the cancelled response and must not restart playback.
                    continue

                transcription = content.input_transcription
                if transcription and transcription.text:
                    input_transcript += transcription.text
                    input_done_ts = time.monotonic()
                    self._last_user_at = time.monotonic()
                    self._turn_open = True

                output_transcription = content.output_transcription
                if output_transcription and output_transcription.text:
                    if not self._response_active:
                        self._response_active = True
                        response_started_at = time.monotonic()
                        response_audio_chunks = 0
                        response_audio_bytes = 0
                        yield ProviderEvent("response_started", {})
                    clean = self._strip_tool_leak(output_transcription.text)
                    if clean:
                        clean = self._strip_speech_artifacts(clean)
                    if clean:
                        assistant_turn_text += clean
                        yield ProviderEvent(
                            "assistant_transcript_delta",
                            {"text": clean},
                        )

                model_turn = content.model_turn
                if model_turn:
                    for part in model_turn.parts or []:
                        inline_data = part.inline_data
                        if inline_data and inline_data.data:
                            if not self._response_active:
                                self._response_active = True
                                response_started_at = time.monotonic()
                                response_audio_chunks = 0
                                response_audio_bytes = 0
                                yield ProviderEvent("response_started", {})
                            response_audio_chunks += 1
                            response_audio_bytes += len(inline_data.data)
                            yield ProviderEvent("audio", {"pcm16": inline_data.data})

                if content.turn_complete:
                    transcript = input_transcript.strip()
                    if transcript:
                        if barge_pending and _looks_like_noise(transcript):
                            # Obvious noise that tripped the VAD — keep it
                            # out of the transcript; the browser pump nudges
                            # Ada to resume (policy P6: ignore gibberish).
                            self.conversation.log_event(
                                "barge_noise", text=transcript[:80])
                            yield ProviderEvent(
                                "barge_noise", {"text": transcript})
                        else:
                            self.conversation.add_user(transcript)
                            yield ProviderEvent("user_transcript",
                                                {"text": transcript})
                    barge_pending = False
                    input_transcript = ""
                    n_tools_this_turn = tool_calls_this_turn
                    if (tool_calls_this_turn == 0
                            and _phantom_claim(assistant_turn_text)):
                        self._emit_ops_event(
                            "phantom_write_claim",
                            "assistant claimed a write with no tool call this "
                            f"turn: {assistant_turn_text.strip()[:160]!r}",
                        )
                    tool_calls_this_turn = 0
                    actuations_this_turn = 0
                    budget_hit = False
                    budget_nudged = False
                    if self._artifact_hold and not self._leak_active:
                        tail = self._flush_artifact_hold()
                        if tail:
                            assistant_turn_text += tail
                            yield ProviderEvent(
                                "assistant_transcript_delta",
                                {"text": tail},
                            )
                    else:
                        self._artifact_hold = ""
                    self._leak_active = False
                    self._artifact_logged = False
                    if assistant_turn_text.strip():
                        self.conversation.add_assistant(assistant_turn_text)
                        assistant_turn_text = ""
                    if self._response_active:
                        elapsed = time.monotonic() - response_started_at if response_started_at else 0.0
                        logger.info(
                            "session=%s assistant response completed duration_ms=%d audio_chunks=%d audio_bytes=%d",
                            self.session_id, elapsed * 1000, response_audio_chunks, response_audio_bytes,
                        )
                        # TTFT ≈ last user-speech transcription chunk → first
                        # model output. Feeds the latency benchmark rollup.
                        self.conversation.log_event(
                            "turn_latency",
                            ttft_ms=(int((response_started_at - input_done_ts)
                                         * 1000) if input_done_ts else None),
                            dur_ms=int(elapsed * 1000),
                            audio_bytes=response_audio_bytes,
                            tools=n_tools_this_turn)
                        yield ProviderEvent("response_completed", {})
                    input_done_ts = 0.0
                    self._response_active = False
                    self._turn_open = False
                    # A notification queued mid-turn — deliver it now that
                    # the response finished, as its own turn.
                    if self._pending_notifications:
                        await self._drain_notifications()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.usage_input_tokens or self.usage_output_tokens:
            logger.info(
                "session=%s usage summary %s", self.session_id, self.usage_summary()
            )
        # Persist conversation to NotebookLM in the background so we don't block disconnect.
        if self.conversation:
            _ = asyncio.create_task(self.conversation.persist())
        if self._session_context is not None:
            try:
                await asyncio.wait_for(
                    self._session_context.__aexit__(None, None, None), timeout=3
                )
            except (TimeoutError, Exception):
                logger.debug("Gemini session close did not complete cleanly", exc_info=True)
        if self._client is not None:
            try:
                await self._client.aio.aclose()
            except Exception:
                logger.debug("Gemini client close did not complete cleanly", exc_info=True)


def create_provider(instructions: str | None = None, tool_runner: Any = None,
                    home_assistant_client: Any = None, habit_state_getter: Any = None,
                    session_id: str | None = None,
                    conversation: ConversationMemory | None = None,
                    caller_name: str | None = None,
                    caller_person: str | None = None) -> RealtimeProvider:
    return GeminiLiveProvider(
        instructions=instructions,
        tool_runner=tool_runner,
        home_assistant_client=home_assistant_client,
        habit_state_getter=habit_state_getter,
        session_id=session_id,
        conversation=conversation,
        caller_name=caller_name,
        caller_person=caller_person,
    )
