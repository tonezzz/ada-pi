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
import re
import shlex
import time
from datetime import datetime, timezone

logger = logging.getLogger("tools.devin_dispatch")

# tony-dell's LAN IP, not its tailnet name: `ssh tony-dell` resolves via
# MagicDNS to the tailscale IP where tailscaled-ssh intercepts and demands a
# periodic interactive re-auth — unusable from an unattended service. The LAN
# address reaches plain sshd with key auth (mn01/idc01 keys are authorized).
HOST = os.environ.get("ADA_DEVIN_DISPATCH_HOST", "192.168.2.67")
BIN = os.environ.get("ADA_DEVIN_DISPATCH_BIN", "~/.local/bin/devin-dispatch")
TIMEOUT_S = float(os.environ.get("ADA_DEVIN_DISPATCH_TIMEOUT_S", "45"))

# List-status output cap: the full registry is noise in a live-voice context
# and every tool result stays in the Gemini session, inflating turn latency.
STATUS_MAX_LINES = int(os.environ.get("ADA_DEVIN_DISPATCH_STATUS_MAX", "8"))

# In-process dedup: the model retries a failed/slow dispatch with rephrased
# task text (seen 2026-09-24: 3 calls in ~2min spawned 3 sessions). Recent
# dispatches are cached; a near-identical task inside the window reuses the
# existing task_id instead of starting a duplicate session.
DEDUP_WINDOW_S = float(os.environ.get("ADA_DEVIN_DISPATCH_DEDUP_S", "600"))
# Containment |∩|/min(|a|,|b|): a retry is often a *shorter* rephrase of the
# same task, which Jaccard under-scores.
DEDUP_MIN_SIM = float(os.environ.get("ADA_DEVIN_DISPATCH_DEDUP_SIM", "0.6"))

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_STOP = {
    "the", "and", "for", "with", "from", "that", "this", "have", "has",
    "been", "into", "then", "than", "them", "they", "what", "when",
    "check", "look", "please", "investigate", "find", "tell", "make",
}


def _task_tokens(text: str) -> frozenset[str]:
    return frozenset(
        w for w in _TOKEN_RE.findall(text.lower())
        if len(w) > 3 and w not in _STOP
    )


# repo -> [(tokens, task_id, monotonic_ts)]
_recent_dispatches: dict[str, list[tuple[frozenset[str], str, float]]] = {}

# devin-dispatch task ids: YYYYMMDD-HHMMSS-<30-char slug>
_TASK_ID_RE = re.compile(r"^(\d{8})-(\d{6})-([a-z0-9-]+)$")


def _dedup_result(task_id: str, repo: str) -> dict:
    return {
        "task_id": task_id,
        "repo": repo,
        "deduplicated": True,
        "note": (
            "A matching task was already dispatched recently; reusing "
            "that session instead of starting a duplicate. Use "
            "devin_status to check progress."
        ),
    }


async def _remote_dupe(repo: str, toks: frozenset[str]) -> str | None:
    """Check the dispatch host's task list for a same-intent task started
    inside the dedup window. Covers retries after a backend restart, where
    the in-process cache is empty."""
    try:
        out = await _run("status")
    except Exception as exc:
        logger.info("dedup remote check skipped: %s", exc)
        return None
    now = datetime.now(timezone.utc)
    for line in out.splitlines():
        fields = line.split()
        if not fields or f"repo={repo}" not in fields:
            continue
        m = _TASK_ID_RE.match(fields[0])
        if not m:
            continue
        try:
            started = datetime.strptime(
                m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        if (now - started).total_seconds() > DEDUP_WINDOW_S:
            continue
        slug_toks = _task_tokens(m.group(3).replace("-", " "))
        if toks and slug_toks:
            sim = len(toks & slug_toks) / min(len(toks), len(slug_toks))
            if sim >= DEDUP_MIN_SIM:
                return fields[0]
    return None


async def _run(*args: str) -> str:
    """Run `devin-dispatch <args>` on the dispatch host. Returns stdout."""
    # ssh re-joins the command into a remote `bash -c` string — every
    # argument must be shell-quoted or task text containing ( ) ; ' etc.
    # breaks the parse (seen 2026-09-24). BIN stays unquoted so the remote
    # shell expands its leading `~`.
    remote_cmd = " ".join([BIN, *(shlex.quote(a) for a in args)])
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        HOST, remote_cmd,
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
    now = time.monotonic()
    toks = _task_tokens(task)
    entries = _recent_dispatches.setdefault(repo, [])
    entries[:] = [e for e in entries if now - e[2] < DEDUP_WINDOW_S]
    dupe = None
    for prev_toks, prev_id, _ in entries:
        if not toks or not prev_toks:
            continue
        sim = len(toks & prev_toks) / min(len(toks), len(prev_toks))
        if sim >= DEDUP_MIN_SIM:
            dupe = prev_id
            break
    if dupe is None:
        dupe = await _remote_dupe(repo, toks)
    if dupe is not None:
        logger.info("dedup: reusing %s for similar task", dupe)
        entries.append((toks, dupe, now))
        return _dedup_result(dupe, repo)
    task_id = (await _run("start", repo, task)).strip()
    entries.append((toks, task_id, now))
    return {
        "task_id": task_id,
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
    out = await _run(*args)
    if task_id:
        return out
    lines = [line for line in out.splitlines() if line.strip()]
    if len(lines) <= STATUS_MAX_LINES:
        return out
    shown = lines[-STATUS_MAX_LINES:]
    return (
        f"showing latest {STATUS_MAX_LINES} of {len(lines)} tasks "
        "(pass a task_id for details)\n" + "\n".join(shown)
    )


async def followup(task_id: str, message: str) -> str:
    """Send a follow-up message to a running (or resumable) session."""
    return await _run("followup", task_id, message)
