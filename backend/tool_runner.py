"""FastAPI-style tool runner for Ada's Home Assistant, memory, and habit tools."""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.event_recorder import HaEventRecorder
from backend.home_assistant import HomeAssistantClient
from backend.instance import ada_instance_id
from backend.memory_banks import (
    MemoryBankRegistry,
    _slug,
    doc_effective_status,
    get_registry,
)

logger = logging.getLogger("tools")

MDDB_BASE_URL = os.environ.get("MDDB_BASE_URL", "http://127.0.0.1:11023/v1")

# Tools that physically actuate a device. These are gated server-side:
# dangerous devices require confirmed=true, all are rate-limited, and all
# are blocked entirely when ADA_READ_ONLY=true.
CONTROL_TOOLS = {
    "control_entity", "control_cover", "press_button",
    "control_media_player", "tv_action",
}

# Tools that mutate curated memory banks. Each bank's write_policy decides
# whether confirmed=true is required (same gate pattern as CONTROL_TOOLS).
MEMORY_WRITE_TOOLS = {"ada_remember", "ada_forget"}

# Minimum vector-search score for a bank hit to count as confident. Below
# this, recall escalates to the NotebookLM deep tier. ~0.45-0.55 is the
# observed "real match" band on this MDDB's embedding model.
ADA_BANK_SEARCH_THRESHOLD = float(os.environ.get("ADA_BANK_SEARCH_THRESHOLD", "0.45"))

# Higher bar for "this is the same fact restated" — when ada_remember finds
# no exact key/subject match, a hit above this score is corrected in place
# instead of creating a near-duplicate.
ADA_BANK_UPDATE_THRESHOLD = float(os.environ.get("ADA_BANK_UPDATE_THRESHOLD", "0.85"))
CONTROL_RATE_WINDOW_S = float(os.environ.get("ADA_CONTROL_RATE_WINDOW_S", "60"))
CONTROL_MAX_PER_ENTITY = int(os.environ.get("ADA_CONTROL_MAX_PER_ENTITY", "5"))
CONTROL_MAX_GLOBAL = int(os.environ.get("ADA_CONTROL_MAX_GLOBAL", "30"))

# LLM callers occasionally use a synonym for a declared parameter. Map the
# alias to the real name only when the method declares it and the caller did
# not already pass the canonical name.
_ARG_ALIASES = {"question": "query"}


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
            logger.error("mddb add_document failed: %s", exc)
            return None

    async def search_documents(
        self,
        collection: str,
        query: str = "*",
        filter_meta: dict[str, list[str]] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Meta-filtered listing. NOTE: /v1/search has no text-query support —
        `query` is accepted for call-site compatibility but ignored by the
        server. Use vector_search() for real text/semantic queries."""
        payload: dict[str, Any] = {
            "collection": collection,
            "limit": limit,
        }
        if filter_meta:
            payload["filterMeta"] = filter_meta
        try:
            resp = await self._client.post(f"{self.base_url}/search", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("mddb search failed: %s", exc)
            return []

    async def vector_search(
        self,
        collection: str,
        query: str,
        limit: int = 5,
        filter_meta: dict[str, list[str]] | None = None,
        threshold: float = 0.0,
    ) -> list[dict[str, Any]] | None:
        """Semantic search via /v1/vector-search. Returns None on failure so
        callers can fall back to search_documents listing."""
        payload: dict[str, Any] = {
            "collection": collection,
            "query": query,
            "topK": limit,
            "includeContent": True,
        }
        if filter_meta:
            payload["filterMeta"] = filter_meta
        if threshold:
            payload["threshold"] = threshold
        try:
            resp = await self._client.post(
                f"{self.base_url}/vector-search", json=payload
            )
            resp.raise_for_status()
            results = resp.json().get("results") or []
            return [
                {**(item.get("document") or {}), "score": item.get("score")}
                for item in results
            ]
        except Exception as exc:
            logger.error("mddb vector_search failed: %s", exc)
            return None

    async def get_document(
        self, collection: str, key: str, lang: str = "en"
    ) -> dict[str, Any] | None:
        try:
            resp = await self._client.post(
                f"{self.base_url}/get",
                json={"collection": collection, "key": key, "lang": lang},
            )
            if resp.status_code == 404 or (
                resp.status_code == 400 and "not found" in resp.text.lower()
            ):
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("mddb get_document failed: %s", exc)
            return None

    async def update_document(
        self,
        collection: str,
        key: str,
        lang: str = "en",
        content_md: str | None = None,
        meta: dict[str, list[str]] | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {"collection": collection, "key": key, "lang": lang}
        if content_md is not None:
            payload["contentMd"] = content_md
        if meta is not None:
            payload["meta"] = meta
        try:
            resp = await self._client.patch(f"{self.base_url}/update", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("mddb update_document failed: %s", exc)
            return None


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

            # Default safety: covers and shutters are dangerous; everything else cautious until marked safe.
            raw_safety = known_safety.get(eid) if eid else None
            if raw_safety not in safety_groups:
                raw_safety = None
            if raw_safety is None:
                if isinstance(eid, str) and eid.startswith("cover."):
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
        self.mddb = MddbClient()
        self.memory = AdaMemoryStore(ha_client, mddb_client=self.mddb, instance_id=instance_id)
        self.events = HaEventRecorder(ha_client, mddb_client=self.mddb, instance_id=instance_id)
        self._instance_id = instance_id
        self._banks: MemoryBankRegistry | None = None
        self._control_calls: list[float] = []
        self._control_entity_calls: dict[str, list[float]] = {}

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

    async def execute(self, name: str, args: dict[str, Any] | None = None) -> Any:
        method = getattr(self, name, None)
        if not method:
            raise KeyError(f"Unknown tool: {name}")
        call_args = dict(args or {})
        if name in CONTROL_TOOLS:
            confirmed = call_args.pop("confirmed", None)
            await self._check_control_allowed(name, call_args, confirmed)
        elif name in MEMORY_WRITE_TOOLS:
            confirmed = call_args.pop("confirmed", None)
            self._check_memory_write_allowed(name, call_args, confirmed)
        call_args = self._normalize_args(name, method, call_args)
        logger.info("tool %s args=%r", name, call_args)
        return await method(**call_args)

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

    async def _check_control_allowed(
        self, name: str, args: dict[str, Any], confirmed: Any,
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
            calls = self._control_entity_calls.setdefault(entity_id, [])
            calls[:] = [t for t in calls if now - t < CONTROL_RATE_WINDOW_S]
            if len(calls) >= CONTROL_MAX_PER_ENTITY:
                logger.warning("denied %s %r: per-entity rate limit", name, args)
                raise PermissionError(
                    f"rate limit exceeded for {entity_id}: more than {CONTROL_MAX_PER_ENTITY} "
                    f"control calls in {int(CONTROL_RATE_WINDOW_S)}s"
                )
            await self.memory._ensure_confidence()
            safety = self.memory._safety.get(entity_id)
            if safety != "dangerous":
                # A related entity can inherit danger: button.gate_motor_my_position
                # physically jogs the dangerous cover.gate_motor.
                object_id = entity_id.split(".", 1)[-1]
                for eid, level in self.memory._safety.items():
                    if level == "dangerous" and object_id.startswith(eid.split(".", 1)[-1] + "_"):
                        safety = "dangerous"
                        break
            if safety == "dangerous" and confirmed is not True:
                logger.warning("denied %s %r: dangerous device without confirmed=true", name, args)
                raise PermissionError(
                    f"{entity_id} is marked dangerous. Call again with confirmed=true "
                    "only after explicit user confirmation."
                )
            calls.append(now)
        self._control_calls.append(now)

    def _check_memory_write_allowed(
        self, name: str, args: dict[str, Any], confirmed: Any,
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
        if not bank.writable:
            logger.warning("denied %s on %r: bank not writable", name, bank_name)
            raise PermissionError(f"memory bank '{bank_name}' is read-only")
        if name not in bank.allowed_tools:
            logger.warning("denied %s on %r: tool not in allowed_tools", name, bank_name)
            raise PermissionError(f"tool {name} is not allowed on memory bank '{bank_name}'")
        if bank.write_policy == "confirmed" and confirmed is not True:
            logger.warning("denied %s on %r: write_policy=confirmed without confirmed=true", name, bank_name)
            raise PermissionError(
                f"memory bank '{bank_name}' requires confirmation. Call again with "
                "confirmed=true only after explicit user confirmation."
            )

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

    async def get_recent_events(self, hours: int = 24, query: str | None = None, limit: int = 50) -> dict[str, Any]:
        return await self.context.ha_client.recent_events(
            hours=int(hours),
            query=str(query) if query else None,
            limit=int(limit),
        )

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

    async def ada_ha_get_device_confidence(self) -> dict[str, list[dict[str, Any]]]:
        """Return controllable devices grouped by user confidence."""
        await self.memory._ensure_confidence()
        return self.memory.confidence_groups()

    async def ada_ha_set_device_confidence(self, entity_id: str, status: str, safety: str | None = None) -> str:
        """Set a device's confidence and/or safety status."""
        return await self.memory.set_confidence(str(entity_id), str(status), str(safety) if safety is not None else None)

    # -- Memory bank tools (curated memory; see ssot.apps.ada-memory-*.yml) --

    @staticmethod
    def _memory_meta(
        bank: Any,
        scope: str,
        today: str,
        kind: str | None,
        subject: str | None,
        attribute: str | None,
        valid_until: str | None,
        applies_to: list[str],
    ) -> dict[str, list[str]]:
        meta: dict[str, list[str]] = {
            "bank": [bank.name],
            "kind": [str(kind or "note")],
            "scope": [scope],
            "status": ["active"],
            "valid_from": [today],
            "last_verified": [today],
            "source": ["voice"],
            "written_by": ["ada_remember"],
        }
        if subject:
            meta["subject"] = [str(subject)]
        if attribute:
            meta["attribute"] = [str(attribute)]
        if valid_until:
            meta["valid_until"] = [str(valid_until)]
        if applies_to:
            meta["applies_to"] = [str(a) for a in applies_to]
        return meta

    async def ada_memory_search(
        self,
        bank: str,
        query: str,
        limit: int = 5,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        """Search a curated memory bank's MDDB collection.

        A real query uses semantic (vector) search; '*' or empty lists by
        metadata filter. Vector search failures fall back to the listing so
        recall degrades gracefully when embeddings are down."""
        b = self.banks.bank(str(bank))
        filter_meta = None if include_inactive else {"status": ["active"]}
        q = str(query or "").strip()
        degraded = False
        if q and q != "*":
            docs = await self.mddb.vector_search(
                collection=b.mddb_collection,
                query=q,
                limit=int(limit) * 3,
                filter_meta=filter_meta,
                threshold=ADA_BANK_SEARCH_THRESHOLD,
            )
            if docs is None:
                degraded = True
                docs = await self.mddb.search_documents(
                    collection=b.mddb_collection,
                    filter_meta=filter_meta,
                    limit=int(limit) * 3,
                )
        else:
            docs = await self.mddb.search_documents(
                collection=b.mddb_collection,
                filter_meta=filter_meta,
                limit=int(limit) * 3,
            )
        hits = []
        for doc in docs or []:
            status = doc_effective_status(doc)
            if not include_inactive and status != "active":
                continue
            meta = doc.get("meta") or {}
            hits.append({
                "key": doc.get("key"),
                "status": status,
                "score": doc.get("score"),
                "content": doc.get("contentMd") or doc.get("content_md") or "",
                "kind": _first(meta.get("kind")),
                "subject": _first(meta.get("subject")),
                "attribute": _first(meta.get("attribute")),
                "last_verified": _first(meta.get("last_verified")),
                "valid_until": _first(meta.get("valid_until")),
            })
            if len(hits) >= int(limit):
                break
        return {
            "bank": b.name,
            "collection": b.mddb_collection,
            "count": len(hits),
            "hits": hits,
            "degraded": degraded,
        }

    async def ada_remember(
        self,
        bank: str,
        text: str,
        key: str | None = None,
        subject: str | None = None,
        attribute: str | None = None,
        kind: str | None = None,
        valid_until: str | None = None,
        applies_to: list[str] | str | None = None,
        supersedes: str | None = None,
    ) -> dict[str, Any]:
        """Write a memory to a bank: create, correct-in-place, or supersede."""
        b = self.banks.bank(str(bank))
        if str(kind or "note") not in b.kinds:
            raise ValueError(
                f"kind {kind!r} not allowed in bank '{b.name}' (allowed: {', '.join(b.kinds)})"
            )
        today = datetime.now(timezone.utc).date().isoformat()
        scope = "shared" if b.scope == "shared" else self.memory.instance
        applies = [applies_to] if isinstance(applies_to, str) else list(applies_to or [])

        if supersedes:
            old = await self.mddb.get_document(b.mddb_collection, str(supersedes))
            if old is None:
                raise ValueError(
                    f"cannot supersede {supersedes!r}: no such document in bank '{b.name}'"
                )
            new_key = str(key) if key else f"{b.name}/{_slug(str(subject or text))}"
            meta = self._memory_meta(b, scope, today, kind, subject, attribute, valid_until, applies)
            meta["supersedes"] = [str(supersedes)]
            await self.mddb.add_document(b.mddb_collection, new_key, "en", str(text), meta)
            old_meta = dict(old.get("meta") or {})
            old_meta["status"] = ["superseded"]
            old_meta["superseded_by"] = [new_key]
            await self.mddb.update_document(b.mddb_collection, str(supersedes), meta=old_meta)
            return {
                "verb": "supersede",
                "bank": b.name,
                "key": new_key,
                "superseded": str(supersedes),
            }

        # Find-then-update: an existing active doc about the same
        # subject/attribute (or at the requested key) is corrected in place.
        target_key = str(key) if key else None
        existing = None
        if target_key:
            existing = await self.mddb.get_document(b.mddb_collection, target_key)
        elif subject:
            filt: dict[str, list[str]] = {"subject": [str(subject)], "status": ["active"]}
            if attribute:
                filt["attribute"] = [str(attribute)]
            docs = await self.mddb.search_documents(
                b.mddb_collection, "*", filter_meta=filt, limit=1
            )
            docs = [d for d in docs if doc_effective_status(d) == "active"]
            if docs:
                target_key = docs[0].get("key")
                existing = docs[0]
        if not target_key:
            target_key = f"{b.name}/{_slug(str(subject or text))}"
            existing = await self.mddb.get_document(b.mddb_collection, target_key)

        if existing is None:
            # Dedupe: a near-identical active memory counts as the same fact —
            # correct it in place rather than stacking a duplicate.
            sims = await self.mddb.vector_search(
                collection=b.mddb_collection,
                query=str(text),
                limit=1,
                filter_meta={"status": ["active"]},
                threshold=ADA_BANK_UPDATE_THRESHOLD,
            )
            if sims and doc_effective_status(sims[0]) == "active":
                target_key = sims[0].get("key")
                existing = sims[0]
                logger.info(
                    "ada_remember dedupe: %r matched %r (score %.3f)",
                    str(text)[:60], target_key, sims[0].get("score") or 0,
                )

        meta = self._memory_meta(b, scope, today, kind, subject, attribute, valid_until, applies)
        if existing is not None:
            old_meta = dict(existing.get("meta") or {})
            for keep in ("valid_from", "supersedes", "superseded_by"):
                if keep in old_meta:
                    meta[keep] = old_meta[keep]
            if kind is None and "kind" in old_meta:
                meta["kind"] = old_meta["kind"]
            await self.mddb.update_document(
                b.mddb_collection, target_key, content_md=str(text), meta=meta
            )
            return {"verb": "correct", "bank": b.name, "key": target_key}
        await self.mddb.add_document(b.mddb_collection, target_key, "en", str(text), meta)
        return {"verb": "create", "bank": b.name, "key": target_key}

    async def ada_forget(self, bank: str, key: str, reason: str | None = None) -> dict[str, Any]:
        """Retract a memory: status becomes retracted; the doc stays auditable."""
        b = self.banks.bank(str(bank))
        doc = await self.mddb.get_document(b.mddb_collection, str(key))
        if doc is None:
            raise ValueError(f"no such document {key!r} in bank '{b.name}'")
        meta = dict(doc.get("meta") or {})
        meta["status"] = ["retracted"]
        meta["last_verified"] = [datetime.now(timezone.utc).date().isoformat()]
        if reason:
            meta["retracted_reason"] = [str(reason)]
        await self.mddb.update_document(b.mddb_collection, str(key), meta=meta)
        return {"verb": "retract", "bank": b.name, "key": str(key)}
