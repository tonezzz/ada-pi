"""Staged memory writes — the approval lane (card ada-memory-staged-writes).

Routing rule (reuses the person_policies access map — no new ACL):

  * ``{full: true}`` identities (the ``_persona_admin`` check) write
    directly — Tony must not start approving every "remember X".
  * restricted/unknown/guest identities stage here instead; the model is
    told ``{ok: true, staged: true, id}`` and narrates "I'll keep that
    for Tony to approve", never "saved".
  * per-bank ``write_policy=confirmed`` still gates the tool CALL
    upstream in ``_check_memory_write_allowed``; staging gates the
    CONTENT for untrusted callers — complementary, both stay.
  * the write-guard scan (backend/memory_write_guard.py) runs BEFORE
    staging — scan refuses outright, stage parks clean content.

Store: a vault pending/ dir — ``<ADA data dir>/memory-pending/<id>.yml``
(``ADA_MEMORY_PENDING_DIR`` overrides). Chosen over an MDDB collection
because the chaba guest instance has no MDDB yet still stages guest
writes, and the file store mirrors ChabaMemory's existing ``pending/``
convention for registrations awaiting promotion.

Pending entry (card-spec shape, plus routing fields):

    id, identity, bank, key, text, staged_at, source_session,
    route, status, text_sha256, args, guest, instance

``text_sha256`` is Hermes' pinning rule: approval applies exactly the
staged bytes — if the stored text changed after staging, approve refuses
and keeps the entry pending. Entries are single-use: a second approve on
a resolved entry is refused (no double-apply).

Approver surface (existing surfaces, no new UI): each staged write lands
a ``memory-staged`` line in the ada events digest (events.md ->
ada-review) and, when ``ADA_MEMORY_REVIEW_CARD`` names a standing board
card, one comms line via board-api. Responding is
``scripts/ada/memory-pending.py list|approve|reject``.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from backend import memory_write_guard

logger = logging.getLogger("tools")

# Same data-dir convention as backend/write_outbox.py.
_DATA_DIR = Path(os.environ.get(
    "ADA_TRANSCRIPT_DIR", os.path.expanduser("~/.local/share/ada/transcripts"))
).parent
PENDING_DIR = Path(os.environ.get(
    "ADA_MEMORY_PENDING_DIR", str(_DATA_DIR / "memory-pending")))

# A flooded review queue is itself an attack — bound it like the outbox.
MAX_PENDING = int(os.environ.get("ADA_MEMORY_PENDING_MAX", "200"))

ROUTES = ("bank", "guest", "guest_private", "vocab")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dir(pending_dir: str | Path | None = None) -> Path:
    return Path(pending_dir) if pending_dir else PENDING_DIR


def _entry_path(pending_id: str, pending_dir: str | Path | None = None) -> Path:
    safe = "".join(c for c in str(pending_id) if c.isalnum() or c in "-_")
    return _dir(pending_dir) / f"{safe}.yml"


def _pin(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _save(path: Path, entry: dict[str, Any]) -> None:
    """Atomic write — tmp + flock + rename, same as ChabaMemory._save_doc."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yaml.safe_dump(entry, f, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def stage(
    *,
    route: str,
    text: Any,
    identity: str | None = None,
    bank: str | None = None,
    key: str | None = None,
    args: dict[str, Any] | None = None,
    guest: dict[str, Any] | None = None,
    source_session: str | None = None,
    instance: str | None = None,
    pending_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Park a write for approval; returns the pending entry.

    Raises ValueError when the content would fail the write-guard scan —
    callers must scan first (loud programmer error, not a user refusal).
    Raises RuntimeError when the queue is full."""
    if route not in ROUTES:
        raise ValueError(f"unknown pending route {route!r}")
    matched = memory_write_guard.scan(text)
    if matched is not None:
        raise ValueError(
            f"pending stage refused by write guard "
            f"({matched['matched_class']}) — scan before staging")
    if len(list_pending(pending_dir)) >= MAX_PENDING:
        raise RuntimeError(
            f"memory pending queue is full ({MAX_PENDING}) — review "
            "existing entries first")
    entry: dict[str, Any] = {
        "id": "mp-" + secrets.token_hex(5),
        "status": "pending",
        "route": route,
        "identity": identity,
        "bank": bank,
        "key": key,
        "text": str(text),
        "text_sha256": _pin(str(text)),
        "args": dict(args or {}),
        "guest": dict(guest) if guest else None,
        "staged_at": _now_iso(),
        "source_session": source_session,
        "instance": instance,
    }
    _save(_entry_path(entry["id"], pending_dir), entry)
    logger.info(
        "memory write staged %s route=%s bank=%s identity=%r session=%s",
        entry["id"], route, bank, identity, source_session)
    return entry


def load(
    pending_id: str, pending_dir: str | Path | None = None
) -> dict[str, Any] | None:
    path = _entry_path(pending_id, pending_dir)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return doc if isinstance(doc, dict) else None


def _iter_entries(pending_dir: str | Path | None = None):
    root = _dir(pending_dir)
    if not root.exists():
        return
    for p in sorted(root.glob("mp-*.yml")):
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(doc, dict):
            yield doc


def list_pending(
    pending_dir: str | Path | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    return [e for e in (_iter_entries(pending_dir) or [])
            if e.get("status") == "pending"][:limit]


def preview(entry: dict[str, Any], chars: int = 80) -> str:
    """One-line text preview for review surfaces — never full payload."""
    return " ".join(str(entry.get("text") or "").split())[:chars]


async def _apply(
    entry: dict[str, Any],
    *,
    mddb: Any,
    registry: Any,
    instance: str | None,
    chaba: Any,
) -> dict[str, Any]:
    """Apply the pinned content through the route's real write path."""
    route = entry["route"]
    if route == "bank":
        if mddb is None or registry is None:
            raise RuntimeError("bank apply needs an MDDB client and registry")
        from backend import memory_ops
        args = dict(entry.get("args") or {})
        return await memory_ops.remember(
            mddb, registry, instance, str(entry["bank"]), str(entry["text"]),
            key=entry.get("key"),
            subject=args.get("subject"),
            attribute=args.get("attribute"),
            kind=args.get("kind"),
            valid_until=args.get("valid_until"),
            applies_to=args.get("applies_to"),
            supersedes=args.get("supersedes"),
            prime=args.get("prime"),
            session_id=entry.get("source_session"),
            person_entity=entry.get("identity"),
        )
    if route == "vocab":
        if mddb is None or registry is None:
            raise RuntimeError("vocab apply needs an MDDB client and registry")
        from backend import memory_ops
        args = dict(entry.get("args") or {})
        return await memory_ops.vocab_append(
            mddb, registry, entry.get("identity"),
            str(entry["text"]), note=args.get("note"))
    if route in ("guest", "guest_private"):
        if chaba is None:
            raise RuntimeError("guest apply needs the chaba store")
        # Re-key a synthetic session onto the staged guest identity so the
        # note lands under the original writer's name — the live session is
        # long gone by approval time.
        sid = f"pending-{entry['id']}"
        g = entry.get("guest") or {}
        chaba.set_identity(sid, str(g.get("kind") or "guest"),
                           str(g.get("name") or ""))
        if route == "guest_private":
            return chaba.remember_private(
                sid, str(entry.get("key") or "note"), str(entry["text"]))
        return chaba.remember(
            sid, str(entry.get("key") or "note"), str(entry["text"]))
    raise ValueError(f"unknown pending route {route!r}")


async def approve(
    pending_id: str,
    *,
    mddb: Any = None,
    registry: Any = None,
    instance: str | None = None,
    chaba: Any = None,
    by: str | None = None,
    pending_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Apply a staged write verbatim. Single-use; pinned-content checked.

    Refusals keep the entry pending: pin mismatch (content changed after
    staging), a post-staging scan hit, or an apply failure the approver
    may retry."""
    path = _entry_path(pending_id, pending_dir)
    if not path.exists():
        return {"ok": False, "error": f"no pending entry {pending_id!r}"}
    # Hold the flock across check+apply+mark — a second approver blocks
    # instead of double-applying.
    with open(path, "r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        entry = yaml.safe_load(f.read()) or {}
        status = entry.get("status")
        if status != "pending":
            return {"ok": False, "id": pending_id,
                    "error": f"already {status} — pending entries are "
                             "single-use (no double-apply)",
                    "status": status}
        text = str(entry.get("text") or "")
        if _pin(text) != entry.get("text_sha256"):
            logger.warning(
                "pending %s pin mismatch — content changed after staging",
                pending_id)
            return {"ok": False, "id": pending_id, "status": "pending",
                    "error": "pin_mismatch — the stored text changed after "
                             "staging; refusing to apply and keeping it "
                             "pending for review"}
        matched = memory_write_guard.scan(text)
        if matched is not None:
            return {"ok": False, "id": pending_id, "status": "pending",
                    "error": f"content now fails the write-guard scan "
                             f"({matched['matched_class']}) — kept pending; "
                             "reject it instead of approving"}
        try:
            applied = await _apply(
                entry, mddb=mddb, registry=registry,
                instance=instance, chaba=chaba)
        except Exception as exc:
            logger.warning("pending %s apply failed: %s", pending_id, exc)
            return {"ok": False, "id": pending_id, "status": "pending",
                    "error": f"apply failed ({exc}) — kept pending, "
                             "safe to retry"}
        entry["status"] = "approved"
        entry["resolved_at"] = _now_iso()
        entry["resolved_by"] = by
        entry["applied"] = applied
        f.seek(0)
        f.truncate()
        yaml.safe_dump(entry, f, allow_unicode=True, sort_keys=False)
    logger.info("pending %s approved by %s -> %s", pending_id, by, applied)
    return {"ok": True, "approved": pending_id, "applied": applied}


def reject(
    pending_id: str,
    *,
    by: str | None = None,
    reason: str | None = None,
    pending_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Mark a staged write refused — the file stays on disk (provenance)."""
    path = _entry_path(pending_id, pending_dir)
    if not path.exists():
        return {"ok": False, "error": f"no pending entry {pending_id!r}"}
    with open(path, "r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        entry = yaml.safe_load(f.read()) or {}
        status = entry.get("status")
        if status != "pending":
            return {"ok": False, "id": pending_id, "status": status,
                    "error": f"already {status}"}
        entry["status"] = "refused"
        entry["resolved_at"] = _now_iso()
        entry["resolved_by"] = by
        if reason:
            entry["reason"] = str(reason)[:300]
        f.seek(0)
        f.truncate()
        yaml.safe_dump(entry, f, allow_unicode=True, sort_keys=False)
    logger.info("pending %s refused by %s (%s)", pending_id, by, reason)
    return {"ok": True, "rejected": pending_id, "status": "refused"}
