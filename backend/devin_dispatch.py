"""Headless Devin session dispatch via `devin-dispatch` on tony-dell.

Ada calls devin-dispatch over ssh (BatchMode); each task runs as a
systemd --user unit in a dedicated git worktree on tony-dell. Completion
is reported back by devin-dispatch-watch.timer (chaba-admin event +
iPhone notify + devin-bank outcome doc), so these tools only deal in
launch/status/followup — never wait for a session to finish.

SSOT: docs/ssot/jobs/ada/2026-09-22-ada-devin-dispatch.yml
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("tools.devin_dispatch")

# tony-dell's LAN IP, not its tailnet name: `ssh tony-dell` resolves via
# MagicDNS to the tailscale IP where tailscaled-ssh intercepts and demands a
# periodic interactive re-auth — unusable from an unattended service. The LAN
# address reaches plain sshd with key auth (mn01/idc01 keys are authorized).
HOST = os.environ.get("ADA_DEVIN_DISPATCH_HOST", "192.168.2.67")
BIN = os.environ.get("ADA_DEVIN_DISPATCH_BIN", "~/.local/bin/devin-dispatch")
TIMEOUT_S = float(os.environ.get("ADA_DEVIN_DISPATCH_TIMEOUT_S", "45"))


async def _run(*args: str) -> str:
    """Run `devin-dispatch <args>` on the dispatch host. Returns stdout."""
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        HOST, BIN, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"devin-dispatch {args[0]} timed out after {TIMEOUT_S}s")
    if proc.returncode != 0:
        msg = err.decode(errors="replace").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"devin-dispatch {args[0]} failed: {msg}")
    return out.decode(errors="replace").strip()


async def dispatch(repo: str, task: str) -> dict:
    """Start a headless Devin session. Returns the task id + registry info."""
    task_id = await _run("start", repo, task)
    return {
        "task_id": task_id.strip(),
        "repo": repo,
        "note": (
            "Session is running unattended on tony-dell in a dedicated "
            "worktree. Use devin_status to check progress; a completion "
            "notification is sent automatically when it finishes."
        ),
    }


async def status(task_id: str | None = None) -> str:
    """List tasks, or show one task's unit state + transcript timestamp."""
    args = ["status"] + ([task_id] if task_id else [])
    return await _run(*args)


async def followup(task_id: str, message: str) -> str:
    """Send a follow-up message to a running (or resumable) session."""
    return await _run("followup", task_id, message)
