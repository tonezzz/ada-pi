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
            return await self._control_verified(
                entity_id, domain, action,
                lambda: self.control_cover(entity_id, action))
        if domain in ("button", "input_button") or action == "press":
            return await self.press_button(entity_id)
        if domain == "media_player":
            return await self._control_verified(
                entity_id, domain, action,
                lambda: self.control_media_player(entity_id, action, source),
                expected=source)
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

    # -- actuation ground truth --
    # HA accepts a service call the device then ignores — a paused or
    # powered-off cast player swallows every media_player service while
    # still returning ok (2026-10-08 transcript 9e96b5a5bc: volume_down
    # x4 on media_player.tony_tv_cast, every call ok:true, the level
    # never moved — same 200-no-op class tv_action's cast_verify covers
    # for casts). The adjustable verbs below carry the attribute or
    # state the call must move; the entity is read before and polled
    # after, and a zero delta fails loudly with the numbers attached.
    # verify.ok=None means HA could not say and never fails the call.
    _CONTROL_DEAD_STATES = frozenset({"off", "standby", "unavailable"})
    _CONTROL_VERIFY = {
        ("media_player", "volume_up"):
            {"kind": "delta", "attr": "volume_level"},
        ("media_player", "volume_down"):
            {"kind": "delta", "attr": "volume_level"},
        ("media_player", "volume_mute"):
            {"kind": "delta", "attr": "is_volume_muted"},
        ("media_player", "mute"):
            {"kind": "delta", "attr": "is_volume_muted"},
        ("media_player", "select_source"):
            {"kind": "attr_target", "attr": "source"},
        ("cover", "open"):
            {"kind": "target", "attr": "current_position", "dir": 1,
             "states": frozenset({"opening", "open"}),
             "fail_states": frozenset({"closed", "closing"})},
        ("cover", "close"):
            {"kind": "target", "attr": "current_position", "dir": -1,
             "states": frozenset({"closing", "closed"}),
             "fail_states": frozenset({"open", "opening"})},
        ("cover", "stop"):
            {"kind": "leave", "states": frozenset({"opening", "closing"})},
    }
    _CONTROL_VERIFY_POLLS = 4
    _CONTROL_VERIFY_INTERVAL_S = 0.7

    async def _control_verified(
        self, entity_id: str, domain: str, action: str, actuate: Any,
        expected: str | None = None,
    ) -> Any:
        """Wrap one actuation with the before/after state-delta check.
        Only the verbs in _CONTROL_VERIFY are checked — everything else
        passes through untouched."""
        spec = self._CONTROL_VERIFY.get((domain, action))
        if spec is None:
            return await actuate()
        before = await self._control_state_read(entity_id)
        out = await actuate()
        out = dict(out) if isinstance(out, dict) else {
            "entity_id": entity_id, "action": action, "result": out}
        verify = await self._control_verify(
            entity_id, spec, before, action, expected)
        if verify.get("ok") is not None:
            out["verify"] = verify
        if verify.get("ok") is False:
            out["ok"] = False
            out["error"] = verify["error"]
        return out

    @staticmethod
    def _attr_value(raw: Any) -> Any:
        """Coerce an HA attribute for delta comparison — numbers compare
        numerically ('0.30' == 0.3), everything else as a string."""
        if raw is None or isinstance(raw, bool):
            return raw
        try:
            return float(raw)
        except (TypeError, ValueError):
            return str(raw).strip().lower()

    async def _control_state_read(
            self, entity_id: str) -> dict[str, Any] | None:
        try:
            st = await self.context.ha_client.get_state(entity_id)
        except Exception:
            return None
        if not isinstance(st, dict):
            return None
        attrs = st.get("attributes")
        return {
            "state": str(st.get("state") or "unknown").lower(),
            # Snapshot the attrs — a 'before' read that keeps a live ref
            # to a shared/mutable dict compares post-actuation values
            # against themselves and always reports a zero delta.
            "attrs": dict(attrs) if isinstance(attrs, dict) else {},
        }

    async def _control_verify(
        self, entity_id: str, spec: dict[str, Any],
        before: dict[str, Any] | None, action: str,
        expected: str | None = None,
    ) -> dict[str, Any]:
        """Poll the entity after actuation and judge whether the call
        landed. delta: the watched attribute must change. attr_target:
        it must reach the requested value (select_source). target: the
        state must reach a goal state or the position attr must move the
        right way (cover open/close). leave: the state must exit a set
        (cover stop — still-moving after stop is a loud failure)."""
        out: dict[str, Any] = {"ok": None, "action": action}
        attr = spec.get("attr")
        if attr:
            out["attr"] = attr
        if before is None:
            return out
        out["before_state"] = before["state"]
        b_val = self._attr_value(before["attrs"].get(attr)) if attr else None
        if attr:
            out["before"] = b_val
        kind = spec["kind"]
        last_state = None
        for attempt in range(self._CONTROL_VERIFY_POLLS):
            if attempt:
                await asyncio.sleep(self._CONTROL_VERIFY_INTERVAL_S)
            after = await self._control_state_read(entity_id)
            if after is None:
                continue
            last_state = after["state"]
            out["state"] = last_state
            a_val = (
                self._attr_value(after["attrs"].get(attr)) if attr else None)
            if attr:
                out["after"] = a_val
                if isinstance(a_val, float) and isinstance(b_val, float):
                    out["delta"] = round(a_val - b_val, 4)
            moved = attr is not None and a_val is not None and a_val != b_val
            dead = last_state in self._CONTROL_DEAD_STATES
            if kind == "delta":
                if moved:
                    out["ok"] = True
                    return out
                if dead:
                    break  # a dead device cannot move — fail now
            elif kind == "attr_target":
                want = re.sub(r"[^a-z0-9]+", "", str(expected or "").lower())
                got = (re.sub(r"[^a-z0-9]+", "", str(a_val).lower())
                       if a_val is not None else None)
                if got is not None and got == want:
                    out["ok"] = True
                    return out
                if dead:
                    break
            elif kind == "target":
                in_dir = (
                    moved and isinstance(a_val, float)
                    and isinstance(b_val, float)
                    and (a_val - b_val) * spec["dir"] > 0)
                if last_state in spec["states"] or in_dir:
                    out["ok"] = True
                    return out
                if last_state in spec["fail_states"] or dead:
                    return self._control_verify_fail(
                        entity_id, spec, out, expected)
            elif kind == "leave":
                if last_state not in spec["states"]:
                    out["ok"] = True
                    return out
        # Poll window closed without the call visibly landing.
        if kind == "delta":
            if (out.get("before") is not None and out.get("after") is not None) \
                    or last_state in self._CONTROL_DEAD_STATES:
                return self._control_verify_fail(
                    entity_id, spec, out, expected)
        elif kind == "attr_target":
            if (out.get("after") is not None
                    or last_state in self._CONTROL_DEAD_STATES):
                return self._control_verify_fail(
                    entity_id, spec, out, expected)
        elif kind == "leave":
            if last_state in spec["states"]:
                return self._control_verify_fail(
                    entity_id, spec, out, expected)
        return out

    @staticmethod
    def _control_verify_fail(
        entity_id: str, spec: dict[str, Any], out: dict[str, Any],
        expected: str | None,
    ) -> dict[str, Any]:
        """ok:false verdict with the before/after numbers and a blunt
        instruction — same 'do not claim it happened' contract as
        tv_action's cast_verify error."""
        out["ok"] = False
        kind = spec["kind"]
        state = out.get("state") or out.get("before_state")
        action = out["action"]
        if kind == "delta":
            out["error"] = (
                f"HA accepted {action} on {entity_id} but "
                f"{out.get('attr')} never moved ({out.get('before')} -> "
                f"{out.get('after')}, state '{state}') — the device "
                "ignored the service call: paused/off cast players "
                "swallow volume commands silently, or it is already at "
                "the limit. Do NOT claim it changed; tell the user it "
                "did not respond.")
        elif kind == "attr_target":
            out["error"] = (
                f"HA accepted {action} on {entity_id} but the source "
                f"stayed '{out.get('after')}' instead of '{expected}' "
                f"(state '{state}') — the device ignored the call; do "
                "not claim the input switched.")
        elif kind == "leave":
            out["error"] = (
                f"HA accepted {action} on {entity_id} but it is still "
                f"'{state}' — it kept moving; do not claim it stopped.")
        else:  # target
            pos = (f", {out['attr']} {out.get('before')} -> "
                   f"{out.get('after')}") if out.get("attr") else ""
            out["error"] = (
                f"HA accepted {action} on {entity_id} but it never "
                f"moved (state '{state}'{pos}) — the call was "
                "swallowed; do not claim it happened.")
        return out

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
        # cmd=type payload guard: the payload sometimes arrives packed
        # into cmd ('type foo') instead of text= — check the effective
        # text either way.
        if str(cmd).strip().lower().split()[:1] == ["type"]:
            problem = self._tv_type_text_problem(
                text or " ".join(str(cmd).split()[1:]))
            if problem:
                raise ValueError(problem)
        # Screen-ownership ACL: personal desktop sources are owner-locked.
        # Deny non-owners here (rest_command swallows the controller's 403)
        # and pass the speaker so cast-browser enforces as backstop too.
        target = text or " ".join(str(cmd).split()[1:])
        # GEV on the TV never goes through shotAndCast — that lane casts a
        # frozen PNG of whatever the headless page had painted (~4s in,
        # Cesium still booting). 2026-10-05: five navs of the /apps/gev/
        # URL all returned {ok,cast:200} while the TV showed a still.
        # Rewrite GEV nav targets to the live workspace stream instead.
        gev_lane = self._gev_tv_target(cmd, target)
        if gev_lane:
            target = text = gev_lane
        self._check_tv_source_owner(target, self._memory_identity())
        out = await self.context.ha_client.tv_action(
            cmd, text, selector=selector or None, role=role or None,
            key=key or None, dx=dx, dy=dy, factor=factor,
            speaker=self._memory_identity() or "",
        )
        if isinstance(out, dict):
            out = dict(out)
        if gev_lane and isinstance(out, dict):
            out["gev_lane"] = gev_lane
            out["gev_note"] = (
                "GEV streams live from the tony-omen workspace — a URL "
                "nav would have cast a still image. GEV must be open on "
                "that workspace; if the picture is wrong, say so rather "
                "than claiming the map is up.")
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
        # Post-cast truth: {ok,cast:200} only proves HA accepted the
        # play_media / camera.play_stream call — an off or idle cast
        # target swallows it silently (the off-Chromecast 200 no-op
        # class, same as the 2026-10-05 GEV failure where six ok results
        # put nothing on the TV). Verify the player actually woke.
        if isinstance(out, dict) and ("cast" in out or "stream" in out):
            try:
                verify = await self._tv_cast_verify()
            except Exception:
                verify = {"ok": None}
            if verify.get("ok") is not None:
                out["cast_verify"] = verify
            if verify.get("ok") is False:
                out["ok"] = False
                out["error"] = (
                    f"the cast went out but {verify.get('entity')} is "
                    f"'{verify.get('state')}' — nothing reached the TV. "
                    "Do not claim it is showing; the cast target is off "
                    "or did not take the stream.")
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
