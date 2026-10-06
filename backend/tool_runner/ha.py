"""Home Assistant device/state/history tools + habit + confidence.

Split out of backend/tool_runner.py (card tool-runner-split, 2026-10-06).
The wildcard import reproduces the original module's global namespace —
helpers, constants, contextvars and backend module handles — so method
bodies moved verbatim and patch("backend.tool_runner.<mod>") targets keep
working (the mixin shares the same imported module objects).
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403


class HaMixin:

    # -- Home Assistant tools --

    async def get_home_state(
        self,
        entity_id: str | None = None,
        domain: str | None = None,
    ) -> Any:
        """Current home state — tools-merge-ha absorbed ada_ha_get_state
        here (domain='memory' returns the stored AdaMemoryStore overview,
        keeping the alias's exact contract). entity_id reads one entity
        live, domain= lists the entities under one HA domain."""
        entity_id = str(entity_id or "").strip()
        domain = str(domain or "").strip().lower()
        if entity_id:
            return await self.context.ha_client.get_state(entity_id)
        if domain == "memory":
            if self.memory is None:
                raise RuntimeError("stored home snapshot not available")
            return await self.memory.overview()
        if domain:
            states = await self.context.ha_client._states()
            out = []
            for item in states:
                eid = str(item.get("entity_id") or "")
                if eid.partition(".")[0] != domain:
                    continue
                attrs = item.get("attributes") or {}
                out.append({
                    "entity_id": eid,
                    "state": str(item.get("state", "unknown")),
                    "name": str(attrs.get("friendly_name") or eid),
                })
            return out[:LIST_HOME_DEVICES_MAX]
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

    async def home_search(
        self,
        query: str = "",
        kind: str = "device",
        limit: int = 10,
        hours: int = 24,
    ) -> Any:
        """Home Assistant finders — tools-merge-ha consolidated
        search_home_devices / list_home_devices / search_sensors /
        list_sensors / ada_ha_search_devices / ada_ha_search_sensors /
        ada_ha_search_events / search_devices behind kind=. A blank
        query lists (bounded) instead of searching."""
        kind = str(kind or "device").strip().lower()
        query = str(query or "").strip()
        if kind == "device":
            if query:
                return await self.search_home_devices(query)
            return await self.list_home_devices()
        if kind == "sensor":
            if query:
                return await self.context.ha_client.sensors(
                    search=query, limit=int(limit))
            return await self.list_sensors()
        if kind == "event":
            if self.events is None or self.mddb is None:
                raise RuntimeError("recorded event memory not available")
            return await self.ada_ha_search_events(
                query=query, hours=int(hours), limit=int(limit))
        raise ValueError(
            f"invalid kind {kind!r}: expected device|sensor|event")

    async def list_home_devices(self) -> list[dict[str, Any]]:
        # Bound the dump — an unbounded entity list stays in the live
        # context for the rest of the session and ballooned one past 1M
        # input tokens on 2026-10-01. search_home_devices is the precise
        # path; the marker tells the model so.
        devices = await self.context.ha_client.entities()
        if len(devices) > LIST_HOME_DEVICES_MAX:
            return devices[:LIST_HOME_DEVICES_MAX] + [{
                "_truncated": (
                    f"{LIST_HOME_DEVICES_MAX} of {len(devices)} devices shown "
                    "— call home_search with a name/keyword for the rest"),
            }]
        return devices

    async def search_home_devices(self, query: str) -> list[dict[str, Any]]:
        return await self.context.ha_client.search_entities(str(query))

    async def control_entity(
        self,
        entity_id: str,
        action: str = "",
        on: bool | None = None,
        source: str | None = None,
    ) -> Any:
        """Actuate one HA entity — tools-merge-ha consolidated
        control_cover / press_button / control_media_player here; the
        entity domain picks the path. on= is the absorbed on/off arg;
        action= carries the domain verbs (open|close|stop for cover.*,
        press for button.*, the media_player verbs, on|off otherwise)."""
        entity_id = str(entity_id or "")
        if not entity_id:
            raise ValueError("entity_id is required")
        domain = entity_id.partition(".")[0]
        action = str(action or "").strip().lower()
        if domain == "cover":
            return await self.control_cover(entity_id, action)
        if domain in ("button", "input_button") or action == "press":
            return await self.press_button(entity_id)
        if domain == "media_player":
            return await self.control_media_player(entity_id, action, source)
        if on is None:
            if action in ("on", "turn_on"):
                on = True
            elif action in ("off", "turn_off"):
                on = False
            else:
                raise ValueError(
                    "pass on=true/false or action=on|off for "
                    f"{domain or entity_id} entities")
        outcome = await self.context.ha_client.set_power(entity_id, bool(on))
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

    async def tv_action(self, cmd: str, text: str = "",
                        selector: str = "", role: str = "",
                        key: str = "", dx: float | None = None,
                        dy: float | None = None,
                        factor: float | None = None) -> dict[str, Any]:
        if not cmd:
            raise ValueError("cmd is required")
        # Screen-ownership ACL: personal desktop sources are owner-locked.
        # Deny non-owners here (rest_command swallows the controller's 403)
        # and pass the speaker so cast-browser enforces as backstop too.
        target = text or " ".join(str(cmd).split()[1:])
        self._check_tv_source_owner(target, self._memory_identity())
        out = await self.context.ha_client.tv_action(
            cmd, text, selector=selector or None, role=role or None,
            key=key or None, dx=dx, dy=dy, factor=factor,
            speaker=self._memory_identity() or "",
        )
        # Post-nav ground truth: cast-browser acking the nav only means
        # the browser took the URL — if the TV's foreground app is the
        # STB or another input, the page loaded but is invisible
        # (2026-10-04: 'casting' for 3min while the TV showed True STB).
        if isinstance(out, dict) and str(cmd).lower() == "nav" and text:
            try:
                await asyncio.sleep(1.0)  # let webOS foreground the app
                inp = await self._tv_input_state()
                if inp.get("on_cast_app") is not None:
                    out["tv_input"] = inp
                    if inp["on_cast_app"] is False:
                        out["input_warn"] = (
                            f"the TV is on '{inp.get('app') or inp.get('state')}' — "
                            "the page loaded in the TV browser but is NOT "
                            "visible; do not claim it is showing. Push the "
                            "URL again or switch the TV input.")
            except Exception:
                pass
        return out

    async def home_status(
        self,
        what: str = "",
        battery_index: int | None = None,
        hours: int = 24,
        tab: str = "",
    ) -> Any:
        """Home status reads — tools-merge-tasks-status consolidated
        get_battery_status / get_battery_detail / get_power_summary /
        get_inverter_status / get_pool_status / get_dashboard_tab /
        get_habit_status into one what= tool. All free reads; what=
        'battery' with battery_index reads one battery's detail."""
        what = (what or "").strip().lower()
        if what == "battery":
            if battery_index in (None, ""):
                return await self.get_battery_status()
            return await self.get_battery_detail(int(battery_index))
        if what == "power":
            return await self.get_power_summary(hours=int(hours))
        if what == "inverter":
            return await self.get_inverter_status()
        if what == "pool":
            return await self.get_pool_status()
        if what == "dashboard":
            return await self.get_dashboard_tab(str(tab))
        if what == "habit":
            return await self.get_habit_status()
        raise ValueError(
            f"invalid what {what!r}: expected "
            "battery|power|inverter|pool|dashboard|habit")

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

    async def home_history(
        self,
        entity_id: str | None = None,
        domain: str | None = None,
        kind: str | None = None,
        hours: int = 24,
        query: str | None = None,
        limit: int = 25,
    ) -> Any:
        """Home history reads — tools-merge-ha consolidated get_logbook /
        get_sensor_history / get_entity_events / get_recent_events /
        ada_ha_history behind kind=. Default routing: entity_id -> the
        entity timeline ('series' for sensor.*, 'timeline' otherwise),
        query -> the whole-home events feed, domain='memory'|'snapshots'
        -> persisted memory snapshots, else the logbook."""
        entity_id = str(entity_id or "").strip()
        domain = str(domain or "").strip().lower()
        kind = str(kind or "").strip().lower()
        if not kind:
            if entity_id:
                kind = ("series" if entity_id.startswith("sensor.")
                        else "timeline")
            elif domain in ("memory", "snapshots"):
                kind = "snapshots"
            elif query:
                kind = "events"
            else:
                kind = "logbook"
        hours = int(hours or 24)
        limit = int(limit or 25)
        if kind == "series":
            if not entity_id:
                raise ValueError("entity_id is required for kind='series'")
            return await self.context.ha_client.history(
                entity_id, hours=hours)
        if kind == "timeline":
            if not entity_id:
                raise ValueError("entity_id is required for kind='timeline'")
            return await self.context.ha_client.state_transitions(
                entity_id, hours=hours)
        if kind == "logbook":
            entries = await self.context.ha_client.logbook(
                entity_id=entity_id or None, hours=hours)
            if domain:
                prefix = f"{domain}."
                entries = [e for e in entries if str(
                    e.get("entity_id") or "").startswith(prefix)]
            return {"hours": hours, "count": len(entries),
                    "entries": entries[:100]}
        if kind == "events":
            return await self.get_recent_events(
                hours=hours, query=query or (domain or None), limit=limit)
        if kind == "snapshots":
            if self.mddb is None:
                raise RuntimeError(
                    "persisted home snapshots not available")
            return await self.ada_ha_history(hours=hours, limit=limit)
        raise ValueError(
            f"invalid kind {kind!r}: expected logbook|events|timeline|"
            "series|snapshots")

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

    async def ha_confidence(
        self,
        entity_id: str = "",
        status: str = "",
        safety: str = "",
    ) -> Any:
        """Device trust/safety registry — tools-merge-ha consolidated
        ada_ha_get_device_confidence + ada_ha_set_device_confidence.

        No args → controllable devices grouped by confidence.
        entity_id alone → that device's entry. entity_id + status and/or
        safety → the write path (the absorbed set tool's contract)."""
        if self.memory is None:
            raise RuntimeError("device confidence not available")
        entity_id = str(entity_id or "").strip()
        status = str(status or "").strip()
        safety = str(safety or "").strip()
        if status or safety:
            if not entity_id:
                raise ValueError(
                    "entity_id is required to set confidence")
            if not status:
                raise ValueError(
                    "status is required — pass the confidence level "
                    "alongside safety")
            return await self.memory.set_confidence(
                entity_id, status, safety or None)
        await self.memory._ensure_confidence()
        groups = self.memory.confidence_groups()
        if not entity_id:
            return groups
        for group_name, devices in groups.items():
            for dev in devices:
                if dev.get("entity_id") == entity_id:
                    return {**dev, "confidence": group_name}
        return {"entity_id": entity_id, "confidence": "unknown"}

    async def ada_ha_get_device_confidence(self) -> dict[str, list[dict[str, Any]]]:
        """Return controllable devices grouped by user confidence."""
        await self.memory._ensure_confidence()
        return self.memory.confidence_groups()

    async def ada_ha_set_device_confidence(self, entity_id: str, status: str, safety: str | None = None) -> str:
        """Set a device's confidence and/or safety status."""
        return await self.memory.set_confidence(str(entity_id), str(status), str(safety) if safety is not None else None)
