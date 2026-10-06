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
        # A delivered command whose every client response is ok:false is a
        # real failure — surface it at top level instead of leaving the
        # error buried inside responses[] where the model may miss it.
        resps = out.get("responses") or []
        errs = [r.get("response") for r in resps
                if isinstance(r.get("response"), dict)
                and r["response"].get("ok") is False]
        if resps and errs and len(errs) == len(resps):
            out["ok"] = False
            out["error"] = errs[0].get("error") or "all GEV clients failed"
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
