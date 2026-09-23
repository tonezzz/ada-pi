"""Gemini Live provider for the browser audio bridge."""

from __future__ import annotations

import abc
import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

from backend.conversation_memory import ConversationMemory
from backend.tool_runner import ToolRunner
from backend.usage_tracker import usage_ledger

logger = logging.getLogger("voice.provider")

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
    "calendar_list_calendars", "calendar_list_events", "calendar_freebusy",
    "plan_day", "calendar_create_event", "calendar_delete_event",
    "tasks_list", "tasks_add", "tasks_complete", "ada_resolve_action",
}

# Same constant pattern as CALENDAR_TOOLS: lets ADA_EXCLUDED_TOOLS strip the
# CMS declarations and their instruction paragraph together.
CMS_TOOLS = {
    "cms_list_pages", "cms_get_page", "cms_verify_page",
    "cms_publish_page", "cms_delete_page",
}

CALENDAR_INSTRUCTIONS = (
    " You have calendar and task tools backed by the user's configured providers: "
    "calendar_list_calendars shows which calendars exist, "
    "calendar_list_events and calendar_freebusy read the schedule, "
    "plan_day returns one merged view of events plus open tasks for a day, "
    "calendar_create_event and calendar_delete_event modify the calendar, and "
    "tasks_list, tasks_add, and tasks_complete manage the task list. "
    "For any schedule question call calendar_list_events or plan_day first and answer "
    "from the result; never recite a schedule from memory. "
    "Interpret relative dates ('tomorrow', 'Friday') in the user's local timezone. "
    "Before creating or deleting an event, or adding or completing a task, restate the "
    "exact details (title, date, time) and get an explicit yes, then call the tool with "
    "confirmed=true — writes are enforced server-side. "
    "If a calendar tool reports an auth error, say the calendar provider needs re-authentication "
    "and stop retrying. Event and task ids are provider-qualified (e.g. 'google:primary/abc') — "
    "pass them back exactly as returned. "
    "When the session context lists pending suggestions from earlier conversations, offer each "
    "once, briefly and early in the conversation; if the user accepts, restate the details, call "
    "the matching calendar/task tool with confirmed=true, then call ada_resolve_action with the "
    "shown key and resolution 'applied'. If declined, call ada_resolve_action with 'dismissed'. "
    "When the user states a plan, appointment, or reminder intention unprompted, proactively "
    "offer to put it on the calendar or task list instead of waiting to be asked."
)

CMS_INSTRUCTIONS = (
    " You maintain the user's miniapp — a small multi-page site whose pages you own. "
    "cms_list_pages lists existing pages, cms_get_page reads one, "
    "cms_publish_page creates or fully replaces a page (slugs are lowercase, e.g. 'pool-notes'), and "
    "cms_delete_page removes one. Page content is written as markdown, html, yaml, or slides markdown. "
    "When the user asks you to prepare a document or update a page, draft the content, "
    "restate the slug and title, get an explicit yes, then call the write tool with "
    "confirmed=true — writes are enforced server-side. "
    "You cannot see the rendered site: after publishing or updating a page, call "
    "cms_verify_page to check the content parses and confirm the structure, then "
    "tell the user the page is live (or fix it if verification failed)."
)

# Same constant pattern as CALENDAR_TOOLS/CMS_TOOLS: lets ADA_EXCLUDED_TOOLS
# strip the devin declarations and their instruction paragraph together.
DEVIN_TOOLS = {"devin_dispatch", "devin_status", "devin_followup"}

DEVIN_INSTRUCTIONS = (
    " You can dispatch unattended Devin coding sessions on tony-dell: "
    "devin_dispatch starts one in a dedicated git worktree (repos: chaba, ada-pi, "
    "sunsynk-card), devin_status lists running and finished tasks, and "
    "devin_followup sends a message into a running session. "
    "Before dispatching, restate the repo and task and get an explicit yes, then "
    "call with confirmed=true — writes are enforced server-side. "
    "Dispatched sessions run unattended; the user is notified on their phone when "
    "one finishes, so report the task id and move on rather than polling. "
    "When discussing an implementation task the user wants built later, offer to "
    "save the spec into the devin-handoff memory bank so a dispatched session can "
    "be told to 'check the ada handoff'."
)


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


def _now_context() -> str:
    tz = ZoneInfo(os.environ.get("ADA_TIMEZONE", "Asia/Bangkok"))
    now = datetime.now(tz)
    return (
        f"\n\nCurrent local date and time: {now:%A, %Y-%m-%d %H:%M} ({now.tzname()}, {now:%z}). "
        "All times and dates the user mentions are in this timezone unless they say otherwise."
    )

DEFAULT_ADA_INSTRUCTIONS = """You are Ada, a polished, highly capable voice assistant running on a Raspberry Pi desk companion.

Personality:
- Sound composed, perceptive, confident, and subtly sassy. Use restrained dry wit and occasional understated sarcasm rather than obvious jokes or constant teasing.
- Your humor should feel effortless and intelligent: a brief raised-eyebrow observation, then move on. Do not announce that you are joking and do not force a punchline into every reply.
- For habits, make it clear that you noticed the pattern, then give one useful, realistic correction. Mild judgment is welcome; mockery and repetitive roasting are not.
- Target the behavior, never the person's identity, appearance, intelligence, or worth. Never be cruel, humiliating, threatening, or relentless.
- Drop the sarcasm for emergencies, genuine distress, medical concerns, or other sensitive moments; be direct and caring instead.

Ada's capabilities:
- You converse through a full-duplex microphone and speakers and may be interrupted naturally.
- You have a camera for current visual context. Describe only what is clearly visible and ask for a better view when uncertain.
- Your animated face can express neutral, sassy, amused, skeptical, annoyed, mad, concerned, surprised, mischievous, serious, or alert.
- You monitor habits such as seated posture. Local pose estimation proposes events, Gemini vision verifies ambiguous ones, and confirmed occurrences can become possible, emerging, or established habits over time.
- You also track home plugs left on while the user is away or the home has remained empty. Home Assistant supplies authoritative person and plug states; local vision supplies home presence while the user is home.
- Habit alerts may arrive with a current image and structured context. Give a brief, dry observation and one practical correction. Distinguish a first possible habit, another occurrence, and an established habit that now clearly needs attention.
- You can discuss current habit status and help the user choose small, realistic corrective actions.

Be witty, factual, and brief. Never claim that a habit occurred unless the application reports a confirmed event. Do not diagnose medical conditions. Respect privacy and do not imply that camera frames are stored."""


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
                 conversation: ConversationMemory | None = None) -> None:
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
        self.home_assistant_client = home_assistant_client
        self.habit_state_getter = habit_state_getter
        # Callers may share one ConversationMemory across provider reconnects
        # so the server-side transcript survives a Gemini session swap.
        self.conversation = conversation or ConversationMemory(session_id or "unknown")
        self._bg_tasks: set[asyncio.Task] = set()
        # Monotonic time of the last confident ada_memory_search hit — used to
        # short-circuit a redundant ada_session_recall in the same turn.
        self._strong_hit_at = 0.0
        self._recall_gate_score = float(os.environ.get("ADA_RECALL_GATE_SCORE", "0.6"))
        self._recall_gate_window = float(os.environ.get("ADA_RECALL_GATE_WINDOW_S", "60"))
        self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.model = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
        self.voice = os.environ.get("GEMINI_LIVE_VOICE", "Kore")
        self.video_resolution = os.environ.get("GEMINI_VIDEO_RESOLUTION", "high").lower()
        base_instructions = instructions or os.environ.get("GEMINI_LIVE_INSTRUCTIONS") or DEFAULT_ADA_INSTRUCTIONS
        self.instructions = (
            f"{base_instructions}\n\n"
            "Your name is Ada. You have a visible animated face. Use the "
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
            "ask the user to hold it steady or move it closer instead. When the user "
            "asks what habits are tracked, their habit status, or their progress, always "
            "call get_habit_status and ground the answer in its current result."
            " When the user asks about token usage, API usage, or what a session "
            "costs, call ada_usage_summary and answer from its numbers."
            " You have Home Assistant device control through several tools: "
            "get_home_state to check occupancy and the state of the configured home plugs, "
            "list_home_devices to list all devices including lights, switches, covers, buttons, and media players, "
            "search_home_devices to find a device by name, "
            "control_entity to turn a light/switch/fan on or off, "
            "control_cover to open, close, or stop a gate or shutter, "
            "press_button to press a button entity, "
            "control_media_player to turn on/off, play, pause, or change source on a TV or speaker, "
            "and tv_action to send a command to the LG TV via rest_command.tv_action. "
            "When the user asks about devices, occupancy, or what is on/off, call get_home_state or list_home_devices first. "
            "When the user asks to turn something on/off, control a gate, or operate the TV, "
            "use search_home_devices to find the exact entity_id or use tv_action with the right cmd/text, then call the matching control tool. "
            "For safety, before using control_cover to open or close the gate or any shutter, "
            "always warn that something could be blocking it and ask the user to confirm explicitly. "
            "Only call control_cover for the gate or a shutter after the user has given a clear second confirmation. "
            "Every controllable device has a safety level: safe, caution, or dangerous. "
            "Use ada_ha_get_device_confidence to check a device's safety before acting. "
            "For safety: dangerous, warn the user, explain the risk, and get explicit confirmation before calling any control tool. "
            "Dangerous devices are enforced server-side: the control call is rejected unless you pass confirmed=true. "
            "Only set confirmed=true after the user has explicitly confirmed the action. "
            "For safety: caution, confirm once before acting. "
            "For safety: safe, proceed directly. "
            "You can update a device's safety level with ada_ha_set_device_confidence. "
            "You also have Ada HA memory tools: ada_ha_get_state for the stored home snapshot, "
            "ada_ha_search_devices to find a device by name, ada_ha_search_sensors to find a sensor, "
            "ada_ha_recall for free-form recall across the stored devices and sensors, "
            "ada_ha_history to list recent home snapshots from memory, "
            "ada_ha_get_device_confidence to list devices by trust level, "
            "ada_ha_set_device_confidence to change a device's trust level, and "
            "ada_session_recall to ask NotebookLM about previous conversations or stored knowledge by topic group, "
            "and curated memory banks you can search and write: "
            "ada_memory_search to find what you know, ada_remember to store or correct a memory "
            "when the user says 'remember that', ada_forget to retract a memory that is no longer true, "
            "and ada_outcome to record how a memory or check turned out when the user reports back "
            "(e.g. 'that shop was fine', 'the fix worked', 'I skipped it') — outcomes update confidence "
            "so future recall trusts knowledge with a good track record. "
            "When the user states a durable fact, preference, or a fix that worked, offer to "
            "remember it, then call ada_remember — pass confirmed=true only after the user agrees. "
            "When the user reports how something turned out ('that worked', 'it failed'), call "
            "ada_outcome on the memory it applies to — find the key with ada_memory_search if needed. "
            "When calling any search or recall tool, always write a fully self-contained query: "
            "resolve 'it', 'that one', 'the same service', and similar references using the "
            "conversation so far — never pass a bare pronoun as the query. "
            "Memories returned with unverified=true are low-confidence: hedge or say you are not sure "
            "rather than stating them as fact. "
            "Prefer the ada_ha_* memory tools for home, device, sensor, or event questions — they answer instantly. "
            "For any factual lookup — people, projects, purchases, procedures, fixes — "
            "call ada_memory_search with bank='all' first; it fans out across every bank "
            "so you never have to guess which one. When its top hit is a confident match, "
            "ground the answer in that result, not in earlier conversation or session "
            "context that may be stale or off-topic. "
            "Reserve ada_session_recall strictly for 'what did we talk about' or "
            "'do you remember' questions — never for fact lookup, and never in the same "
            "turn as a confident ada_memory_search result; it can take up to 20 seconds, "
            "so keep the user informed while it runs. "
            "Use ada_ha_get_device_confidence when the user asks what is broken, new, needs setup, or trusted. "
            "For event history: get_logbook gives the friendly Home Assistant event log, "
            "get_recent_events answers what opened, closed, or changed recently across the home, "
            "get_entity_events gives one entity's open/close timeline with durations, and "
            "ada_ha_search_events searches recorded events from memory. "
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
        )
        self._client: Any = None
        self._session_context: Any = None
        self._session: Any = None
        self._closed = False
        self._send_lock = asyncio.Lock()
        self.session_id = session_id or "-"
        self.resumption_handle: str | None = None
        self.go_away_time_left: str | None = None
        self._response_active = False
        # current_speaker / current_speaker_ha_person initialized in __init__ prologue
        self.usage_input_tokens = 0
        self.usage_output_tokens = 0
        self.usage_input_by_modality: dict[str, int] = {}
        self.usage_output_by_modality: dict[str, int] = {}

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
            ", ".join(sorted(n for n, b in banks.items() if b.writable))
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
            plan = await asyncio.wait_for(svc.plan_day("today"), timeout=4.0)
        except Exception as exc:
            logger.info("session=%s agenda prefetch failed: %s",
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
            logger.info("session=%s proposals prefetch failed: %s",
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
            "input_audio_transcription": {},
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
                        "active five-minute or latched habit condition. Use this when asked "
                        "about home plugs, occupancy, or whether plugs were left on."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "control_entity",
                    "description": (
                        "Turn on or off a Home Assistant light, switch, fan, or input_boolean. "
                        "Use this when the user asks to turn something on or off."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The Home Assistant entity_id to control, e.g. light.living_room.",
                            },
                            "on": {
                                "type": "boolean",
                                "description": "True to turn the entity on, false to turn it off.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required for dangerous-safety entities; set true only after explicit user confirmation.",
                            },
                        },
                        "required": ["entity_id", "on"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "list_home_devices",
                    "description": (
                        "Lists every controllable Home Assistant device (lights, switches, fans, input_booleans) "
                        "with its entity_id, friendly name, current state, domain, and availability. "
                        "Use this to answer 'what devices are available', 'what can I control', or to find "
                        "the exact entity_id before calling control_entity."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "search_home_devices",
                    "description": (
                        "Searches controllable Home Assistant devices by name or entity_id. "
                        "Returns the best matching devices with entity_id, friendly name, state, domain, and availability. "
                        "Use this when the user asks to control a device by name (e.g. 'turn on kitchen table') "
                        "and you need to find the exact entity_id."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "A device name or keyword to search, e.g. 'kitchen table', 'front gate', or 'outlet'.",
                            }
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "list_sensors",
                    "description": (
                        "Lists available Home Assistant sensor entities with their current state, unit, and friendly name. "
                        "Use this when the user asks 'what sensors do we have', 'what can we monitor', or about environmental/power/energy information."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "search_sensors",
                    "description": (
                        "Searches Home Assistant sensor entities by name or entity_id. "
                        "Returns matching sensors with current state, unit, and friendly name. "
                        "Use this when the user asks about a specific reading like 'what is the pool temperature' or 'what is the pool energy'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "A sensor name or keyword to search, e.g. 'pool', 'temperature', 'pv power'.",
                            }
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "control_cover",
                    "description": (
                        "Open, close, or stop a Home Assistant cover such as a gate or roller shutter. "
                        "Use this when the user asks to open/close the gate, garage, or shutter. "
                        "Only call this tool after the user has explicitly confirmed there is nothing blocking the gate or shutter."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The cover.* entity_id, e.g. cover.gate_motor.",
                            },
                            "action": {
                                "type": "string",
                                "enum": ["open", "close", "stop"],
                                "description": "The cover action: open, close, or stop.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required for dangerous-safety covers; set true only after explicit user confirmation.",
                            },
                        },
                        "required": ["entity_id", "action"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "press_button",
                    "description": (
                        "Press a Home Assistant button entity. "
                        "Use this for 'my position' buttons or one-shot commands."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The button.* entity_id, e.g. button.gate_motor_my_position.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required for buttons attached to dangerous devices; set true only after explicit user confirmation.",
                            },
                        },
                        "required": ["entity_id"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "control_media_player",
                    "description": (
                        "Control a Home Assistant media player (TV, speaker). "
                        "Supports turn_on, turn_off, media_play, media_pause, media_stop, "
                        "volume_up, volume_down, volume_mute, and select_source. "
                        "Use this to turn the TV on or off or change playback."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The media_player.* entity_id, e.g. media_player.lg_webos_tv_nano81tsa.",
                            },
                            "action": {
                                "type": "string",
                                "enum": ["turn_on", "turn_off", "media_play", "media_pause", "media_stop", "volume_up", "volume_down", "volume_mute", "select_source"],
                                "description": "The media_player action.",
                            },
                            "source": {
                                "type": "string",
                                "description": "Required for select_source; the input/source name to select.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required for dangerous-safety entities; set true only after explicit user confirmation.",
                            },
                        },
                        "required": ["entity_id", "action"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "tv_action",
                    "description": (
                        "Send a command to the LG TV through the Home Assistant rest_command.tv_action service. "
                        "Use this for casting or navigation commands such as cmd='nav' with text='screenlive:workspace:1' or 'tony-omen:workspace:1'. "
                        "The cmd and text values are passed straight to the TV action REST command."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "cmd": {
                                "type": "string",
                                "description": "Command key, e.g. 'nav' or 'power'.",
                            },
                            "text": {
                                "type": "string",
                                "description": "Command text/payload, e.g. 'screenlive:workspace:1' or 'tony-omen:workspace:1'.",
                            },
                        },
                        "required": ["cmd"],
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
                        "Use this when the user asks about the weather station, wind, or the RK600 card. "
                        "Do NOT use this for forecast/Met.no weather; use search_sensors('weather') for that."
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
                    "name": "get_sensor_history",
                    "description": (
                        "Fetches the history of a single Home Assistant sensor for the requested hours. "
                        "Use this when the user asks about a specific sensor's behavior over time."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The exact Home Assistant entity_id, e.g. sensor.inverters_1_pv_power.",
                            },
                            "hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 168,
                                "description": "How many hours of history to include. Defaults to 24.",
                            }
                        },
                        "required": ["entity_id"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_logbook",
                    "description": (
                        "Fetches the Home Assistant logbook: friendly event entries like "
                        "'Front door was opened', 'Kitchen light turned on', or automation runs. "
                        "Use this when the user asks about the event log or what happened recently."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "Optional entity_id to limit the logbook to one entity.",
                            },
                            "hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 168,
                                "description": "How many hours of logbook to include. Defaults to 24.",
                            }
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_recent_events",
                    "description": (
                        "Returns recent state-change events across the whole home: doors/windows "
                        "opening and closing, covers moving, locks, presence changes, and "
                        "lights/switches turning on or off. "
                        "Use this when the user asks 'what opened or closed', 'what changed recently', "
                        "or 'did anything happen while I was away'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 168,
                                "description": "How many hours back to scan. Defaults to 24.",
                            },
                            "query": {
                                "type": "string",
                                "description": "Optional keyword to limit events, e.g. 'door', 'gate', 'kitchen'.",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 200,
                                "description": "Maximum events to return. Defaults to 50.",
                            }
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_entity_events",
                    "description": (
                        "Returns the event timeline for one entity: every state change with a "
                        "timestamp and how long it stayed in that state. For a door or window "
                        "sensor this yields open/close times and durations. "
                        "Use this when the user asks 'when was the door last opened' or "
                        "'how long was the gate open'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The exact Home Assistant entity_id, e.g. binary_sensor.front_door or cover.gate_motor.",
                            },
                            "hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 168,
                                "description": "How many hours back to include. Defaults to 24.",
                            }
                        },
                        "required": ["entity_id"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_ha_search_events",
                    "description": (
                        "Searches the recorded Home Assistant event memory: transitions captured "
                        "by the event recorder plus persisted event batches. "
                        "Use this when the user asks about events from before the current window, "
                        "e.g. 'when did the gate open earlier' or 'any door events this week'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Keyword to search, e.g. 'door', 'gate', 'opened'.",
                            },
                            "hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 720,
                                "description": "How many hours of recorded events to search. Defaults to 24.",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100,
                                "description": "Maximum events to return. Defaults to 20.",
                            }
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "report_habit_observation",
                    "description": (
                        "Reports the structured result of a water or junk-food camera "
                        "observation requested by the ADA backend. Call this only when a "
                        "backend prompt supplies a challenge_id, after reviewing the full "
                        "observation window. Mere containers or food presence are not consumption."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "challenge_id": {"type": "string"},
                            "habit_key": {"type": "string", "enum": ["not_drinking_enough_water", "junk_food"]},
                            "observed": {"type": "boolean"},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "reason": {"type": "string"},
                            "item_identified": {"type": "string", "description": "For junk-food checks, the specific visible food or drink; empty when none is identifiable."},
                            "consumption_visible": {"type": "boolean", "description": "For junk-food checks, true only when actual eating or drinking is visibly confirmed."},
                            "classified_unhealthy": {"type": "boolean", "description": "For junk-food checks, true only when the identified item clearly belongs to the configured unhealthy categories."},
                        },
                        "required": ["challenge_id", "habit_key", "observed", "confidence", "reason"],
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
                    "name": "ada_ha_get_state",
                    "description": (
                        "Returns the preloaded memory snapshot of the configured Home Assistant: person, home plugs, "
                        "and counts of controllable devices and sensors. Use this when the user "
                        "asks about the stored home state or what is in memory."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                }, {
                    "name": "ada_ha_search_devices",
                    "description": (
                        "Search the stored Home Assistant memory for controllable devices by name or entity_id. "
                        "Use this when the user asks 'what devices do we have' or 'find the kitchen light'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "A device name or keyword, e.g. 'kitchen table'.",
                            }
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_ha_search_sensors",
                    "description": (
                        "Search the stored Home Assistant memory for sensor entities by name or entity_id. "
                        "Use this when the user asks 'what sensors do we have about power' or 'find the pool temperature sensor'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "A sensor name or keyword, e.g. 'pool temperature'.",
                            }
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_ha_recall",
                    "description": (
                        "Free-form recall across the stored Home Assistant memory for devices and sensors. "
                        "Use this for broad questions like 'what sensors are about power' or 'pool devices'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The question or keyword to recall.",
                            }
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_ha_history",
                    "description": (
                        "Returns recent persisted snapshots of the Home Assistant state from memory. "
                        "Use this when the user asks what changed, what the state was earlier, or for a history of the home."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 168,
                                "description": "How many hours back to include. Defaults to 24.",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 50,
                                "description": "Maximum snapshots to return. Defaults to 10.",
                            }
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_ha_get_device_confidence",
                    "description": (
                        "Returns controllable devices grouped by user confidence: trusted_working, "
                        "trusted_broken, learning, or needs_integration. "
                        "Each device also includes a safety level: safe, caution, or dangerous. "
                        "Use this when the user asks what is broken, what needs setup, what is new, "
                        "what is trusted, or what is dangerous."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                }, {
                    "name": "ada_ha_set_device_confidence",
                    "description": (
                        "Set a controllable device's confidence and/or safety status. "
                        "Use this when the user says a device is broken, new, trusted, dangerous, safe, or needs caution."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The exact Home Assistant entity_id.",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["trusted_working", "trusted_broken", "learning", "needs_integration"],
                                "description": "The confidence level to assign.",
                            },
                            "safety": {
                                "type": "string",
                                "enum": ["safe", "caution", "dangerous"],
                                "description": "The safety level to assign. Use this to mark devices that are dangerous or safe.",
                            }
                        },
                        "required": ["entity_id", "status"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_session_recall",
                    "description": (
                        "Recall previous voice conversations. Use ONLY when the user asks "
                        "'what did we talk about', 'do you remember', or wants something "
                        "from an earlier conversation — not for fact lookup (use "
                        "ada_memory_search bank='all' for that). "
                        "Pick the group or bank that best matches the topic. "
                        "The recall runs in the background and can take up to ~20 seconds; "
                        "the result will be spoken when ready."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The recall question, e.g. 'what did we discuss in the previous session?'.",
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
                        },
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_memory_search",
                    "description": (
                        "Search curated memory banks for stored facts, preferences, people, "
                        "procedures, and notes — the FIRST tool for any factual lookup; "
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
                    "name": "ada_remember",
                    "description": (
                        "Store or update a curated memory in a bank when the user says "
                        "'remember that…' or states a fact worth keeping. "
                        "Find-then-update: if you pass a key or a subject+attribute pair that "
                        "matches an existing memory, it is corrected in place; otherwise a new "
                        "memory is created. Pass supersedes=<key> to replace a misattributed "
                        "fact with a new one. Some banks require confirmed=true — only set it "
                        "after the user has explicitly confirmed the write."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "bank": {
                                "type": "string",
                                "description": "Memory bank name ({writable_banks}).",
                            },
                            "text": {
                                "type": "string",
                                "description": "The fact or note to store, phrased as a standalone sentence.",
                            },
                            "key": {
                                "type": "string",
                                "description": "Existing document key to correct in place (from ada_memory_search). Omit to find-or-create.",
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
                                "enum": ["fact", "preference", "person", "procedure", "note"],
                                "description": "Memory kind. Defaults to note.",
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
                        },
                        "required": ["bank", "text"],
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
                        },
                        "required": ["bank", "key", "outcome"],
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
                    "name": "calendar_list_calendars",
                    "description": (
                        "Lists every calendar across the configured providers (Google, etc.) "
                        "with provider, id, and which provider receives new events by default. "
                        "Use this when the user asks which calendars exist or before writing to "
                        "a non-default calendar."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "calendar_list_events",
                    "description": (
                        "Lists calendar events for a day or range across all configured "
                        "providers, sorted by start time. Use this FIRST whenever the user asks "
                        "about their schedule, what's next, or whether they're free."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "day": {
                                "type": "string",
                                "description": "Day to list: 'today', 'tomorrow', or ISO date 'YYYY-MM-DD'. Default 'today'.",
                            },
                            "days": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 31,
                                "description": "Number of days to span starting at day. Default 1; use 7 for 'this week'.",
                            },
                            "query": {
                                "type": "string",
                                "description": "Optional text filter on event titles, e.g. 'dentist'.",
                            },
                            "calendar": {
                                "type": "string",
                                "description": "Optional 'provider:calendar_id' to read one calendar only (from calendar_list_calendars).",
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "calendar_freebusy",
                    "description": (
                        "Returns busy time slots across all configured providers for a day or range. "
                        "Use this for 'am I free between X and Y' or finding an open slot."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "day": {
                                "type": "string",
                                "description": "Day to check: 'today', 'tomorrow', or ISO date 'YYYY-MM-DD'. Default 'today'.",
                            },
                            "days": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 31,
                                "description": "Number of days to span. Default 1.",
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "plan_day",
                    "description": (
                        "Merged view of one day's calendar events plus all open tasks — the data "
                        "for 'plan my day' or 'what does tomorrow look like'. Read-only; speak the "
                        "proposed plan but never create events without approval."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "day": {
                                "type": "string",
                                "description": "Day to plan: 'today', 'tomorrow', or ISO date 'YYYY-MM-DD'. Default 'today'.",
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "calendar_create_event",
                    "description": (
                        "Creates a calendar event on the default write provider (or a named "
                        "'provider:calendar_id'). Always restate title/date/time to the user and "
                        "get an explicit yes, then pass confirmed=true."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Event title as the user phrased it.",
                            },
                            "start": {
                                "type": "string",
                                "description": "Start as ISO 8601 datetime ('2026-09-22T14:00:00') or date ('2026-09-22') for all-day.",
                            },
                            "end": {
                                "type": "string",
                                "description": "End as ISO 8601 datetime or date (exclusive for all-day).",
                            },
                            "notes": {
                                "type": "string",
                                "description": "Optional event description/notes.",
                            },
                            "location": {
                                "type": "string",
                                "description": "Optional location string.",
                            },
                            "calendar": {
                                "type": "string",
                                "description": "Optional 'provider:calendar_id' to write a non-default calendar.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                        },
                        "required": ["title", "start", "end"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "calendar_delete_event",
                    "description": (
                        "Deletes a calendar event by its provider-qualified id "
                        "(e.g. 'google:primary/abc123', from calendar_list_events). "
                        "Always confirm the event with the user first, then pass confirmed=true."
                    ),
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "event_id": {
                                "type": "string",
                                "description": "Provider-qualified event id exactly as returned by calendar_list_events.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                        },
                        "required": ["event_id"],
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
                        },
                        "required": ["task_id"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "cms_list_pages",
                    "description": (
                        "List the pages in the user's miniapp. Returns each page's "
                        "slug, title, format, and last-updated timestamp."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "Max pages to return (default 50).",
                            },
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "cms_get_page",
                    "description": (
                        "Read one miniapp page by slug — returns its title, format, "
                        "and full content. Use before updating a page."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "slug": {
                                "type": "string",
                                "description": "Page slug, e.g. 'pool-notes' (from cms_list_pages).",
                            },
                        },
                        "required": ["slug"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "cms_verify_page",
                    "description": (
                        "Verify a published miniapp page — re-reads it and checks the "
                        "content parses for its declared format, returning a structural "
                        "summary (title, sections/items, headings, slide count). You "
                        "cannot see the rendered site, so call this after publishing "
                        "or updating a page."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "slug": {
                                "type": "string",
                                "description": "Page slug to verify (from cms_list_pages).",
                            },
                        },
                        "required": ["slug"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "cms_publish_page",
                    "description": (
                        "Create or fully replace a page in the user's miniapp. The slug "
                        "is the page's URL-friendly id; publishing an existing slug "
                        "overwrites it. Restate the slug, title, and what will change, "
                        "get an explicit yes, then call with confirmed=true."
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
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                        },
                        "required": ["slug", "title", "content"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "cms_delete_page",
                    "description": (
                        "Delete a miniapp page by slug. Restate which page will be "
                        "removed, get an explicit yes, then call with confirmed=true."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "slug": {
                                "type": "string",
                                "description": "Page slug to delete (from cms_list_pages).",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
                            },
                        },
                        "required": ["slug"],
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
                                "description": "Repository the session works in.",
                            },
                            "task": {
                                "type": "string",
                                "description": "The task prompt for the Devin session.",
                            },
                            "confirmed": {
                                "type": "boolean",
                                "description": "Required; set true only after explicit user confirmation.",
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
                        },
                        "required": ["task_id", "message"],
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
                    "get_home_state to check occupancy and the state of the configured home plugs, ",
                    "",
                )
                .replace(
                    "When the user asks about devices, occupancy, or what is on/off, call get_home_state or list_home_devices first. ",
                    "When the user asks about devices, occupancy, or what is on/off, call list_home_devices or ada_ha_get_state first. ",
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
        config["system_instruction"] += await self._session_context_tail(excluded)
        # Per-instance memory banks: descriptions name only banks this
        # instance can actually use ({banks}=readable, {writable_banks}=
        # writable), so the model never calls a bank that fails loudly.
        all_banks, writable_banks = self._memory_bank_names()
        config["tools"][0]["function_declarations"] = _fill_bank_placeholders(
            config["tools"][0]["function_declarations"], all_banks, writable_banks
        )
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

    async def send_text_turn(self, text: str) -> None:
        if self._session is None:
            raise RuntimeError("provider is not connected")
        async with self._send_lock:
            await self._session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=text)],
                ),
                turn_complete=True,
            )

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

        while not self._closed:
            async for message in self._session.receive():
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
                    for call in tool_call.function_calls:
                        logger.info(
                            "session=%s function_call received id=%s name=%s args=%r",
                            self.session_id, call.id, call.name, call.args,
                        )
                        yield ProviderEvent("tool_call", {
                            "name": str(call.name),
                            "args": _safe_args(call.args),
                        })
                        requested = (call.args or {}).get("expression")
                        if call.name == "set_facial_expression" and requested in EXPRESSION_NAMES:
                            yield ProviderEvent("expression", {"name": requested})
                            result = {"output": f"Ada is now {requested}"}
                        elif call.name == "get_home_state" and self.home_assistant_client is not None:
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
                        elif call.name == "list_home_devices" and self.home_assistant_client is not None:
                            try:
                                devices = await self.home_assistant_client.entities()
                                result = {"output": devices}
                            except Exception as exc:
                                result = {"error": f"list_home_devices failed: {exc}"}
                        elif call.name == "search_home_devices" and self.home_assistant_client is not None:
                            try:
                                query = (call.args or {}).get("query", "")
                                devices = await self.home_assistant_client.search_entities(str(query))
                                result = {"output": devices}
                            except Exception as exc:
                                result = {"error": f"search_home_devices failed: {exc}"}
                        elif call.name == "list_sensors" and self.home_assistant_client is not None:
                            try:
                                sensors = await self.home_assistant_client.sensors(limit=50)
                                result = {"output": sensors}
                            except Exception as exc:
                                result = {"error": f"list_sensors failed: {exc}"}
                        elif call.name == "search_sensors" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                query = args.get("query", "")
                                sensors = await self.home_assistant_client.sensors(search=str(query), limit=10)
                                result = {"output": sensors}
                            except Exception as exc:
                                result = {"error": f"search_sensors failed: {exc}"}
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
                        elif call.name == "get_sensor_history" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                entity_id = args.get("entity_id")
                                hours = int(args.get("hours", 24))
                                if not entity_id:
                                    result = {"error": "entity_id is required"}
                                else:
                                    history = await self.home_assistant_client.history(entity_id, hours=hours)
                                    result = {"output": history}
                            except Exception as exc:
                                result = {"error": f"get_sensor_history failed: {exc}"}
                        elif call.name == "report_habit_observation":
                            args = dict(call.args or {})
                            yield ProviderEvent("habit_observation", args)
                            result = {"output": "Observation delivered to the habit monitor"}
                        elif call.name == "get_habit_status" and self.habit_state_getter is not None:
                            result = {"output": self.habit_state_getter()}
                        elif call.name == "ada_session_recall":
                            if time.monotonic() - self._strong_hit_at < self._recall_gate_window:
                                result = {"output": (
                                    "ada_memory_search already returned a confident match "
                                    "moments ago — answer from those hits. Session recall "
                                    "skipped (redundant).")}
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
                        elif call.name == "ada_decision_check" and self.tool_runner is not None:
                            result = {"output": self._start_decision_check(dict(call.args or {}))}
                        else:
                            if self.tool_runner is not None:
                                try:
                                    call_args = dict(call.args or {})
                                    if call.name == "ada_memory_search":
                                        q = call_args.get("query")
                                        if q:
                                            call_args["query"] = self.conversation.expand_query(str(q))
                                    output = await self.tool_runner.execute(str(call.name), call_args)
                                    result = {"output": output}
                                    if call.name == "ada_memory_search" and isinstance(output, dict):
                                        hits = output.get("hits") or []
                                        top = float(hits[0].get("score") or 0) if hits else 0.0
                                        if top >= self._recall_gate_score:
                                            self._strong_hit_at = time.monotonic()
                                except Exception as exc:
                                    result = {"error": f"{call.name} failed: {exc}"}
                            else:
                                result = {"error": "Unsupported or unavailable function"}
                        yield ProviderEvent("tool_result", {
                            "name": str(call.name),
                            "result": _safe_args(result) if isinstance(result, dict) else {"value": str(result)[:500]},
                        })
                        function_responses.append(types.FunctionResponse(
                            id=call.id,
                            name=call.name or "set_facial_expression",
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
                    yield ProviderEvent("response_interrupted", {})
                    # Gemini 3.1 can include several content parts in one event.
                    # Any audio/transcript accompanying an interruption belongs
                    # to the cancelled response and must not restart playback.
                    continue

                transcription = content.input_transcription
                if transcription and transcription.text:
                    input_transcript += transcription.text

                output_transcription = content.output_transcription
                if output_transcription and output_transcription.text:
                    if not self._response_active:
                        self._response_active = True
                        response_started_at = time.monotonic()
                        response_audio_chunks = 0
                        response_audio_bytes = 0
                        yield ProviderEvent("response_started", {})
                    assistant_turn_text += output_transcription.text
                    yield ProviderEvent(
                        "assistant_transcript_delta",
                        {"text": output_transcription.text},
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
                        self.conversation.add_user(transcript)
                        yield ProviderEvent("user_transcript", {"text": transcript})
                    input_transcript = ""
                    if assistant_turn_text.strip():
                        self.conversation.add_assistant(assistant_turn_text)
                        assistant_turn_text = ""
                    if self._response_active:
                        elapsed = time.monotonic() - response_started_at if response_started_at else 0.0
                        logger.info(
                            "session=%s assistant response completed duration_ms=%d audio_chunks=%d audio_bytes=%d",
                            self.session_id, elapsed * 1000, response_audio_chunks, response_audio_bytes,
                        )
                        yield ProviderEvent("response_completed", {})
                    self._response_active = False

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
                    conversation: ConversationMemory | None = None) -> RealtimeProvider:
    return GeminiLiveProvider(
        instructions=instructions,
        tool_runner=tool_runner,
        home_assistant_client=home_assistant_client,
        habit_state_getter=habit_state_getter,
        session_id=session_id,
        conversation=conversation,
    )
