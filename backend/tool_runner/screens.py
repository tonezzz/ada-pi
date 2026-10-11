"""Screens: yt/cctv/cast/vcast display tools.

Split out of backend/tool_runner.py (card tool-runner-split, 2026-10-06).
The wildcard import reproduces the original module's global namespace —
helpers, constants, contextvars and backend module handles — so method
bodies moved verbatim and patch("backend.tool_runner.<mod>") targets keep
working (the mixin shares the same imported module objects).
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403


class ScreensMixin:

    # -- YouTube -> TV casting (yt-live shim on tony-dell) --

    @staticmethod
    def _yt_api(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        import urllib.request
        base = os.environ.get("YT_LIVE_API", "http://tony-dell:8791")
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            base + path, data=data,
            headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    async def yt(
        self,
        action: str = "status",
        query: str = "",
        url: str = "",
        language: str = "th",
    ) -> dict[str, Any]:
        """YouTube surface — tools-merge-yt (2026-10-05) consolidated
        yt_cast / yt_cast_status / yt_cast_stop / yt_transcript into one
        action= tool (plus list = cast-ready cached library). Ungated:
        cast/stop actuate the TV but the old names were never
        confirm-gated; status/list/transcript are reads."""
        action = (action or "status").strip().lower()
        if action == "cast":
            return await self.yt_cast(query=query or url, language=language)
        if action == "status":
            return await self.yt_cast_status()
        if action == "stop":
            return await self.yt_cast_stop()
        if action == "list":
            return await self._yt_cached_list()
        if action == "transcript":
            return await self.yt_transcript(url=url or query,
                                            language=language)
        raise ValueError(
            f"invalid action {action!r}: expected cast|status|stop|list|transcript")

    async def yt_cast(self, query: str, language: str = "th") -> dict[str, Any]:
        """Cast a YouTube video to the living-room TV with translated
        subtitles — or a direct media file URL (.mp4/.m4v/.webm/.mkv/.mp3/
        .m4a/.m3u8), which plays instantly without transcoding (e.g. the
        dubbed demos under https://tony-dell.taila0626a.ts.net/apps/yt-live/).
        `query` is a YouTube URL, media file URL, or a search phrase — prefer
        the video title plus channel name for accuracy. TV ONLY — if the user
        names a numbered screen, use cast_to_screen(action='play') instead;
        vcast displays auto-embed YouTube URLs.

        Result fields: `player` = media_player.tony_tv_cast state (must be
        'playing' for real playback — HTTP 200 alone is NOT proof, the box
        can be idle/booting), `box` = media_player.tony_tv (the TrueID box's
        Android state), `panel` is always 'unsensed' — the Samsung panel is
        IR-toggle only with no power sensor, so 'playing' does not guarantee
        a lit screen. The box outputs to HDMI2 — if the user reports a dark
        screen while state is 'playing', the panel is likely off or on the
        wrong input: wake via remote.tony_tv command POWER (CEC) or offer
        script.tv_power (IR toggle — could turn OFF a lit panel, ask first),
        then script.tv_source to reach HDMI2. Nuclear cold-boot:
        script.cast_power_on(target='box')."""
        import asyncio
        return await asyncio.to_thread(
            self._yt_api, "/cast", {"q": query, "lang": language})

    async def yt_cast_status(self) -> dict[str, Any]:
        """Progress of the current YouTube cast (transcode state, segments)."""
        import asyncio
        return await asyncio.to_thread(self._yt_api, "/status")

    async def yt_cast_stop(self) -> dict[str, Any]:
        """Stop the currently casting YouTube video on the TV."""
        import asyncio
        return await asyncio.to_thread(self._yt_api, "/stop", {})

    async def _yt_cached_list(self) -> dict[str, Any]:
        """List cast-ready dubbed/cached videos in the yt-live library.
        Each entry has `name` and a LAN `url` ready for
        yt(action='cast', url=...). When the user asks to 'play the <name>
        video on TV', call this first to resolve the file, then cast its
        url."""
        import asyncio
        return await asyncio.to_thread(self._yt_api, "/list")

    # -- YouTube transcript (yt-dlp on mn01 — Thai news sites block scrapers,
    #    YouTube auto-captions are the open lane) --

    # -- CCTV peek: single frame via go2rtc on tony-dell, saved to the HA
    #    /local/ static dir so the TV/vcast browsers can load it without auth --

    _CCTV_CAMS = {
        # go2rtc stream -> HA camera / friendly label (tony-dell :1984)
        "coffee corner": "ip_cam_65_hd", "coffee": "ip_cam_65_hd",
        "c201": "xiaomi_c201_hd", "xiaomi c201": "xiaomi_c201_hd",
        "c100": "xiaomi_c100_hd", "xiaomi c100": "xiaomi_c100_hd",
        "ip_cam_65": "ip_cam_65_hd", "ip_cam_65_hd": "ip_cam_65_hd",
        "ip_cam_65_low": "ip_cam_65_low",
        "xiaomi_c201": "xiaomi_c201_hd", "xiaomi_c201_hd": "xiaomi_c201_hd",
        "xiaomi_c100": "xiaomi_c100_hd", "xiaomi_c100_hd": "xiaomi_c100_hd",
        "xiaomi_c201_sd": "xiaomi_c201", "xiaomi_c100_sd": "xiaomi_c100",
    }
    _YT_CAMS = {
        # YouTube live "cameras" — a single frame is grabbed server-side
        # by ~/.local/bin/yt-frame.sh on ADA_CCTV_SSH (yt-dlp + ffmpeg),
        # published as a relay asset, shown as a plain image. Easier and
        # more reliable than an iframe for feeds like safari cams.
        "safari": "https://www.youtube.com/watch?v=ydYDqZQpim8",
        "africam": "https://www.youtube.com/watch?v=ydYDqZQpim8",
        "namibia": "https://www.youtube.com/watch?v=ydYDqZQpim8",
        "namib": "https://www.youtube.com/watch?v=ydYDqZQpim8",
        "namib desert": "https://www.youtube.com/watch?v=ydYDqZQpim8",
        "watering hole": "https://www.youtube.com/watch?v=ydYDqZQpim8",
    }

    @staticmethod
    def _vms_publish(camera: str) -> dict[str, Any]:
        """VMS-channel path: pull one frame from the XMEye shim (mn01:8377)
        and publish it as a relay asset so vcast pages get a SAME-ORIGIN
        URL — the canvas stays clean for vcast_snapshot verification.
        Returns {"ok", "url"|"error"}."""
        from . import ToolRunner  # deferred: package init
        import base64, time
        vms = os.environ.get("ADA_VMS_SNAP_URL", "").rstrip("/")
        if not vms:
            return {"ok": False, "error": "VMS snapshot service not configured"}
        try:
            import urllib.parse
            url = f"{vms}/snap?ch={urllib.parse.quote(camera)}"
            # P2P streams are flaky — a dead pane now 503s; retry once.
            png = b""
            last_exc: Exception | None = None
            for _try in range(2):
                try:
                    with urllib.request.urlopen(
                            urllib.request.Request(url), timeout=75) as r:
                        png = r.read()
                    if len(png) >= 500:
                        break
                except Exception as exc:
                    last_exc = exc
                    png = b""
                    time.sleep(2)
            if last_exc is not None and not png:
                raise last_exc
            if len(png) < 500:
                return {"ok": False, "error": f"no frame from {camera!r} (camera may be offline)"}
            slug = "".join(c if c.isalnum() else "-"
                           for c in camera.lower()).strip("-")
            token = f"cam:{slug}-{int(time.time())}"
            ToolRunner._vcast_api("/frame", {
                "screen": 0, "token": token,
                "data": "data:image/png;base64," + base64.b64encode(png).decode(),
                "state": "asset"})
            # URL the DISPLAY fetches — must be the public same-origin https
            # route (vcast pages sit under tony-dell.../apps/), never the
            # local VCAST_API base (http cross-origin -> canvas taint).
            pub = os.environ.get(
                "VCAST_PUBLIC_API",
                "https://tony-dell.taila0626a.ts.net/api/input-bridge")
            return {"ok": True, "url": f"{pub}/frame?screen=0&token={token}",
                    "camera": camera}
        except Exception as exc:
            return {"ok": False, "error": f"VMS snapshot failed: {exc}"}

    @staticmethod
    def _yt_publish(camera: str, yt_url: str) -> dict[str, Any]:
        """YouTube-live path: one frame via yt-frame.sh on ADA_CCTV_SSH,
        published as a relay asset (same-origin /frame?token=) so the
        vcast canvas stays clean. Returns {"ok", "url"|"error"}."""
        from . import ToolRunner  # deferred: package init
        import base64, subprocess, time
        host = os.environ.get("ADA_CCTV_SSH", "tony-dell-m2m")
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 host, f"~/.local/bin/yt-frame.sh '{yt_url}' /dev/stdout"],
                capture_output=True, timeout=75)
            jpg = proc.stdout or b""
            if len(jpg) < 500:
                return {"ok": False,
                        "error": "no frame from youtube "
                                 f"(stream offline? {proc.stderr.decode()[-160:]})"}
            slug = "".join(c if c.isalnum() else "-"
                           for c in camera.lower()).strip("-")
            token = f"cam:{slug}-{int(time.time())}"
            ToolRunner._vcast_api("/frame", {
                "screen": 0, "token": token,
                "data": "data:image/jpeg;base64," + base64.b64encode(jpg).decode(),
                "state": "asset"})
            pub = os.environ.get(
                "VCAST_PUBLIC_API",
                "https://tony-dell.taila0626a.ts.net/api/input-bridge")
            return {"ok": True, "url": f"{pub}/frame?screen=0&token={token}",
                    "camera": camera}
        except Exception as exc:
            return {"ok": False, "error": f"youtube frame failed: {exc}"}

    @staticmethod
    def _cctv_grab(camera: str) -> dict[str, Any]:
        """Fetch one JPEG via go2rtc on tony-dell into the HA /local/ dir.
        Falls back to the VMS shim for estate cameras (front road, pool,
        tennis, etc). Returns {"ok", "url"|"error"}."""
        from . import ToolRunner  # deferred: package init
        import subprocess
        import time
        src = ToolRunner._CCTV_CAMS.get(camera.strip().lower())
        if not src:
            yt = ToolRunner._YT_CAMS.get(camera.strip().lower())
            if yt:
                return ToolRunner._yt_publish(camera, yt)
            # not a go2rtc home cam — try the VMS estate channel set
            return ToolRunner._vms_publish(camera)
        # Try the preferred stream, then fall back through SD/base variants —
        # _hd streams die when a cam degrades while the base stream survives
        # (c100/ip65 returned 200+0B on _hd while the plain names were fine).
        variants = [src]
        if src.endswith("_hd"):
            variants += [src[:-3], src[:-3] + "_sd"]
        name = f"snap-{src}-{int(time.time())}.jpg"
        host = os.environ.get("ADA_CCTV_SSH", "tony-dell-m2m")
        tried = []
        ok = False
        for v in variants:
            cmd = (
                f"mkdir -p ~/.config/home-assistant/www/cam && "
                f"curl -sf -m 20 -o ~/.config/home-assistant/www/cam/{name} "
                f"--size-limit 500 "
                f"'http://127.0.0.1:1984/api/frame.jpeg?src={v}' "
                f"&& [ -s ~/.config/home-assistant/www/cam/{name} ] && echo ok"
            )
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, cmd],
                capture_output=True, text=True, timeout=45)
            tried.append(v)
            if proc.stdout.strip() == "ok":
                src = v
                ok = True
                break
        if not ok:
            return {"ok": False,
                    "error": f"no frame from {src} (camera may be offline; tried {tried})"}
        base = os.environ.get(
            "ADA_CCTV_PUBLIC_BASE",
            "https://tony-dell.taila0626a.ts.net:8123/local/cam/")
        return {"ok": True, "url": base + name, "camera": src}

    async def ada_camera_snapshot(
            self, view: str = "", source: str = "auto",
            mode: str = "live", screen: int = 0, target: str = "",
            query: str = "", lat: float | None = None,
            lon: float | None = None, heading: float | None = None,
            channel: str = "", camera: str = "") -> dict[str, Any]:
        """One still frame from a camera, optionally pushed to a display.

        Canonical for the camera merge (tools-merge-camera): absorbs
        cctv_snapshot (snap + show on TV/screen) and traffic_camera
        (public Thailand traffic cams). source: 'auto' (default — traffic
        only when traffic-style args arrive), 'vms' (property CCTV +
        go2rtc home cams), 'traffic' (Longdo/iTIC). mode='cached' serves
        the camwall's last-good frame instead of a live pull.

        Runner path (tool_runner.execute / REST) — the frame can't attach
        to a live turn from here, so the result carries a fetchable url.
        The provider's dispatch attaches the image itself."""
        import asyncio
        src = str(source or "auto").lower()
        if src == "traffic" or (src == "auto" and (
                str(query or "").strip() or lat is not None
                or lon is not None or heading is not None)):
            return await asyncio.to_thread(
                self._traffic_snapshot, query or view, lat, lon, heading)
        if src not in ("auto", "vms"):
            return {"ok": False,
                    "error": f"unknown source {source!r} — use auto|vms|traffic"}
        want = str(view or channel or camera or "").strip()
        if not want:
            return {"ok": False,
                    "error": "view is required — a camera name "
                             "('swimming pool', 'c201', 'coffee corner')"}
        if str(mode or "").lower() == "cached":
            url, note = await asyncio.to_thread(self._camwall_fallback, want)
            if not url:
                return {"ok": False,
                        "error": note or f"no stored frame for {want!r}"}
            shot = {"ok": True, "url": url, "camera": want, "stored": True}
        else:
            shot = await asyncio.to_thread(self._cctv_grab, want)
        if not shot.get("ok"):
            return shot
        url = shot["url"]
        t = (target or "").strip().lower()
        n = int(screen or 0)
        if n or t == "screen" or t.startswith("vcast"):
            n = n or 1
            await self._check_screen_owner(n, self._memory_identity())
            out = await asyncio.to_thread(
                self._vcast_api, "/pub",
                {"screen": n, "msg": {"type": "image", "url": url}})
            out.update({"url": url, "camera": shot["camera"], "screen": n})
            if (n in self._tv_cfg()["screens"]
                    and out.get("delivered", 1)):
                tv = await self._tv_input_verify(url)
                out.update(tv)
                if tv.get("tv_note"):
                    out["note"] = tv["tv_note"]
            return out
        if t == "tv":
            out = await self.tv_action(cmd="nav", text=url)
            if isinstance(out, dict):
                out.update({"url": url, "camera": shot["camera"]})
            return out
        return {"ok": True, "url": url, "camera": shot["camera"],
                "output": f"Still frame from camera '{shot['camera']}' "
                          f"at {url}"}

    @staticmethod
    def _traffic_snapshot(query: str, lat: float | None,
                          lon: float | None,
                          heading: float | None) -> dict[str, Any]:
        """traffic_camera absorb (runner path): Longdo/iTIC search, snap
        the best match, publish a same-origin relay asset. Same flow as
        the provider's _traffic_camera minus the frame attachment."""
        from backend import traffic_camera as tc
        query = str(query or "").strip()
        if not query and lat is None:
            return {"ok": False,
                    "error": "pass a query (area/road) or lat+lon "
                             "(+optional heading)"}
        try:
            cams = tc.find_cams(query, lat, lon, heading)
        except Exception as exc:
            return {"ok": False, "error": f"camera feed unavailable: {exc}"}
        if not cams:
            return {"ok": False,
                    "error": f"no traffic camera matched "
                             f"{query or 'that position'}."}
        if cams[0].get("suspended"):
            return {"ok": False,
                    "error": cams[0]["title"] +
                             " — the feed currently has live frames for "
                             "Bangkok and Nonthaburi cams only."}
        dead = []
        for cand in cams[:4]:
            try:
                res = tc.snap(cand, 10)
            except Exception:
                res = None
            if res and res[0]:
                jpeg, _mime = res
                slug = re.sub(r"[^a-z0-9]+", "-",
                              (cand.get("camid") or "cam").lower())
                out = {"ok": True, "camid": cand["camid"],
                       "title": cand["title"], "matches": len(cams),
                       "output": f"Traffic camera '{cand['title']}'"}
                if cand.get("dist_km") is not None:
                    out["dist_km"] = cand["dist_km"]
                cast_url = tc.publish_relay(jpeg, slug)
                if cast_url:
                    out["cast_url"] = cast_url
                return out
            dead.append(cand["title"][:60])
        return {"ok": False,
                "error": f"{len(dead)} matched camera(s) returned no "
                         f"usable frame ({', '.join(dead)})."}

    def _camwall_fallback(self, cam: str) -> tuple[str, str]:
        """Resolve `cam` to the camwall puller's cached frame URL.

        Returns (url, note) — url empty when no manifest matches. The puller
        writes data/<zone>/<key>.jpg + manifest-<zone>.json per zone on
        tony-dell; the manifest marks each cam ok/err + ts so we prefer the
        freshest working frame over a stale one."""
        import difflib
        import urllib.parse
        import urllib.request
        # puller edges serve camwall data on :8380; Ada runs on idc01 so
        # loopback is the shortest path — env or VCAST host as fallback.
        cbase = os.environ.get("ADA_CAMWALL_BASE", "").rstrip("/")
        if not cbase:
            pub = os.environ.get(
                "VCAST_PUBLIC_API",
                "https://tony-dell.taila0626a.ts.net/api/input-bridge")
            parts = urllib.parse.urlsplit(pub)
            cbase = f"{parts.scheme}://{parts.netloc}/apps/camwall"
        try:
            wall = self._vcast_api("/camwall")
            zones = list((wall.get("zones") or {}).keys())
        except Exception:
            zones = []
        if not zones:
            from vms_camera import VMS_CAMWALL_ZONES
            zones = list(VMS_CAMWALL_ZONES)
        want = cam.strip().lower()
        best: tuple[float, str] | None = None  # (ts, url)
        for zone in zones:
            try:
                with urllib.request.urlopen(
                        f"{cbase}/data/{zone}/manifest-{zone}.json",
                        timeout=8) as r:
                    man = json.load(r)
            except Exception:
                continue
            for c in man.get("cams") or []:
                label = str(c.get("label") or c.get("key") or "").lower()
                key = str(c.get("key") or "")
                if want in label or label in want or \
                        difflib.SequenceMatcher(None, want, label).ratio() > 0.6:
                    url = f"{cbase}/data/{zone}/{key}.jpg"
                    ts = float(c.get("ts") or 0)
                    # a cam currently erroring still has its last frame —
                    # prefer ok cams, else newest ts wins
                    score = ts + (1e12 if c.get("ok") else 0)
                    if best is None or score > best[0]:
                        best = (score, url)
        if best:
            return best[1], "last cached frame"
        return "", "no camwall cached frame either"

    async def yt_transcript(self, url: str, language: str = "th") -> dict[str, Any]:
        """Fetch a YouTube video's auto-captions as plain text. `url` is a
        YouTube URL or video ID. The extraction runs on the transcript host
        (mn01) via `yt-transcript.sh`. Returns title, language and up to ~6k
        chars of transcript text — enough to summarize for a spoken report.
        Thai news sites block scrapers; this is the news-source fallback."""
        import asyncio
        import subprocess
        host = os.environ.get("ADA_YT_TRANSCRIPT_HOST", "mn01")
        script = os.environ.get(
            "ADA_YT_TRANSCRIPT_BIN", "~/.local/bin/yt-transcript.sh")
        proc = await asyncio.to_thread(
            subprocess.run,
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             host, script, url, language],
            capture_output=True, text=True, timeout=90)
        out = (proc.stdout or "").strip()
        if out.startswith("NO_CAPTIONS"):
            return {"ok": False, "error": "no captions available",
                    "title": out[11:].strip() or url}
        lines = out.splitlines()
        title = next((l[7:] for l in lines if l.startswith("TITLE: ")), url)
        lang = next((l[6:] for l in lines if l.startswith("LANG: ")), language)
        text = "\n".join(
            l for l in lines
            if not l.startswith(("TITLE:", "LANG:"))).strip()
        if not text:
            return {"ok": False, "error": "empty transcript", "title": title}
        return {"ok": True, "title": title, "language": lang,
                "transcript": text, "chars": len(text)}

    # -- vcast virtual displays (input-bridge relay on tony-dell :3010) --

    @staticmethod
    def _vcast_api(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        import urllib.request
        base = os.environ.get(
            "VCAST_API", "https://tony-dell.taila0626a.ts.net/api/input-bridge")
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            base + path, data=data,
            headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    async def cctv_wall(self, action: str, zone: str, screen: int = 0,
                        pane: int | None = None,
                        settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start/stop the periodic-thumbnail camera wall on a vcast screen.
        Enables the zone in the relay's /camwall state (the puller on
        tony-dell refreshes thumbs into /apps/camwall/data/<zone>/) and casts
        the wall page. Zones: zone-a, noble-park, tony-house, vms-noble-club,
        vms-noble-a, rama9 (demo traffic wall), traffic (DOH Bangkok),
        burapha (Bangna–Burapha expressway), chonburi (Chonburi corridor).
        Walls keep warm thumbs even while disabled, so casting an area is
        instant — the puller refreshes in the background.
        settings (optional) tunes the zone in the relay: {"interval": s,
        "jpeg_q": 1-8, "thumb_w": px, "cams_skip": [key], "cams_extra":
        [{label,kind,url}], "effects": ["timestamp","grid",
        "yolo:person,car@0.35"]}. yolo overlays detections and appends a
        rolling detections-<zone>.jsonl while the effect stays on."""
        import asyncio
        action = (action or "start").strip().lower()
        zone = (zone or "").strip().lower().replace(" ", "-")
        valid = {"zone-a", "noble-park", "tony-house",
                 "vms-noble-club", "vms-noble-a", "rama9",
                 "traffic", "burapha", "chonburi"}
        if zone not in valid:
            return {"error": f"unknown zone {zone!r} — valid: {sorted(valid)}"}
        if action == "settings":
            if not settings:
                return {"error": "settings action requires a settings object"}
            # the model sometimes double-wraps settings —
            # {"settings": {...}} — flatten before relay (2026-09-30: a
            # nested write silently failed to clear the yolo effect)
            if isinstance(settings, dict) and isinstance(
                    settings.get("settings"), dict):
                inner = settings.pop("settings")
                inner.update(settings)
                settings = inner
            out = await asyncio.to_thread(
                self._vcast_api, "/camwall",
                {"zone": zone, "settings": settings})
            if isinstance(out, dict):
                out["applied"] = settings
            return out
        if action == "status":
            # read the zone manifest (public static file on tony-dell) —
            # roster, per-cam freshness, yolo counts, wall health
            import urllib.request
            man_url = (os.environ.get(
                           "ADA_CAMWALL_BASE",
                           "https://tony-dell.taila0626a.ts.net/apps/camwall/")
                       + f"data/{zone}/manifest-{zone}.json")
            try:
                man = await asyncio.to_thread(
                    lambda: json.load(urllib.request.urlopen(
                        urllib.request.Request(man_url), timeout=15)))
            except Exception as exc:
                return {"ok": False, "zone": zone,
                        "error": f"manifest fetch failed: {exc}"}
            now = time.time()
            cams = [{
                "label": c.get("label"), "ok": bool(c.get("ok")),
                "age_s": int(now - c["ts"]) if c.get("ts") else None,
                "det": c.get("det"), "err": c.get("err"),
            } for c in man.get("cams", [])]
            out = {"ok": True, "zone": zone,
                   "live": sum(1 for c in cams if c["ok"]),
                   "total": len(cams), "cams": cams,
                   "wall_url": man_url.rsplit("/data/", 1)[0]
                               + f"/?zone={zone}"}
            # surface the relay's zone settings (interval, effects, …) so the
            # model sees what's tunable and reports the current config instead
            # of guessing — 2026-10-01: wall_ops turn failed with 'the tool
            # doesn't support intervals' because settings were invisible
            try:
                st = await asyncio.to_thread(self._vcast_api, "/camwall")
                zs = (st.get("zones") or {}).get(zone) or {}
                if zs:
                    out["enabled"] = zs.get("enabled")
                    out["settings"] = zs.get("settings") or {}
                    out["settings_hint"] = (
                        "change with action='settings', settings={...} "
                        "(interval s, jpeg_q 1-8, thumb_w px, cams_skip, "
                        "cams_extra, effects)")
            except Exception:
                pass
            return out
        if action == "stop":
            await asyncio.to_thread(
                self._vcast_api, "/camwall", {"zone": zone, "enabled": False})
            if screen:
                await asyncio.to_thread(
                    self._vcast_api, "/pub",
                    {"screen": int(screen), "msg": {"type": "stop"}})
                try:
                    await asyncio.to_thread(self._vcast_api, "/capture",
                                            {"screen": int(screen),
                                             "active": False})
                except Exception:
                    pass
            return {"ok": True, "zone": zone, "stopped": True}
        n = int(screen or 1)
        payload = {"zone": zone, "enabled": True, "screen": n}
        if settings:
            payload["settings"] = settings
        if n:
            await self._check_screen_owner(n, self._memory_identity())
        busy = await self._screen_busy(n, pane) if n else None
        await asyncio.to_thread(self._vcast_api, "/camwall", payload)
        if pane is None:
            try:
                await asyncio.to_thread(self._vcast_api, "/capture", {
                    "screen": n, "source": "camwall", "ch": zone,
                    "active": True, "by": "ada"})
            except Exception:
                pass
        # root-relative: displays may load vcast from LAN (no tailnet) —
        # a hardcoded tailnet URL iframes to nothing there (black screen).
        # Every tony-dell origin serves /apps/camwall (Caddy -> pull edges).
        url = f"/apps/camwall/?zone={zone}"
        # cycle/zones ride as URL params on the page — page-side display
        # behavior, not relay state, so they don't touch /camwall.
        cyc = (settings or {}).get("cycle_s")
        if cyc:
            url += f"&cycle={int(cyc)}"
        zlist = (settings or {}).get("zones")
        if zlist:
            url += f"&zones={','.join(zlist)}&zone_s={int((settings or {}).get('zone_s') or 45)}"
        if (settings or {}).get("random"):
            url += "&random=1"
        nav_msg: dict[str, Any] = {"type": "nav", "url": url}
        if pane is not None:
            nav_msg["pane"] = int(pane)
        out = await asyncio.to_thread(
            self._vcast_api, "/pub", {"screen": n, "msg": nav_msg})
        # wall health from the manifest — Ada warns immediately when the
        # zone is dead instead of the user discovering a blank wall
        try:
            st = await self.cctv_wall("status", zone)
            out["live_cams"] = st.get("live")
            out["total_cams"] = st.get("total")
            if st.get("ok") is False or (st.get("total") and not st.get("live")):
                out["note_extra"] = (
                    "ALL cameras in this wall are currently down — "
                    "warn the user the wall will show errors until the "
                    "source recovers.")
            elif st.get("live") is not None and st.get("live") < st.get("total"):
                out["note_extra"] = (
                    f"only {st['live']}/{st['total']} cameras are live — "
                    "tell the user which are down (see 'cams' in a status "
                    "call if they ask).")
        except Exception:
            pass
        out.update({
            "zone": zone, "screen": n, "url": url,
            "note": ("thumbs refresh in the background (VMS cams ~60s, "
                     "house ~30s) — the wall fills in within a minute."),
        })
        if busy:
            out["replaced"] = busy
            out["note"] += (f" It interrupted {busy['desc']} — "
                            "acknowledge that to the user.")
        if settings:
            out["applied"] = settings
        # TV-hosted screen: 'delivered' only means the relay acked — if
        # the TV's foreground app is another input the wall is invisible.
        # Verify via HA and push the wall URL at the TV browser on
        # mismatch (the proven workaround) instead of claiming success.
        if n in self._tv_cfg()["screens"]:
            tv = await self._tv_input_verify(url)
            out.update(tv)
            if tv.get("tv_note"):
                out["note"] += " " + tv["tv_note"]
        return out

    async def vcast_list(self) -> dict[str, Any]:
        """List registered vcast virtual displays (screen number, device,
        online/offline, current state), plus the relay's ground truth:
        active camera-capture leases and enabled cam-wall zones."""
        import asyncio
        data = await asyncio.to_thread(self._vcast_api, "/displays")
        out: dict[str, Any] = {
            "screens": [
                {
                    "screen": s["screen"],
                    "name": s["name"],
                    "device": s.get("label") or s["name"],
                    "online": s.get("connected", False),
                    "state": s.get("state") or "idle",
                    "detail": s.get("state_detail") or "",
                    "panes": s.get("panes"),
                }
                for s in data.get("screens", [])
            ],
            "pending": len(data.get("pending", [])),
        }
        try:
            caps = await asyncio.to_thread(self._vcast_api, "/capture")
            out["active_captures"] = caps.get("captures") or {}
        except Exception:
            pass
        try:
            wall = await asyncio.to_thread(self._vcast_api, "/camwall")
            zones = wall.get("zones") or {}
            out["camwall_zones"] = {
                z: v for z, v in zones.items() if v.get("enabled")}
            # Ground-truth check: the zone registry says a wall is on a
            # screen, but the screen's own state report is authoritative —
            # a user nav/reconnect can leave them diverged (2026-09-29:
            # wall claimed on screen 1 while the user saw a single cam).
            mismatches = []
            for z, v in out["camwall_zones"].items():
                scr = v.get("screen")
                s = next((x for x in out["screens"]
                          if x.get("screen") == scr), None)
                if s and s.get("online") and "camwall" not in (
                        s.get("detail") or ""):
                    mismatches.append(
                        f"zone '{z}' registered on screen {scr} but the "
                        f"screen reports '{s.get('state')}: "
                        f"{s.get('detail') or 'no detail'}' — the wall is "
                        "NOT actually showing; the zone flag is stale. Do "
                        "NOT tell the user it is up — restart it with "
                        "cctv_wall if they want it.")
            if mismatches:
                out["state_mismatch"] = mismatches
        except Exception:
            pass
        # TV input visibility: screens rendered by the TV's browser are
        # only visible while the TV's foreground app IS the browser —
        # surface it so Ada doesn't claim a wall/cast is showing while
        # the TV sits on the STB input (2026-10-04 incident).
        try:
            tvcfg = self._tv_cfg()
            for s in out.get("screens") or []:
                if s.get("screen") in tvcfg["screens"]:
                    s["on_tv"] = True
            ti = await self._tv_input_state()
            if ti.get("on_cast_app") is not None:
                out["tv_input"] = ti
                if ti["on_cast_app"] is False:
                    out["tv_input"]["warning"] = (
                        f"the TV is on '{ti.get('app') or ti.get('state')}' — "
                        "casts to TV-hosted screens are NOT visible right "
                        "now; say so or push the URL via tv_action nav.")
        except Exception:
            pass
        return out

    async def vcast_snapshot(self, screen: int) -> dict[str, Any]:
        """vcast_snapshot (runner path): ask the display to capture its
        own frame — /pub {type:snap-request,token} then poll
        /frame?screen&token until the JPEG lands. GEV iframes can't be
        read by the vcast page, so the same call also fires the GEV
        remote capture_frame command at that screen — that command is
        retired INTO this tool (tools-merge-camera), not a separate
        surface. The provider's live path attaches the frame to the
        turn; here the /frame URL is the result."""
        import asyncio
        import urllib.request
        try:
            n = int(screen)
        except (TypeError, ValueError):
            return {"ok": False,
                    "error": "screen number required — call "
                             "cast_to_screen(action='list') to see "
                             "registered displays."}
        base = os.environ.get(
            "VCAST_API",
            "https://tony-dell.taila0626a.ts.net/api/input-bridge")
        token = f"snap-{int(time.time() * 1000)}-runner"
        try:
            out = await asyncio.to_thread(
                self._vcast_api, "/pub",
                {"screen": n,
                 "msg": {"type": "snap-request", "token": token}})
        except Exception as exc:
            return {"ok": False, "error": f"snap-request failed: {exc}"}
        if not out.get("delivered", 0):
            return {"ok": False,
                    "error": f"screen {n} is not connected — "
                             "check cast_to_screen(action='list') for "
                             "online displays."}

        def _post(url: str, payload: dict):
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            return urllib.request.urlopen(req, timeout=10)

        def _get(url: str):
            return urllib.request.urlopen(url, timeout=10)

        # Fire the retired capture_frame command at this screen's GEV
        # remotes; whichever path lands the frame first wins the poll.
        try:
            await asyncio.to_thread(
                _post,
                os.environ.get(
                    "GEV_CMD_URL",
                    "https://tony-dell.taila0626a.ts.net"
                    "/apps/gev-cmd/command"),
                {"name": "capture_frame", "args": {"token": token},
                 "screen": n, "wait": 0})
        except Exception:
            pass  # bridge down or no GEV client — snap-request may land
        last_err = None
        for _ in range(15):
            try:
                r = await asyncio.to_thread(
                    _get, f"{base}/frame?screen={n}&token={token}")
                if r.headers.get("Content-Type", "").startswith("image/"):
                    pub = os.environ.get("VCAST_PUBLIC_API", base)
                    return {"ok": True, "screen": n,
                            "url": f"{pub}/frame?screen={n}&token={token}",
                            "output": f"Still frame captured from vcast "
                                      f"screen {n}."}
                body = json.loads(r.read() or b"{}")
                if body.get("error") == "simulated":
                    return {"ok": False,
                            "error": f"screen {n} is a headless/simulated "
                                     "display — no real pixels to capture "
                                     f"(state "
                                     f"'{body.get('state') or 'unknown'}')."}
                if body.get("error") and body["error"] != "image-load-failed":
                    last_err = body
                elif body.get("error"):
                    return {"ok": False,
                            "error": f"screen {n} could not capture: "
                                     f"{body['error']} (state="
                                     f"{body.get('state') or 'unknown'})."}
            except Exception as exc:
                last_err = {"error": str(exc)}
            await asyncio.sleep(0.8)
        return {"ok": False,
                "error": f"screen {n} did not return a frame in time"
                         + (f" ({last_err.get('error')})"
                            if last_err else "")}

    async def vcast_gesture(self, screen: int | None = None,
                            mode: str = "off") -> dict[str, Any]:
        """Toggle gesture control on a vcast display — publishes a
        {type:gesture, mode} command into the screen's room. The display
        acks via its reported state (gesture:<mode> on success, an
        error state if the camera or mode is unavailable)."""
        import asyncio
        if screen is None:
            return {"error": "screen required — call "
                             "cast_to_screen(action='list')"}
        mode = str(mode or "off").lower()
        if mode not in ("off", "room", "hand"):
            return {"error": "mode must be off|room|hand"}
        out = await asyncio.to_thread(
            self._vcast_api, "/pub",
            {"screen": screen, "msg": {"type": "gesture", "mode": mode}})
        if not out.get("ok"):
            return {"ok": False, "screen": screen, "error": out.get("error")
                    or "publish failed"}
        res = {"ok": True, "screen": screen, "mode": mode,
               "delivered": out.get("delivered", 0)}
        if not res["delivered"]:
            res["warning"] = ("screen connected but nothing delivered — "
                              "check cast_to_screen(action='list')")
        # give the page a beat to report its new state, then echo it back
        await asyncio.sleep(1.5)
        try:
            data = await asyncio.to_thread(self._vcast_api, "/displays")
            s = next((x for x in data.get("screens", [])
                      if x.get("screen") == screen), None)
            if s:
                res["screen_state"] = s.get("state")
                res["screen_detail"] = s.get("state_detail")
        except Exception:
            pass
        return res

    @staticmethod
    def _cast_screens_cfg() -> dict[str, Any]:
        """vcast screen-ownership registry (screen -> person.*|'shared',
        speaker aliases -> person). Missing file = no gating."""
        import json as _json
        try:
            with open(os.path.expanduser(
                    os.environ.get("ADA_CAST_SCREENS",
                                   "~/.local/share/ada-pi/cast-screens.json"))) as f:
                return _json.load(f)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _private_screen_denial(what: str, owner: str, person: str) -> PermissionError:
        """Owner-lock denial text. Must tell the model that the ACL lives in
        cast-screens.json — a memory note (ada_remember) can never change it,
        so 'record it as public then retry' loops forever."""
        who = owner.replace("person.", "")
        lock = ("owner-locks live in cast-screens.json; memory notes cannot "
                f"change them — {who} flips the entry to \"shared\" there "
                "if it should be public")
        if person == "anonymous":
            return PermissionError(
                f"denied: {what} is {who}'s private screen — speaker "
                f"unidentified. If the speaker is {who}, retry once "
                f"speaker-ID resolves them; {lock}")
        return PermissionError(
            f"denied: {what} is {who}'s private screen "
            f"(speaker={person.replace('person.', '')}) — {lock}")

    async def _check_screen_owner(self, screen: int, ident: str | None) -> None:
        """vcast screens are owner-locked: a speaker may only cast to their
        own screen unless the screen is 'shared'."""
        cfg = self._cast_screens_cfg()
        if not cfg:
            return  # no registry — don't gate
        owner = (cfg.get("screens") or {}).get(str(int(screen)), "shared")
        s = (ident or "").strip()
        person = (s if s.startswith("person.")
                  else (cfg.get("aliases") or {}).get(s, "anonymous"))
        if owner == "shared" or owner == person:
            return
        raise self._private_screen_denial(f"screen {screen}", owner, person)

    # cmd=type payload guard (card ada-tv-action-hallucinated-input,
    # 2026-10-09): Ada typed the literal scaffolds 'your-username' /
    # 'your-password' into a TV login form — model-invented placeholder
    # text, not user dictation. type may only carry the exact text the
    # user just said; empty or placeholder-looking text is refused.
    _TV_TYPE_PLACEHOLDER_RES = (
        re.compile(r"(?i)^your[\s\-_]"),          # your-username, your_password
        re.compile(r"<[^>\n]{0,80}>"),            # <username>, <...>
        re.compile(r"\{[^{}\n]{0,80}\}"),         # {token}
        re.compile(r"(?i)^x{2,}$"),               # xxx / xxxx masks
        re.compile(r"^[*•●.…\s]{2,}$"),           # ***, •••, "..."
    )
    _TV_TYPE_PLACEHOLDER_MAX = 40

    def _tv_type_text_problem(self, text: Any) -> str | None:
        """Refusal reason for a cmd=type payload, or None when the text
        is plausibly real user dictation."""
        t = str(text or "").strip()
        if not t:
            return (
                "tv_action cmd=type refused: text is empty — type "
                "carries only the exact text the user just dictated. If "
                "they have not given you the literal text, ask for it "
                "first; never type a placeholder scaffold.")
        if len(t) <= self._TV_TYPE_PLACEHOLDER_MAX and any(
                rx.search(t) for rx in self._TV_TYPE_PLACEHOLDER_RES):
            return (
                f"tv_action cmd=type refused: text {t!r} looks like a "
                "placeholder, not something the user said — type carries "
                "only the exact text the user just dictated. Ask for the "
                "literal text; to reach a field, use click/press instead.")
        return None

    def _check_tv_source_owner(self, target: str, ident: str | None) -> None:
        """Gate personal desktop streams in tv_action nav targets
        (screenlive:* / workspace:* / screen:* = the tony-dell seat,
        tony-omen:* = Tony's desktop). HA's rest_command swallows the
        controller's 403, so deny here before the call ever leaves.
        cast-browser still enforces as the authoritative backstop."""
        cfg = self._cast_screens_cfg()
        if not cfg:
            return
        t = (target or "").lower()
        source = None
        if re.match(r"^(screenlive|screen:|workspace)", t):
            source = "seat"
        elif t.startswith("tony-omen:"):
            source = "omen"
        if not source:
            return  # plain URL / named page — no personal screen involved
        owner = (cfg.get("sources") or {}).get(source, "shared")
        s = (ident or "").strip()
        person = (s if s.startswith("person.")
                  else (cfg.get("aliases") or {}).get(s, "anonymous"))
        if owner == "shared" or owner == person:
            return
        raise self._private_screen_denial(source, owner, person)

    _TV_DEFAULT_ENTITY = "media_player.tony_tv"
    _TV_DEFAULT_OK_APPS = ("browser", "com.webos.app.browser")

    def _tv_cfg(self) -> dict[str, Any]:
        """TV input-visibility config. A vcast screen rendered in the
        TV's webOS browser is only VISIBLE when the TV's foreground app
        is the browser — a delivered cast on another input looks like a
        no-op to the viewer (2026-10-04: cctv_wall 'delivered' while the
        TV sat on the True STB app for 3min). Registry 'tv' block in
        cast-screens.json: {"entity": "media_player.tony_tv",
        "screens": [5], "ok_apps": ["browser"]} — ok_apps match the
        media_player's app_id/source as substrings (case-insensitive).
        Env overrides: ADA_TV_ENTITY, ADA_TV_SCREENS (csv),
        ADA_TV_OK_APPS (csv)."""
        raw = self._cast_screens_cfg()
        tv = raw.get("tv") if isinstance(raw.get("tv"), dict) else {}
        entity = (os.environ.get("ADA_TV_ENTITY") or tv.get("entity")
                  or self._TV_DEFAULT_ENTITY)
        env_screens = os.environ.get("ADA_TV_SCREENS")
        if env_screens is not None:
            screens = {int(s) for s in env_screens.split(",")
                       if s.strip().isdigit()}
        else:
            screens = {int(s) for s in (tv.get("screens") or [])
                       if str(s).strip().isdigit()}
        env_apps = os.environ.get("ADA_TV_OK_APPS")
        if env_apps is not None:
            ok_apps = [a.strip().lower() for a in env_apps.split(",")
                       if a.strip()]
        else:
            ok_apps = [str(a).lower() for a in
                       (tv.get("ok_apps") or self._TV_DEFAULT_OK_APPS)]
        return {"entity": entity, "screens": screens, "ok_apps": ok_apps}

    async def _tv_input_state(self) -> dict[str, Any]:
        """Read the TV's foreground app via its HA media_player entity.
        Returns {entity, state, app, on_cast_app} — on_cast_app is True
        when the TV shows the cast surface (browser), False on a foreign
        input/app or when the TV is off, and None when HA cannot say
        (unreachable, entity missing, no app attribute). Never raises."""
        cfg = self._tv_cfg()
        out: dict[str, Any] = {"entity": cfg["entity"],
                               "on_cast_app": None}
        try:
            st = await self.context.ha_client.get_state(cfg["entity"])
        except Exception:
            return out
        if not isinstance(st, dict):
            return out
        out["state"] = str(st.get("state") or "unknown")
        attrs = st.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        app = str(attrs.get("app_id") or attrs.get("app_name")
                  or attrs.get("source") or "")
        out["app"] = app
        if out["state"] in ("off", "standby", "unavailable", "unknown"):
            out["on_cast_app"] = False
        elif app:
            out["on_cast_app"] = any(
                pat in app.lower() for pat in cfg["ok_apps"])
        return out

    _GEV_TV_TARGET_DEFAULT = "tony-omen:workspace:4"

    def _gev_tv_target(self, cmd: str, target: str) -> str | None:
        """Nav targets that mean God's Eye View resolve to the live-
        desktop lane spec ('tony-omen:workspace:N' style — the stream,
        workspace and camera are all cast-browser-side). 'gev',
        "god's eye view" spellings and /apps/gev/ URLs (relative or
        absolute) all match — a raw URL nav would cast a still PNG.
        The lane comes from cast-screens.json "gev".target, overridable
        via ADA_GEV_TV_TARGET."""
        parts = str(cmd or "").lower().split()
        if not parts or parts[0] != "nav":
            return None
        t = str(target or "").strip().lower()
        if not t:
            return None
        if not (re.fullmatch(r"god'?s?\s*eye\s*view|gev", t)
                or re.search(r"/apps/gev(?:[/?#]|$)", t)):
            return None
        cfg = self._cast_screens_cfg()
        gev = cfg.get("gev") if isinstance(cfg.get("gev"), dict) else {}
        return (os.environ.get("ADA_GEV_TV_TARGET")
                or str(gev.get("target") or "").strip()
                or self._GEV_TV_TARGET_DEFAULT)

    _TV_CAST_FAIL_STATES = frozenset(
        {"off", "idle", "standby", "unavailable", "unknown"})
    _TV_CAST_SELECT = "input_select.tv_cast_target"
    _TV_CAST_SELECT_MAP = {"TONY-TV Cast": "media_player.tony_tv_cast"}
    _TV_CAST_DEFAULT_ENTITY = "media_player.tony_tv_cast"

    async def _tv_cast_entity(self) -> str:
        """The media_player cast-browser actually casts to — mirrors
        its getCastTarget(): input_select.tv_cast_target wins (mapped
        through the 'TONY-TV Cast' alias or slugged), else the
        Chromecast default. ADA_TV_CAST_ENTITY overrides outright."""
        env = os.environ.get("ADA_TV_CAST_ENTITY")
        if env:
            return env
        try:
            st = await self.context.ha_client.get_state(
                self._TV_CAST_SELECT)
            sel = str(st.get("state") or "").strip()
            if sel:
                if sel in self._TV_CAST_SELECT_MAP:
                    return self._TV_CAST_SELECT_MAP[sel]
                if sel.startswith("media_player."):
                    return sel
                slug = _slug(sel)
                if slug:
                    return f"media_player.{slug}"
        except Exception:
            pass
        return self._TV_CAST_DEFAULT_ENTITY

    async def _tv_cast_verify(self) -> dict[str, Any]:
        """Post-cast state check on the cast target: poll a few seconds
        for the player to leave the dead states (off/idle/standby/
        unavailable). {ok:True,state} when it woke, {ok:False,state}
        when it stayed dead, {ok:None} when HA can't say — an
        inconclusive read never fails the cast."""
        out: dict[str, Any] = {"ok": None}
        try:
            entity = await self._tv_cast_entity()
            out["entity"] = entity
            last = None
            for _ in range(5):
                st = await self.context.ha_client.get_state(entity)
                last = str(st.get("state") or "unknown").lower()
                if last not in self._TV_CAST_FAIL_STATES:
                    out["ok"] = True
                    out["state"] = last
                    return out
                await asyncio.sleep(1.5)
            out["ok"] = False
            out["state"] = last
        except Exception:
            pass
        return out

    def _tv_abs_url(self, url: str) -> str:
        """Root-relative vcast paths can't be pushed at the TV browser —
        resolve them against the public relay origin (same base the
        camwall fallback uses; every tony-dell origin serves /apps)."""
        url = str(url or "")
        if url.startswith(("http://", "https://")) or not url:
            return url
        import urllib.parse
        parts = urllib.parse.urlsplit(os.environ.get(
            "VCAST_PUBLIC_API",
            "https://tony-dell.taila0626a.ts.net/api/input-bridge"))
        return f"{parts.scheme}://{parts.netloc}{url}"

    async def _tv_input_verify(self, url: str) -> dict[str, Any]:
        """Post-cast truth check for a TV-hosted screen. Reads the TV's
        foreground app via HA; a foreign app means the delivered cast is
        invisible. Remediation is the proven 2026-10-04 workaround: push
        the URL straight at the TV browser (tv_action nav), which
        foregrounds the browser app and shows the page. The result block
        always reports what actually happened — a still-wrong input is a
        visible failure, not a silent 'ok'."""
        first = await self._tv_input_state()
        if first.get("on_cast_app") is not False:
            return {"tv_input": first}
        full_url = self._tv_abs_url(url)
        rem: dict[str, Any] = {"action": "tv_browser_nav",
                               "url": full_url}
        try:
            nav = await self.tv_action(cmd="nav", text=full_url)
            rem["ok"] = not (isinstance(nav, dict)
                             and nav.get("ok") is False)
        except Exception as exc:
            rem["ok"] = False
            rem["error"] = str(exc)
            nav = None
        # The nav's own post-check (runner.tv_action appends tv_input on
        # cmd=nav) doubles as the re-read; fall back to a direct read.
        after = nav.get("tv_input") if isinstance(nav, dict) else None
        if not isinstance(after, dict) or after.get("on_cast_app") is None:
            after = await self._tv_input_state()
        if isinstance(after, dict) and after.get("app"):
            rem["after_app"] = after["app"]
        visible = after.get("on_cast_app")
        if visible is True:
            note = ("the TV was on a different input — the URL was pushed "
                    "to the TV browser and is now showing; tell the user "
                    "the TV input was switched to it.")
        elif visible is False:
            note = ("WARNING: the TV is still on "
                    f"{after.get('app') or after.get('state')} — the cast "
                    "is NOT visible. Do not claim it is showing; tell the "
                    "user plainly and suggest switching the TV "
                    "input/source manually.")
        else:
            note = ("the TV was on a different input — pushed the URL to "
                    "the TV browser, but the TV's input state could not "
                    "be re-verified; do not claim it is showing until "
                    "checked.")
        return {"tv_input_mismatch": True, "tv_input": first,
                "tv_remediation": rem, "tv_input_after": after,
                "tv_note": note}

    async def _screen_busy(self, screen: int,
                           pane: int | None = None) -> dict[str, Any] | None:
        """Is a screen running a flow a new cast would interrupt? Returns
        {'kind','desc'} for active capture leases, enabled camwall zones
        and playing media; None for idle/nav/image/speak states.
        pane=N targets a sub-pane: exempt when the screen is actually
        split (panes>1 in the registry) — it fills a cell, not the whole
        display. On a non-split screen pane 0 is the whole screen."""
        import asyncio
        if pane is not None:
            try:
                disp = await asyncio.to_thread(self._vcast_api, "/displays")
                n = next((s.get("panes") or 1
                          for s in disp.get("screens", [])
                          if str(s.get("screen") or "") == str(screen)), 1)
                if n > 1:
                    return None
            except Exception:
                pass
        try:
            caps = await asyncio.to_thread(self._vcast_api, "/capture")
            for k, cap in (caps.get("captures") or {}).items():
                if str(k) == str(screen) and cap.get("active"):
                    # A capture lease outlives its display: the wall page
                    # clears it on nav, but a user cast or reconnect leaves
                    # it active — false would_interrupt forces a manual
                    # reset (2026-09-30 transcript). Trust the screen's own
                    # reported state: if it's connected and NOT showing a
                    # capture, the lease is stale — release it.
                    try:
                        disp = await asyncio.to_thread(
                            self._vcast_api, "/displays")
                        scr = next(
                            (s for s in disp.get("screens", [])
                             if str(s.get("screen") or "") == str(screen)),
                            {})
                    except Exception:
                        scr = {}
                    st = str(scr.get("state") or "")
                    det = str(scr.get("state_detail") or "")
                    if (scr.get("connected")
                            and "camwall" not in st + det
                            and "uplink" not in st + det
                            and "capture" not in st + det):
                        try:
                            await asyncio.to_thread(
                                self._vcast_api, "/capture",
                                {"screen": int(k), "active": False})
                        except Exception:
                            pass
                        break
                    return {"kind": "capture",
                            "desc": f"a camera capture/uplink "
                                    f"({cap.get('source') or 'cam'}) is "
                                    "live on it"}
        except Exception:
            pass
        try:
            wall = await asyncio.to_thread(self._vcast_api, "/camwall")
            zones = {z for z, v in (wall.get("zones") or {}).items()
                     if v.get("enabled")
                     and str(v.get("screen") or "") == str(screen)}
            if zones:
                # A zone's screen binding goes stale constantly: the puller
                # keeps refreshing thumbs long after the display moved on.
                # Trust the screen's own reported state over the flag —
                # only treat the wall as "busy" if the display still shows
                # it (or the display didn't answer at all).
                try:
                    disp = await asyncio.to_thread(
                        self._vcast_api, "/displays")
                    scr = next(
                        (s for s in disp.get("screens", [])
                         if str(s.get("screen") or "") == str(screen)), {})
                except Exception:
                    scr = {}
                detail = str(scr.get("state_detail") or "")
                live_wall = ("camwall" in detail
                             or any(z in detail for z in zones))
                if scr.get("state") is None or live_wall:
                    z = sorted(zones)[0]
                    return {"kind": "camwall", "zone": z,
                            "desc": f"the '{z}' camera wall is "
                                    "refreshing on it"}
                # stale flag — clear it so the next check is clean
                for z in zones:
                    try:
                        await asyncio.to_thread(
                            self._vcast_api, "/camwall",
                            {"zone": z, "screen": None})
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            disp = await asyncio.to_thread(self._vcast_api, "/displays")
            st = next((s.get("state") for s in disp.get("screens", [])
                       if str(s.get("screen") or "") == str(screen)), None)
            if st == "playing":
                return {"kind": "playing",
                        "desc": "a stream/video is playing on it"}
        except Exception:
            pass
        return None

    async def cast_to_screen(self, screen: int | str | None = None,
                             action: str = "nav",
                             url: str = "", pane: int | None = None,
                             panes: int | None = None,
                             mode: str | None = None,
                             interval: int | None = None,
                             text: str = "",
                             shortcut: str = "",
                             name: str = "",
                             to_screen: int | None = None,
                             confirmed: bool = False) -> dict[str, Any]:
        """Cast to a numbered vcast virtual display (NOT the TV).
        action: nav|play|image|audio|stop|layout|zoom|unzoom|uplink|
        uplink-stop, plus the merged vcast_* seats (tools-merge-display):
        'cast' (auto-route by content), 'say' (was vcast_say — narrate on
        the display), 'list' (was vcast_list — enumerate displays),
        'status' (was vcast_status — one screen's live state) and
        'shortcut' (was vcast_shortcut — nav a named /apps/<name>/ app).
        url required for nav/play/image/audio/cast; text for say.

        Display management seats:
        'claim' — pair a pending display by the 4-digit code shown on its
        QR screen: text="<code>", optional name="<label>", optional
        screen=<number it should take>. "Ada, claim display 4821 as
        tv-corner on screen 9."
        'assign' — retitle or renumber a registered display:
        name="<new label>" sets the human name, to_screen=<M> moves the
        number. screen may be a number or the display's current label.
        'background' — veil the screen while its content keeps running
        (audio continues; used for the percussion app and similar):
        mode='off' brings it back to the foreground.

        action='image' is for still frames (JPEG/PNG) — optional
        interval=N re-fetches the image every N seconds (good for
        traffic-cam stills that refresh server-side). 'play' is ONLY for
        real video streams (mp4/HLS) — a still image sent to play renders
        a black video pane, so this tool auto-routes image content to
        'image'.

        Content routing: web pages -> 'nav'; video files/streams (mp4,
        m3u8) and YouTube/Vimeo watch URLs -> 'play' (the display
        auto-rewrites them to embed players — pass the URL as-is, do NOT
        use yt action='cast', which is the TV only); still images -> 'image';
        audio-only -> 'audio'.

        Chat screens: the ada-chat-card auto-opens a per-chat vcast display
        labeled 'ada-chat-<id>' — action='list' shows its screen number.
        "Show X on my chat screen" = cast to that screen. To mirror the
        chat screen onto another display (screen-1, tony-tv lanes), read
        the chat screen's state via action='status' and re-cast the same
        URL/content to the target screen.

        Split-screen: action='layout' + panes=2..5 splits the screen into
        that many sub-panes; subsequent casts take pane=0..N-1 (0 is
        left/top). action='zoom' + pane=N makes one pane fullscreen,
        'unzoom' (or zoom pane=-1) returns to the grid. A stop with pane=N
        clears just that pane; stop alone resets the whole screen to
        single-pane idle."""
        import asyncio
        action = str(action or "nav").lower()
        # Merged vcast_* seats — the reads run without a screen and before
        # the owner check (listing displays was never owner-locked); 'say'
        # delegates to the absorbed vcast_say body, which owner-checks the
        # screen itself.
        if action == "list":
            return await self.vcast_list()
        if action == "status":
            return await self._vcast_display_status(screen)
        if action == "claim":
            # Voice pairing — the unpaired display shows a 4-digit code
            # under its QR; the relay resolves code -> pending sid and runs
            # the normal claim (mint ada key, push api_key to the display).
            code = re.sub(r"\D", "", text or "")
            if len(code) != 4:
                return {"ok": False, "delivered": 0,
                        "error": "claim needs the 4-digit code shown on "
                                 "the display's pair screen — pass it in "
                                 "text (e.g. text='4821')."}
            body: dict[str, Any] = {"code": code}
            if str(name or "").strip():
                body["name"] = str(name).strip()
            if screen is not None and str(screen).strip().isdigit():
                body["screen"] = int(screen)   # honored as want_screen
            out = await asyncio.to_thread(self._vcast_api, "/claim", body)
            if out.get("ok"):
                out["claimed"] = (f"display claimed as screen "
                                  f"{out.get('screen')} ({out.get('name')})")
            return out
        # display labels double as addresses — 'the living-room screen'
        if isinstance(screen, str) and screen.strip() and not screen.strip().isdigit():
            found = await asyncio.to_thread(self._screen_by_label, screen)
            if found is None:
                return {"ok": False, "delivered": 0, "error": (
                    f"no display labeled/named {screen!r} — "
                    "cast_to_screen(action='list') shows them all.")}
            screen = found
        if screen is None:
            return {"ok": False, "delivered": 0,
                    "error": "screen number required — "
                             "cast_to_screen(action='list') shows the "
                             "registered displays."}
        screen = int(screen)
        if action == "assign":
            # retitle (name=label) and/or renumber (to_screen=M) a
            # registered display — the relay pushes a 'rescreen' to the
            # live display so its HUD follows without a reload.
            body = {"screen": screen}
            if str(name or "").strip():
                body["label"] = str(name).strip()
            if to_screen is not None:
                body["move_to"] = int(to_screen)
            if len(body) == 1:
                return {"ok": False, "delivered": 0, "error": (
                    "assign needs name='<label>' and/or to_screen=<n>")}
            return await asyncio.to_thread(self._vcast_api, "/screen", body)
        if action == "background":
            # veil the screen while its content keeps running — used for
            # audio apps like /apps/percussion/ ("background the music").
            # mode='off' lifts the veil; absent mode defaults to on.
            on = str(mode or "on").lower() not in {"off", "0", "false", "no"}
            return await asyncio.to_thread(
                self._vcast_api, "/pub",
                {"screen": screen,
                 "msg": {"type": "bg", "on": on}})
        if action == "say":
            return await self.vcast_say(screen, text)
        if action == "shortcut":
            url = self._cast_shortcut_url(url or shortcut)
            if not url:
                return {"ok": False, "delivered": 0, "error": (
                    "action='shortcut' needs an app name ('gev', "
                    "'camwall', …) or a URL/path in url")}
            action = "nav"
        await self._check_screen_owner(screen, self._memory_identity())
        # Interrupt gate: replacing content on a busy screen (camera
        # capture, camwall zone, playing stream) needs the user's yes —
        # Ada must say what's running and get consent before clobbering.
        # layout alone is exempt: it reshapes the screen but keeps the
        # content panes it can carry (a capture lease isn't clobbered by
        # regridding). zoom/unzoom/stop-of-pane are user-directed UI ops.
        busy = None
        # 'cast' is checked pre-routing too — it always lands on a
        # content action, and an unprobed 'cast' onto a busy screen must
        # confirm just like a nav/play would.
        if action in {"nav", "play", "image", "audio", "uplink", "cast"}:
            busy = await self._screen_busy(screen, pane)
            if busy and confirmed is not True:
                return {"ok": False, "delivered": 0,
                        "would_interrupt": busy,
                        "needs_confirm": (
                            f"screen {screen} is busy: {busy['desc']} "
                            "Tell the user what is running, ask whether to "
                            "replace it, then call again with "
                            "confirmed=true only after they say yes. "
                            "If the user instead says to reset/clear/stop "
                            "the screen, use cast_to_screen(action='stop') "
                            "first — that releases it without confirm.")}
        probe: dict[str, Any] = {}
        auto_image = False
        cast_routed = False
        if action == "layout":
            msg: dict[str, Any] = {"type": "layout",
                                   "panes": int(panes or 1)}
            if mode:
                msg["mode"] = str(mode)
        elif action in {"zoom", "unzoom"}:
            msg = {"type": "zoom",
                   "pane": -1 if action == "unzoom" else int(pane or 0)}
        elif action in {"stop", "uplink", "uplink-stop"}:
            msg = {
                "type": "uplink-start" if action == "uplink" else action}
        else:
            if action == "cast":
                # Generic cast: pick the pane type from what the URL
                # actually serves — a still image in <video> renders
                # black, a page in an image pane never loads.
                if not url:
                    raise ValueError("url is required for action='cast'")
                if str(url).startswith(("http://", "https://")):
                    try:
                        probe = await asyncio.to_thread(
                            self._frame_check, str(url))
                    except Exception:
                        probe = {}
                ctype = str(probe.get("content_type") or "").lower()
                if ctype.startswith("image/"):
                    action = "image"
                elif ctype.startswith("audio/"):
                    action = "audio"
                elif (ctype.startswith("video/") or "mpegurl" in ctype
                      or re.search(
                          r"(youtube\.com|youtu\.be|youtube-nocookie\.com"
                          r"|vimeo\.com)", str(url))
                      or re.search(
                          r"\.(m3u8|mp4|webm|mov|m4v)(\?|#|$)",
                          str(url), re.I)):
                    action = "play"
                else:
                    action = "nav"
                cast_routed = True
            if action not in {"nav", "play", "image", "audio"}:
                raise ValueError(
                    f"unknown action {action!r} (cast|nav|play|image|audio|stop|layout|zoom|unzoom|uplink|uplink-stop|say|list|status|shortcut)")
            if not url:
                raise ValueError("url is required for " + action)
            # Pre-flight for nav/play: probe the target BEFORE pubbing —
            # a dead URL or a still image sent to <video> both render as a
            # black pane that looks identical to "it didn't work"
            # (2026-10-01: Ada cast a JPEG snapshot with action='play' and
            # an invented URL that didn't even resolve; screen 1 just
            # stayed on the previous camera with no visible error).
            if (action in {"nav", "play"} and not probe
                    and str(url).startswith(("http://", "https://"))):
                try:
                    probe = await asyncio.to_thread(
                        self._frame_check, str(url))
                except Exception:
                    probe = {}
            if probe.get("dead"):
                return {
                    "ok": False, "delivered": 0,
                    "error": (f"URL is unreachable ({probe['dead']}) — not "
                              "casting. Never invent a URL: re-fetch a "
                              "current one from the source tool (e.g. "
                              "ada_camera_snapshot returns cast_url) and "
                              "retry "
                              "with that exact value.")}
            if (action == "play"
                    and str(probe.get("content_type") or "").lower()
                    .startswith("image/")):
                # Still image in a <video> element never decodes —
                # auto-route to the image pane so the cast works instead
                # of producing a black video-vw0 pane.
                action = "image"
                auto_image = True
            msg = {"type": action, "url": url}
            if interval is not None and action == "image":
                msg["interval"] = int(interval)
        if pane is not None and action not in {"layout", "unzoom"}:
            msg["pane"] = int(pane)
        # Snapshot what the screen is showing BEFORE a full-frame nav/play/
        # image replaces it — a camwall nav'd over a GEV page silently kills
        # the map session (2026-10-02 ZA tour: cctv_wall to screen 5 evicted
        # the tour with no warning; Ada should have used a PiP pane).
        displaced: str | None = None
        if action in {"nav", "play", "image", "audio"} and pane is None:
            try:
                disp = await asyncio.to_thread(self._vcast_api, "/displays")
                scr = next(
                    (s for s in disp.get("screens", [])
                     if str(s.get("screen") or "") == str(screen)), None)
                if scr and scr.get("connected"):
                    pd = str(scr.get("state_detail") or "")
                    if scr.get("state") == "nav" and pd and "gev" in pd:
                        displaced = ("GEV session — the map/annotations on "
                                     "this screen are gone; for a camera "
                                     "alongside the map use a split layout "
                                     "and cast_to_screen(image, pane=N)")
                    elif scr.get("state") == "nav" and pd:
                        displaced = f"the '{pd[:60]}' page"
            except Exception:
                pass
        warn = probe.get("warn")
        out = await asyncio.to_thread(
            self._vcast_api, "/pub", {"screen": screen, "msg": msg})
        if warn:
            out["frame_warn"] = warn
        if auto_image:
            out["action_fixed"] = (
                "url serves a still image — cast as 'image', not 'play' "
                "(a video element cannot decode it and shows black)")
        if cast_routed:
            out["action_routed"] = (
                f"action='cast' resolved to '{action}' from the URL's "
                "content type")
        # capture lease bookkeeping — the relay's /capture state is ground
        # truth for the ask-before-stopping contract; the display also POSTs
        # on uplink-start, but this covers display-offline cases
        if action in {"uplink", "uplink-stop", "stop"}:
            try:
                await asyncio.to_thread(self._vcast_api, "/capture", {
                    "screen": screen,
                    "active": action == "uplink",
                    "source": "cam",
                    "by": "ada",
                })
            except Exception:
                pass
        try:
            caps = await asyncio.to_thread(self._vcast_api, "/capture")
            out["active_captures"] = caps.get("captures") or {}
        except Exception:
            pass
        if busy:
            out["replaced"] = busy
            out["note"] = ("this cast interrupted something that was "
                           "running — acknowledge it to the user "
                           f"({busy['desc']}).")
        if displaced:
            out["displaced"] = displaced
        # Post-cast ground truth: pub only means the relay accepted the
        # frame — the display may still be showing the old page (iframe
        # refused, browser didn't navigate, stale client).  Re-read the
        # screen's own report so Ada claims only what the display claims.
        try:
            await asyncio.sleep(0.8)  # let the display ack state
            disp = await asyncio.to_thread(self._vcast_api, "/displays")
            scr = next(
                (s for s in disp.get("screens", [])
                 if str(s.get("screen") or "") == str(screen)), None)
            if scr is not None:
                detail = str(scr.get("state_detail") or "")
                out["screen_state"] = scr.get("state")
                out["screen_detail"] = detail
                if action in {"nav", "play", "image", "audio"} and url:
                    host = str(url).split("/")[2] if "//" in str(url) else str(url)
                    if url not in detail and host not in detail:
                        out["verify_warn"] = (
                            f"screen {screen} still reports "
                            f"'{detail[:80] or scr.get('state')}' — the new "
                            "page may not have loaded; do not claim it "
                            "changed.")
                # Dead render detection: 'video-vw0' means the video
                # element decoded nothing (a still image cast with
                # action='play' lands here — black pane); the image pane
                # reports 'image-load-failed' outright. Do not claim the
                # cast worked when the pane is dead.
                if (scr.get("state") == "image-load-failed"
                        or "video-vw0" in detail):
                    out["render_warn"] = (
                        f"screen {screen} reports no decodable content "
                        f"('{detail[:80] or scr.get('state')}') — it is "
                        "likely showing black or the previous page. If the "
                        "source is a still image, re-cast with "
                        "action='image'; if the URL is dead, fetch a fresh "
                        "one from the source tool. Do NOT tell the user it "
                        "changed.")
        except Exception:
            pass
        # TV-hosted screen: the display page can ack the nav while the TV
        # is on another input — verify and remediate via the TV browser
        # rather than claiming the cast is visible.
        if (action in {"nav", "play", "image", "audio"} and url
                and screen in self._tv_cfg()["screens"]
                and out.get("delivered", 1)):
            tv = await self._tv_input_verify(str(url))
            out.update(tv)
            if tv.get("tv_note"):
                out["note"] = (str(out.get("note") or "") + " "
                               + tv["tv_note"]).strip()
        return out

    async def _vcast_display_status(
            self, screen: int | None = None) -> dict[str, Any]:
        """cast_to_screen(action='status') — the seat for the absorbed
        vcast_status census name: one screen's live state (what it shows,
        panes, its capture lease and camwall zone). No screen -> the same
        full report as action='list'."""
        data = await self.vcast_list()
        if screen is None:
            return data
        n = int(screen)
        for s in data.get("screens") or []:
            if s.get("screen") == n:
                out = dict(s)
                out["ok"] = True
                caps = data.get("active_captures") or {}
                if str(n) in caps:
                    out["capture"] = caps[str(n)]
                zones = [z for z, v in (data.get("camwall_zones") or {})
                         .items() if v.get("screen") == n]
                if zones:
                    out["camwall_zones"] = zones
                if data.get("state_mismatch"):
                    out["state_mismatch"] = data["state_mismatch"]
                return out
        return {"ok": False,
                "error": f"screen {n} is not a registered display",
                "screens": [s.get("screen")
                            for s in data.get("screens") or []]}

    def _screen_by_label(self, label: str) -> int | None:
        """Resolve a display's label or registry name ('living-room',
        'screen-5') to its screen number — labels are how operators refer
        to screens once assigned."""
        try:
            data = self._vcast_api("/displays")
        except Exception:
            return None
        want = str(label).strip().lower()
        for s in data.get("screens") or []:
            for cand in (s.get("label"), s.get("name")):
                if str(cand or "").strip().lower() == want:
                    try:
                        return int(s["screen"])
                    except (KeyError, TypeError, ValueError):
                        break
        return None

    def _cast_shortcut_url(self, target: str) -> str | None:
        """cast_to_screen(action='shortcut') — resolve a short app name
        to its same-origin /apps/<name>/ page (the display resolves it
        against whatever origin served it — LAN or tailnet). Full URLs
        and / paths pass through unchanged."""
        t = str(target or "").strip()
        if not t:
            return None
        if t.startswith(("http://", "https://", "/")):
            return t
        slug = re.sub(r"[^a-z0-9_-]+", "", t.lower())
        return f"/apps/{slug}/" if slug else None

    @staticmethod
    def _frame_check(url: str) -> dict[str, Any]:
        """HEAD the nav/play target. Returns {warn, content_type, dead}:
        'warn' for XFO/CSP frame-ancestors blocks (or a dead YouTube id —
        oembed 404 caught a 'Video unavailable' cast 2026-09-29),
        'content_type' lets the caller reroute still images off 'play',
        'dead' marks a URL that cannot be fetched at all (DNS/connrefused
        — an invented or stale URL renders as a silent black pane).

        'dead' is only declared once the DISPLAY's side of the world
        agrees: the backend host's vantage is not the display's (idc03
        has no route to the 192.168.2.x LAN the vcast displays live on —
        2026-10-10 a yt-live .mp4 serving 206 on the LAN was refused as
        'unreachable'). A transport-dead probe to a non-public host is
        re-HEADed via _lan_frame_probe; only when that vantage also fails
        (or the host is public) is the URL dead. An inconclusive re-probe
        downgrades to 'warn' so the display can still try — the post-cast
        verify catches a black pane."""
        import urllib.request
        import urllib.error
        import urllib.parse
        out: dict[str, Any] = {}
        if re.search(r"(youtube\.com|youtu\.be|youtube-nocookie\.com)", url):
            try:
                oe = urllib.request.urlopen(
                    "https://www.youtube.com/oembed?format=json&url="
                    + urllib.parse.quote(url, safe=""), timeout=6)
                if oe.status == 200:
                    return out
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    out["warn"] = (
                        f"{url} is not a playable video (oembed 404 — "
                        "dead/removed/unlisted). Do NOT cast it; find "
                        "another video id first.")
                    return out
            except Exception:
                return out  # inconclusive — let the display try
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "ada-vcast/1.0"})
        try:
            resp = urllib.request.urlopen(req, timeout=6)
        except urllib.error.HTTPError as exc:
            resp = exc  # still carries headers
        except urllib.error.URLError as exc:
            # DNS failure / connrefused mean the display will fetch the
            # same dead URL — hard-fail instead of casting a black pane.
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, ConnectionRefusedError)) \
                    or "gaierror" in type(reason).__name__ \
                    or "timed out" in str(reason).lower() \
                    or "Name or service" in str(reason) \
                    or "refused" in str(reason).lower():
                if ScreensMixin._frame_vantage_blind(url):
                    remote = ScreensMixin._lan_frame_probe(url)
                    if remote is not None:
                        return remote
                    out["warn"] = (
                        f"{url} is unreachable from this backend and the "
                        "LAN re-probe was inconclusive — its host sits on "
                        "a network this host cannot route to, so the "
                        "display may still fetch it. Cast is proceeding "
                        "unverified.")
                    return out
                out["dead"] = f"{type(reason).__name__}: {reason}"
                return out
            return out  # other transport errors: let the display try
        except Exception:
            return out
        return ScreensMixin._frame_hdr_verdict(url, resp.headers.get)

    @staticmethod
    def _frame_vantage_blind(url: str) -> bool:
        """True when this backend failing to reach the URL's host proves
        nothing about what the display can fetch: the host is a
        non-public address (RFC1918/loopback/link-local/CGNAT — a literal
        IP, a name that resolves only to one, or a name that doesn't
        resolve here at all). Public hosts return False — a transport
        failure there is vantage-independent."""
        import ipaddress
        import socket
        import urllib.parse
        host = (urllib.parse.urlsplit(url).hostname or "").strip("[]")
        if not host:
            return False
        try:
            return not ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(
                host, None, proto=socket.IPPROTO_TCP)
        except OSError:
            # Unresolvable from the backend — a LAN-only name the
            # display's DNS may still know.
            return True
        return bool(infos) and all(
            not ipaddress.ip_address(i[4][0]).is_global for i in infos)

    @staticmethod
    def _lan_frame_probe(url: str) -> dict[str, Any] | None:
        """Re-HEAD a URL from the LAN vantage — curl over ssh to
        ADA_CCTV_SSH (tony-dell, which sits on 192.168.2.x with the
        displays). Returns a _frame_check-shaped dict, or None when the
        probe itself is inconclusive: the ssh vantage is down, or the
        target is loopback (the display's own localhost is a third
        vantage neither side can see)."""
        import ipaddress
        import shlex
        import subprocess
        import urllib.parse
        host = (urllib.parse.urlsplit(url).hostname or "").strip("[]")
        try:
            if ipaddress.ip_address(host).is_loopback:
                return None
        except ValueError:
            if host.lower().rstrip(".") == "localhost":
                return None
        ssh = os.environ.get("ADA_CCTV_SSH", "tony-dell-m2m")
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 ssh, "curl -sSI -m 8 " + shlex.quote(url)],
                capture_output=True, text=True, timeout=20)
        except Exception:
            return None
        if proc.returncode != 0:
            # curl rc 5/6/7/28 = resolve/proxy/connect/timeout — the LAN
            # vantage can't fetch it either, so 'dead' finally means
            # dead. Other rc (255 ssh, missing curl) is inconclusive.
            if proc.returncode in {5, 6, 7, 28}:
                err = (proc.stderr or "").strip().replace("\n", " ")[-140:]
                return {"dead": (f"lan-probe curl rc={proc.returncode}"
                                 + (f": {err}" if err else ""))}
            return None
        hdrs = {}
        for ln in proc.stdout.splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        return ScreensMixin._frame_hdr_verdict(
            url, lambda k: hdrs.get(k.lower()))

    @staticmethod
    def _frame_hdr_verdict(url: str, get_hdr) -> dict[str, Any]:
        """Shared header->verdict mapping for _frame_check and the LAN
        re-probe: {content_type, warn} from Content-Type /
        X-Frame-Options / CSP frame-ancestors."""
        out: dict[str, Any] = {}
        ctype = (get_hdr("Content-Type") or "").split(";")[0].strip()
        if ctype:
            out["content_type"] = ctype
        xfo = (get_hdr("X-Frame-Options") or "").upper()
        if xfo.startswith(("DENY", "SAMEORIGIN")):
            out["warn"] = (
                f"{url} forbids iframe embedding "
                f"(X-Frame-Options: {xfo}) — the screen will show "
                "blank. Pick a different source or snapshot the feed "
                "instead of nav-ing the page.")
            return out
        csp = get_hdr("Content-Security-Policy") or ""
        m = re.search(r"frame-ancestors\s+([^;]+)", csp, re.I)
        if m and "'*'" not in m.group(1) and "https:" not in m.group(1):
            out["warn"] = (
                f"{url} restricts framing via CSP frame-ancestors "
                f"({m.group(1).strip()}) — the screen may show blank; "
                "prefer a different source.")
        return out

    async def vcast_say(self, screen: int, text: str) -> dict[str, Any]:
        """Speak a short narration line on a vcast display. Web Speech
        synthesis is tried first; when the text is non-Latin (Thai) and the
        display lacks a matching voice it falls back to a backend-synthesized
        Gemini-TTS WAV fetched from the relay — same voice as Ada herself.
        If the display hasn't been tapped for audio yet, it shows a toast
        and reports speak-blocked instead of speaking."""
        import asyncio
        screen = int(screen)
        # Scrub markup before it reaches a synthesizer — CMS/memory content
        # carries HTML entities and markdown remnants the TTS would speak
        # verbatim ("&nbsp;ชัดเจน", "][พาสเจอร์ไรซ์]" — transcript
        # 519088cb6d). Truncate first so sanitize also drops a cut-off
        # partial entity at the tail.
        text = speech_sanitize.sanitize_speech(str(text or "").strip()[:300])
        if not text.strip():
            raise ValueError("text required")
        await self._check_screen_owner(screen, self._memory_identity())
        msg: dict[str, Any] = {"type": "speak", "text": text}
        # Backend TTS only for text the average lab browser can't voice —
        # Thai (or any non-Latin) reliably falls back to an English voice
        # otherwise (2026-10-02: every vcast_say on screen 5 came out
        # English despite th-TH lang tag — no Thai voice installed).
        if re.search(r"[\u0E00-\u0E7F\u0100-\u024F\u0370-\u03FF"
                     r"\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]", text):
            audio_url = await self._vcast_tts(text)
            if audio_url:
                msg["audio"] = audio_url
        return await asyncio.to_thread(
            self._vcast_api, "/pub", {"screen": screen, "msg": msg})

    async def _vcast_tts(self, text: str) -> str | None:
        """Synthesize `text` with Gemini TTS (same key + voice as the live
        session) and publish the WAV to the relay's /frame store — returns
        the public same-origin URL a vcast display can <audio>-fetch, or
        None on any failure (caller falls back to client-side TTS)."""
        import base64, io, wave, urllib.error, urllib.request
        model = os.environ.get(
            "VCAST_TTS_MODEL", "gemini-2.5-flash-preview-tts")
        api_key = gemini_pool.next_key(model)
        if not api_key:
            return None
        try:
            from backend import voice_config
            def _synth() -> bytes | None:
                # plain REST — the google-genai sync client dies inside
                # to_thread with 'client has been closed'
                payload = json.dumps({
                    "contents": [{"parts": [{"text": text}]}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {"voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": voice_config.current_voice()}}},
                    }}).encode()
                req = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/"
                    f"models/{model}:generateContent?key={api_key}",
                    data=payload,
                    headers={"Content-Type": "application/json"})
                resp = json.load(urllib.request.urlopen(req, timeout=30))
                for part in (resp.get("candidates", [{}])[0]
                             .get("content", {}).get("parts") or []):
                    data = (part.get("inlineData") or {}).get("data")
                    if data:
                        return base64.b64decode(data)  # 24kHz s16le PCM
                return None
            pcm = await asyncio.to_thread(_synth)
            if not pcm:
                return None
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(pcm)
            token = f"say:{secrets.token_hex(4)}"
            self._vcast_api("/frame", {
                "screen": 0, "token": token,
                "data": "data:audio/wav;base64,"
                        + base64.b64encode(buf.getvalue()).decode(),
                "state": "asset"})
            pub = os.environ.get(
                "VCAST_PUBLIC_API",
                "https://tony-dell.taila0626a.ts.net/api/input-bridge")
            return f"{pub}/frame?screen=0&token={token}"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Quota-shaped: mark the key so later TTS calls skip the
                # doomed request, and surface the burn in the ops feed —
                # the client-side TTS fallback stays silent UX-wise.
                gemini_pool.mark_exhausted(
                    api_key, model, e, tool="vcast_tts")
                gemini_pool.emit_ops_event(
                    self.mddb, "vcast_tts", e,
                    session_id=str(self.session_id or "runner"))
            logger.exception("vcast tts failed")
            return None
        except Exception:
            logger.exception("vcast tts failed")
            return None
