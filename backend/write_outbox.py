"""Durable outbox for MDDB writes that fail on transient errors.

A confirmed user-facing write (cms_publish_page, devin_answer, ada_remember,
…) that hits an mddb outage used to evaporate — callers either never checked
the helper's None return or the session moved on. Callers now pass
``durable=True`` to the MddbClient write helpers; on failure the request lands
in a local JSONL outbox and a background task retries with exponential
backoff for a bounded window (ADA_OUTBOX_WINDOW_S, default 15 min). Entries
that outlive the window become dead letters — kept on disk for audit/repair
and surfaced to the user verbally via drain_notices() at the next turn
boundary (realtime_provider).

Files (under <ADA data dir>/outbox/, ADA_OUTBOX_DIR overrides):

  pending.jsonl      — one entry per line, rewritten atomically on state change
  dead-letters.jsonl — append-only record of permanently failed writes
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.instance import ada_instance_id

logger = logging.getLogger("tools")

_DATA_DIR = Path(os.environ.get(
    "ADA_TRANSCRIPT_DIR", os.path.expanduser("~/.local/share/ada/transcripts"))
).parent
OUTBOX_DIR = Path(os.environ.get("ADA_OUTBOX_DIR", str(_DATA_DIR / "outbox")))
# Bounded retry window — an mddb restart/index-reload takes minutes; beyond
# this the write is declared lost and surfaced instead of retried forever.
WINDOW_S = float(os.environ.get("ADA_OUTBOX_WINDOW_S", "900"))
MAX_ATTEMPTS = int(os.environ.get("ADA_OUTBOX_MAX_ATTEMPTS", "12"))
MAX_ENTRIES = int(os.environ.get("ADA_OUTBOX_MAX_ENTRIES", "200"))
POLL_S = float(os.environ.get("ADA_OUTBOX_POLL_S", "5"))
BACKOFF_BASE_S = 5.0
BACKOFF_MAX_S = 60.0


def is_queued(res: Any) -> bool:
    """True when a durable write helper queued instead of completing."""
    return isinstance(res, dict) and bool(res.get("queued_for_retry"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WriteOutbox:
    """JSONL-backed retry queue. One instance per process (default())."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self.dir = Path(directory) if directory else OUTBOX_DIR
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self._mddb: Any = None
        self._notices: list[str] = []

    @property
    def _pending_path(self) -> Path:
        return self.dir / "pending.jsonl"

    @property
    def _dead_path(self) -> Path:
        return self.dir / "dead-letters.jsonl"

    # -- enqueue -----------------------------------------------------------

    def enqueue(
        self,
        op: str,
        collection: str,
        key: str,
        lang: str = "en",
        content_md: str | None = None,
        meta: dict[str, list[str]] | None = None,
        tool: str = "",
        session_id: str | None = None,
        mddb: Any = None,
    ) -> dict[str, Any] | None:
        """Persist a failed write for background replay. Returns the queued
        sentinel dict, or None when the outbox itself can't take it (disk
        error / over capacity) — callers then keep their plain failure path."""
        try:
            with self._lock:
                pending = self._load_pending()
                if len(pending) >= MAX_ENTRIES:
                    logger.error(
                        "write outbox full (%d entries) — refusing %s %s/%s",
                        len(pending), op, collection, key)
                    return None
                self.dir.mkdir(parents=True, exist_ok=True)
                entry = {
                    "id": secrets.token_hex(6),
                    "op": str(op),
                    "collection": str(collection),
                    "key": str(key),
                    "lang": str(lang or "en"),
                    "content_md": content_md,
                    "meta": meta or {},
                    "tool": str(tool or ""),
                    "session_id": str(session_id or ""),
                    "enqueued_at": _now_iso(),
                    "enqueued_ts": time.time(),
                    "attempts": 0,
                    "next_retry_at": time.time() + BACKOFF_BASE_S,
                    "last_error": "",
                }
                pending.append(entry)
                self._save_pending(pending)
        except Exception as exc:
            logger.error("write outbox enqueue failed: %s", exc)
            return None
        logger.warning(
            "write outbox queued %s %s/%s (tool=%s) id=%s — will retry for "
            "%.0fs", op, collection, key, tool, entry["id"], WINDOW_S)
        if mddb is not None:
            self._mddb = mddb
        self._ensure_loop()
        return {
            "status": "queued",
            "queued_for_retry": True,
            "outbox_id": entry["id"],
        }

    # -- retry loop --------------------------------------------------------

    def ensure_running(self, mddb: Any) -> None:
        """(Re)start the retry loop — called from tool dispatch so entries
        queued before a restart resume on the first tool call."""
        if mddb is not None:
            self._mddb = mddb
        self._ensure_loop()

    def _ensure_loop(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._mddb is None or not self._load_pending():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # sync context (tests, shutdown) — next call retries
        self._task = loop.create_task(self._run())

    async def _run(self) -> None:
        while self._load_pending():
            try:
                await self.flush_once(self._mddb)
            except Exception:
                logger.debug("outbox flush pass failed", exc_info=True)
            await asyncio.sleep(POLL_S)

    async def flush_once(self, mddb: Any) -> None:
        """One retry pass over pending entries — also used by tests."""
        now = time.time()
        working = self._load_pending()
        resolved: set[str] = set()
        for entry in working:
            if now < float(entry.get("next_retry_at") or 0):
                continue
            if (now - float(entry.get("enqueued_ts") or now) > WINDOW_S
                    or int(entry.get("attempts") or 0) >= MAX_ATTEMPTS):
                self._mark_dead(entry, "retry window exhausted")
                resolved.add(entry["id"])
                continue
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            if await self._replay(mddb, entry) is None:
                entry["last_error"] = "replay failed (mddb still unreachable)"
                entry["next_retry_at"] = now + min(
                    BACKOFF_BASE_S * (2 ** int(entry["attempts"])),
                    BACKOFF_MAX_S)
            else:
                resolved.add(entry["id"])
                logger.info(
                    "write outbox delivered %s %s/%s (tool=%s) after %d "
                    "attempts", entry["op"], entry["collection"],
                    entry["key"], entry.get("tool"), entry["attempts"])
        with self._lock:
            # Merge against the CURRENT file — an enqueue may have appended
            # during the awaits; mutations live on the working copies.
            mutated = {e["id"]: e for e in working if e["id"] not in resolved}
            merged = []
            for e in self._load_pending():
                if e.get("id") in resolved:
                    continue
                merged.append(mutated.pop(e.get("id"), e))
            self._save_pending(merged)

    async def _replay(self, mddb: Any, entry: dict[str, Any]) -> Any:
        try:
            if entry["op"] == "add":
                return await mddb.add_document(
                    entry["collection"], entry["key"], entry["lang"],
                    entry.get("content_md") or "", meta=entry.get("meta"))
            if entry["op"] == "update":
                # Re-run the read-merge-write so the replay merges onto the
                # freshest doc rather than a stale snapshot.
                return await mddb.update_document(
                    entry["collection"], entry["key"], entry["lang"],
                    content_md=entry.get("content_md"),
                    meta=entry.get("meta"))
            if entry["op"] == "delete":
                return await mddb.delete_document(
                    entry["collection"], entry["key"], entry["lang"])
            entry["last_error"] = f"unknown op {entry['op']!r}"
        except Exception as exc:
            entry["last_error"] = f"{type(exc).__name__}: {exc}"
        return None

    # -- dead letters + user notices ----------------------------------------

    def _mark_dead(self, entry: dict[str, Any], reason: str) -> None:
        entry["dead_at"] = _now_iso()
        entry["reason"] = reason
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self._dead_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.error("write outbox dead-letter append failed: %s", exc)
        notice = (
            f"a {entry.get('tool') or 'tool'} write to "
            f"{entry['collection']}/{entry['key']} could not be saved — "
            f"mddb stayed unreachable for the whole retry window "
            f"({WINDOW_S / 60:.0f} min). The payload is preserved in "
            f"{self._dead_path} and can be re-submitted."
        )
        with self._lock:
            self._notices.append(notice)
        logger.error(
            "write outbox dead letter %s %s/%s (tool=%s): %s",
            entry["op"], entry["collection"], entry["key"],
            entry.get("tool"), reason)
        self._emit_dead_ops_event(entry)

    def drain_notices(self) -> list[str]:
        """Dead-letter notices not yet surfaced — the provider drains these
        at the next turn boundary so a permanent write failure is spoken."""
        with self._lock:
            notices, self._notices = self._notices, []
        return notices

    def _emit_dead_ops_event(self, entry: dict[str, Any]) -> None:
        """Best-effort ops event — dead letters almost always mean mddb is
        down, so this usually fails too; the dead-letters file is the record."""
        mddb = self._mddb
        if mddb is None:
            return
        try:
            collection = f"ada-ha-events-{ada_instance_id()}"
        except Exception:
            return

        async def _post() -> None:
            try:
                await mddb.add_document(
                    collection=collection,
                    key=f"ops-outbox-dead-{entry['id']}",
                    lang="en",
                    content_md=(
                        f"write outbox dead letter: {entry['op']} "
                        f"{entry['collection']}/{entry['key']} "
                        f"(tool={entry.get('tool')}, "
                        f"session={entry.get('session_id')})"),
                    meta={
                        "kind": ["ops-event"], "type": ["outbox_dead"],
                        "tool": [str(entry.get("tool") or "")],
                        "session_id": [str(entry.get("session_id") or "")],
                        "ts": [_now_iso()],
                    },
                )
            except Exception:
                pass
        try:
            asyncio.get_running_loop().create_task(_post())
        except RuntimeError:
            return

    # -- file plumbing ------------------------------------------------------

    def _load_pending(self) -> list[dict[str, Any]]:
        try:
            lines = self._pending_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except Exception as exc:
            logger.error("write outbox read failed: %s", exc)
            return []
        entries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                logger.warning("write outbox: skipping corrupt line %r",
                               line[:80])
        return entries

    def _save_pending(self, entries: list[dict[str, Any]]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._pending_path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
            encoding="utf-8")
        tmp.replace(self._pending_path)

    def pending_count(self) -> int:
        return len(self._load_pending())

    def status(self) -> dict[str, Any]:
        pending = self._load_pending()
        dead = 0
        try:
            dead = sum(1 for line in
                       self._dead_path.read_text(encoding="utf-8").splitlines()
                       if line.strip())
        except FileNotFoundError:
            pass
        return {
            "dir": str(self.dir),
            "pending": len(pending),
            "dead_letters": dead,
            "window_s": WINDOW_S,
            "oldest": min((e.get("enqueued_at") for e in pending),
                          default=None),
        }


# -- module singleton --------------------------------------------------------

_default: WriteOutbox | None = None


def default() -> WriteOutbox:
    global _default
    if _default is None:
        _default = WriteOutbox()
    return _default


def enqueue(mddb: Any = None, **kwargs: Any) -> dict[str, Any] | None:
    return default().enqueue(mddb=mddb, **kwargs)


def ensure_running(mddb: Any) -> None:
    default().ensure_running(mddb)


def drain_notices() -> list[str]:
    return default().drain_notices()
