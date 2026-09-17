"""Conversation memory and NotebookLM recall for Ada voice sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger("voice.conversation")

NOTEBOOKLM_BASE_URL = os.environ.get(
    "NOTEBOOKLM_REST_BASE_URL", "http://127.0.0.1:3011/v1"
)
NOTEBOOKLM_API_KEY = os.environ.get("NOTEBOOKLM_REST_API_KEY")
NOTEBOOKLM_NOTEBOOK_ID = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID")

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
        await self.client.add_text_source(title, self.transcript())

    def start_recall(
        self,
        question: str,
        on_complete: Callable[[str | None], Awaitable[None]],
        group: str | None = None,
    ) -> str:
        if not self.client.configured:
            return "My notes are not connected."
        asyncio.create_task(self._recall(question, on_complete, group))
        return "One moment, I'm checking my notes."

    async def _recall(
        self,
        question: str,
        on_complete: Callable[[str | None], Awaitable[None]],
        group: str | None = None,
    ) -> None:
        answer = await self.client.ask(question, group=group)
        if answer:
            await on_complete(answer)
        else:
            await on_complete("I couldn't find anything in my notes.")
