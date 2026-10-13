"""Text providers — two modes on one class (card ada-text-mode-sessions).

DEGRADED (tools=False, unchanged): OpenRouter free-model chain when Gemini
Live can't connect — no tools, caption-only images, DEGRADED_NOTE.

TEXT MODE (tools=True): relay text sessions that never open a Gemini Live
session at all. Primary transport is Gemini REST through
gemini_pool.generate() — rides the LiteLLM gateway (ADA_GEMINI_REST_*)
with paid->free->OR->ollama chains and per-lane spend tags, so relay text
is metered per call instead of billed as realtime audio minutes. The tool
loop reuses GeminiLiveProvider._session_config() on a throwaway probe —
identical lane-filtered declarations + system instruction, no schema
fork. OpenRouter remains the fallback when Gemini REST fails.

Contract: same RealtimeProvider surface — send_text_turn feeds history,
emits response_started -> assistant_transcript_delta -> response_completed,
plus tool_call/tool_result frames the ws loop already forwards.

Config:
  OPENROUTER_API_KEY            required for the OR fallback leg
  ADA_OR_FALLBACK_MODEL         model id or comma chain (OR leg)
  ADA_TEXT_MODEL                Gemini REST model — default
                                gemini-3.5-flash-lite (gateway-routed)
  ADA_GEMINI_REST_BASE_URL/_API_KEY  LiteLLM gateway transport
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any, AsyncIterator

import httpx

from . import gemini_pool
from .realtime_provider import (
    ProviderEvent,
    RealtimeProvider,
    DEFAULT_ADA_INSTRUCTIONS,
    _now_context,
    _safe_args,
)
from .tool_runner import normalize_tool_result

logger = logging.getLogger(__name__)

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
# 2026-10-11 probe: google-hosted gemma :free models 400 upstream on this
# account ("API key not valid" from the provider); nvidia/liquid/apodex
# :free serve fine. Chain ordered big→small.
DEFAULT_MODELS = ("nvidia/nemotron-3-super-120b-a12b:free",
                  "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
                  "liquid/lfm-2.5-2.6b:free")

DEGRADED_NOTE = (
    "(system) DEGRADED MODE: the Gemini voice/live backend is unreachable "
    "right now — you are answering through a plain text model with NO "
    "tools (no smart-home, memory, kanban, or screens). Answer plainly "
    "from what you know, say you can't reach your tools when asked to do "
    "them, and keep replies short. Do not pretend actions happened."
)

TEXT_MODE_NOTE = (
    "(system) TEXT MODE: you are answering over a plain REST transport — "
    "there is no voice session behind this chat. Your tools work exactly "
    "as documented: call them for actions/lookups; results come back as "
    "function responses. Keep replies plain text for a chat client. If a "
    "tool fails, say so honestly — never pretend an action happened."
)

# send_text_turn payloads that are context injections, not user messages
# (session prime, speaker notes, chaba guest context, channel mirrors).
_CONTEXT_PREFIXES = ("(system)", "Session note:", "Recent sessions:")

# Per-turn ceilings — same spirit as the Live budgets (tool storms cost
# money here too). Live also caps actuations; REST turns keep it simple:
# the call budget bounds the whole loop.
_TOOL_BUDGET = 10
_TOOL_HOPS = 12


def _is_context_turn(text: str) -> bool:
    t = text.lstrip()
    return t.startswith(_CONTEXT_PREFIXES) or "\n(system)" in text


class OpenRouterTextProvider(RealtimeProvider):
    """Text-only provider — OR degraded chain, or Gemini REST + tools."""

    def __init__(self, instructions: str | None = None,
                 tool_runner: Any = None,
                 session_id: str | None = None,
                 conversation: Any = None,
                 channel: str | None = None,
                 tools: bool = False,
                 caller_name: str | None = None,
                 caller_person: str | None = None) -> None:
        self.channel = channel
        self.tool_runner = tool_runner
        self.session_id = session_id or "unknown"
        self.conversation = conversation
        self.tools_enabled = tools
        # Identity pins — same semantics as GeminiLiveProvider: owner is
        # bound at connect (the API-key person); the speaker may personalize
        # but never upgrades permissions.
        self.caller_name = caller_name
        self.caller_person = caller_person
        self.session_owner = caller_person or caller_name
        self.current_speaker_ha_person = getattr(
            tool_runner, "current_speaker_ha_person", None)
        self.speaker_session = None  # pwa_server may attach one; unused w/o audio
        self.models = [m.strip() for m in os.environ.get(
            "ADA_OR_FALLBACK_MODEL", ",".join(DEFAULT_MODELS)).split(",")
            if m.strip()]
        self.model = self.models[0] if self.models else DEFAULT_MODELS[0]
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "")
        # Gateway model CLASS, not a raw model name — the lane virtual
        # keys allow alias classes only (403 key_model_access_denied on
        # real names). tool-call rides gemini-3.5-flash-lite paid->free->OR.
        self._text_model = os.environ.get("ADA_TEXT_MODEL", "tool-call")
        self._queue: asyncio.Queue[ProviderEvent] = asyncio.Queue()
        self._closed = False
        self._pending: asyncio.Task | None = None
        self._ops_collection: Any = None
        self._ops_events_sent = 0
        # Transport-neutral history: {"role": user|model|system|tool,
        # "parts": [...]}. system = context injections; tool =
        # function_response parts. Converted per transport at call time.
        self._turns: list[dict] = []
        self._system = (
            (instructions or DEFAULT_ADA_INSTRUCTIONS)
            + _now_context()
            + "\n\n" + (TEXT_MODE_NOTE if tools else DEGRADED_NOTE)
        )
        self._gemini_decls: list[dict] | None = None   # None = not harvested
        self._probe: Any = None
        self._response_active = False
        self.conversation_memory = conversation

    # ------------------------------------------------------------------ connect

    def _gemini_ok(self) -> bool:
        return bool(os.environ.get("ADA_GEMINI_REST_BASE_URL", "").strip()
                    or os.environ.get("GEMINI_API_KEY", "").strip())

    async def connect(self, resumption_handle: str | None = None) -> None:
        if not self.tools_enabled:
            if not self.api_key:
                raise RuntimeError("OPENROUTER_API_KEY not set — degraded "
                                   "fallback unavailable")
            return
        # Text mode: harvest the lane's real config on a Live-provider
        # probe — no session is opened; _session_config only builds dicts
        # (and writes the tool-decls cache file).
        from .realtime_provider import GeminiLiveProvider
        probe = GeminiLiveProvider(
            tool_runner=self.tool_runner, session_id=self.session_id,
            conversation=self.conversation, caller_name=self.caller_name,
            caller_person=self.caller_person, channel=self.channel)
        try:
            cfg = await probe._session_config()
            self._probe = probe
            decls = (cfg.get("tools") or [{}])[0].get(
                "function_declarations") or []
            self._gemini_decls = [d for d in decls if isinstance(d, dict)]
            # Lane instruction set already carries identity + context tail —
            # reuse it verbatim so a text session sounds identical to Live.
            self._system = ((cfg.get("system_instruction") or self._system)
                            + "\n\n" + TEXT_MODE_NOTE)
            logger.info(
                "session=%s text mode: %d lane tools declared (model=%s)",
                self.session_id, len(self._gemini_decls), self._text_model)
        except Exception as exc:
            logger.warning(
                "session=%s decl harvest failed: %s — tools off this "
                "session", self.session_id, exc)
            self._gemini_decls = []
        if not self._gemini_ok() and not self.api_key:
            raise RuntimeError(
                "text mode: no transport — ADA_GEMINI_REST_BASE_URL/"
                "GEMINI_API_KEY and OPENROUTER_API_KEY are all unset")

    # ------------------------------------------------------------------ input

    async def send_audio(self, pcm16: bytes) -> None:
        return  # text-only lane

    async def send_video(self, jpeg: bytes) -> None:
        return  # text-only lane

    async def send_image_turn(self, image: bytes, mime: str,
                              caption: str = "") -> None:
        if self.tools_enabled and self._gemini_ok():
            parts = [{"inline_data": {
                "mime_type": mime or "image/jpeg",
                "data": base64.b64encode(image).decode()}}]
            if caption.strip():
                parts.append({"text": caption})
            self._turns.append({"role": "user", "parts": parts})
            await self._kick()
            return
        # Degraded: vision is offline; a caption still gets an answer.
        self._turns.append({"role": "system", "parts": [{"text":
            "(system) The user sent an image — vision is offline in "
            "degraded mode, you cannot see it."
            + (f" Caption: {caption}" if caption else "")}]})
        if caption.strip():
            self._turns.append({"role": "user",
                                "parts": [{"text": caption}]})
            await self._kick()

    async def send_text_turn(self, text: str) -> None:
        if self._closed:
            return
        if _is_context_turn(text):
            self._turns.append({"role": "system",
                                "parts": [{"text": text}]})
            return
        self._turns.append({"role": "user", "parts": [{"text": text}]})
        await self._kick()

    # ------------------------------------------------------------------ turns

    async def _kick(self) -> None:
        if self._pending and not self._pending.done():
            return  # a reply is already in flight; user text is in history
        self._pending = asyncio.create_task(self._complete())

    async def _complete(self) -> None:
        started = time.monotonic()
        reply: str | None = None
        last_err: Exception | None = None
        try:
            if self.tools_enabled and self._gemini_ok():
                try:
                    reply = await self._gemini_turn()
                except Exception as exc:
                    last_err = exc
                    logger.warning(
                        "session=%s gemini text turn failed: %s — "
                        "trying openrouter", self.session_id, exc)
                    self._emit_ops_event(
                        "text_mode_fallback",
                        f"gemini REST turn failed: {exc}")
            if reply is None:
                reply = await self._or_turn(last_err)
            reply = (reply or "").strip()
            if not reply:
                return
            self._turns.append({"role": "model",
                                "parts": [{"text": reply}]})
            if self.conversation is not None:
                try:
                    self.conversation.add_assistant(reply)
                except Exception:
                    pass
            self._response_active = True
            await self._queue.put(ProviderEvent("response_started", {}))
            step = 48
            for i in range(0, len(reply), step):
                if self._closed:
                    break
                await self._queue.put(ProviderEvent(
                    "assistant_transcript_delta", {"text": reply[i:i + step]}))
            self._response_active = False
            await self._queue.put(ProviderEvent("response_completed", {}))
            self._emit_ops_event(
                "text_mode_reply" if self.tools_enabled else "or_fallback_reply",
                f"text reply via {self._text_model if self.tools_enabled else self.model} "
                f"({time.monotonic() - started:.1f}s)")
        except Exception as exc:
            self._response_active = False
            logger.warning("session=%s text turn failed: %s",
                           self.session_id, exc)
            await self._queue.put(ProviderEvent(
                "response_interrupted", {"reason": f"text: {exc}"}))

    # ---- history renderers -------------------------------------------------

    def _contents(self) -> list[dict]:
        """self._turns -> Gemini contents. Consecutive same-role turns are
        merged (the API wants alternating roles); system notes and tool
        responses both ride as 'user'."""
        out: list[dict] = []
        for t in self._turns:
            role = "model" if t["role"] == "model" else "user"
            parts = list(t["parts"])
            if out and out[-1]["role"] == role:
                out[-1]["parts"].extend(parts)
            else:
                out.append({"role": role, "parts": parts})
        return out

    def _or_messages(self) -> list[dict]:
        """self._turns -> OpenAI messages for the OR leg (no tool schema —
        tool history flattens to text so context survives the fallback)."""
        msgs = [{"role": "system", "content": self._system}]
        if self.tools_enabled:
            msgs.append({"role": "system", "content":
                         "(system) Tools are unavailable on this fallback "
                         "turn — answer plainly without them."})
        for t in self._turns:
            text = ""
            for p in t["parts"]:
                if "text" in p:
                    text += p["text"] + "\n"
                if "inline_data" in p:
                    text += "(image attached — vision unavailable)\n"
                fc = p.get("function_call")
                if fc:
                    text += (f"(called tool {fc.get('name')}("
                             f"{json.dumps(fc.get('args') or {})[:400]}))\n")
                fr = p.get("function_response")
                if fr:
                    text += (f"(tool {fr.get('name')} returned: "
                             f"{json.dumps(fr.get('response'), default=str)[:800]})\n")
            role = {"system": "system",
                    "model": "assistant"}.get(t["role"], "user")
            if text.strip():
                msgs.append({"role": role, "content": text.strip()})
        return msgs[-24:]

    # ---- Gemini REST tool loop --------------------------------------------

    async def _gemini_turn(self) -> str:
        from google.genai import types as gtypes
        contents = self._contents()
        cfg_kw: dict[str, Any] = {"system_instruction": self._system}
        if self._gemini_decls:
            cfg_kw["tools"] = [{"function_declarations": self._gemini_decls}]
        calls_made = 0
        for _hop in range(_TOOL_HOPS):
            resp = await gemini_pool.generate(
                model=self._text_model, contents=contents,
                config=gtypes.GenerateContentConfig(**cfg_kw),
                tool="text-relay", session_id=self.session_id)
            cands = getattr(resp, "candidates", None) or []
            if not cands:
                raise RuntimeError("gemini text turn: no candidates")
            content = cands[0].content
            parts = list(getattr(content, "parts", None) or [])
            fcalls = [p.function_call for p in parts
                      if getattr(p, "function_call", None)]
            text = "".join(p.text or "" for p in parts
                           if getattr(p, "text", None))
            if not fcalls:
                return text
            # Echo the model turn (with its function_call parts) so the
            # next hop sees its own calls in context.
            mparts: list[dict] = []
            for p in parts:
                if getattr(p, "text", None):
                    mparts.append({"text": p.text})
                elif getattr(p, "function_call", None):
                    fc = p.function_call
                    mparts.append({"function_call": {
                        "name": str(fc.name),
                        "args": dict(fc.args or {})}})
            self._turns.append({"role": "model", "parts": mparts})
            contents.append({"role": "model", "parts": mparts})
            resp_parts: list[dict] = []
            for fc in fcalls:
                name = str(fc.name)
                args = dict(fc.args or {})
                # Self-asserted 'confirmed' never counts — the only legit
                # replay path is a bound confirm_token (Live parity).
                args.pop("confirmed", None)
                fid = str(getattr(fc, "id", None) or "")
                await self._queue.put(ProviderEvent("tool_call", {
                    "id": fid, "name": name, "args": _safe_args(args)}))
                calls_made += 1
                if calls_made > _TOOL_BUDGET:
                    result = {"ok": False, "error":
                              "Tool budget for this turn exhausted — stop "
                              "calling tools and answer from what you have."}
                elif self._probe is not None and self._probe._tool_excluded(name):
                    result = {"error": "Unsupported or unavailable function"}
                else:
                    try:
                        owner = self.session_owner
                        out = await self.tool_runner.execute(
                            name, args,
                            identity=(owner
                                      or self.current_speaker_ha_person),
                            speaker=self.current_speaker_ha_person,
                            speaker_session=self.speaker_session,
                            owner=owner,
                            session=self.session_id)
                        result = normalize_tool_result(
                            out if isinstance(out, dict)
                            else {"ok": True, "output": out})
                    except Exception as exc:
                        result = {"ok": False,
                                  "error": f"{name} failed: {exc}"}
                if self.conversation is not None:
                    try:
                        self.conversation.log_event(
                            "tool_call", tool=name,
                            ok=bool(result.get("ok", True)))
                    except Exception:
                        pass
                await self._queue.put(ProviderEvent("tool_result", {
                    "id": fid, "name": name,
                    "result": _safe_args(result) if isinstance(result, dict)
                            else {"value": str(result)[:500]}}))
                resp_parts.append({"function_response": {
                    "name": name, "response": result}})
            self._turns.append({"role": "tool", "parts": resp_parts})
            contents.append({"role": "user", "parts": resp_parts})
        return ("I ran into my tool-call limit for this turn — please "
                "repeat the request if it still needs doing.")

    # ---- OpenRouter leg ----------------------------------------------------

    async def _or_turn(self, last_err: Exception | None = None) -> str | None:
        reply = ""
        async with httpx.AsyncClient(timeout=60) as client:
            for model in self.models:
                try:
                    r = await client.post(
                        OR_URL,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json"},
                        json={"model": model,
                              "messages": self._or_messages(),
                              "max_tokens": 600})
                    r.raise_for_status()
                    reply = (r.json().get("choices") or [{}])[0] \
                        .get("message", {}).get("content") or ""
                    reply = reply.strip()
                    if reply:
                        self.model = model
                        return reply
                except Exception as exc:
                    last_err = exc
                    logger.warning(
                        "session=%s openrouter model %s failed: %s",
                        self.session_id, model, exc)
        if last_err is not None:
            raise last_err
        return None

    # ------------------------------------------------------------------ misc

    def _emit_ops_event(self, ev_type: str, detail: str,
                        tool: str | None = None) -> None:
        """Same signature as GeminiLiveProvider — fire-and-forget ops note."""
        if self._ops_events_sent >= 5:
            return
        self._ops_events_sent += 1
        tr = self.tool_runner
        if tr is not None and getattr(tr, "event_log", None) is not None:
            tr.event_log.append({"at": time.time(), "kind": ev_type,
                                 "detail": detail})

    def events(self) -> AsyncIterator[ProviderEvent]:
        async def _gen():
            while not self._closed:
                try:
                    ev = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    yield ev
                except asyncio.TimeoutError:
                    continue
        return _gen()

    async def close(self) -> None:
        self._closed = True
        if self._pending and not self._pending.done():
            self._pending.cancel()
