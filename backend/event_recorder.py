"""Background Home Assistant state_changed recorder with MDDB persistence.

Subscribes to the HA websocket ``state_changed`` stream, keeps a bounded
in-memory ring of transitions (open/close, on/off, home/away, lock/unlock),
and periodically persists batches to MDDB so events survive restarts and are
searchable through the ada_ha memory tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import websockets

logger = logging.getLogger("ha.events")

DEFAULT_WATCHED_DOMAINS = {
    "binary_sensor", "cover", "lock", "person", "device_tracker",
    "alarm_control_panel", "light", "switch", "fan", "input_boolean",
    "media_player",
}


def _source_slug(base_url: str) -> str:
    return (
        str(base_url).rstrip("/")
        .replace("://", "-")
        .replace("/", "-")
        .replace(":", "-")
        .replace(".", "-")
    )


class HaEventRecorder:
    """Records HA state transitions in memory and persists batches to MDDB."""

    def __init__(
        self,
        ha_client: Any,
        mddb_client: Any = None,
        watched_domains: set[str] | None = None,
        max_events: int = 2000,
        flush_interval: float = 30.0,
        flush_batch: int = 20,
    ) -> None:
        self.ha_client = ha_client
        self.mddb = mddb_client
        self.watched_domains = watched_domains or DEFAULT_WATCHED_DOMAINS
        self.collection = "ada-ha-events-" + _source_slug(ha_client.base_url)
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._pending: list[dict[str, Any]] = []
        self.flush_interval = flush_interval
        self.flush_batch = flush_batch
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_connected_at: str | None = None

    @property
    def enabled(self) -> bool:
        return os.environ.get("DISABLE_EVENT_RECORDER") != "true"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "collection": self.collection,
            "events_buffered": len(self._events),
            "pending_flush": len(self._pending),
            "watched_domains": sorted(self.watched_domains),
            "last_connected_at": self._last_connected_at,
        }

    async def start(self) -> bool:
        """Start the background listener. Returns True when a task was spawned."""
        if not self.enabled or self._task is not None:
            return False
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("ha event recorder started (collection=%s)", self.collection)
        return True

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._flush()

    async def _run(self) -> None:
        backoff = 5.0
        while self._running:
            try:
                await self._listen()
                backoff = 5.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("ha event recorder disconnected: %s", exc)
            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _listen(self) -> None:
        await self.ha_client._ensure_access_token()
        base = (
            self.ha_client.base_url
            .replace("http://", "ws://")
            .replace("https://", "wss://")
        )
        ws_url = f"{base.rstrip('/')}/api/websocket"
        async with websockets.connect(ws_url) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"unexpected websocket hello: {hello}")
            await ws.send(json.dumps({
                "type": "auth",
                "access_token": self.ha_client._access_token,
            }))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
            if ack.get("type") != "auth_ok":
                raise RuntimeError(f"websocket auth failed: {ack}")
            await ws.send(json.dumps({
                "id": 1,
                "type": "subscribe_events",
                "event_type": "state_changed",
            }))
            result = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
            if result.get("type") != "result" or not result.get("success"):
                raise RuntimeError(f"subscribe_events failed: {result}")
            self._last_connected_at = datetime.now(timezone.utc).isoformat()
            logger.info("ha event recorder subscribed to state_changed")
            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.flush_interval)
                except asyncio.TimeoutError:
                    await self._flush()
                    continue
                message = json.loads(raw)
                if message.get("type") == "event":
                    self.record(message.get("event", {}))
                if len(self._pending) >= self.flush_batch:
                    await self._flush()

    def record(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Record one HA state_changed event dict; returns the stored entry or None."""
        data = event.get("data", {}) if isinstance(event, dict) else {}
        new_state = data.get("new_state")
        old_state = data.get("old_state")
        if not isinstance(new_state, dict):
            return None
        entity_id = str(new_state.get("entity_id", ""))
        domain, _, _ = entity_id.partition(".")
        if not entity_id or domain not in self.watched_domains:
            return None
        new_value = str(new_state.get("state", ""))
        old_value = str(old_state.get("state", "")) if isinstance(old_state, dict) else ""
        if new_value == old_value:
            return None
        attributes = new_state.get("attributes") if isinstance(new_state.get("attributes"), dict) else {}
        at = event.get("time_fired") or new_state.get("last_changed") or datetime.now(timezone.utc).isoformat()
        entry = {
            "at": str(at),
            "entity_id": entity_id,
            "domain": domain,
            "name": str(attributes.get("friendly_name") or entity_id),
            "device_class": str(attributes.get("device_class") or ""),
            "from": old_value,
            "to": new_value,
        }
        self._events.append(entry)
        self._pending.append(entry)
        return entry

    def recent(
        self,
        hours: float = 24,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Newest-first in-memory transitions, filtered by window and keyword."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        q = (query or "").strip().lower()
        results = []
        for entry in reversed(self._events):
            parsed = self._parse_at(entry.get("at"))
            if parsed is not None and parsed < cutoff:
                break
            if q:
                haystack = " ".join([
                    entry.get("entity_id", ""),
                    entry.get("name", ""),
                    entry.get("from", ""),
                    entry.get("to", ""),
                ]).lower()
                if q not in haystack:
                    continue
            results.append(entry)
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _parse_at(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    async def _flush(self) -> None:
        """Persist pending events as one MDDB markdown document."""
        if not self._pending or self.mddb is None:
            self._pending = []
            return
        batch, self._pending = self._pending, []
        now = datetime.now(timezone.utc)
        lines = [
            f"# Home Assistant events — {self.ha_client.base_url}",
            "",
        ]
        for entry in batch:
            lines.append(
                f"- {entry['at']} — {entry['name']} (`{entry['entity_id']}`): "
                f"{entry['from']} → {entry['to']}"
            )
        key = f"events-{_source_slug(self.ha_client.base_url)}-{now.strftime('%Y%m%d%H%M%S%f')}"
        await self.mddb.add_document(
            collection=self.collection,
            key=key,
            lang="en",
            content_md="\n".join(lines),
            meta={
                "source": [str(self.ha_client.base_url)],
                "kind": ["events"],
                "count": [str(len(batch))],
                "period_start": [batch[0]["at"]],
                "period_end": [batch[-1]["at"]],
            },
        )
        logger.debug("ha event recorder flushed %d events to mddb", len(batch))
