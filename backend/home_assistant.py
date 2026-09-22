"""Narrow, read-only Home Assistant state client for ADA monitors."""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import websockets


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
# Domains whose state changes are meaningful "events" a user asks about
# (opened/closed, locked/unlocked, arrived/left, turned on/off).
EVENT_DOMAINS = {
    "binary_sensor", "cover", "lock", "person", "device_tracker",
    "alarm_control_panel", "light", "switch", "fan", "input_boolean",
    "media_player",
}
# Ordered so the most event-worthy domains survive the entity cap.
EVENT_DOMAIN_PRIORITY = {
    "binary_sensor": 0, "cover": 1, "lock": 2, "person": 3,
    "device_tracker": 4, "alarm_control_panel": 5, "light": 6,
    "switch": 7, "fan": 8, "input_boolean": 9, "media_player": 10,
}
MAX_EVENT_ENTITIES = 100

G3_POWER_ENTITIES = [
    "sensor.inverters_1_pv_power",
    "sensor.inverters_1_pv_power_1",
    "sensor.inverters_1_pv_power_2",
    "sensor.inverters_1_pv_power_3",
    "sensor.inverters_1_pv_power_4",
    "sensor.inverters_1_load_power",
    "sensor.inverters_1_load_power_essential",
    "sensor.inverters_1_grid_power",
    "sensor.totals_battery_power",
    "sensor.batteries_1_power",
    "sensor.batteries_2_power",
    "sensor.batteries_3_power",
    "sensor.weather_pv_power_predicted",
    "sensor.totals_pv_power",
]
POWER_SUMMARY_LABELS = {
    "sensor.inverters_1_pv_power": "pv",
    "sensor.inverters_1_pv_power_1": "pv1",
    "sensor.inverters_1_pv_power_2": "pv2",
    "sensor.inverters_1_pv_power_3": "pv3",
    "sensor.inverters_1_pv_power_4": "pv4",
    "sensor.inverters_1_load_power": "load",
    "sensor.inverters_1_load_power_essential": "load_essential",
    "sensor.inverters_1_grid_power": "grid",
    "sensor.totals_battery_power": "battery_total",
    "sensor.batteries_1_power": "battery_1",
    "sensor.batteries_2_power": "battery_2",
    "sensor.batteries_3_power": "battery_3",
    "sensor.weather_pv_power_predicted": "weather_pv",
    "sensor.totals_pv_power": "totals_pv",
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
        self.client_id = os.environ.get("HOME_ASSISTANT_CLIENT_ID", "").strip()
        self.person_entity = os.environ.get("HOME_ASSISTANT_PERSON", "person.naz").strip()
        configured = os.environ.get("HOME_ASSISTANT_HOME_PLUGS", "")
        self.home_plug_entities = tuple(
            item.strip() for item in configured.split(",") if item.strip()
        ) or DEFAULT_HOME_PLUGS
        self._client = client
        self._owns_client = client is None
        self._access_token: str | None = self.token if not self.client_id else None
        self._token_expires: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def _ensure_access_token(self) -> None:
        """Refresh HA access token when using a refresh token + client_id."""
        if not self.client_id:
            self._access_token = self.token
            return
        now = time.time()
        if self._access_token and now < self._token_expires - 120:
            return
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
            data = {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": self.token,
            }
            resp = await client.post(
                "/auth/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            payload = resp.json()
            self._access_token = payload["access_token"]
            self._token_expires = now + payload.get("expires_in", 1800)
            if self._client is not None:
                self._client.headers["Authorization"] = f"Bearer {self._access_token}"

    async def _http_client(self) -> Any:
        if not self.token:
            raise RuntimeError("HOME_ASSISTANT_TOKEN is not set")
        await self._ensure_access_token()
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self._access_token}"},
                timeout=5.0,
            )
        return self._client

    async def snapshot(self, person_entity: str | None = None) -> HomeAssistantSnapshot:
        """Return home state. If *person_entity* is given (e.g. from speaker
        ID), use it instead of the instance default HOME_ASSISTANT_PERSON."""
        if not self.token:
            raise RuntimeError("HOME_ASSISTANT_TOKEN is not set")
        client = await self._http_client()
        response = await client.get("/api/states")
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
            person_state=states.get(person_entity or self.person_entity, "unavailable"),
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

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        client = await self._http_client()
        response = await client.get(f"/api/states/{entity_id}")
        response.raise_for_status()
        return response.json()

    async def sensors(self, search: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Return sensor entities, optionally filtered by a search term."""
        states = await self._states()
        q = (search or "").lower()
        sensors = []
        for item in states:
            entity_id = str(item.get("entity_id", ""))
            domain, _, _ = entity_id.partition(".")
            if domain != "sensor":
                continue
            attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            name = str(attributes.get("friendly_name") or entity_id).lower()
            if q and q not in entity_id.lower() and q not in name:
                continue
            sensors.append({
                "entity_id": entity_id,
                "name": attributes.get("friendly_name") or entity_id,
                "state": str(item.get("state", "unknown")),
                "unit": attributes.get("unit_of_measurement", ""),
            })
        return sensors[:limit]

    async def history(
        self,
        entity_ids: list[str] | str,
        hours: int = 24,
        end: datetime | None = None,
    ) -> list[list[dict[str, Any]]]:
        """Fetch HA history for one or more entities for the last N hours."""
        if not self.token:
            raise RuntimeError("HOME_ASSISTANT_TOKEN is not set")
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        if not entity_ids:
            return []
        if end is None:
            end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        # Home Assistant expects a URL-encoded ISO 8601 string; the "+00:00"
        # offset is not parsed correctly if left unencoded, so use Z notation.
        start_str = start.replace(microsecond=0).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end.replace(microsecond=0).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        client = await self._http_client()
        filters = ",".join(entity_ids)
        url = (
            f"/api/history/period/{start_str}"
            f"?filter_entity_id={filters}"
            f"&end_time={end_str}"
            f"&minimal_response&no_attributes"
        )
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []

    async def logbook(
        self,
        entity_id: str | None = None,
        hours: int = 24,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch the HA logbook: friendly event entries (opens, closes, automations)."""
        if not self.token:
            raise RuntimeError("HOME_ASSISTANT_TOKEN is not set")
        if end is None:
            end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        start_str = start.replace(microsecond=0).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end.replace(microsecond=0).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        client = await self._http_client()
        # HA 2026.x: GET /api/logbook?end_time=<iso>&period=<days>&entity=<id>
        # Older HA:  GET /api/logbook/period/<start-iso>?end_time=<iso>&entity=<id>
        days = max(1, round(hours / 24))
        url = f"/api/logbook?end_time={end_str}&period={days}"
        if entity_id:
            url += f"&entity={entity_id}"
        response = await client.get(url)
        if response.status_code == 404:
            url = f"/api/logbook/period/{start_str}?end_time={end_str}"
            if entity_id:
                url += f"&entity={entity_id}"
            response = await client.get(url)
        if response.status_code == 404:
            raise ValueError(
                "The logbook integration is not enabled on this Home Assistant "
                "instance; use get_recent_events or get_entity_events instead."
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        # The query API rounds the window up to whole days; trim to `start`.
        return [
            entry for entry in payload
            if (when := self._parse_ts(entry.get("when"))) is None or when >= start
        ]

    @staticmethod
    def _point_state(point: dict[str, Any]) -> str | None:
        state = point.get("state", point.get("s"))
        return None if state is None else str(state)

    @staticmethod
    def _parse_ts(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    @classmethod
    def _point_at(cls, point: dict[str, Any]) -> datetime | None:
        return cls._parse_ts(
            point.get("last_changed") or point.get("lc")
            or point.get("last_updated") or point.get("lu")
        )

    async def _entity_info(self) -> dict[str, dict[str, str]]:
        """Map entity_id -> {name, device_class} from the current state list."""
        states = await self._states()
        info: dict[str, dict[str, str]] = {}
        for item in states:
            eid = str(item.get("entity_id", ""))
            if not eid:
                continue
            attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            info[eid] = {
                "name": str(attributes.get("friendly_name") or eid),
                "device_class": str(attributes.get("device_class") or ""),
            }
        return info

    @staticmethod
    def _transitions_from_series(
        series_list: list[list[dict[str, Any]]],
        info: dict[str, dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Collapse raw HA history rows into state-change transitions."""
        transitions: list[dict[str, Any]] = []
        for series in series_list:
            eid: str | None = None
            prev_state: str | None = None
            for point in series:
                if not isinstance(point, dict):
                    continue
                eid = point.get("entity_id") or eid
                state = HomeAssistantClient._point_state(point)
                at = HomeAssistantClient._point_at(point)
                if state is None or at is None or eid is None:
                    continue
                if state == prev_state:
                    continue
                entry = info.get(eid, {})
                transitions.append({
                    "entity_id": eid,
                    "name": entry.get("name", eid),
                    "device_class": entry.get("device_class", ""),
                    "state": state,
                    "at": at.isoformat(),
                    "at_dt": at,
                })
                prev_state = state
        transitions.sort(key=lambda t: t["at_dt"], reverse=True)
        for t in transitions:
            del t["at_dt"]
        return transitions

    async def state_transitions(
        self,
        entity_ids: list[str] | str,
        hours: int = 24,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        """Formatted per-entity event timeline with state durations."""
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        if end is None:
            end = datetime.now(timezone.utc)
        info, series_list = await asyncio.gather(
            self._entity_info(),
            self.history(entity_ids, hours=hours, end=end),
        )
        by_entity: dict[str, list[dict[str, Any]]] = {}
        for series in series_list:
            eid: str | None = None
            points: list[tuple[datetime, str]] = []
            prev_state: str | None = None
            for point in series:
                if not isinstance(point, dict):
                    continue
                eid = point.get("entity_id") or eid
                state = self._point_state(point)
                at = self._point_at(point)
                if state is None or at is None or eid is None:
                    continue
                if state == prev_state:
                    continue
                points.append((at, state))
                prev_state = state
            if eid is None:
                continue
            transitions = []
            for index, (at, state) in enumerate(points):
                next_at = points[index + 1][0] if index + 1 < len(points) else end
                transitions.append({
                    "state": state,
                    "at": at.isoformat(),
                    "duration_seconds": max(0, int((next_at - at).total_seconds())),
                })
            entry = info.get(eid, {})
            by_entity[eid] = {
                "entity_id": eid,
                "name": entry.get("name", eid),
                "device_class": entry.get("device_class", ""),
                "changes": len(transitions),
                "transitions": transitions,
            }
        return {
            "hours": hours,
            "entities": list(by_entity.values()),
        }

    async def recent_events(
        self,
        hours: int = 24,
        query: str | None = None,
        limit: int = 100,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        """Cross-entity 'what changed' query across event-worthy domains."""
        if end is None:
            end = datetime.now(timezone.utc)
        states = await self._states()
        q = (query or "").strip().lower()
        info: dict[str, dict[str, str]] = {}
        candidates: list[tuple[int, str]] = []
        for item in states:
            eid = str(item.get("entity_id", ""))
            domain, _, _ = eid.partition(".")
            if not eid or domain not in EVENT_DOMAINS:
                continue
            attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            name = str(attributes.get("friendly_name") or eid)
            info[eid] = {
                "name": name,
                "device_class": str(attributes.get("device_class") or ""),
            }
            if q and q not in eid.lower() and q not in name.lower():
                continue
            candidates.append((EVENT_DOMAIN_PRIORITY.get(domain, 99), eid))
        candidates.sort()
        entity_ids = [eid for _, eid in candidates[:MAX_EVENT_ENTITIES]]
        truncated = len(candidates) > len(entity_ids)
        if not entity_ids:
            return {"hours": hours, "count": 0, "events": [], "entities_scanned": 0}
        series_list = await self.history(entity_ids, hours=hours, end=end)
        events = self._transitions_from_series(series_list, info)[:limit]
        return {
            "hours": hours,
            "count": len(events),
            "entities_scanned": len(entity_ids),
            "entities_truncated": truncated,
            "events": events,
        }

    async def power_summary(self, hours: int = 24) -> dict[str, Any]:
        """Return current and historical summary for G3 power sensors."""
        if not self.token:
            raise RuntimeError("HOME_ASSISTANT_TOKEN is not set")
        states = await self._states()
        by_id = {item.get("entity_id"): item for item in states if isinstance(item, dict)}

        now = {}
        for entity_id in G3_POWER_ENTITIES:
            item = by_id.get(entity_id)
            attributes = item.get("attributes") if isinstance(item, dict) and isinstance(item.get("attributes"), dict) else {}
            now[POWER_SUMMARY_LABELS.get(entity_id, entity_id)] = {
                "entity_id": entity_id,
                "state": str(item.get("state", "unknown")) if isinstance(item, dict) else "unknown",
                "name": attributes.get("friendly_name") or entity_id,
                "unit": attributes.get("unit_of_measurement", ""),
            }

        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        series_list = await self.history(G3_POWER_ENTITIES, hours=hours, end=end)
        history: dict[str, Any] = {}
        for series in series_list:
            if not series:
                continue
            first = series[0]
            eid = first.get("entity_id", "")
            key = POWER_SUMMARY_LABELS.get(eid, eid)
            values = []
            for point in series:
                try:
                    ts = point.get("last_updated") or point.get("last_changed")
                    value = float(point.get("state"))
                    values.append({"time": ts, "value": value})
                except (ValueError, TypeError):
                    continue
            if values:
                nums = [v["value"] for v in values]
                history[key] = {
                    "entity_id": eid,
                    "points": len(nums),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "min": round(min(nums), 2),
                    "max": round(max(nums), 2),
                    "mean": round(statistics.mean(nums), 2),
                }
            else:
                history[key] = {"entity_id": eid, "points": 0}

        return {
            "now": now,
            f"last_{hours}h": history,
        }

    def _battery_bucket(self, eid: str) -> str:
        lower = eid.lower()
        if "battery_1" in lower or "batteries_1" in lower or lower.endswith("_1") and "battery" in lower:
            return "battery_1"
        if "battery_2" in lower or "batteries_2" in lower:
            return "battery_2"
        if "battery_3" in lower or "batteries_3" in lower:
            return "battery_3"
        if "totals_battery" in lower:
            return "totals"
        if "inverters_1_battery" in lower:
            return "inverter_1"
        if "solis" in lower and "battery" in lower:
            return "solis_bms"
        return "other"

    async def battery_status(self) -> dict[str, Any]:
        states = await self._states()
        out = {
            "summary": {
                "total_battery_entities": 0,
                "batteries_tracked": ["battery_1", "battery_2", "battery_3"],
                "note": (
                    "Use get_battery_detail with battery_index=1, 2, or 3 for per-battery readings. "
                    "battery_status gives a grouped overview."
                ),
            },
            "groups": {},
        }
        for item in states:
            eid = str(item.get("entity_id", ""))
            if not eid or "battery" not in eid.lower():
                continue
            bucket = self._battery_bucket(eid)
            if bucket not in out["groups"]:
                out["groups"][bucket] = {}
            attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            out["groups"][bucket][eid] = {
                "state": str(item.get("state", "unknown")),
                "name": attributes.get("friendly_name") or eid,
                "unit": attributes.get("unit_of_measurement", ""),
            }
            out["summary"]["total_battery_entities"] += 1
        return out

    async def battery_detail(self, battery_index: int) -> dict[str, Any]:
        if battery_index not in (1, 2, 3):
            return {"error": "battery_index must be 1, 2, or 3"}
        states = await self._states()
        key = f"battery_{battery_index}"
        out = {"battery_index": battery_index, "entities": {}}
        for item in states:
            eid = str(item.get("entity_id", ""))
            if not eid or "battery" not in eid.lower():
                continue
            if self._battery_bucket(eid) != key:
                continue
            attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            out["entities"][eid] = {
                "state": str(item.get("state", "unknown")),
                "name": attributes.get("friendly_name") or eid,
                "unit": attributes.get("unit_of_measurement", ""),
            }
        return out

    def _inverter_bucket(self, eid: str) -> str:
        lower = eid.lower()
        if "pv" in lower:
            return "pv"
        if "load" in lower:
            return "load"
        if "grid" in lower:
            return "grid"
        if "ac_output" in lower:
            return "ac_output"
        if "battery" in lower:
            return "battery"
        if any(k in lower for k in ("mode", "temperature", "fault", "status")):
            return "status"
        return "other"

    async def inverter_status(self) -> dict[str, Any]:
        states = await self._states()
        out = {
            "summary": {
                "total_inverter_entities": 0,
                "note": (
                    "Grouped inverter and power sensor readings. "
                    "Use this for solar generation, home load, grid, battery, and inverter health."
                ),
            },
            "groups": {},
        }
        for item in states:
            eid = str(item.get("entity_id", ""))
            if not eid or "inverters_1" not in eid and "totals_" not in eid:
                continue
            bucket = self._inverter_bucket(eid)
            if bucket not in out["groups"]:
                out["groups"][bucket] = {}
            attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            out["groups"][bucket][eid] = {
                "state": str(item.get("state", "unknown")),
                "name": attributes.get("friendly_name") or eid,
                "unit": attributes.get("unit_of_measurement", ""),
            }
            out["summary"]["total_inverter_entities"] += 1
        return out

    async def pool_status(self) -> dict[str, Any]:
        states = await self._states()
        out = {
            "available": {},
            "unavailable_or_unknown": {},
            "note": (
                "Pool pump switches that show 'unavailable' are not currently connected or powered. "
                "The active smart meter switch is 'switch.pool_wifi_smart_meter_switch_switch'."
            ),
        }
        for item in states:
            eid = str(item.get("entity_id", ""))
            if not eid or not any(k in eid.lower() for k in ("pool", "tze200_cirvgep4")):
                continue
            attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            state = str(item.get("state", "unknown"))
            entry = {
                "state": state,
                "name": attributes.get("friendly_name") or eid,
                "unit": attributes.get("unit_of_measurement", ""),
            }
            if state in ("unavailable", "unknown"):
                out["unavailable_or_unknown"][eid] = entry
            else:
                out["available"][eid] = entry
        return out

    async def rk600_weather(self) -> dict[str, Any]:
        states = await self._states()
        out = {
            "source": "Rika RK600-07B weather station",
            "note": (
                "This is the local RK600 weather station data. "
                "For the forecast/compare view, use search_sensors with 'weather' or ask about the HA weather entities."
            ),
            "sensors": {},
        }
        for item in states:
            eid = str(item.get("entity_id", ""))
            if not eid or not eid.startswith("sensor.rk600"):
                continue
            attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            out["sensors"][eid] = {
                "state": str(item.get("state", "unknown")),
                "name": attributes.get("friendly_name") or eid,
                "unit": attributes.get("unit_of_measurement", ""),
            }
        return out

    async def lovelace_config(self, url_path: str | None = None) -> dict[str, Any]:
        if url_path is None:
            url_path = os.environ.get("ADA_DASHBOARD_URL_PATH", "tony-test")
        """Fetch a Lovelace dashboard config over the Home Assistant websocket."""
        if not self.token:
            raise RuntimeError("HOME_ASSISTANT_TOKEN is not set")
        await self._ensure_access_token()
        base = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{base.rstrip('/')}/api/websocket"
        async with websockets.connect(ws_url) as ws:
            # Wait for the auth_required hello.
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"unexpected websocket hello: {hello}")
            await ws.send(json.dumps({"type": "auth", "access_token": self._access_token}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
            if ack.get("type") != "auth_ok":
                raise RuntimeError(f"websocket auth failed: {ack}")
            msg_id = 1
            await ws.send(json.dumps({"id": msg_id, "type": "lovelace/config", "url_path": url_path}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                payload = json.loads(raw)
                if payload.get("id") == msg_id:
                    if payload.get("type") == "result" and payload.get("success"):
                        return payload.get("result", {})
                    raise RuntimeError(f"lovelace/config failed: {payload}")

    async def dashboard_tab(
        self,
        tab: str,
        url_path: str | None = None,
    ) -> dict[str, Any]:
        """Return the entities and their current states for a named dashboard tab."""
        if not self.token:
            raise RuntimeError("HOME_ASSISTANT_TOKEN is not set")
        config = await self.lovelace_config(url_path=url_path)
        views = config.get("views", []) if isinstance(config, dict) else []
        target = next((v for v in views if isinstance(v, dict) and v.get("title", "").lower() == tab.lower()), None)
        if target is None:
            view_titles = [v.get("title") for v in views if isinstance(v, dict)]
            return {"error": f"tab not found: {tab!r}", "available_tabs": view_titles}

        entity_ids = set()
        card_info = []

        def _collect(obj: Any, path: str = "") -> None:
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key in ("entity", "entity_id") and isinstance(value, str) and value:
                        if "." in value:
                            entity_ids.add(value)
                            card_info.append({"field": key, "entity_id": value, "path": path})
                    elif key in ("id", "source", "target") and isinstance(value, str) and value and "." in value:
                        entity_ids.add(value)
                        card_info.append({"field": key, "entity_id": value, "path": path})
                    elif key == "entity_input" and isinstance(value, str) and value:
                        entity_ids.add(value)
                        card_info.append({"field": key, "entity_id": value, "path": path})
                    elif key == "entities" and isinstance(value, dict):
                        for eid in value.values():
                            if isinstance(eid, str) and "." in eid:
                                entity_ids.add(eid)
                                card_info.append({"field": "entities", "entity_id": eid, "path": path})
                    elif key in ("series", "pfg_charts", "cards"):
                        _collect(value, f"{path}/{key}")
                    elif isinstance(value, (dict, list)):
                        _collect(value, f"{path}/{key}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _collect(item, f"{path}/[{i}]")

        for card in target.get("cards", []) if isinstance(target.get("cards"), list) else []:
            _collect(card, "card")

        states = await self._states()
        by_id = {s.get("entity_id"): s for s in states if isinstance(s, dict) and s.get("entity_id")}
        entities = []
        for eid in sorted(entity_ids):
            item = by_id.get(eid)
            if not item:
                continue
            attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            entities.append({
                "entity_id": eid,
                "name": attrs.get("friendly_name") or eid,
                "state": str(item.get("state", "unknown")),
                "unit": attrs.get("unit_of_measurement", ""),
                "domain": eid.split(".")[0],
            })

        return {
            "tab": tab,
            "path": target.get("path"),
            "title": target.get("title"),
            "badges": [b for b in target.get("badges", []) if isinstance(b, str) and "." in b],
            "entity_count": len(entities),
            "entities": entities,
            "card_snippets": card_info[:25],
        }

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
