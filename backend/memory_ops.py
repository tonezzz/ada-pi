"""Curated memory-bank operations — search, remember (create/correct/
supersede), and retract.

These take (mddb, registry, instance) explicitly instead of living on
ToolRunner, so the write path is testable in isolation and the runner stays
focused on dispatch + Home Assistant tooling.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from backend.mddb_client import MddbClient
from backend.memory_banks import (
    MemoryBank,
    MemoryBankRegistry,
    _meta_first,
    _slug,
    doc_effective_status,
)

logger = logging.getLogger("tools")

# Minimum vector-search score for a bank hit to count as confident. Below
# this, recall escalates to the NotebookLM deep tier. ~0.45-0.55 is the
# observed "real match" band on this MDDB's embedding model.
ADA_BANK_SEARCH_THRESHOLD = float(os.environ.get("ADA_BANK_SEARCH_THRESHOLD", "0.45"))

# Higher bar for "this is the same fact restated" — when ada_remember finds
# no exact key/subject match, a hit above this score is corrected in place
# instead of creating a near-duplicate.
ADA_BANK_UPDATE_THRESHOLD = float(os.environ.get("ADA_BANK_UPDATE_THRESHOLD", "0.85"))

# Below this stored confidence, a hit is tagged unverified so the model must
# hedge or escalate rather than state it as fact. record_outcome() is what
# moves a doc's confidence over time — this gate makes that matter.
# See docs/ada-memory/tony-projects/knowledge-circle.md (chaba repo).
ADA_BANK_CONFIDENCE_MIN = float(os.environ.get("ADA_BANK_CONFIDENCE_MIN", "0.4"))

# How each recorded outcome nudges a doc's confidence (clamped 0.05..1.0).
# Good outcomes also bump last_verified — the verify stage of the knowledge
# circle. "skipped" is neutral: the check ran but was never exercised.
OUTCOME_CONFIDENCE_DELTA = {
    "good": 0.1,
    "worked": 0.1,
    "bought_good": 0.1,
    "partial": -0.05,
    "skipped": 0.0,
    "bad": -0.2,
    "failed": -0.2,
    "bought_bad": -0.2,
}


def memory_meta(
    bank: MemoryBank,
    scope: str,
    today: str,
    kind: str | None,
    subject: str | None,
    attribute: str | None,
    valid_until: str | None,
    applies_to: list[str],
    session_id: str | None = None,
) -> dict[str, list[str]]:
    meta: dict[str, list[str]] = {
        "bank": [bank.name],
        "kind": [str(kind or "note")],
        "scope": [scope],
        "status": ["active"],
        "valid_from": [today],
        "last_verified": [today],
        "source": ["voice"],
        "written_by": ["ada_remember"],
    }
    if subject:
        meta["subject"] = [str(subject)]
    if attribute:
        meta["attribute"] = [str(attribute)]
    if valid_until:
        meta["valid_until"] = [str(valid_until)]
    if applies_to:
        meta["applies_to"] = [str(a) for a in applies_to]
    if session_id and session_id != "unknown":
        meta["session_id"] = [session_id]
    return meta


def _warn_unknown_meta(registry: MemoryBankRegistry, meta: dict[str, list[str]]) -> None:
    unknown = registry.validate_meta(meta)
    if unknown:
        logger.warning(
            "memory meta fields not in SSOT meta_schema: %s "
            "(extend docs/ssot/apps/ssot.apps.ada-memory-schema.yml)", unknown,
        )


async def memory_search(
    mddb: MddbClient,
    registry: MemoryBankRegistry,
    bank: str,
    query: str,
    limit: int = 5,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """Search a curated memory bank's MDDB collection.

    A real query uses semantic (vector) search; '*' or empty lists by
    metadata filter. Vector search failures fall back to the listing so
    recall degrades gracefully when embeddings are down."""
    b = registry.bank(str(bank))
    # Read-only banks (kb imports, devin summaries) have no lifecycle meta —
    # filtering on status would exclude every doc. Post-filter still runs
    # (missing status is treated as active).
    filter_meta = None if (include_inactive or not b.writable) else {"status": ["active"]}
    q = str(query or "").strip()
    degraded = False
    if q and q != "*":
        docs = await mddb.vector_search(
            collection=b.mddb_collection,
            query=q,
            limit=int(limit) * 3,
            filter_meta=filter_meta,
            threshold=ADA_BANK_SEARCH_THRESHOLD,
        )
        if docs is None:
            degraded = True
            docs = await mddb.search_documents(
                collection=b.mddb_collection,
                filter_meta=filter_meta,
                limit=int(limit) * 3,
            )
    else:
        docs = await mddb.search_documents(
            collection=b.mddb_collection,
            filter_meta=filter_meta,
            limit=int(limit) * 3,
        )
    hits = []
    used_docs = []
    for doc in docs or []:
        status = doc_effective_status(doc)
        if not include_inactive and status != "active":
            continue
        meta = doc.get("meta") or {}
        hit = {
            "key": doc.get("key"),
            "status": status,
            "score": doc.get("score"),
            "content": doc.get("contentMd") or doc.get("content_md") or "",
            "kind": _meta_first(meta, "kind"),
            "subject": _meta_first(meta, "subject"),
            "attribute": _meta_first(meta, "attribute"),
            "last_verified": _meta_first(meta, "last_verified"),
            "valid_until": _meta_first(meta, "valid_until"),
        }
        try:
            confidence = float(_meta_first(meta, "confidence"))
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None:
            hit["confidence"] = confidence
            if confidence < ADA_BANK_CONFIDENCE_MIN:
                hit["unverified"] = True
        hits.append(hit)
        used_docs.append(doc)
        if len(hits) >= int(limit):
            break
    # Knowledge-circle bookkeeping: a real query that surfaced docs counts as
    # "applied" — the observable proxy for the doc being used in the answer.
    # Listing/audit queries (empty, '*', include_inactive) don't count.
    if used_docs and not include_inactive and q and q != "*":
        record_use_bg(mddb, b.mddb_collection, used_docs)
    return {
        "bank": b.name,
        "collection": b.mddb_collection,
        "count": len(hits),
        "hits": hits,
        "degraded": degraded,
    }


async def record_use(
    mddb: MddbClient, collection: str, doc: dict[str, Any]
) -> None:
    """Bump use_count/last_used on a surfaced doc. Best-effort bookkeeping —
    recall never depends on it succeeding."""
    key = doc.get("key")
    if not key:
        return
    meta = dict(doc.get("meta") or {})
    try:
        count = int(_meta_first(meta, "use_count") or 0)
    except ValueError:
        count = 0
    meta["use_count"] = [str(count + 1)]
    meta["last_used"] = [datetime.now(timezone.utc).date().isoformat()]
    await mddb.update_document(collection, str(key), meta=meta)


async def _record_use_safe(
    mddb: MddbClient, collection: str, doc: dict[str, Any]
) -> None:
    try:
        await record_use(mddb, collection, doc)
    except Exception as exc:
        logger.debug("record_use failed for %r: %s", doc.get("key"), exc)


def record_use_bg(
    mddb: MddbClient, collection: str, docs: list[dict[str, Any]]
) -> None:
    """Fire-and-forget use accounting — never blocks or breaks recall.
    Silently skipped without a running loop (unit tests, sync callers)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for doc in docs:
        loop.create_task(_record_use_safe(mddb, collection, doc))


async def record_outcome(
    mddb: MddbClient,
    registry: MemoryBankRegistry,
    bank: str,
    key: str,
    outcome: str,
    note: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Record how applied knowledge turned out — the verify stage that
    closes the learn->apply loop. Sets meta outcome, nudges confidence per
    OUTCOME_CONFIDENCE_DELTA, and bumps last_verified on good outcomes."""
    b = registry.bank(str(bank))
    outcome = str(outcome)
    delta = OUTCOME_CONFIDENCE_DELTA.get(outcome)
    if delta is None:
        raise ValueError(
            f"unknown outcome {outcome!r} "
            f"(allowed: {', '.join(sorted(OUTCOME_CONFIDENCE_DELTA))})"
        )
    doc = await mddb.get_document(b.mddb_collection, str(key))
    if doc is None:
        raise ValueError(f"no such document {key!r} in bank '{b.name}'")
    meta = dict(doc.get("meta") or {})
    today = datetime.now(timezone.utc).date().isoformat()
    meta["outcome"] = [outcome]
    meta["last_used"] = [today]
    try:
        conf = float(_meta_first(meta, "confidence") or 0.6)
    except ValueError:
        conf = 0.6
    meta["confidence"] = [str(round(min(1.0, max(0.05, conf + delta)), 2))]
    if delta > 0:
        meta["last_verified"] = [today]
    if note:
        meta["outcome_note"] = [str(note)]
    if session_id and session_id != "unknown":
        meta["outcome_session"] = [session_id]
    _warn_unknown_meta(registry, meta)
    await mddb.update_document(b.mddb_collection, str(key), meta=meta)
    return {
        "verb": "outcome",
        "bank": b.name,
        "key": str(key),
        "outcome": outcome,
        "confidence": meta["confidence"][0],
    }


async def remember(
    mddb: MddbClient,
    registry: MemoryBankRegistry,
    instance: str,
    bank: str,
    text: str,
    key: str | None = None,
    subject: str | None = None,
    attribute: str | None = None,
    kind: str | None = None,
    valid_until: str | None = None,
    applies_to: list[str] | str | None = None,
    supersedes: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Write a memory to a bank: create, correct-in-place, or supersede."""
    b = registry.bank(str(bank))
    if str(kind or "note") not in b.kinds:
        raise ValueError(
            f"kind {kind!r} not allowed in bank '{b.name}' (allowed: {', '.join(b.kinds)})"
        )
    today = datetime.now(timezone.utc).date().isoformat()
    scope = "shared" if b.scope == "shared" else instance
    applies = [applies_to] if isinstance(applies_to, str) else list(applies_to or [])

    if supersedes:
        old = await mddb.get_document(b.mddb_collection, str(supersedes))
        if old is None:
            raise ValueError(
                f"cannot supersede {supersedes!r}: no such document in bank '{b.name}'"
            )
        new_key = str(key) if key else f"{b.name}/{_slug(str(subject or text))}"
        meta = memory_meta(b, scope, today, kind, subject, attribute, valid_until, applies, session_id)
        _warn_unknown_meta(registry, meta)
        meta["supersedes"] = [str(supersedes)]
        await mddb.add_document(b.mddb_collection, new_key, "en", str(text), meta)
        old_meta = dict(old.get("meta") or {})
        old_meta["status"] = ["superseded"]
        old_meta["superseded_by"] = [new_key]
        await mddb.update_document(b.mddb_collection, str(supersedes), meta=old_meta)
        return {
            "verb": "supersede",
            "bank": b.name,
            "key": new_key,
            "superseded": str(supersedes),
        }

    # Find-then-update: an existing active doc about the same
    # subject/attribute (or at the requested key) is corrected in place.
    target_key = str(key) if key else None
    existing = None
    if target_key:
        existing = await mddb.get_document(b.mddb_collection, target_key)
    elif subject:
        filt: dict[str, list[str]] = {"subject": [str(subject)], "status": ["active"]}
        if attribute:
            filt["attribute"] = [str(attribute)]
        docs = await mddb.search_documents(
            b.mddb_collection, "*", filter_meta=filt, limit=1
        )
        docs = [d for d in docs if doc_effective_status(d) == "active"]
        if docs:
            target_key = docs[0].get("key")
            existing = docs[0]
    if not target_key:
        target_key = f"{b.name}/{_slug(str(subject or text))}"
        existing = await mddb.get_document(b.mddb_collection, target_key)

    if existing is None:
        # Dedupe: a near-identical active memory counts as the same fact —
        # correct it in place rather than stacking a duplicate.
        sims = await mddb.vector_search(
            collection=b.mddb_collection,
            query=str(text),
            limit=1,
            filter_meta={"status": ["active"]},
            threshold=ADA_BANK_UPDATE_THRESHOLD,
        )
        if sims and doc_effective_status(sims[0]) == "active":
            target_key = sims[0].get("key")
            existing = sims[0]
            logger.info(
                "ada_remember dedupe: %r matched %r (score %.3f)",
                str(text)[:60], target_key, sims[0].get("score") or 0,
            )

    meta = memory_meta(b, scope, today, kind, subject, attribute, valid_until, applies, session_id)
    _warn_unknown_meta(registry, meta)
    if existing is not None:
        old_meta = dict(existing.get("meta") or {})
        for keep in ("valid_from", "supersedes", "superseded_by"):
            if keep in old_meta:
                meta[keep] = old_meta[keep]
        if kind is None and "kind" in old_meta:
            meta["kind"] = old_meta["kind"]
        await mddb.update_document(
            b.mddb_collection, target_key, content_md=str(text), meta=meta
        )
        return {"verb": "correct", "bank": b.name, "key": target_key}
    await mddb.add_document(b.mddb_collection, target_key, "en", str(text), meta)
    return {"verb": "create", "bank": b.name, "key": target_key}


async def forget(
    mddb: MddbClient,
    registry: MemoryBankRegistry,
    bank: str,
    key: str,
    reason: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Retract a memory: status becomes retracted; the doc stays auditable."""
    b = registry.bank(str(bank))
    doc = await mddb.get_document(b.mddb_collection, str(key))
    if doc is None:
        raise ValueError(f"no such document {key!r} in bank '{b.name}'")
    meta = dict(doc.get("meta") or {})
    meta["status"] = ["retracted"]
    meta["last_verified"] = [datetime.now(timezone.utc).date().isoformat()]
    if reason:
        meta["retracted_reason"] = [str(reason)]
    if session_id and session_id != "unknown":
        meta["retracted_by_session"] = [session_id]
    await mddb.update_document(b.mddb_collection, str(key), meta=meta)
    return {"verb": "retract", "bank": b.name, "key": str(key)}
