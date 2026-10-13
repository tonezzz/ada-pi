"""OpenRouter text fallback provider — degraded-mode chat when Gemini
Live can't connect (billing dunning, quota, outage).

Card: ada-chat-or-fallback. Voice sessions have no equivalent path and
fail loudly as before; text channels (telegram/line/pwa) degrade here
instead of dying at provider connect.

Contract: same RealtimeProvider surface — send_text_turn() feeds the
chat history, emits response_started -> assistant_transcript_delta ->
response_completed so the session loop needs zero special-casing.
Tool surface is OFF in degraded mode (no function calling); the client
is told once via a {"type":"degraded"} frame at fallback time.

Context vs user turns: session priming and system notes also arrive via
send_text_turn. They stay in history as context but must NOT trigger a
completion — Live lets the model decide; a plain chat API can't. The
heuristic below covers every context prefix the session loop emits.

Config:
  OPENROUTER_API_KEY            required — same key the fleet rotates
  ADA_OR_FALLBACK_MODEL         model id, or comma-separated fallback
                                chain tried in order (default
                                nvidia/nemotron-3-super-120b-a12b:free)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, AsyncIterator

import httpx

from .realtime_provider import (
    ProviderEvent,
    RealtimeProvider,
    DEFAULT_ADA_INSTRUCTIONS,
    _now_context,
)

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

# send_text_turn payloads that are context injections, not user messages
# (session prime, speaker notes, chaba guest context, channel mirrors).
_CONTEXT_PREFIXES = ("(system)", "Session note:", "Recent sessions:")


def _is_context_turn(text: str) -> bool:
    t = text.lstrip()
    return t.startswith(_CONTEXT_PREFIXES) or "\n(system)" in text


class OpenRouterTextProvider(RealtimeProvider):
    """Minimal text-only provider — chat completions, no tools, no audio."""

    def __init__(self, instructions: str | None = None,
                 tool_runner: Any = None,
                 session_id: str | None = None,
                 conversation: Any = None,
                 channel: str | None = None) -> None:
        self.channel = channel
        self.tool_runner = tool_runner
        self.session_id = session_id or "unknown"
        self.conversation = conversation
        self.models = [m.strip() for m in os.environ.get(
            "ADA_OR_FALLBACK_MODEL", ",".join(DEFAULT_MODELS)).split(",")
            if m.strip()]
        self.model = self.models[0] if self.models else DEFAULT_MODELS[0]
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self._queue: asyncio.Queue[ProviderEvent] = asyncio.Queue()
        self._closed = False
        self._pending: asyncio.Task | None = None
        self._ops_collection: Any = None
        self._ops_events_sent = 0
        self._history: list[dict] = [{
            "role": "system",
            # Same clock injection as the Live path — degraded sessions
            # otherwise have no 'now' and hallucinate dates (Kaewta wrong
            # date 2026-10-13 while ada-kk sat on OpenRouter during
            # free-key 1011s).
            "content": (instructions or DEFAULT_ADA_INSTRUCTIONS)
            + _now_context()
            + "\n\n" + DEGRADED_NOTE,
        }]
        self._response_active = False
        self.session_owner = None
        self.conversation_memory = conversation

    async def connect(self, resumption_handle: str | None = None) -> None:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set — degraded "
                               "fallback unavailable")

    async def send_audio(self, pcm16: bytes) -> None:
        return  # text-only lane

    async def send_video(self, jpeg: bytes) -> None:
        return  # text-only lane

    async def send_image_turn(self, image: bytes, mime: str,
                              caption: str = "") -> None:
        # Vision is offline in degraded mode; a caption still gets an
        # answer so the turn isn't silently dropped.
        self._history.append({
            "role": "system",
            "content": "(system) The user sent an image — vision is "
                       "offline in degraded mode, you cannot see it."
                       + (f" Caption: {caption}" if caption else "")})
        if caption.strip():
            self._history.append({"role": "user", "content": caption})
            await self._kick()

    async def send_text_turn(self, text: str) -> None:
        if self._closed:
            return
        if _is_context_turn(text):
            self._history.append({"role": "system", "content": text})
            return
        self._history.append({"role": "user", "content": text})
        await self._kick()

    async def _kick(self) -> None:
        if self._pending and not self._pending.done():
            return  # a reply is already in flight; user text is in history
        self._pending = asyncio.create_task(self._complete())

    async def _complete(self) -> None:
        started = time.monotonic()
        reply = ""
        last_err: Exception | None = None
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                for model in self.models:
                    try:
                        r = await client.post(
                            OR_URL,
                            headers={
                                "Authorization": f"Bearer {self.api_key}",
                                "Content-Type": "application/json"},
                            json={"model": model,
                                  "messages": self._history[-24:],
                                  "max_tokens": 600})
                        r.raise_for_status()
                        reply = (r.json().get("choices") or [{}])[0] \
                            .get("message", {}).get("content") or ""
                        reply = reply.strip()
                        if reply:
                            self.model = model
                            break
                    except Exception as exc:
                        last_err = exc
                        logger.warning(
                            "session=%s openrouter model %s failed: %s",
                            self.session_id, model, exc)
                if not reply and last_err is not None:
                    raise last_err
            reply = reply.strip()
            reply = reply.strip()
            if not reply:
                return
            self._history.append({"role": "assistant", "content": reply})
            if self.conversation is not None:
                try:
                    self.conversation.add_assistant(reply)
                except Exception:
                    pass
            self._response_active = True
            await self._queue.put(ProviderEvent("response_started", {}))
            # Emit in chunks so the client renders progressively.
            step = 48
            for i in range(0, len(reply), step):
                if self._closed:
                    break
                await self._queue.put(ProviderEvent(
                    "assistant_transcript_delta", {"text": reply[i:i + step]}))
            self._response_active = False
            await self._queue.put(ProviderEvent("response_completed", {}))
            self._emit_ops_event(
                "or_fallback_reply",
                f"degraded reply via {self.model} "
                f"({time.monotonic() - started:.1f}s)")
        except Exception as exc:  # degrade further: surface the failure
            self._response_active = False
            logger.warning("session=%s openrouter turn failed: %s",
                           self.session_id, exc)
            await self._queue.put(ProviderEvent(
                "response_interrupted", {"reason": f"openrouter: {exc}"}))

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
