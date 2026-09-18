"""Conversation memory and NotebookLM recall for Ada voice sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger("voice.conversation")

NOTEBOOKLM_BASE_URL = os.environ.get(
    "NOTEBOOKLM_REST_BASE_URL", "http://127.0.0.1:3011/v1"
)
NOTEBOOKLM_API_KEY = os.environ.get("NOTEBOOKLM_REST_API_KEY")
NOTEBOOKLM_NOTEBOOK_ID = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID")

# Speak a second filler line if recall exceeds this many seconds.
RECALL_SLOW_AFTER_S = float(os.environ.get("ADA_RECALL_SLOW_AFTER_S", "10"))

# Rolling "recent sessions" summary, persisted to MDDB so it survives
# restarts. Updated locally after each persisted transcript (fast Gemini
# text call, ~3s); seeded once from NotebookLM on first use so history
# predating this feature is covered. Recalls answer with the cached
# summary immediately while NotebookLM handles the detail query.
_SUMMARY_QUESTION = (
    "Briefly summarize what we discussed in our recent sessions, "
    "in two or three sentences."
)
_SUMMARY_KEY = "recent-sessions"
_SUMMARY_MAX_TRANSCRIPT_CHARS = int(
    os.environ.get("ADA_SUMMARY_MAX_TRANSCRIPT_CHARS", "8000")
)
_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
_SUMMARY_MODEL = os.environ.get("ADA_SUMMARY_MODEL", "gemini-2.5-flash-lite")
_summary_cache: dict[str, Any] = {"text": None, "ts": 0.0, "task": None}


def _summary_collection() -> str:
    try:
        from backend.instance import ada_instance_id
        instance = ada_instance_id()
    except RuntimeError:
        instance = os.environ.get("ADA_INSTANCE_ID", "unknown")
    return f"ada-ha-recall-summary-{instance}"


def _mddb():
    from backend.tool_runner import MddbClient  # lazy: avoid import cycle
    return MddbClient()


async def _save_summary_to_mddb(text: str) -> None:
    try:
        await _mddb().add_document(
            collection=_summary_collection(),
            key=_SUMMARY_KEY,
            lang="en",
            content_md=text,
            meta={"kind": ["recall-summary"], "source": ["conversation_memory"]},
        )
    except Exception as exc:
        logger.warning("summary mddb save failed: %s", exc)


async def _load_summary_from_mddb() -> str | None:
    try:
        docs = await _mddb().search_documents(
            collection=_summary_collection(),
            query="*",
            filter_meta={"kind": ["recall-summary"]},
            limit=1,
        )
    except Exception as exc:
        logger.warning("summary mddb load failed: %s", exc)
        return None
    if not docs:
        return None
    text = docs[0].get("contentMd") or docs[0].get("content_md") or ""
    return str(text).strip() or None


async def _summarize(prev: str | None, transcript: str) -> str | None:
    """Update the rolling summary with one new session via Gemini REST."""
    if not _GEMINI_API_KEY:
        return None
    try:
        from google import genai
        prompt = (
            "Existing summary of our recent voice sessions:\n"
            f"{prev or '(none yet)'}\n\n"
            "Transcript of the latest session:\n"
            f"{transcript[-_SUMMARY_MAX_TRANSCRIPT_CHARS:]}\n\n"
            "Update the summary to cover the recent sessions in two or three "
            "sentences. Keep it concise and factual."
        )
        resp = await genai.Client(api_key=_GEMINI_API_KEY).aio.models.generate_content(
            model=_SUMMARY_MODEL, contents=prompt
        )
        return (resp.text or "").strip() or None
    except Exception as exc:
        logger.warning("summary update failed: %s", exc)
        return None


async def _refresh_summary(client: "NotebooklmClient") -> None:
    """One-time seed from NotebookLM; rolling updates take over afterwards."""
    try:
        answer = await client.ask(_SUMMARY_QUESTION, group="memory")
    except Exception as exc:
        logger.warning("summary refresh failed: %s", exc)
        return
    if answer:
        _summary_cache["text"] = answer
        _summary_cache["ts"] = time.time()
        await _save_summary_to_mddb(answer)

# Optional per-group routing: {"memory": "<nb-id>", "infra": "<nb-id>", ...}
# Falls back to NOTEBOOKLM_NOTEBOOK_ID for any group not in the map.
try:
    NOTEBOOKLM_NOTEBOOK_IDS: dict[str, str] = json.loads(
        os.environ.get("NOTEBOOKLM_NOTEBOOK_IDS_JSON", "") or "{}"
    )
except json.JSONDecodeError:
    NOTEBOOKLM_NOTEBOOK_IDS = {}


class NotebooklmClient:
    """Async client for the tony-dell NotebookLM REST API."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        notebook_id: str | None = None,
    ) -> None:
        self.base_url = (base_url or NOTEBOOKLM_BASE_URL).rstrip("/")
        self.api_key = api_key or NOTEBOOKLM_API_KEY
        self.notebook_id = notebook_id or NOTEBOOKLM_NOTEBOOK_ID
        self.notebooks = dict(NOTEBOOKLM_NOTEBOOK_IDS)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(180.0))
        self._headers: dict[str, str] = {}
        if self.api_key:
            self._headers["X-API-Key"] = self.api_key

    def notebook_for(self, group: str | None) -> str | None:
        """Resolve a recall group to a notebook id; default is the memory group."""
        if group and group in self.notebooks:
            return self.notebooks[group]
        return self.notebooks.get("memory") or self.notebook_id

    @property
    def configured(self) -> bool:
        return bool(self.api_key and (self.notebook_id or self.notebooks))

    async def add_text_source(self, title: str, content: str) -> dict[str, Any] | None:
        notebook = self.notebook_for("memory")
        if not self.configured or not notebook:
            return None
        try:
            resp = await self._client.post(
                f"{self.base_url}/notebooks/{notebook}/sources/text",
                headers=self._headers,
                json={"title": title, "content": content},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("notebooklm add source failed: %s", exc)
            return None

    async def ask(self, question: str, group: str | None = None) -> str | None:
        notebook = self.notebook_for(group)
        if not self.configured or not notebook:
            return None
        try:
            resp = await self._client.post(
                f"{self.base_url}/notebooks/{notebook}/chat/ask",
                headers=self._headers,
                json={"question": question},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                return None
            result = data.get("result", {})
            if isinstance(result, dict):
                return result.get("answer") or result.get("response")
            if isinstance(result, str):
                return result
            return None
        except Exception as exc:
            logger.warning("notebooklm ask failed: %s", exc)
            return None


class ConversationMemory:
    """Captures a voice session transcript and can persist/recall it via NotebookLM."""

    def __init__(
        self,
        session_id: str,
        client: NotebooklmClient | None = None,
    ) -> None:
        self.session_id = session_id
        self.client = client or NotebooklmClient()
        self._turns: list[dict[str, str]] = []
        self._recall_task: asyncio.Task | None = None
        self.warm_summary()

    def add_user(self, text: str) -> None:
        if text.strip():
            self._turns.append({"role": "user", "text": text.strip()})

    def add_assistant(self, text: str) -> None:
        if text.strip():
            self._turns.append({"role": "assistant", "text": text.strip()})

    def transcript(self) -> str:
        lines = [f"# Ada voice session {self.session_id}", ""]
        for turn in self._turns:
            role = "User" if turn["role"] == "user" else "Ada"
            lines.append(f"## {role}")
            lines.append(turn["text"])
            lines.append("")
        return "\n".join(lines)

    async def persist(self) -> None:
        if not self._turns or not self.client.configured:
            return
        title = f"Ada session {self.session_id} {datetime.now(timezone.utc).isoformat()}"
        result = await self.client.add_text_source(title, self.transcript())
        if result is not None:
            # New history exists — fold it into the rolling summary.
            _summary_cache["task"] = asyncio.create_task(self._update_summary())

    async def _update_summary(self) -> None:
        prev = _summary_cache["text"] or await _load_summary_from_mddb()
        new = await _summarize(prev, self.transcript())
        if new:
            _summary_cache["text"] = new
            _summary_cache["ts"] = time.time()
            await _save_summary_to_mddb(new)

    def warm_summary(self) -> None:
        """Kick a background warm: load the persisted summary, or seed it."""
        if _summary_cache["text"] is not None:
            return
        task = _summary_cache.get("task")
        if task is not None and not task.done():
            return
        try:
            _summary_cache["task"] = asyncio.create_task(self._warm_summary())
        except RuntimeError:
            pass  # no running loop (e.g. unit tests)

    async def _warm_summary(self) -> None:
        text = await _load_summary_from_mddb()
        if text:
            _summary_cache["text"] = text
            _summary_cache["ts"] = time.time()
            return
        if self.client.configured:
            await _refresh_summary(self.client)

    def start_recall(
        self,
        question: str,
        on_complete: Callable[[str | None], Awaitable[None]],
        group: str | None = None,
        on_slow: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        if not self.client.configured:
            return "My notes are not connected."
        if self._recall_task is not None and not self._recall_task.done():
            return "I'm still checking my notes."
        self.warm_summary()
        self._recall_task = asyncio.create_task(
            self._recall(question, on_complete, group, on_slow)
        )
        summary = _summary_cache["text"]
        if summary and group in (None, "memory"):
            return (
                "The detailed notes search is running in the background and "
                "the result will be spoken when ready. Meanwhile, tell the "
                "user this summary of recent sessions: "
                f"{summary}"
            )
        return "One moment, I'm checking my notes."

    async def _recall(
        self,
        question: str,
        on_complete: Callable[[str | None], Awaitable[None]],
        group: str | None = None,
        on_slow: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        ask = asyncio.create_task(self.client.ask(question, group=group))
        if on_slow is not None:
            done, _ = await asyncio.wait({ask}, timeout=RECALL_SLOW_AFTER_S)
            if not done:
                try:
                    await on_slow()
                except Exception as exc:
                    logger.warning("recall slow filler failed: %s", exc)
        answer = await ask
        if answer:
            await on_complete(answer)
        else:
            await on_complete("I couldn't find anything in my notes.")
