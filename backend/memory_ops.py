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

# Banks whose auto-extracted draft notes may surface in recall before human
# promotion. Restricted to same-instance docs — extraction noise stays
# private to the instance that produced it.
DRAFT_VISIBLE_BANKS = {
    s.strip()
    for s in os.environ.get("ADA_DRAFT_VISIBLE_BANKS", "personal,general").split(",")
    if s.strip()
}
# Drafts must clear a higher score bar than active docs and age out of
# recall after this many days, so stale extraction noise can't linger.
ADA_DRAFT_MIN_SCORE = float(os.environ.get("ADA_DRAFT_MIN_SCORE", "0.55"))
ADA_DRAFT_MAX_AGE_DAYS = int(os.environ.get("ADA_DRAFT_MAX_AGE_DAYS", "7"))

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

# Reconnect priming: when a websocket session opens, the gap since the
# previous session ended is classified so session_prime_text can tell the
# model how to greet the user. Boundaries are deliberately coarse — the
# greeting should feel natural, not like a stopwatch. Scenario coverage:
# tests/scenarios/reconnect_continuity.yaml.
AWAY_TIER_RESUME = "resume"  # brief disconnect — pick up mid-thought
AWAY_TIER_RETURN = "return"  # same-day hours — welcome back, offer to resume
AWAY_TIER_RECAP = "recap"    # most of a day — offer a one-line recap
AWAY_TIER_DAYS = "days"      # several days — greet, recap, don't assume active
AWAY_TIER_LONG = "long"      # a week or more — treat as a fresh start

_AWAY_TIERS: list[tuple[float, str]] = [
    (15 * 60, AWAY_TIER_RESUME),
    (4 * 3600, AWAY_TIER_RETURN),
    (24 * 3600, AWAY_TIER_RECAP),
    (7 * 86400, AWAY_TIER_DAYS),
]


def away_tier(away_seconds: float | None) -> str | None:
    """Reconnect tier for a gap, or None for a first-ever session."""
    if away_seconds is None or away_seconds < 0:
        return None
    for limit, tier in _AWAY_TIERS:
        if away_seconds < limit:
            return tier
    return AWAY_TIER_LONG


def _format_away(away_seconds: float) -> str:
    if away_seconds < 120:
        return "a minute or two"
    if away_seconds < 3600:
        return f"about {max(1, round(away_seconds / 60))} minutes"
    if away_seconds < 86400:
        return f"about {round(away_seconds / 3600)} hours"
    return f"about {round(away_seconds / 86400)} days"


def reconnect_directive(away_seconds: float | None, last_tail: str = "") -> str | None:
    """Session note telling the model how to greet a returning user, or
    None for a first-ever session (no prior disconnect on record)."""
    tier = away_tier(away_seconds)
    if tier is None:
        return None
    gap = _format_away(float(away_seconds))
    if tier == AWAY_TIER_RESUME:
        note = (
            f"Session note: the user's connection dropped {gap} ago — this is "
            "the same conversation resuming, not a new one. Greet them very "
            "briefly and pick up right where you left off."
        )
        if last_tail.strip():
            note += (
                "\nConversation just before the disconnect:\n"
                + last_tail.strip()
            )
        return note
    if tier == AWAY_TIER_RETURN:
        return (
            f"Session note: the user is back after {gap}. Welcome them back "
            "and offer to continue what you were discussing."
        )
    if tier == AWAY_TIER_RECAP:
        return (
            f"Session note: the user has been away for {gap}. Welcome them "
            "back and offer a one-line recap of what you were working on, "
            "then ask if they want to continue."
        )
    if tier == AWAY_TIER_DAYS:
        return (
            f"Session note: the user has been away for {gap}. Greet them "
            "warmly, mention briefly what you last discussed, and ask what "
            "they'd like to do — don't assume the old task is still active."
        )
    return (
        f"Session note: the user has been away for {gap}. Treat this as a "
        "fresh start — greet them and ask what they'd like to do. Use the "
        "context below only if it's clearly relevant."
    )


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


def _must(res: Any, what: str) -> None:
    # mddb write helpers return None on failure; surface it instead of
    # reporting a successful write.
    if res is None:
        raise RuntimeError(f"mddb write failed: {what}")


def _draft_visible(bank: MemoryBank, doc: dict[str, Any], instance: str) -> bool:
    """True when a status=draft doc may surface in recall: only for
    allowlisted banks, only the extracting instance's own docs, only
    recent, and only above the draft score bar."""
    if bank.name not in DRAFT_VISIBLE_BANKS:
        return False
    meta = doc.get("meta") or {}
    if _meta_first(meta, "scope") != instance:
        return False
    score = doc.get("score")
    if score is not None and float(score) < ADA_DRAFT_MIN_SCORE:
        return False
    try:
        created = datetime.fromisoformat(str(_meta_first(meta, "valid_from") or ""))
    except ValueError:
        return False
    age = datetime.now(timezone.utc).date() - created.date()
    return 0 <= age.days <= ADA_DRAFT_MAX_AGE_DAYS


def _doc_to_hit(
    bank: MemoryBank,
    doc: dict[str, Any],
    instance: str,
    include_inactive: bool,
    with_bank: bool = False,
) -> dict[str, Any] | None:
    """Shape one doc into a search hit, or None when filtered out."""
    status = doc_effective_status(doc)
    is_draft = False
    if status != "active":
        if include_inactive:
            pass
        elif status == "draft" and _draft_visible(bank, doc, instance):
            is_draft = True
        else:
            return None
    meta = doc.get("meta") or {}
    hit: dict[str, Any] = {
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
    if with_bank:
        hit["bank"] = bank.name
    if is_draft:
        hit["draft"] = True
        hit["unverified"] = True
    else:
        try:
            confidence = float(_meta_first(meta, "confidence"))
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None:
            hit["confidence"] = confidence
            if confidence < ADA_BANK_CONFIDENCE_MIN:
                hit["unverified"] = True
    return hit


async def _bank_docs(
    mddb: MddbClient,
    bank: MemoryBank,
    q: str,
    limit: int,
    include_inactive: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch candidate docs for one bank: vector search on a real query,
    meta-filtered listing otherwise; falls back to listing when the
    embedding path is down. Returns (docs, degraded)."""
    # Read-only banks (kb imports, devin summaries) have no lifecycle meta —
    # filtering on status would exclude every doc. Post-filter still runs
    # (missing status is treated as active). Draft-visible banks widen the
    # filter so recent same-instance drafts can surface as unverified hits.
    filter_meta = None
    if not include_inactive and bank.writable:
        statuses = ["active", "draft"] if bank.name in DRAFT_VISIBLE_BANKS else ["active"]
        filter_meta = {"status": statuses}
    if q and q != "*":
        docs = await mddb.vector_search(
            collection=bank.mddb_collection,
            query=q,
            limit=int(limit) * 3,
            filter_meta=filter_meta,
            threshold=ADA_BANK_SEARCH_THRESHOLD,
        )
        if docs is None:
            return (
                await mddb.search_documents(
                    collection=bank.mddb_collection,
                    filter_meta=filter_meta,
                    limit=int(limit) * 3,
                ),
                True,
            )
        return docs or [], False
    return (
        await mddb.search_documents(
            collection=bank.mddb_collection,
            filter_meta=filter_meta,
            limit=int(limit) * 3,
        ),
        False,
    )


async def memory_search(
    mddb: MddbClient,
    registry: MemoryBankRegistry,
    bank: str,
    query: str,
    limit: int = 5,
    include_inactive: bool = False,
    person_entity: str | None = None,
) -> dict[str, Any]:
    """Search a curated memory bank's MDDB collection.

    A real query uses semantic (vector) search; '*' or empty lists by
    metadata filter. Vector search failures fall back to the listing so
    recall degrades gracefully when embeddings are down.
    bank='all' fans out across every bank assigned to this instance and
    merges hits by score — the model doesn't have to guess which bank.

    When *person_entity* is set (speaker ID active), the default 'personal'
    bank is swapped for the speaker's person-scoped bank (e.g. personal-kk)
    in both single-bank and bank='all' modes, so personal memory never
    crosses person boundaries."""
    q = str(query or "").strip()
    if str(bank).lower() in ("all", "*"):
        banks = list(registry.banks_for_person(person_entity).values())
        results = await asyncio.gather(
            *(
                _bank_docs(mddb, b, q, limit, include_inactive)
                for b in banks
            )
        )
        hits: list[dict[str, Any]] = []
        degraded = False
        for b, (docs, deg) in zip(banks, results):
            degraded = degraded or deg
            used: list[dict[str, Any]] = []
            for doc in docs:
                hit = _doc_to_hit(b, doc, registry.instance, include_inactive, with_bank=True)
                if hit is None:
                    continue
                hits.append(hit)
                used.append(doc)
            if used and not include_inactive and q and q != "*":
                record_use_bg(mddb, b.mddb_collection, used)
        hits.sort(key=lambda h: float(h.get("score") or 0), reverse=True)
        hits = hits[: int(limit)]
        return {
            "bank": "all",
            "count": len(hits),
            "hits": hits,
            "degraded": degraded,
        }

    # Resolve personal → person-scoped bank when speaker is identified
    if str(bank) == "personal" and person_entity:
        bank = registry.personal_bank_name(person_entity)
    b = registry.bank(str(bank))
    docs, degraded = await _bank_docs(mddb, b, q, limit, include_inactive)
    hits = []
    used_docs = []
    for doc in docs:
        hit = _doc_to_hit(b, doc, registry.instance, include_inactive)
        if hit is None:
            continue
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
    person_entity: str | None = None,
) -> dict[str, Any]:
    """Record how applied knowledge turned out — the verify stage that
    closes the learn->apply loop. Sets meta outcome, nudges confidence per
    OUTCOME_CONFIDENCE_DELTA, and bumps last_verified on good outcomes.

    When *person_entity* is set and bank is 'personal', the outcome is
    recorded on the speaker's person-scoped bank."""
    if str(bank) == "personal" and person_entity:
        bank = registry.personal_bank_name(person_entity)
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
    _must(await mddb.update_document(b.mddb_collection, str(key), meta=meta),
          f"update {b.mddb_collection}/{key}")
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
    person_entity: str | None = None,
) -> dict[str, Any]:
    """Write a memory to a bank: create, correct-in-place, or supersede.

    When *person_entity* is set and bank is 'personal', the write is routed
    to the speaker's person-scoped bank (e.g. personal-kk) so personal
    memories never cross person boundaries."""
    if str(bank) == "personal" and person_entity:
        bank = registry.personal_bank_name(person_entity)
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
        if new_key == str(supersedes):
            # The derived key collides with the doc being superseded — writing
            # there would resurrect-then-supersede the same doc. Bump the key.
            base = new_key
            n = 2
            while await mddb.get_document(b.mddb_collection, new_key) is not None:
                new_key = f"{base}-{n}"
                n += 1
        meta = memory_meta(b, scope, today, kind, subject, attribute, valid_until, applies, session_id)
        _warn_unknown_meta(registry, meta)
        meta["supersedes"] = [str(supersedes)]
        _must(await mddb.add_document(
            b.mddb_collection, new_key, "en", str(text), meta),
            f"add {b.mddb_collection}/{new_key}")
        old_meta = dict(old.get("meta") or {})
        old_meta["status"] = ["superseded"]
        old_meta["superseded_by"] = [new_key]
        _must(await mddb.update_document(
            b.mddb_collection, str(supersedes), meta=old_meta),
            f"supersede-mark {b.mddb_collection}/{supersedes}")
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
        _must(await mddb.update_document(
            b.mddb_collection, target_key, content_md=str(text), meta=meta
        ), f"correct {b.mddb_collection}/{target_key}")
        return {"verb": "correct", "bank": b.name, "key": target_key}
    _must(await mddb.add_document(
        b.mddb_collection, target_key, "en", str(text), meta),
        f"add {b.mddb_collection}/{target_key}")
    return {"verb": "create", "bank": b.name, "key": target_key}


async def forget(
    mddb: MddbClient,
    registry: MemoryBankRegistry,
    bank: str,
    key: str,
    reason: str | None = None,
    session_id: str | None = None,
    person_entity: str | None = None,
) -> dict[str, Any]:
    """Retract a memory: status becomes retracted; the doc stays auditable.

    When *person_entity* is set and bank is 'personal', the retraction
    targets the speaker's person-scoped bank."""
    if str(bank) == "personal" and person_entity:
        bank = registry.personal_bank_name(person_entity)
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
    _must(await mddb.update_document(b.mddb_collection, str(key), meta=meta),
          f"retract {b.mddb_collection}/{key}")
    return {"verb": "retract", "bank": b.name, "key": str(key)}


async def session_prime_text(
    mddb: MddbClient,
    registry: MemoryBankRegistry,
    summary: str | None = None,
    max_facts: int = 6,
    max_chars: int = 160,
    away_seconds: float | None = None,
    last_tail: str = "",
) -> str | None:
    """Build the session-start context injection: the rolling recent-sessions
    summary plus a few facts from the personal/general banks, so Ada starts
    aware of general info instead of blank. When away_seconds is set, a
    reconnect directive is prepended so the greeting matches how long the
    user was gone. Returns None when there is nothing worth injecting."""
    if os.environ.get("ADA_SESSION_PRIME", "1") in ("0", "false", "no"):
        return None
    parts: list[str] = []
    directive = reconnect_directive(away_seconds, last_tail)
    if directive:
        parts.append(directive)
    if summary:
        parts.append(f"Recent sessions: {summary.strip()}")
    # Short reconnect (<1h): the conversation tail already carries the live
    # threads — bank facts would inject stale context and drown them.
    # First session or long gap: facts still prime cold-start awareness.
    facts: list[str] = []
    if not (directive and (away_seconds or 0) < 3600):
        for name in ("personal", "general"):
            try:
                b = registry.bank(name)
            except KeyError:
                continue
            try:
                docs = await mddb.search_documents(
                    collection=b.mddb_collection,
                    filter_meta={"status": ["active"]},
                    limit=max_facts,
                )
            except Exception as exc:
                logger.debug("session prime listing failed for %r: %s", name, exc)
                continue
            for doc in docs or []:
                body = str(doc.get("contentMd") or doc.get("content_md") or "").strip()
                if body:
                    facts.append(body[:max_chars])
                if len(facts) >= max_facts:
                    break
            if len(facts) >= max_facts:
                break
    if facts:
        parts.append("Known facts:\n" + "\n".join(f"- {f}" for f in facts))
    if not parts:
        return None
    header = (
        "(system) Reconnect context — act on the session note below, treat "
        "the rest as background information:"
        if directive
        else "(system) Session context — background information only, do not "
        "announce or answer it:"
    )
    return header + "\n" + "\n\n".join(parts)
