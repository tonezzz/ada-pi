"""FastAPI-style tool runner for Ada's Home Assistant, memory, and habit tools."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.home_assistant import HomeAssistantClient

logger = logging.getLogger("tools")

MDDB_BASE_URL = os.environ.get("MDDB_BASE_URL", "http://127.0.0.1:11023/v1")


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


class MddbClient:
    """Thin async client for the MDDB v1 HTTP API."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or MDDB_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))

    async def add_document(
        self,
        collection: str,
        key: str,
        lang: str,
        content_md: str,
        meta: dict[str, list[str]] | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "collection": collection,
            "key": key,
            "lang": lang,
            "contentMd": content_md,
        }
        if meta:
            payload["meta"] = meta
        try:
            resp = await self._client.post(f"{self.base_url}/add", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("mddb add_document failed: %s", exc)
            return None

    async def search_documents(
        self,
        collection: str,
        query: str,
        filter_meta: dict[str, list[str]] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "collection": collection,
            "query": query,
            "limit": limit,
        }
        if filter_meta:
            payload["filter_meta"] = filter_meta
        try:
            resp = await self._client.post(f"{self.base_url}/search", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("mddb search failed: %s", exc)
            return []


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
    ) -> None:
        self.ha_client = ha_client
        self.mddb = mddb_client
        self._collection = (
            "ada-ha-snapshots-"
            + str(ha_client.base_url).rstrip("/")
            .replace("://", "-")
            .replace("/", "-")
            .replace(":", "-")
            .replace(".", "-")
        )
        self._confidence_collection = (
            "ada-ha-device-confidence-"
            + str(ha_client.base_url).rstrip("/")
            .replace("://", "-")
            .replace("/", "-")
            .replace(":", "-")
            .replace(".", "-")
        )
        self._devices: list[dict[str, Any]] | None = None
        self._sensors: list[dict[str, Any]] | None = None
        self._overview: dict[str, Any] | None = None
        self._confidence: dict[str, str] = {}
        self._confidence_groups: dict[str, list[dict[str, Any]]] | None = None
        self._last_refresh: datetime | None = None

    async def _ensure(self) -> None:
        if self._devices is not None:
            return
        await self.refresh()

    async def refresh(self) -> None:
        states = await self.ha_client._states()
        controllable = await self.ha_client.entities()
        sensors = await self.ha_client.sensors(limit=200)
        self._devices = controllable
        self._sensors = sensors
        known = await self._load_confidence()
        self._classify_and_sync(controllable, known)
        self._overview = {
            "person_entity": self.ha_client.person_entity,
            "home_plugs": list(self.ha_client.home_plug_entities),
            "total_entities": len(states),
            "controllable_count": len(controllable),
            "sensor_count": len(sensors),
            "source": self.ha_client.base_url,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._last_refresh = datetime.now(timezone.utc)
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
        source = str(self.ha_client.base_url).rstrip("/").replace("://", "-").replace("/", "-")
        key = f"snapshot-{source}-{ts}"
        content_md = self._build_content_md(states, controllable, sensors)
        await self.mddb.add_document(
            collection=self._collection,
            key=key,
            lang="en",
            content_md=content_md,
            meta={
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

    def _classify_and_sync(self, devices: list[dict[str, Any]], known: dict[str, str]) -> None:
        groups: dict[str, list[dict[str, Any]]] = {
            "trusted_working": [],
            "trusted_broken": [],
            "learning": [],
            "needs_integration": [],
        }
        for dev in devices:
            eid = dev.get("entity_id")
            state = str(dev.get("state", "unknown"))
            available = bool(dev.get("available"))
            status = known.get(eid) if eid else None
            if status not in groups:
                status = None
            if not available or state in {"unavailable", "unknown"}:
                status = "trusted_broken"
            elif status is None:
                status = "needs_integration"
            dev["confidence"] = status
            groups.setdefault(status, []).append(dev)
        self._confidence = {d.get("entity_id", ""): d.get("confidence", "needs_integration") for d in devices if d.get("entity_id")}
        self._confidence_groups = groups

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
                "source": [str(self.ha_client.base_url)],
            },
        )

    async def set_confidence(self, entity_id: str, status: str) -> str:
        if status not in {"trusted_working", "trusted_broken", "learning", "needs_integration"}:
            raise ValueError(f"Invalid confidence status: {status}")
        if self._devices is None:
            await self.refresh()
        for dev in self._devices or []:
            if dev.get("entity_id") == entity_id:
                dev["confidence"] = status
                break
        if self._confidence_groups is not None:
            # Rebuild groups quickly
            for group in self._confidence_groups.values():
                for dev in group:
                    if dev.get("entity_id") == entity_id:
                        dev["confidence"] = status
                        break
        self._confidence[entity_id] = status
        if self.mddb is not None:
            await self._save_confidence(entity_id, status)
        return f"{entity_id} is now {status}"

    def confidence_groups(self) -> dict[str, list[dict[str, Any]]]:
        return self._confidence_groups or {
            "trusted_working": [],
            "trusted_broken": [],
            "learning": [],
            "needs_integration": [],
        }

    def _match(self, query: str, items: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
        q = str(query).lower()
        matches = []
        for item in items:
            text = " ".join(str(item.get(k, "")).lower() for k in keys)
            if q in text:
                matches.append(item)
        return matches

    async def overview(self) -> dict[str, Any]:
        await self._ensure()
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

    def __init__(self, ha_client: HomeAssistantClient, habit_state_getter: Any | None = None) -> None:
        self.context = ToolContext(ha_client=ha_client, habit_state_getter=habit_state_getter)
        self.mddb = MddbClient()
        self.memory = AdaMemoryStore(ha_client, mddb_client=self.mddb)

    async def execute(self, name: str, args: dict[str, Any] | None = None) -> Any:
        method = getattr(self, name, None)
        if not method:
            raise KeyError(f"Unknown tool: {name}")
        return await method(**(args or {}))

    # -- Home Assistant tools --

    async def get_home_state(self) -> dict[str, Any]:
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

    async def list_home_devices(self) -> list[dict[str, Any]]:
        return await self.context.ha_client.entities()

    async def search_home_devices(self, query: str) -> list[dict[str, Any]]:
        return await self.context.ha_client.search_entities(str(query))

    async def control_entity(self, entity_id: str, on: bool) -> str:
        if not entity_id or not isinstance(on, bool):
            raise ValueError("entity_id and on are required")
        outcome = await self.context.ha_client.set_power(entity_id, on)
        return f"Turned {'on' if on else 'off'} {entity_id}: {outcome}"

    async def list_sensors(self) -> list[dict[str, Any]]:
        return await self.context.ha_client.sensors(limit=50)

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

    async def tv_action(self, cmd: str, text: str = "") -> dict[str, Any]:
        if not cmd:
            raise ValueError("cmd is required")
        return await self.context.ha_client.tv_action(cmd, text)

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
        return await self.memory.search_all(str(query), int(limit))

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

    async def ada_ha_get_device_confidence(self) -> dict[str, list[dict[str, Any]]]:
        """Return controllable devices grouped by user confidence."""
        await self.memory._ensure()
        return self.memory.confidence_groups()

    async def ada_ha_set_device_confidence(self, entity_id: str, status: str) -> str:
        """Set a device's confidence status."""
        return await self.memory.set_confidence(str(entity_id), str(status))
