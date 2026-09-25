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

from backend import memory_ops
from backend import chaba_memory
from backend import devin_dispatch as devin_dispatch_mod
from backend import doc_archive_client
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
from backend.usage_tracker import usage_ledger

logger = logging.getLogger("tools")

# Tools that physically actuate a device. These are gated server-side:
# dangerous devices require confirmed=true, all are rate-limited, and all
# are blocked entirely when ADA_READ_ONLY=true.
CONTROL_TOOLS = {
    "control_entity", "control_cover", "press_button",
    "control_media_player", "tv_action",
}

# Tools that mutate curated memory banks. Each bank's write_policy decides
# whether confirmed=true is required (same gate pattern as CONTROL_TOOLS).
MEMORY_WRITE_TOOLS = {"ada_remember", "ada_forget", "ada_outcome"}

# Calendar/task writes: creating or deleting events and tasks mutates the
# user's real calendar, so all writes require confirmed=true.
CALENDAR_WRITE_TOOLS = {
    "calendar_create_event", "calendar_delete_event",
    "tasks_add", "tasks_complete",
}

# Miniapp/CMS page writes: publishing or deleting a page changes what the
# miniapp renders, so all writes require confirmed=true.
CMS_WRITE_TOOLS = {"cms_publish_page", "cms_delete_page"}

# Devin dispatch: launching an unattended agent session (devin_dispatch) or
# injecting a message into one (devin_followup) both cause autonomous code
# changes, so they require confirmed=true. devin_status is read-only.
DEVIN_CONFIRMED_TOOLS = {"devin_dispatch", "devin_followup"}

# Document archive/print: ada_doc_archive writes pages to gdrive:ada-documents
# and ada_doc_print sends real pages to the printer — both require
# confirmed=true. ada_doc_search and ada_doc_get are read-only.
DOC_CONFIRMED_TOOLS = {"ada_doc_archive", "ada_doc_print"}
# ALL doc tools (incl. read-only) are scoped to identities that can see the
# `documents` bank — the tools hit MDDB/the archive service directly and
# would otherwise bypass person_policies (e.g. a KK/guest session reading
# document metadata for an ID card).
DOC_TOOLS = DOC_CONFIRMED_TOOLS | {"ada_doc_search", "ada_doc_get"}
DOC_BANK = "documents"

CMS_COLLECTION = os.environ.get("ADA_CMS_COLLECTION", "ada-cms-pages")
CMS_FORMATS = {"markdown", "html", "yaml", "slides"}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

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
        # Shared L0 doc-work log — the provider assigns this to the
        # conversation's doc_items list so ada_doc_* calls land in the
        # session report timeline. None = doc actions not recorded.
        self.doc_log: list[dict[str, Any]] | None = None
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

    def _memory_identity(self) -> str | None:
        """Memory-policy identity: identified speaker's HA person first,
        then the session's issued-key/caller name, else None (anonymous)."""
        return self.current_speaker_ha_person or self.session_caller_name

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
        elif name in CALENDAR_WRITE_TOOLS:
            confirmed = call_args.pop("confirmed", None)
            self._check_calendar_write_allowed(name, call_args, confirmed)
        elif name in CMS_WRITE_TOOLS:
            confirmed = call_args.pop("confirmed", None)
            self._check_cms_write_allowed(name, call_args, confirmed)
        elif name in DEVIN_CONFIRMED_TOOLS:
            confirmed = call_args.pop("confirmed", None)
            self._check_devin_confirmed(name, call_args, confirmed)
        elif name in DOC_TOOLS:
            ident = self._memory_identity()
            if not self.banks.bank_allowed(DOC_BANK, ident):
                logger.warning(
                    "denied %s for identity %r: documents bank policy",
                    name, ident)
                raise PermissionError(
                    "document tools are outside this session's access policy")
            if name in DOC_CONFIRMED_TOOLS:
                confirmed = call_args.pop("confirmed", None)
                self._check_doc_confirmed(name, call_args, confirmed)
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
            ident = self._memory_identity()
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
        if not self.banks.bank_allowed(bank.name, self._memory_identity()):
            logger.warning(
                "denied %s on %r for identity %r",
                name, bank.name, self._memory_identity(),
            )
            allowed = ", ".join(
                self.banks.banks_for_person(self._memory_identity())
            )
            raise PermissionError(
                f"memory bank '{bank.name}' is not available for this speaker"
                + (f" — allowed banks: {allowed}" if allowed else "")
            )
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

    def _check_calendar_write_allowed(
        self, name: str, args: dict[str, Any], confirmed: Any,
    ) -> None:
        """Server-side gate for calendar/task writes. Raises PermissionError on denial."""
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("calendar writes are disabled (ADA_READ_ONLY=true)")
        if confirmed is not True:
            logger.warning("denied %s %r: calendar write without confirmed=true", name, args)
            raise PermissionError(
                f"{name} requires confirmation. Restate the details to the user, "
                "get an explicit yes, then call again with confirmed=true."
            )

    def _check_cms_write_allowed(
        self, name: str, args: dict[str, Any], confirmed: Any,
    ) -> None:
        """Server-side gate for miniapp page writes. Raises PermissionError on denial."""
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("CMS writes are disabled (ADA_READ_ONLY=true)")
        if confirmed is not True:
            logger.warning("denied %s %r: CMS write without confirmed=true", name, args)
            raise PermissionError(
                f"{name} requires confirmation. Restate the page slug, title, and "
                "what will change, get an explicit yes, then call again with confirmed=true."
            )

    def _check_devin_confirmed(
        self, name: str, args: dict[str, Any], confirmed: Any,
    ) -> None:
        """Server-side gate for devin dispatch/followup. Raises PermissionError on denial."""
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("devin tools are disabled (ADA_READ_ONLY=true)")
        if confirmed is not True:
            logger.warning("denied %s %r: devin call without confirmed=true", name, args)
            raise PermissionError(
                f"{name} requires confirmation. Restate the repo, task, and "
                "that an unattended Devin session will make code changes, get an "
                "explicit yes, then call again with confirmed=true."
            )

    # -- Devin dispatch tools (headless sessions on tony-dell; job SSOT:
    #    docs/ssot/jobs/ada/2026-09-22-ada-devin-dispatch.yml) --

    async def devin_dispatch(self, repo: str, task: str) -> dict[str, Any]:
        """Start an unattended Devin session on tony-dell for a task.

        Runs in a dedicated git worktree as a systemd unit; completion is
        reported back via chaba-admin event + iPhone notification.
        """
        return await devin_dispatch_mod.dispatch(repo, task)

    async def devin_status(self, task_id: str | None = None) -> str:
        """List dispatched tasks, or show one task's unit state."""
        return await devin_dispatch_mod.status(task_id or None)

    async def devin_followup(self, task_id: str, message: str) -> str:
        """Send a follow-up message into a dispatched session."""
        return await devin_dispatch_mod.followup(task_id, message)

    def _check_doc_confirmed(
        self, name: str, args: dict[str, Any], confirmed: Any,
    ) -> None:
        """Server-side gate for doc archive/print. Raises PermissionError on denial."""
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("document tools are disabled (ADA_READ_ONLY=true)")
        if confirmed is not True:
            logger.warning("denied %s %r: doc call without confirmed=true", name, args)
            raise PermissionError(
                f"{name} requires confirmation. Restate the archive/print "
                "target, get an explicit yes, then call again with confirmed=true."
            )

    # -- Document archive tools (doc-archive service on idc01 + MDDB
    #    `documents` collection — the shared Ada/Devin document index) --

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

    # -- Token usage reporting (usage_tracker.py) --

    async def ada_usage_summary(self, source: str = "all", reset: bool = False) -> dict[str, Any]:
        report = usage_ledger.snapshot(
            None if str(source).lower() in ("", "all") else str(source).lower()
        )
        if reset:
            usage_ledger.reset()
            report["reset"] = True
        return report

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

    async def plan_day(self, day: str = "today") -> dict[str, Any]:
        return await self._calendar_svc().plan_day(day=str(day))

    async def ada_resolve_action(self, key: str, resolution: str) -> str:
        """Resolve a pending action proposal (applied|dismissed). Bookkeeping
        only — not a calendar write, so no confirmed gate."""
        from backend.conversation_memory import resolve_action_proposal
        return await resolve_action_proposal(str(key), str(resolution))

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

    async def get_recent_events(self, hours: int = 24, query: str | None = None, limit: int = 25) -> dict[str, Any]:
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
    # Implementation lives in backend.memory_ops — these delegates keep the
    # dispatch surface (method names/signatures) stable.

    async def ada_memory_search(
        self,
        query: str,
        bank: str = "all",
        limit: int = 5,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        """Search a curated memory bank's MDDB collection."""
        return await memory_ops.memory_search(
            self.mddb, self.banks, bank, query, limit, include_inactive,
            person_entity=self._memory_identity(),
        )

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
        return await memory_ops.remember(
            self.mddb, self.banks, self.memory.instance,
            bank, text, key, subject, attribute, kind, valid_until, applies_to,
            supersedes, session_id=self.session_id,
            person_entity=self._memory_identity(),
        )

    async def ada_persona(
        self, action: str, knob: str | None = None, value: Any = None
    ) -> dict[str, Any]:
        """Read or adjust the current speaker's stored style preferences."""
        identity = self._memory_identity()
        action = str(action or "show").lower()
        if action == "show":
            p = await memory_ops.get_persona(self.mddb, self.banks, identity)
            return {"verb": "show", **p}
        if action == "reset":
            return await memory_ops.reset_persona(self.mddb, self.banks, identity)
        if action == "set":
            if not knob:
                raise ValueError("set requires a knob name")
            result = await memory_ops.set_persona(
                self.mddb, self.banks, identity, str(knob), value
            )
            # Tell the model to apply it now — the persisted doc covers
            # future sessions via the prime injection.
            result["apply"] = (
                f"Preference saved and active now: {knob}={value}. "
                "Honor it in your next replies without announcing the mechanism."
            )
            return result
        raise ValueError(f"unknown persona action {action!r} (set|show|reset)")

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

    async def ada_enroll_speaker(
        self,
        name: str,
        ha_person: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Enroll the current speaker's voice from buffered audio.

        Captures the last ~3.5 seconds of audio from the active
        SpeakerSession buffer — the user was just speaking, so their voice
        is already captured. Call this when the user asks to enroll their
        voice or when Ada offers enrollment.
        """
        if self.speaker_session is None:
            return {
                "error": "speaker identification is not active on this session "
                "(speaker ID may be disabled or not configured)"
            }
        if not ha_person:
            # Auto-resolve 'Name' -> person.<slug> so the enrollment maps to
            # the speaker's HA person (memory banks + actuation ACL follow)
            # even when the model doesn't pass ha_person explicitly.
            slug = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
            if slug:
                candidate = f"person.{slug}"
                try:
                    state = await self.context.ha_client.get_state(candidate)
                    if state.get("entity_id") == candidate:
                        ha_person = candidate
                except Exception:
                    pass
        try:
            result = self.speaker_session.enroll_from_buffer(
                str(name),
                ha_person=ha_person or None,
                display_name=display_name or None,
            )
            return {
                "status": "enrolled",
                "name": result.get("name"),
                "ha_person": result.get("ha_person"),
                "display_name": result.get("display_name"),
                "duration_s": result.get("duration_s"),
            }
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
        """Register the visitor's name for admin promotion to a named user."""
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

        return {
            "slug": first("slug") or doc.get("key"),
            "title": first("title") or doc.get("key"),
            "format": first("format") or "markdown",
            "updated": first("updated"),
        }

    async def cms_list_pages(self, limit: int = 50) -> list[dict[str, Any]]:
        """List pages in the miniapp CMS collection."""
        docs = await self.mddb.search_documents(
            CMS_COLLECTION, filter_meta={"kind": ["page"]}, limit=limit
        )
        return [self._cms_page_summary(d) for d in docs]

    async def cms_get_page(self, slug: str) -> dict[str, Any] | None:
        """Fetch one page's content and metadata by slug."""
        doc = await self.mddb.get_document(CMS_COLLECTION, self._cms_slug(slug))
        if not doc:
            return None
        page = self._cms_page_summary(doc)
        page["content"] = doc.get("contentMd") or doc.get("content") or ""
        return page

    async def cms_publish_page(
        self,
        slug: str,
        title: str,
        content: str,
        format: str = "markdown",
    ) -> dict[str, Any]:
        """Create or update a miniapp page. Upserts by slug."""
        slug = self._cms_slug(slug)
        fmt = (format or "markdown").strip().lower()
        if fmt not in CMS_FORMATS:
            raise ValueError(
                f"invalid format {format!r}: expected one of {sorted(CMS_FORMATS)}"
            )
        if not (title or "").strip():
            raise ValueError("title is required")
        updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
        result = await self.mddb.add_document(
            CMS_COLLECTION,
            slug,
            "en",
            content,
            meta={
                "kind": ["page"],
                "slug": [slug],
                "title": [title.strip()],
                "format": [fmt],
                "updated": [updated],
                "instance": [self._instance_id or "ada"],
            },
        )
        if result is None:
            return {"status": "error", "error": "mddb write failed", "slug": slug}
        return {
            "status": "published",
            "slug": slug,
            "title": title.strip(),
            "format": fmt,
            "updated": updated,
        }

    async def cms_delete_page(self, slug: str) -> dict[str, Any]:
        """Delete a miniapp page by slug."""
        slug = self._cms_slug(slug)
        result = await self.mddb.delete_document(CMS_COLLECTION, slug)
        if result is None:
            return {"status": "not_found", "slug": slug}
        return {"status": "deleted", "slug": slug}

    async def cms_verify_page(self, slug: str) -> dict[str, Any]:
        """Re-read a page and check its content parses for its declared format.

        The assistant can't see the rendered miniapp — this is the feedback
        loop after publish: returns a structural summary of what the viewer
        renders, or the parse error to fix.
        """
        page = await self.cms_get_page(slug)
        if page is None:
            return {"ok": False, "status": "not_found", "slug": slug}
        content = page["content"]
        fmt = page["format"]
        report: dict[str, Any] = {
            "ok": True,
            "slug": page["slug"],
            "title": page["title"],
            "format": fmt,
            "chars": len(content),
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
        else:  # markdown
            headings = [
                ln.strip() for ln in content.splitlines() if ln.lstrip().startswith("#")
            ]
            report["summary"] = {"headings": headings[:20]}
        return report
