"""XMEye VMS camera snapshot client.

Calls the vms-snap HTTP shim (chaba stacks/services/xmeye-vms) over the
tailnet. The shim drives the Wine VMS UI — xdotool channel select in the
device tree, then an xwd capture of the monitor pane — and returns a PNG
still. Only configured when ADA_VMS_SNAP_URL is set; instances without it
never see the ada_camera_snapshot tool.
"""

from __future__ import annotations

import os

import httpx


def snap_url() -> str:
    return os.environ.get("ADA_VMS_SNAP_URL", "").rstrip("/")


async def snapshot(channel: str, settle: float | None = None) -> tuple[bytes, str]:
    """Return (png_bytes, resolved_channel_name) for `channel`.

    Raises LookupError (message lists available channels) on a bad name.
    """
    base = snap_url()
    if not base:
        raise RuntimeError("camera snapshot service is not configured on this instance")
    params = {"ch": channel}
    if settle is not None:
        params["settle"] = str(settle)
    import asyncio
    r = None
    # Serial shim: a snap holds a global lock and can take ~110s worst
    # case (select, settle, 8-frame poll, one re-select on dead attach).
    # 75s was fitting the old fail-fast path — timeouts surfaced as
    # "service unreachable" mid-turn (2026-09-30 transcript).
    async with httpx.AsyncClient(timeout=httpx.Timeout(150.0)) as client:
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
