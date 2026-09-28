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
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0)) as client:
        r = await client.get(f"{base}/snap", params=params)
    if r.status_code == 404:
        try:
            names = r.json().get("channels", [])
        except Exception:
            names = []
        raise LookupError(
            f"unknown camera '{channel}' — available: {', '.join(names)}")
    r.raise_for_status()
    return r.content, r.headers.get("x-channel", channel)
