"""Narrow, read-only Home Assistant state client for ADA monitors."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx


DEFAULT_HOME_PLUGS = (
    "light.left_office_light",
    "light.right_office_light",
    "light.office_chest_overhead_light",
    "light.office_desk_overhead_light",
    "light.office_desk_rbg_light",
)
CONTROLLABLE_DOMAINS = {"light", "switch", "fan", "input_boolean"}
DISCOVERABLE_DOMAINS = CONTROLLABLE_DOMAINS | {"cover", "button", "media_player"}
DOMAIN_PRIORITY = {"light": 0, "fan": 1, "switch": 2, "input_boolean": 3, "media_player": 4, "cover": 5, "button": 6}
COVER_ACTIONS = {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"}
MEDIA_PLAYER_ACTIONS = {
    "turn_on", "turn_off", "media_play", "media_pause", "media_stop",
    "volume_up", "volume_down", "volume_mute", "select_source",
}


@dataclass(slots=True)
class HomeAssistantSnapshot:
    person_state: str
    plugs_on: tuple[str, ...]
    plug_states: dict[str, str]
    plug_names: dict[str, str]


class HomeAssistantClient:
    def __init__(self, client: Any = None) -> None:
        self.base_url = os.environ.get("HOME_ASSISTANT_URL", "http://127.0.0.1:8123").rstrip("/")
        self.token = os.environ.get("HOME_ASSISTANT_TOKEN", "").strip()
        self.person_entity = os.environ.get("HOME_ASSISTANT_PERSON", "person.naz").strip()
        configured = os.environ.get("HOME_ASSISTANT_HOME_PLUGS", "")
        self.home_plug_entities = tuple(
            item.strip() for item in configured.split(",") if item.strip()
        ) or DEFAULT_HOME_PLUGS
        self._client = client
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def snapshot(self) -> HomeAssistantSnapshot:
        if not self.token:
            raise RuntimeError("HOME_ASSISTANT_TOKEN is not set")
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=5.0,
            )
        response = await self._client.get("/api/states")
        response.raise_for_status()
        states = {}
        names = {}
        for item in response.json():
            if isinstance(item, dict) and item.get("entity_id"):
                entity_id = str(item.get("entity_id"))
                states[entity_id] = str(item.get("state", "unknown"))
                attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
                names[entity_id] = str(attributes.get("friendly_name") or entity_id)
        plug_states = {entity: states.get(entity, "unavailable") for entity in self.home_plug_entities}
        plug_names = {entity: names.get(entity, entity) for entity in self.home_plug_entities}
        return HomeAssistantSnapshot(
            person_state=states.get(self.person_entity, "unavailable"),
            plugs_on=tuple(entity for entity, state in plug_states.items() if state == "on"),
            plug_states=plug_states,
            plug_names=plug_names,
        )

    async def entities(self) -> list[dict[str, Any]]:
        """Return the safe subset of entities that support simple on/off control."""
        states = await self._states()
        entities_by_object_id: dict[str, dict[str, Any]] = {}
        for item in states:
            entity_id = str(item.get("entity_id", ""))
            domain, separator, object_id = entity_id.partition(".")
            if domain not in DISCOVERABLE_DOMAINS:
                continue
            attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            entity = {
                "entity_id": entity_id,
                "domain": domain,
                "name": str(attributes.get("friendly_name") or entity_id),
                "state": str(item.get("state", "unknown")),
                "available": str(item.get("state", "unknown")) not in {"unknown", "unavailable"},
            }
            # Some integrations expose one physical load as both light.foo and
            # switch.foo. Keep the richer domain while preserving similarly named
            # devices whose actual Home Assistant object IDs differ.
            existing = entities_by_object_id.get(object_id)
            if existing is None or DOMAIN_PRIORITY[domain] < DOMAIN_PRIORITY[existing["domain"]]:
                entities_by_object_id[object_id] = entity
        entities = list(entities_by_object_id.values())
        return sorted(entities, key=lambda entity: (entity["domain"], entity["name"].lower()))

    async def search_entities(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return controllable entities whose name or entity_id best matches the query."""
        q = query.strip().lower()
        if not q:
            return []
        all_entities = await self.entities()

        def score(entity: dict[str, Any]) -> int:
            name = entity.get("name", "").lower()
            eid = entity.get("entity_id", "").lower()
            object_id = eid.split(".", 1)[-1]
            if q == name or q == eid or q == object_id:
                return 100
            if q in name or q in eid:
                return 50
            tokens = q.split()
            return sum(15 for token in tokens if token in name or token in object_id)

        scored = [(score(entity), entity) for entity in all_entities]
        scored = [pair for pair in scored if pair[0] > 0]
        scored.sort(key=lambda pair: (-pair[0], pair[1]["name"].lower()))
        return [entity for _, entity in scored[:limit]]

    async def set_power(self, entity_id: str, turn_on: bool) -> dict[str, Any]:
        domain, separator, _ = entity_id.partition(".")
        if not separator or domain not in CONTROLLABLE_DOMAINS:
            raise ValueError("Only lights, switches, fans, and input booleans can be controlled")
        client = await self._http_client()
        service = "turn_on" if turn_on else "turn_off"
        response = await client.post(f"/api/services/{domain}/{service}", json={"entity_id": entity_id})
        response.raise_for_status()
        return {"entity_id": entity_id, "state": "on" if turn_on else "off"}

    async def control_cover(self, entity_id: str, action: str) -> dict[str, Any]:
        """Open, close, or stop a cover (gate, shutter, curtain)."""
        domain, separator, _ = entity_id.partition(".")
        if domain != "cover":
            raise ValueError("control_cover only accepts cover.* entity_ids")
        if action not in COVER_ACTIONS:
            raise ValueError(f"action must be one of: {', '.join(COVER_ACTIONS)}")
        client = await self._http_client()
        service = COVER_ACTIONS[action]
        response = await client.post(f"/api/services/cover/{service}", json={"entity_id": entity_id})
        response.raise_for_status()
        return {"entity_id": entity_id, "action": action, "service": service}

    async def press_button(self, entity_id: str) -> dict[str, Any]:
        """Press a Home Assistant button entity."""
        domain, separator, _ = entity_id.partition(".")
        if domain != "button":
            raise ValueError("press_button only accepts button.* entity_ids")
        client = await self._http_client()
        response = await client.post("/api/services/button/press", json={"entity_id": entity_id})
        response.raise_for_status()
        return {"entity_id": entity_id, "pressed": True}

    async def control_media_player(self, entity_id: str, action: str, source: str | None = None) -> dict[str, Any]:
        """Turn on/off, play, pause, or select a source on a media player."""
        domain, separator, _ = entity_id.partition(".")
        if domain != "media_player":
            raise ValueError("control_media_player only accepts media_player.* entity_ids")
        if action not in MEDIA_PLAYER_ACTIONS:
            raise ValueError(f"action must be one of: {', '.join(sorted(MEDIA_PLAYER_ACTIONS))}")
        client = await self._http_client()
        if action == "select_source":
            if not source:
                raise ValueError("select_source requires a source value")
            response = await client.post("/api/services/media_player/select_source", json={"entity_id": entity_id, "source": source})
        else:
            response = await client.post(f"/api/services/media_player/{action}", json={"entity_id": entity_id})
        response.raise_for_status()
        return {"entity_id": entity_id, "action": action, "source": source}

    async def tv_action(self, cmd: str, text: str = "") -> dict[str, Any]:
        """Call the Home Assistant rest_command.tv_action service."""
        client = await self._http_client()
        response = await client.post("/api/services/rest_command/tv_action", json={"cmd": cmd, "text": text})
        response.raise_for_status()
        return {"cmd": cmd, "text": text}

    async def _http_client(self) -> Any:
        if not self.token:
            raise RuntimeError("HOME_ASSISTANT_TOKEN is not set")
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=5.0,
            )
        return self._client

    async def _states(self) -> list[dict[str, Any]]:
        client = await self._http_client()
        response = await client.get("/api/states")
        response.raise_for_status()
        payload = response.json()
        return [item for item in payload if isinstance(item, dict)]

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None
