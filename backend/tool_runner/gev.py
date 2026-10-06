"""gev_command — GEV tour/display remote.

Split out of backend/tool_runner.py (card tool-runner-split, 2026-10-06).
The wildcard import reproduces the original module's global namespace —
helpers, constants, contextvars and backend module handles — so method
bodies moved verbatim and patch("backend.tool_runner.<mod>") targets keep
working (the mixin shares the same imported module objects).
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403


class GevMixin:

    _TOURS_PATH = Path(__file__).resolve().parent.parent / "gev_tours.json"

    # Per-command arg whitelist (gev-command-whitelist card) — mirrors
    # the GEV tool schema in chaba stacks/tony-dell/gev-gemini/tools.json
    # (extracted from GEV_REALTIME_TOOLS in gods-eye-view/vite.config.js).
    # args: {key: kind-spec} is the allowed key set; required keys must be
    # present; requires_one lists alternative groups where one group must
    # be fully present. Commands absent from the map pass through — the
    # whitelist only short-circuits calls the client is guaranteed to
    # refuse, it never narrows a command's real surface.
    _GEV_ARG_SCHEMAS: dict[str, dict[str, Any]] = {
        "fly_to_location": {
            "args": {"locationId": "enum[austin|sf|nyc|tokyo|london|paris|dubai|dc]", "query": "str", "latitude": "num", "longitude": "num", "viewMode": "enum[close|overview]", "rangeM": "num", "waitForArrival": "bool"},
            "required": [],
            "requires_one": [["locationId"], ["query"], ["latitude", "longitude"]],
        },
        "select_nearest_aircraft": {
            "args": {"layerId": "enum[flights|military]", "locationId": "enum[austin|sf|nyc|tokyo|london|paris|dubai|dc]", "locationQuery": "str", "latitude": "num", "longitude": "num"},
            "required": ["layerId"],
        },
        "adjust_camera_zoom": {
            "args": {"direction": "enum[in|out]", "amount": "enum[little|medium|lot]"},
            "required": ["direction", "amount"],
        },
        "zoom_to_globe": {
            "args": {},
            "required": [],
        },
        "set_layer_visibility": {
            "args": {"layerId": "enum[flights|military|earthquakes|satellites|rocket-launches|traffic|cctv|radio|bikeshare|ais-live-vessels|local-datacenters|local-dams|telegeography-submarine-cables|local-firms|local-flood-inundation-high|local-flood-inundation-medium|local-flood-inundation-low]", "enabled": "bool"},
            "required": ["layerId", "enabled"],
        },
        "show_data_layers_menu": {
            "args": {"layerId": "enum[flights|military|earthquakes|satellites|traffic|cctv|radio|bikeshare|ais-live-vessels|local-datacenters|local-dams|telegeography-submarine-cables|local-firms|local-flood-inundation-high|local-flood-inundation-medium|local-flood-inundation-low]"},
            "required": [],
        },
        "set_panel_open": {
            "args": {"panelId": "enum[data-panel|location-bar|control-panel|cctv-panel|radio-panel|scene-panel|pp-toggles|global-context-panel]", "open": "bool"},
            "required": ["panelId", "open"],
        },
        "set_context_mode": {
            "args": {"mode": "enum[off|contacts|flights|space-missions|missions]"},
            "required": ["mode"],
        },
        "control_cockpit": {
            "args": {"action": "enum[enter|exit|previous|next|prev|status]", "targetLayer": "enum[flights|military|ais-live-vessels|military-installations]", "aircraftClass": "str"},
            "required": ["action"],
        },
        "set_visual_style": {
            "args": {"style": "enum[normal|retro|surveillance|thermal|anime|noir|snow]"},
            "required": ["style"],
        },
        "get_entity_context": {
            "args": {"scope": "enum[auto|selected|in_view]", "layerId": "enum[local-datacenters|local-dams|telegeography-submarine-cables|local-firms]", "limit": "num"},
            "required": [],
        },
        "get_current_view_state": {
            "args": {},
            "required": [],
        },
        "set_hud": {
            "args": {"visible": "enum[on|off|auto]", "layout": "enum[tactical|operator|minimal]"},
            "required": [],
        },
        "set_detection": {
            "args": {"enabled": "bool", "mode": "enum[sparse|balanced|dense]", "densityPct": "num", "allocationStrategy": "enum[elastic|weighted]"},
            "required": [],
        },
        "set_map_stack": {
            "args": {"stack": "enum[photoreal|bing-aerial|bing-labels|esri-imagery|osm]"},
            "required": ["stack"],
        },
        "set_post_processing": {
            "args": {"bloom": "obj", "sharpen": "obj"},
            "required": [],
        },
        "control_scene": {
            "args": {"action": "enum[list|play|stop|next|status]", "sceneId": "str"},
            "required": ["action"],
        },
        "control_cctv": {
            "args": {"action": "enum[enable|disable|select|next|prev|nearest|focus|coverage|viewshed|adjust|projection|autohop]", "cameraQuery": "str", "enabled": "bool"},
            "required": ["action"],
        },
        "control_radio": {
            "args": {"action": "enum[enable|disable|play|resume|pause|stop|next|previous|volume|select|status]", "volumePct": "num", "category": "enum[all|news|talk|weather|public-safety|aviation-marine|traffic-transit|music]", "locationId": "enum[austin|sf|nyc|tokyo|london|paris|dubai|dc]", "locationQuery": "str", "latitude": "num", "longitude": "num", "country": "str", "stationQuery": "str"},
            "required": ["action"],
        },
        "track_entity": {
            "args": {"query": "str", "layerId": "str"},
            "required": ["query"],
        },
        "stop_tracking": {
            "args": {},
            "required": [],
        },
        "frame_overhead": {
            "args": {"target": "enum[flights|military|satellites|vessels]", "radiusKm": "num"},
            "required": ["target"],
        },
        "annotate_map": {
            "args": {"annotations": "list", "flyTo": "bool", "persist": "bool"},
            "required": ["annotations"],
        },
        "clear_annotations": {
            "args": {},
            "required": [],
        },
        "move_camera": {
            "args": {"motion": "enum[orbit|pan|tilt|rotate|zoom|fly|stop]", "direction": "enum[left|right|up|down|in|out|forward|back]", "speed": "enum[slow|normal|fast]", "mode": "enum[once|continuous]", "amount": "enum[little|medium|lot]"},
            "required": ["motion"],
        },
        "fly_route": {
            "args": {"label": "str", "speed": "enum[slow|normal|fast]"},
            "required": [],
        },
        "analyst_query": {
            "args": {"layers": "list", "scope": "obj", "filters": "list", "sortBy": "str", "sortDir": "enum[asc|desc]", "limit": "num", "followUp": "bool"},
            "required": [],
        },
        "next_iss_pass": {
            "args": {"latitude": "num", "longitude": "num", "minElevationDeg": "num"},
            "required": [],
        },
    }

    def _gev_args_error(self, name: str,
                        args: Any) -> dict[str, Any] | None:
        """Local arg check for known GEV commands — rejects unknown or
        missing keys with the expected schema instead of spending a ws
        relay round-trip on a call the client is guaranteed to refuse
        (the {name,args}-in-args wrap bug class, 0bb8e9b). Commands
        without a map entry pass through untouched."""
        schema = (self._GEV_ARG_SCHEMAS.get(name)
                  if isinstance(name, str) else None)
        if schema is None:
            return None
        problems = []
        if args is None:
            args = {}
        if not isinstance(args, dict):
            problems.append(f"args must be an object, got {type(args).__name__}")
        else:
            unknown = sorted(k for k in args if k not in schema["args"])
            if unknown:
                problems.append(f"unknown args {unknown}")
            missing = [k for k in schema["required"] if k not in args]
            if missing:
                problems.append(f"missing required args {missing}")
            groups = schema.get("requires_one") or []
            if groups and not any(
                    all(k in args for k in g) for g in groups):
                problems.append(
                    "needs one of " + " | ".join(
                        "+".join(g) for g in groups))
        if not problems:
            return None
        required = schema["required"]
        expected = ", ".join(
            f"{k}{'*' if k in required else ''}: {v}"
            for k, v in schema["args"].items())
        return {"ok": False,
                "error": (f"gev_command '{name}' rejected before relay: "
                          f"{'; '.join(problems)} — expected args "
                          f"schema: {{{expected}}}"),
                "expected": schema}

    async def gev_command(self, name: str | None = None,
                          args: dict[str, Any] | None = None,
                          screen: int | None = None,
                          pane: int | None = None,
                          wait: float = 3.0,
                          tour: str | None = None) -> dict[str, Any]:
        """Send a command to God's Eye View clients — forwards a
        function_call frame through the gev-gemini bridge to connected GEV
        browsers (including casted ones). screen=N targets that display;
        pane=N narrows to that split-screen pane; wait (seconds,
        0=fire-and-forget) collects the clients' tool_response so queries
        like get_current_view_state can answer. tools-merge-gev
        (2026-10-05): also handles what used to be gev_tour — pass
        tour='<id or alias>' for a tour's executable card; a bare call
        (no name) lists tours."""
        import asyncio
        import urllib.request
        if tour is not None or not name:
            return self._gev_tour_card(tour)
        # The model intermittently wraps the call envelope inside args:
        #   args={'args': {'query': '...'}, 'name': 'fly_to_location'}
        # — GEV then sees no query/coords and errors cryptically
        # ("needs a locationId, query, or latitude/longitude"). Unwrap once
        # when args looks like a nested call envelope.
        if (isinstance(args, dict) and "name" in args
                and isinstance(args.get("args"), dict)):
            args = args["args"]
        # Arg whitelist (gev-command-whitelist): known commands get their
        # args checked against the GEV schema locally — a guaranteed-refuse
        # call returns the expected schema instead of a relay round-trip.
        err = self._gev_args_error(name, args)
        if err is not None:
            return err
        base = os.environ.get(
            "GEV_CMD_URL",
            "https://tony-dell.taila0626a.ts.net/apps/gev-cmd/command")
        payload = json.dumps({
            "name": name, "args": args or {},
            "screen": screen,
            "pane": pane,
            "wait": min(float(wait or 0), 10.0),
        }).encode()
        def _post():
            req = urllib.request.Request(
                base, data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        try:
            out = await asyncio.to_thread(_post)
        except Exception as e:
            return {"ok": False, "error": f"gev command relay: {e}"}
        if not out.get("delivered"):
            return {"ok": False, "error":
                    "no GEV clients connected — cast /apps/gev/ first"}
        # get_current_view_state returns ~5k tokens of layer/detection/hud
        # config per call — it lands in session history and inflates every
        # subsequent turn. Slim to what Ada actually narrates.
        for resp in out.get("responses") or []:
            r = resp.get("response")
            if not isinstance(r, dict) or "layers" not in r:
                continue
            r["layers"] = [
                {k: l.get(k) for k in ("id", "name", "count", "error")}
                for l in r.get("layers") or [] if l.get("enabled")
            ]
            for dead in ("controls", "detection", "scenePlayback",
                         "celestalRing", "bloom", "sharpen"):
                r.pop(dead, None)
        # The all-failed -> top-level-error lift (0bb8e9b) is generalized
        # at the execute() boundary — normalize_tool_result surfaces a
        # responses[] where every client reported ok:false as a top-level
        # failure for every tool, not just this one.
        return out

    def _gev_tour_card(self, tour: str | None) -> dict[str, Any]:
        """Named GEV flyover tours — the absorbed gev_tour body
        (tools-merge-gev). tour=None lists them; an id/alias returns the
        executable card. A tour card is NOT self-running: drive each stop
        with gev_command/vcast_say/cast_to_screen per the card's per_stop
        recipe."""
        try:
            data = json.loads(self._TOURS_PATH.read_text())
        except Exception as exc:
            return {"error": f"tour registry unreadable: {exc}"}
        tours = data.get("tours") or {}
        if not tour:
            return {"tours": {k: {"title": v.get("title"),
                                  "stops": len(v.get("stops") or []),
                                  "aliases": v.get("aliases")}
                              for k, v in tours.items()},
                    "hint": "gev_command(tour='<id or alias>') returns the "
                            "stop list to execute"}
        q = tour.strip().lower()
        hit = next((k for k, v in tours.items()
                    if q == k or q in (v.get("aliases") or [])
                    or q in str(v.get("title") or "").lower()), None)
        if not hit:
            return {"error": f"no tour matches '{tour}'",
                    "tours": sorted(tours)}
        t = dict(tours[hit])
        t["id"] = hit
        t["per_stop"] = (
            "For each stop: gev_command fly_to_location "
            "{latitude:<lat>, longitude:<lon>} → annotate_map with "
            "COORDINATES {annotations:[{type:'pin', latitude:<lat>, "
            "longitude:<lon>, label:<label>}]} — place-name resolution "
            "is unreliable, never annotate by 'target' name → "
            "cast_to_screen(action='say', text=...) narration from 'say'. If the stop has a "
            "frame_url, also cast_to_screen(action='image', url=frame_url, "
            "pane=1) to pin the nearest CCTV camera next to the map. "
            "If the tour has route_points instead of stops: annotate_map "
            "{type:'route', points:[...]}, then fly_route {speed:'fast'}.")
        return {"output": t}
