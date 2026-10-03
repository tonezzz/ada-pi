"""XMEye VMS camera snapshot client.

Calls the vms-snap HTTP shim (chaba stacks/services/xmeye-vms) over the
tailnet. The shim drives the Wine VMS UI — xdotool channel select in the
device tree, then an xwd capture of the monitor pane — and returns a PNG
still. Only configured when ADA_VMS_SNAP_URL is set; instances without it
never see the ada_camera_snapshot tool.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


def snap_url() -> str:
    return os.environ.get("ADA_VMS_SNAP_URL", "").rstrip("/")


async def snapshot(channel: str, settle: float | None = None) -> tuple[bytes, str]:
    """Return (png_bytes, resolved_channel_name) for `channel`.

    Raises LookupError (message lists available channels) on a bad name.
    """
    base = snap_url()
    if not base:
        raise RuntimeError("camera snapshot service is not configured on this instance")
    # native=1 — the shim's own VMS snapshot path returns the channel at
    # native decode res (2560x1440) instead of the screen crop, for ~+2s.
    params = {"ch": channel, "native": "1"}
    if settle is not None:
        params["settle"] = str(settle)
    import asyncio
    r = None
    # Serial shim: a snap holds a global lock and can take ~160s worst
    # case (select, settle, up to 3 attach rounds — each 8-poll × 1.5s +
    # re-select). 75s was fitting the old fail-fast path — timeouts
    # surfaced as "service unreachable" mid-turn (2026-09-30 transcript).
    async with httpx.AsyncClient(timeout=httpx.Timeout(200.0)) as client:
        for _try in range(2):
            try:
                r = await client.get(f"{base}/snap", params=params)
            except httpx.HTTPError:
                r = None
            if r is not None and r.status_code == 404:
                break
            if r is not None and r.status_code == 200:
                break
            await asyncio.sleep(2)  # dead pane / flaky P2P — retry once
    if r is None:
        raise RuntimeError("camera snapshot service unreachable")
    if r.status_code == 404:
        try:
            names = r.json().get("channels", [])
        except Exception:
            names = []
        import difflib
        close = difflib.get_close_matches(channel, names, n=2, cutoff=0.3)
        hint = (f" Did you mean: {', '.join(close)}? Retry with a listed "
                "name instead of asking the user." if close else "")
        raise LookupError(
            f"unknown camera '{channel}' — available: {', '.join(names)}.{hint}")
    r.raise_for_status()
    return r.content, r.headers.get("x-channel", channel)


# --- last-good fallback ----------------------------------------------------
# Camwall zones whose thumbs come from this same VMS shim — when a live
# snap fails, the puller's last-good thumb is a guaranteed image to show
# (marked stale by its mtime, never presented as live).
VMS_CAMWALL_ZONES = ("vms-noble-club", "vms-noble-a")  # retired zone-a + noble-park 2026-10-02


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")


async def stale_snapshot(channel: str) -> tuple[bytes, str, int, bytes | None] | None:
    """Fetch the last-known thumb for `channel` from the camwall cache.

    Returns (thumb_jpeg, cam_label, age_s, status_card_jpeg|None) or None.
    The status card (<key>-status.jpg, generated per failed pull cycle) is
    the display artifact — dimmed thumb + OFFLINE/STALE banner — while the
    raw thumb is what the vision helper describes. Fetches each VMS zone's
    manifest (tiny JSON) to find the cam key."""
    base = os.environ.get(
        "ADA_CAMWALL_BASE",
        "https://tony-dell.taila0626a.ts.net/apps/camwall").rstrip("/")
    want = _slug(channel)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as c:
            for zone in VMS_CAMWALL_ZONES:
                try:
                    man = (await c.get(
                        f"{base}/data/{zone}/manifest-{zone}.json")).json()
                except Exception:
                    continue
                for cam in man.get("cams") or []:
                    if _slug(cam.get("key") or "") != want and \
                            _slug(cam.get("label") or "") != want:
                        continue
                    img = await c.get(
                        f"{base}/data/{zone}/{cam['key']}.jpg")
                    if img.status_code != 200 or len(img.content) < 5000:
                        log.info("stale snap %r: %s -> %d %dB",
                                 channel, cam["key"], img.status_code,
                                 len(img.content))
                        return None
                    age = int(__import__("time").time()
                              - (cam.get("ts") or 0))
                    card = None
                    try:
                        cr = await c.get(
                            f"{base}/data/{zone}/{cam['key']}-status.jpg")
                        if cr.status_code == 200 and len(cr.content) > 5000:
                            card = cr.content
                    except Exception:
                        pass
                    log.info("stale snap %r: %s/%s.jpg %dB age=%ds card=%s",
                             channel, zone, cam["key"],
                             len(img.content), age, bool(card))
                    return (img.content, cam.get("label") or channel,
                            age, card)
    except Exception:
        return None
    return None
