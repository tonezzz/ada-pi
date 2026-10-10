"""Conversation memory and NotebookLM recall for Ada voice sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from backend import gemini_pool
from backend.mddb_client import MddbClient

logger = logging.getLogger("voice.conversation")

NOTEBOOKLM_BASE_URL = os.environ.get(
    "NOTEBOOKLM_REST_BASE_URL", "http://127.0.0.1:3011/v1"
)
NOTEBOOKLM_API_KEY = os.environ.get("NOTEBOOKLM_REST_API_KEY")
NOTEBOOKLM_NOTEBOOK_ID = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID")

# Speak a second filler line if recall exceeds this many seconds.
RECALL_SLOW_AFTER_S = float(os.environ.get("ADA_RECALL_SLOW_AFTER_S", "10"))

# Rolling "recent sessions" summary, persisted to MDDB so it survives
# restarts. Updated locally after each persisted transcript (fast Gemini
# text call, ~3s); seeded once from NotebookLM on first use so history
# predating this feature is covered. Recalls answer with the cached
# summary immediately while NotebookLM handles the detail query.
_SUMMARY_QUESTION = (
    "Briefly summarize what we discussed in our recent sessions, "
    "in two or three sentences."
)
_SUMMARY_KEY = "recent-sessions"
_SUMMARY_MAX_TRANSCRIPT_CHARS = int(
    os.environ.get("ADA_SUMMARY_MAX_TRANSCRIPT_CHARS", "8000")
)
_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
_SUMMARY_MODEL = os.environ.get("ADA_SUMMARY_MODEL", "gemini-3.5-flash-lite")
_REPORT_MODEL = os.environ.get("ADA_REPORT_MODEL", _SUMMARY_MODEL)
_SESSION_REPORT = os.environ.get("ADA_SESSION_REPORT", "1").lower() not in (
    "0", "false", "no")
# Rolling session-memory.md keeps at most this many '## ' entries —
# mirrors chaba_memory's rolling log cap.
_SESSION_LOG_KEEP = int(os.environ.get("ADA_SESSION_LOG_KEEP", "30"))

# Raw transcript archive — local-only. Transcripts can contain private
# details and credential-shaped text, so they are never pushed to git or
# public MDDB collections. scripts/ada/export-transcripts.py pulls this
# directory for offline review (e.g. a Devin session audit).
TRANSCRIPT_DIR = os.environ.get(
    "ADA_TRANSCRIPT_DIR",
    os.path.expanduser("~/.local/share/ada/transcripts"),
)
_summary_cache: dict[str, Any] = {"text": None, "ts": 0.0, "task": None}

# Per-instance deep-tier answer cache: a bank/group miss costs a ~20-60s
# NotebookLM chat/ask; identical-or-near-identical questions inside the TTL
# window are served from MDDB instead.
_NLM_CACHE_THRESHOLD = float(os.environ.get("ADA_NLM_CACHE_THRESHOLD", "0.90"))
_NLM_CACHE_TTL_DAYS = int(os.environ.get("ADA_NLM_CACHE_TTL_DAYS", "7"))


def _nlm_cache_collection() -> str:
    from backend.instance import ada_instance_id
    return f"ada-ha-nlm-answers-{ada_instance_id()}"


def recent_summary() -> str | None:
    """The rolling recent-sessions summary, when warmed."""
    return _summary_cache.get("text")

# Failure surface: the last error per subsystem plus timestamps, exposed via
# conversation_health(). Fail-quick policy — errors are recorded and logged at
# ERROR level, never silently converted into "empty"/"not found" results.
_health: dict[str, Any] = {
    "notebooklm_error": None,
    "summary_error": None,
    "summary_mddb_error": None,
    "ts": None,
}


def _report_failure(area: str, exc: Any) -> None:
    _health[f"{area}_error"] = str(exc)[:300]
    _health["ts"] = time.time()
    logger.error("%s failed: %s", area, exc)
    # Loud-fail quota surface (card ada-gemini-free-tier-quota): a
    # quota-shaped gemini error also lands in the ops feed so the burn
    # shows in the hourly digest instead of hiding in health errors.
    try:
        if gemini_pool.is_quota_error(exc):
            gemini_pool.emit_ops_event(
                _mddb(), area, exc, ev_type=f"{area}_quota")
    except Exception:
        pass


def conversation_health() -> dict[str, Any]:
    """Snapshot of recall/memory subsystem health for status endpoints."""
    return {
        "summary_cached": _summary_cache["text"] is not None,
        "summary_ts": _summary_cache["ts"],
        "recall_running": bool(
            _summary_cache.get("task") and not _summary_cache["task"].done()
        ),
        "gemini": gemini_pool.status(),
        "errors": {
            k: v for k, v in _health.items() if v is not None
        },
    }


def _summary_collection() -> str:
    # Fail fast on identity: no "unknown" collection — a misnamed write would
    # silently orphan the summary (same rule as ADA_INSTANCE_ID).
    from backend.instance import ada_instance_id
    return f"ada-ha-recall-summary-{ada_instance_id()}"


# Reconnect continuity: a keyed marker doc records when the last websocket
# session ended (and its transcript tail) so a service restart still knows
# how long the user has been away. Upserted on every session close — one
# doc, always the latest end.
_SESSION_END_KEY = "last-session-end"
_SESSION_END_TAIL_CHARS = 400

# Cross-channel history (card ada-reads-text-chat): every session feeds a
# process-wide channel log so voice, text-chat and relay sessions share one
# timeline while they are live. A per-owner rolling tail doc in the SHARED
# recall-summary collection carries the same tail across ada backends
# (ada-pi-pwa chat -> ada-ha-tony voice) and restarts — the per-instance
# collections cannot bridge that split.
_CHANNEL_LOG_KEEP = int(os.environ.get("ADA_CHANNEL_LOG_KEEP", "300"))
_CHANNEL_TAIL_TURNS = int(os.environ.get("ADA_CHANNEL_TAIL_TURNS", "8"))
_CHANNEL_TAIL_CHARS = int(os.environ.get("ADA_CHANNEL_TAIL_CHARS", "1400"))
_CHANNEL_TAIL_MAX_AGE_S = float(
    os.environ.get("ADA_CHANNEL_TAIL_MAX_AGE_S", str(6 * 3600)))
_channel_log: deque = deque(maxlen=_CHANNEL_LOG_KEEP)


def _channel_collection() -> str:
    # Deliberately NOT instance-suffixed: this is the unified voice+chat
    # history — every ada backend reads the same docs. The
    # ada-ha-recall-summary-* prefix routes it to the ops store (no
    # embedding burn on per-turn tail upserts).
    return "ada-ha-recall-summary-shared"


def _channel_owner_key(owner: str | None) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", str(owner or "").strip().lower())
    return f"channel-tail-{slug or 'anon'}"


def channel_tail_text(
    owner: str | None = None,
    exclude_session: str | None = None,
    max_turns: int | None = None,
    max_chars: int | None = None,
    max_age_s: float | None = None,
) -> str:
    """Recent turns from OTHER sessions on this backend, labelled by
    channel — the shared voice+chat history a new session primes with.
    Strict owner match: a session only ever sees its own owner's turns."""
    now = time.time()
    max_age = (_CHANNEL_TAIL_MAX_AGE_S if max_age_s is None else max_age_s)
    rows = [
        it for it in _channel_log
        if it.get("session_id") != exclude_session
        and it.get("owner") == owner
        and now - float(it.get("ts") or 0) <= max_age
    ][-(max_turns or _CHANNEL_TAIL_TURNS):]
    lines = []
    for it in rows:
        hhmm = datetime.fromtimestamp(
            float(it["ts"]), timezone.utc).strftime("%H:%M")
        chan = it.get("channel") or "voice"
        who = "User" if it.get("role") == "user" else "Ada"
        label = who if chan == "voice" else f"{who} [{chan}]"
        lines.append(f"{hhmm} {label}: {it['text']}")
    return "\n".join(lines)[: (max_chars or _CHANNEL_TAIL_CHARS)]


def channel_log() -> list[dict[str, Any]]:
    """Raw shared channel-history entries (oldest first) — diagnostics."""
    return [dict(it) for it in _channel_log]


async def record_channel_tail(
    mddb: Any, owner: str | None, tail: str
) -> None:
    """Upsert this instance's channel tail for `owner` into the shared
    collection. Keyed per instance so concurrent backends never clobber
    each other's tail — readers merge the freshest docs."""
    if not owner or not str(tail or "").strip():
        return
    from backend.instance import ada_instance_id
    ts = time.time()
    iso = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    try:
        await mddb.add_document(
            collection=_channel_collection(),
            key=f"{_channel_owner_key(owner)}-{ada_instance_id()}",
            lang="en",
            content_md=str(tail).strip(),
            meta={
                "kind": ["channel-tail"],
                "owner": [str(owner)],
                "instance": [ada_instance_id()],
                "updated_at": [iso],
                "updated_ts": [f"{ts:.3f}"],
            },
        )
    except Exception as exc:
        logger.warning("channel-tail write failed: %s", exc)


async def read_channel_tails(
    mddb: Any,
    owner: str | None,
    exclude_instance: str | None = None,
    limit: int = 2,
    max_age_s: float = 24 * 3600,
) -> list[str]:
    """Persisted channel tails for `owner` written by OTHER instances —
    covers sessions that ran on another ada backend (the ada-pi-pwa chat
    card) plus any tail that outlived a restart."""
    if not owner:
        return []
    try:
        docs = await mddb.search_documents(
            collection=_channel_collection(),
            query="*",
            filter_meta={"kind": ["channel-tail"], "owner": [str(owner)]},
            limit=12,
        )
    except Exception as exc:
        logger.debug("channel-tail read failed: %s", exc)
        return []

    def _first(meta: dict, field: str) -> str:
        v = (meta or {}).get(field) or []
        return str(v[0] if isinstance(v, list) and v else v or "")

    now = time.time()
    ranked: list[tuple[float, str]] = []
    for d in docs or []:
        meta = d.get("meta") or {}
        if exclude_instance and _first(meta, "instance") == exclude_instance:
            continue
        body = str(d.get("contentMd") or d.get("content_md") or "").strip()
        if not body:
            continue
        try:
            ts = float(_first(meta, "updated_ts"))
        except ValueError:
            ts = 0.0
        if ts and now - ts > max_age_s:
            continue
        ranked.append((ts, body))
    ranked.sort(key=lambda kv: kv[0], reverse=True)
    return [body for _, body in ranked[:limit]]


async def record_session_end(
    mddb: Any, tail: str, ended_at: float | None = None
) -> None:
    """Upsert the session-end marker into the recall-summary collection."""
    from backend.instance import ada_instance_id
    ts = float(ended_at if ended_at is not None else time.time())
    iso = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    tail = str(tail or "").strip()[-_SESSION_END_TAIL_CHARS:]
    try:
        await mddb.add_document(
            collection=_summary_collection(),
            key=_SESSION_END_KEY,
            lang="en",
            content_md=(
                f"Last voice session ended {iso}."
                + (f"\nLast exchange:\n{tail}" if tail else "")
            ),
            meta={
                "kind": ["session-end"],
                "scope": [ada_instance_id()],
                "ended_at": [iso],
                "ended_at_ts": [f"{ts:.3f}"],
                "last_tail": [tail] if tail else [],
            },
        )
    except Exception as exc:
        logger.warning("session-end marker write failed: %s", exc)


async def last_session_end(mddb: Any) -> tuple[float | None, str]:
    """(ended_at unix ts, transcript tail) from the persisted marker, or
    (None, "") when no session has ended since tracking began."""
    try:
        doc = await mddb.get_document(_summary_collection(), _SESSION_END_KEY)
    except Exception as exc:
        logger.debug("session-end marker read failed: %s", exc)
        return None, ""
    if not doc:
        return None, ""
    meta = doc.get("meta") or {}

    def _first(field: str) -> str:
        v = meta.get(field) or []
        return str(v[0] if isinstance(v, list) and v else v or "")

    ts: float | None = None
    raw = _first("ended_at_ts")
    if raw:
        try:
            ts = float(raw)
        except ValueError:
            ts = None
    if ts is None:
        try:
            ts = datetime.fromisoformat(_first("ended_at")).timestamp()
        except ValueError:
            pass
    return ts, _first("last_tail")


def _actions_collection() -> str:
    # Pending action proposals live in a per-instance operational
    # collection (like ada-ha-events-*, not a memory bank).
    from backend.instance import ada_instance_id
    return f"ada-ha-actions-{ada_instance_id()}"


async def pending_action_proposals(limit: int = 5) -> list[dict[str, str]]:
    """Unresolved {key, text} proposals for session-start context."""
    try:
        docs = await _mddb().search_documents(
            collection=_actions_collection(),
            query="*",
            filter_meta={"status": ["pending"], "kind": ["action-proposal"]},
            limit=limit,
        )
    except Exception as exc:
        _report_failure("actions_mddb", exc)
        return []
    out: list[dict[str, str]] = []
    for d in docs or []:
        body = str(d.get("contentMd") or d.get("content_md") or "").strip()
        if body:
            out.append({"key": str(d.get("key") or ""),
                        "text": body.splitlines()[0]})
    return out


async def resolve_action_proposal(key: str, resolution: str) -> str:
    """Mark a pending proposal applied|dismissed (ada_resolve_action tool)."""
    if resolution not in ("applied", "dismissed"):
        return "resolution must be 'applied' or 'dismissed'"
    if os.environ.get("ADA_READ_ONLY") == "true":
        return "action proposals are disabled (ADA_READ_ONLY=true)"
    doc = await _mddb().get_document(_actions_collection(), key)
    if doc is None:
        return f"no proposal with key {key!r}"
    meta = dict(doc.get("meta") or {})
    meta["status"] = [resolution]
    meta["resolved_at"] = [datetime.now(timezone.utc).isoformat()]
    result = await _mddb().update_document(
        _actions_collection(), key, meta=meta
    )
    return f"marked {resolution}" if result is not None else "update failed"


def _mddb():
    return MddbClient()


async def _save_summary_to_mddb(text: str) -> None:
    try:
        await _mddb().add_document(
            collection=_summary_collection(),
            key=_SUMMARY_KEY,
            lang="en",
            content_md=text,
            meta={"kind": ["recall-summary"], "source": ["conversation_memory"]},
        )
    except Exception as exc:
        _report_failure("summary_mddb", exc)


async def _load_summary_from_mddb() -> str | None:
    try:
        docs = await _mddb().search_documents(
            collection=_summary_collection(),
            query="*",
            filter_meta={"kind": ["recall-summary"]},
            limit=1,
        )
    except Exception as exc:
        _report_failure("summary_mddb", exc)
        return None
    if not docs:
        return None
    text = docs[0].get("contentMd") or docs[0].get("content_md") or ""
    return str(text).strip() or None


async def _summarize(prev: str | None, transcript: str) -> str | None:
    """Update the rolling summary with one new session via Gemini REST."""
    if not _GEMINI_API_KEY:
        _report_failure("summary", "GEMINI_API_KEY not set — rolling summary disabled")
        return None
    try:
        prompt = (
            "Existing summary of our recent voice sessions:\n"
            f"{prev or '(none yet)'}\n\n"
            "Transcript of the latest session:\n"
            f"{transcript[-_SUMMARY_MAX_TRANSCRIPT_CHARS:]}\n\n"
            "Update the summary to cover the recent sessions in two or three "
            "sentences. Keep it concise and factual. Attribute utterances, "
            "never assert identity: write 'the user said/introduced themselves "
            "as X' rather than 'the user is X' — names, roles, and claims "
            "spoken aloud (including roleplay or language practice) are "
            "claims, not facts about who the user is."
        )
        resp = await gemini_pool.generate(
            model=_SUMMARY_MODEL, contents=prompt, tool="summary")
        return (resp.text or "").strip() or None
    except Exception as exc:
        _report_failure("summary", exc)
        return None


async def _summarize_session(transcript: str) -> str | None:
    """Standalone summary of one session — the substrate for tiered rollups."""
    if not _GEMINI_API_KEY:
        return None  # _summarize already reported the missing key
    try:
        prompt = (
            "Transcript of one voice session:\n"
            f"{transcript[-_SUMMARY_MAX_TRANSCRIPT_CHARS:]}\n\n"
            "Summarize this session in two or three sentences. Include any "
            "facts, decisions, preferences, or requests worth remembering. "
            "Attribute utterances, never assert identity: 'the user said/"
            "introduced themselves as X', not 'the user is X' — spoken "
            "self-descriptions (including roleplay or practice) are claims, "
            "not facts about the user's identity."
        )
        resp = await gemini_pool.generate(
            model=_SUMMARY_MODEL, contents=prompt, tool="session_summary")
        return (resp.text or "").strip() or None
    except Exception as exc:
        _report_failure("session_summary", exc)
        return None


# Content markers that force auto-extracted memories into the session
# speaker's personal bank — private documents, identity papers and named
# persons in a document context must not leak into shared banks (they are
# KK/guest-visible). Shared with ada_remember via memory_banks.
from backend.memory_banks import SENSITIVE_MEMORY_RE as _SENSITIVE_MEMORY_RE


async def _session_report(transcript: str, date: str, session_id: str) -> dict | None:
    """Structured per-session report — the L1 layer feeding
    session-memory.md (rolling log) and offline focus rollups.
    Canonical English per the ada-memory-banks language_policy."""
    if not _GEMINI_API_KEY:
        return None
    try:
        prompt = (
            "You are auditing one Ada voice-assistant session transcript.\n\n"
            "SESSION META (authoritative — use exactly in memory_block "
            f"heading): date={date} session_id={session_id}\n\n"
            "Reply with ONE JSON object only, no prose, matching:\n"
            "{summary, focus, topics, actions_taken, "
            "actions_proposed_pending, open_loops, people, "
            "audit{missed_actions, recall_failures, tool_issues, "
            "prompt_gaps, noise_or_asr_issues, notes}, memory_block}\n"
            "- focus: 1-3 emergent kebab-case thread tags reused across "
            "sessions for rollup grouping.\n"
            "- Write ALL fields in English even when the transcript is "
            "Thai (canonical index layer).\n"
            "- memory_block starts with exactly "
            f"'## {date} {session_id}' then 3-6 short lines — what was "
            "discussed, done, and left open — for an assistant reading "
            "it cold in a later session.\n"
            "- audit: only findings actually visible in the text; flag "
            "foreign-script/ambient-noise turns in noise_or_asr_issues.\n\n"
            f"TRANSCRIPT:\n{transcript[-_SUMMARY_MAX_TRANSCRIPT_CHARS:]}"
        )
        resp = await gemini_pool.generate(
            model=_REPORT_MODEL,
            contents=prompt,
            config={"response_mime_type": "application/json",
                    "temperature": 0.2},
            tool="session_report")
        return json.loads(resp.text)
    except Exception as exc:
        _report_failure("session_report", exc)
        return None


def _save_session_report_files(report: dict, date: str, session_id: str) -> None:
    """Write the report JSON and append memory_block to session-memory.md
    (rolling '## ' log, capped — same format chaba_memory uses)."""
    try:
        base = Path(TRANSCRIPT_DIR).parent
        rdir = base / "reports"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{date}-{session_id}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8")
        block = str(report.get("memory_block") or "").strip()
        if not block:
            return
        body = block.split("\n", 1)[1] if "\n" in block else ""
        entry = (
            f"## {date} {session_id}\n"
            f"- ref: report:{date}-{session_id}\n{body}"
        ).rstrip()
        log = base / "session-memory.md"
        prev = log.read_text(encoding="utf-8") if log.exists() else ""
        entries = [e for e in re.split(r"\n(?=## )", prev) if e.strip()]
        entries.append(entry)
        log.write_text("\n\n".join(entries[-_SESSION_LOG_KEEP:]) + "\n",
                       encoding="utf-8")
    except Exception as exc:
        _report_failure("session_report_file", exc)


async def _save_session_summary(session_id: str, text: str) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    key = f"session-{today}-{session_id}"
    try:
        await _mddb().add_document(
            collection=_summary_collection(),
            key=key,
            lang="en",
            content_md=text,
            meta={
                "kind": ["session-summary"],
                "session_id": [session_id],
                "date": [today],
                "source": ["conversation_memory"],
            },
        )
    except Exception as exc:
        _report_failure("session_summary_mddb", exc)


async def _refresh_summary(client: "NotebooklmClient") -> None:
    """One-time seed from NotebookLM; rolling updates take over afterwards."""
    try:
        answer = await client.ask(_SUMMARY_QUESTION, group="memory")
    except Exception as exc:
        _report_failure("notebooklm", exc)
        return
    if answer:
        _summary_cache["text"] = answer
        _summary_cache["ts"] = time.time()
        await _save_summary_to_mddb(answer)

# Optional per-group routing: {"memory": "<nb-id>", "infra": "<nb-id>", ...}
# Falls back to NOTEBOOKLM_NOTEBOOK_ID for any group not in the map.
try:
    NOTEBOOKLM_NOTEBOOK_IDS: dict[str, str] = json.loads(
        os.environ.get("NOTEBOOKLM_NOTEBOOK_IDS_JSON", "") or "{}"
    )
except json.JSONDecodeError:
    NOTEBOOKLM_NOTEBOOK_IDS = {}


class NotebooklmClient:
    """Async client for the tony-dell NotebookLM REST API."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        notebook_id: str | None = None,
    ) -> None:
        self.base_url = (base_url or NOTEBOOKLM_BASE_URL).rstrip("/")
        self.api_key = api_key or NOTEBOOKLM_API_KEY
        self.notebook_id = notebook_id or NOTEBOOKLM_NOTEBOOK_ID
        self.notebooks = dict(NOTEBOOKLM_NOTEBOOK_IDS)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(180.0))
        self._headers: dict[str, str] = {}
        if self.api_key:
            self._headers["X-API-Key"] = self.api_key

    def notebook_for(self, group: str | None) -> str | None:
        """Resolve a recall group to a notebook id; default is the memory group."""
        if group and group in self.notebooks:
            return self.notebooks[group]
        return self.notebooks.get("memory") or self.notebook_id

    @property
    def configured(self) -> bool:
        return bool(self.api_key and (self.notebook_id or self.notebooks))

    async def add_text_source(self, title: str, content: str) -> dict[str, Any] | None:
        notebook = self.notebook_for("memory")
        if not self.configured or not notebook:
            return None
        try:
            resp = await self._client.post(
                f"{self.base_url}/notebooks/{notebook}/sources/text",
                headers=self._headers,
                json={"title": title, "content": content},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            _report_failure("notebooklm", exc)
            return None

    async def ask(
        self,
        question: str,
        group: str | None = None,
        notebook: str | None = None,
    ) -> str | None:
        """Ask a notebook. Raises on transport/API errors — callers must not
        mistake a failure for "nothing found"."""
        notebook = notebook or self.notebook_for(group)
        if not self.configured or not notebook:
            return None
        resp = await self._client.post(
            f"{self.base_url}/notebooks/{notebook}/chat/ask",
            headers=self._headers,
            json={"question": question},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return None
        result = data.get("result", {})
        if isinstance(result, dict):
            return result.get("answer") or result.get("response")
        if isinstance(result, str):
            return result
        return None


class ConversationMemory:
    """Captures a voice session transcript and can persist/recall it via NotebookLM."""

    def __init__(
        self,
        session_id: str,
        client: NotebooklmClient | None = None,
    ) -> None:
        self.session_id = session_id
        self.client = client or NotebooklmClient()
        self._turns: list[dict[str, str]] = []
        self._recall_task: asyncio.Task | None = None
        # L0 structured doc-work log — shared with ToolRunner (provider
        # assigns tool_runner.doc_log = conversation.doc_items); appended
        # by ada_doc_* calls, folded into the session report timeline and
        # the unclosed-work proposal at session end.
        self.doc_items: list[dict[str, Any]] = []
        # L0 session-mechanics log (connect identity, speaker matches,
        # barge-ins, tool denials, reconnects) — folded into the session
        # report alongside doc_items so reports carry how the session went,
        # not just what was said.
        self.session_items: list[dict[str, Any]] = []
        # Session-bound identity (identified speaker's HA person, else the
        # caller name) — used to route sensitive auto-extracted memories to
        # the speaker's personal bank instead of shared banks.
        self.speaker_identity: str | None = None
        # Channel surface for the shared voice+chat history: "voice" for
        # PWA/audio sessions, the relay name for text channels
        # (chat/telegram/line). owner_identity is the pinned session owner
        # — turns only ever merge/mirror between sessions of the same owner.
        self.channel: str = "voice"
        self.owner_identity: str | None = None
        self.warm_summary()

    def log_event(self, kind: str, **fields: Any) -> None:
        """Record a session-mechanics event for the session report."""
        # `turn` = index of the last recorded transcript turn at event time —
        # the drill-down link into the raw transcript (transcript:...#t<turn>).
        self.session_items.append({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": str(kind), "turn": len(self._turns), **fields})

    def _channel_append(self, role: str, text: str) -> None:
        """Log the turn into the shared cross-channel history. no_persist
        sessions (scenario/test runs) stay out — their turns must never
        surface in real sessions' prime or mirrors."""
        if getattr(self, "no_persist", False):
            return
        _channel_log.append({
            "ts": time.time(), "role": role, "text": text.strip()[:500],
            "channel": self.channel, "session_id": self.session_id,
            "owner": self.owner_identity,
        })

    def add_user(self, text: str) -> None:
        if text.strip():
            text = text.strip()
            self._turns.append({"role": "user", "text": text, "ts": time.time()})
            self._channel_append("user", text)

    def add_assistant(self, text: str) -> None:
        if text.strip():
            text = text.strip()
            self._turns.append(
                {"role": "assistant", "text": text, "ts": time.time()})
            self._channel_append("assistant", text)

    def add_channel_mirror(self, role: str, text: str, via: str) -> None:
        """A sibling session's turn copied into this transcript, labelled
        by surface — the unified-history record. Does NOT re-enter the
        channel log (the origin session already logged it there)."""
        text = str(text or "").strip()
        if text:
            self._turns.append({
                "role": role if role in ("user", "assistant") else "user",
                "text": f"[via {via}] {text}",
                "ts": time.time(),
                "mirrored": True,
            })

    def turns(self) -> list[dict[str, Any]]:
        """Structured transcript turns ({role, text, ts}), oldest first."""
        return [dict(t) for t in self._turns]

    def recent_context(self, max_turns: int = 4, max_chars: int = 500) -> str:
        """Compact tail of this session's transcript for query expansion."""
        lines = []
        for turn in self._turns[-max_turns:]:
            role = "User" if turn["role"] == "user" else "Ada"
            lines.append(f"{role}: {turn['text']}")
        return "\n".join(lines)[-max_chars:]

    # Short utterances like "what about the other one?" embed terribly as a
    # bare vector-search query. When a query is anaphoric or very short,
    # append the recent conversation so the embedding lands near the topic
    # actually being discussed.
    _COREF_RE = re.compile(
        r"\b(it|its|that|this|they|them|the other|another|the same|those|"
        r"he|she|his|her|again|previous|earlier)\b",
        re.IGNORECASE,
    )

    def expand_query(self, query: str) -> str:
        q = str(query or "").strip()
        if not q:
            return q
        if len(q.split()) > 4 and not self._COREF_RE.search(q):
            return q
        ctx = self.recent_context()
        if not ctx:
            return q
        expanded = f"{q}\n\nConversation context:\n{ctx}"
        logger.info("query expanded with context: %r -> %d chars", q[:80], len(expanded))
        return expanded

    def transcript(self) -> str:
        lines = [f"# Ada voice session {self.session_id}", ""]
        for turn in self._turns:
            role = "User" if turn["role"] == "user" else "Ada"
            ts = turn.get("ts")
            stamp = (
                f" [{datetime.fromtimestamp(float(ts), timezone.utc).strftime('%H:%M:%S')}]"
                if ts else ""
            )
            lines.append(f"## {role}{stamp}")
            lines.append(turn["text"])
            lines.append("")
        return "\n".join(lines)

    def _save_transcript_file(self) -> None:
        """Archive the raw transcript to the local transcript directory."""
        if os.environ.get("ADA_TRANSCRIPT_SAVE", "1") in ("0", "false", "no"):
            return
        try:
            d = Path(TRANSCRIPT_DIR)
            d.mkdir(parents=True, exist_ok=True)
            day = datetime.now(timezone.utc).date().isoformat()
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.session_id)
            (d / f"{day}-{safe}.md").write_text(
                f"<!-- ref: transcript:{day}-{safe} -->\n" + self.transcript(),
                encoding="utf-8",
            )
        except Exception as exc:
            _report_failure("transcript_file", exc)

    async def persist(self) -> None:
        if getattr(self, "no_persist", False) or not self._turns:
            return
        self._save_transcript_file()
        if self.client.configured:
            title = f"Ada session {self.session_id} {datetime.now(timezone.utc).isoformat()}"
            await self.client.add_text_source(title, self.transcript())
        # Summaries/candidates live in MDDB, not NotebookLM — run them even
        # when the deep tier is unconfigured or unreachable.
        _summary_cache["task"] = asyncio.create_task(self._update_summary())

    async def _update_summary(self) -> None:
        prev = _summary_cache["text"] or await _load_summary_from_mddb()
        transcript = self.transcript()
        new = await _summarize(prev, transcript)
        if new:
            _summary_cache["text"] = new
            _summary_cache["ts"] = time.time()
            await _save_summary_to_mddb(new)
        # Per-session doc: distinct recallable unit + substrate for the
        # planned daily/weekly/monthly rollup tier.
        session_text = await _summarize_session(transcript)
        if session_text:
            await _save_session_summary(self.session_id, session_text)
            # Daily rollup tier: keep today's digest current so
            # ada_daily_summary/ada_weekly_comparison read fresh data.
            from backend import summary_rollups
            await summary_rollups.refresh_daily(
                _mddb(), day=datetime.now(timezone.utc).date().isoformat())
        if _SESSION_REPORT:
            day = datetime.now(timezone.utc).date().isoformat()
            report = await _session_report(transcript, day, self.session_id)
            if report:
                self._fold_doc_items(report)
                self._fold_session_items(report)
                _save_session_report_files(report, day, self.session_id)
        await self._extract_candidates(transcript)
        await self._extract_actions(transcript)
        await self._doc_followups()

    def _fold_doc_items(self, report: dict) -> None:
        """L0→L1 fold: append the deterministic doc-work timeline to the
        session report — the LLM summary can't misstate which actions ran."""
        items = self.doc_items
        if not items:
            return
        report["documents"] = items
        bits = []
        for it in items:
            hhmm = str(it.get("ts") or "")[11:16]
            act = it.get("action") or "?"
            target = (it.get("slug") or it.get("filename") or
                      it.get("query") or "")
            extra = ""
            if act == "archive" and it.get("pages"):
                extra = f" {it['pages']}p"
            elif act == "print" and it.get("pages"):
                extra = f" pages {it['pages']}"
            bits.append(f"{hhmm} {act} {target}{extra}".strip())
        block = str(report.get("memory_block") or "").rstrip()
        report["memory_block"] = (
            block + "\n- documents: " + "; ".join(bits)).strip()

    def _fold_session_items(self, report: dict) -> None:
        """Fold the session-mechanics log into the report + a one-line
        summary in memory_block (owner attribution, speakers seen,
        barge-ins/denials) so session reports are auditable per user."""
        items = self.session_items
        if not items:
            return
        report["session_events"] = items
        owner = next(
            (str(it.get("owner")) for it in items
             if it.get("kind") == "connect" and it.get("owner")),
            None,
        )
        speakers = [
            str(it.get("display") or it.get("name") or it.get("ha_person") or "?")
            for it in items
            if it.get("kind") in ("speaker_identified", "secondary_speaker")
        ]
        kinds: dict[str, int] = {}
        for it in items:
            k = str(it.get("kind") or "?")
            kinds[k] = kinds.get(k, 0) + 1
        bits: list[str] = []
        if owner:
            bits.append(f"owner={owner}")
        if speakers:
            bits.append("speakers=" + ",".join(dict.fromkeys(speakers)))
        if kinds.get("barge_in"):
            n = kinds["barge_in"]
            noise = kinds.get("barge_noise", 0)
            bits.append(
                f"barge-ins={n}" + (f" ({noise} noise)" if noise else ""))
        for k in ("secondary_speaker", "tool_denied", "live_reconnect",
                  "speaker_unrecognized"):
            if kinds.get(k):
                bits.append(f"{k.replace('_', '-')}={kinds[k]}")
        if not bits:
            bits.append(f"events={len(items)}")
        block = str(report.get("memory_block") or "").rstrip()
        report["memory_block"] = (
            block + "\n- session: " + "; ".join(bits)).strip()

    async def _doc_followups(self) -> None:
        """Unclosed doc work → pending action-proposal: uploads that were
        assessed but never archived/printed this session resurface in the
        next session's context tail (immediate memory while in focus)."""
        try:
            from backend import document_check
            eng = document_check.engine()
        except Exception:
            return
        archived = {k for it in self.doc_items if it.get("action") == "archive"
                    for k in (it.get("keys") or [])}
        first_ts = (self._turns[0]["ts"] if self._turns
                    else time.time() - 3600)
        today = datetime.now(timezone.utc).date().isoformat()
        made = 0
        for key, held in list(getattr(eng, "_held", {}).items()):
            meta = held.meta or {}
            try:
                cts = datetime.fromisoformat(str(meta.get("created") or "")).timestamp()
            except (TypeError, ValueError):
                continue
            if cts < first_ts or key in archived:
                continue
            fn = meta.get("filename") or key
            dt = meta.get("doc_type") or "document"
            line = (f"task: uploaded document '{fn}' ({dt}) was assessed "
                    f"but not archived or printed — intake key {key} held")
            m = {
                "kind": ["action-proposal"], "status": ["pending"],
                "action_type": ["task"], "source": ["doc-intake"],
                "written_by": ["conversation_memory"],
                "session_id": [self.session_id], "date": [today],
            }
            try:
                await _mddb().add_document(
                    collection=_actions_collection(),
                    key=f"doc-followup-{today}-{self.session_id}-{made}",
                    lang="en", content_md=line, meta=m)
                made += 1
            except Exception as exc:
                _report_failure("doc_followup", exc)
        if made:
            logger.info("session %s: %d unclosed doc upload(s) proposed",
                        self.session_id, made)

    async def _extract_candidates(self, transcript: str) -> None:
        """Auto-distill durable facts/decisions from the session into bank
        docs with status=draft — invisible to recall until a human promotes
        them (vault inbox -> consolidate flips draft -> active)."""
        if os.environ.get("ADA_EXTRACT_CANDIDATES", "1") in ("0", "false", "no"):
            return
        if not _GEMINI_API_KEY:
            return
        try:
            from backend.memory_banks import get_registry
            registry = get_registry()
            bank_names = [b.name for b in registry.banks().values() if b.writable]
            personal_bank = "personal"
            if self.speaker_identity:
                scoped = registry._scoped_bank_name(self.speaker_identity)
                if scoped:
                    personal_bank = scoped
            prompt = (
                "Transcript of one voice session:\n"
                f"{transcript[-_SUMMARY_MAX_TRANSCRIPT_CHARS:]}\n\n"
                "IMPORTANT routing: anything about private documents "
                "(deeds, ID cards, passports, bank/legal papers), a named "
                "person's identifying details, or private transactions must "
                f"use bank '{personal_bank}' — never shared banks (general, "
                "home, people, purchase). Shared banks are for household "
                "facts everyone may see.\n"
                "Extract up to 5 durable items worth remembering long-term — "
                "things a future session should know, not small talk or "
                "one-off questions. Look especially for: facts, decisions, "
                "preferences; procedures that worked (kind: procedure); "
                "user corrections of something previously said or remembered "
                "(set 'corrects' to the subject of the older fact, e.g. the "
                "user says 'no, it's actually in the cabinet'); and questions "
                "the assistant could not answer (kind: note, subject starting "
                "with 'gap-'). "
                "Return a JSON array of objects with keys "
                f'"text", "bank" (one of: {", ".join(bank_names)}), '
                '"subject", "kind" (fact|preference|person|procedure|note), '
                'and optional "corrects" (subject of the older fact this '
                'corrects). Return [] if nothing is worth keeping.'
            )
            resp = await gemini_pool.generate(
                model=_SUMMARY_MODEL, contents=prompt,
                tool="extract_candidates")
            text = (resp.text or "").strip()
            m = re.search(r"\[.*\]", text, re.DOTALL)
            candidates = json.loads(m.group(0)) if m else []
        except Exception as exc:
            _report_failure("extract_candidates", exc)
            return
        today = datetime.now(timezone.utc).date().isoformat()
        personal_bank_name = "personal"
        if self.speaker_identity:
            personal_bank_name = (
                registry._scoped_bank_name(self.speaker_identity)
                or "personal")
        for i, cand in enumerate(candidates[:5]):
            if not isinstance(cand, dict) or not cand.get("text"):
                continue
            try:
                bank = registry.bank(str(cand.get("bank") or ""))
            except Exception:
                bank = None
            if bank is None or not bank.writable:
                bank = registry.bank("personal")  # safest: per-instance
            # Sensitive content must never land in a shared bank — reroute
            # to the session speaker's personal bank (drafts in shared banks
            # surface to everyone who can read the bank).
            if (bank.name != personal_bank_name
                    and _SENSITIVE_MEMORY_RE.search(
                        str(cand.get("text") or "")
                        + " " + str(cand.get("subject") or ""))):
                bank = registry.bank(personal_bank_name)
            kind = str(cand.get("kind") or "note")
            if kind not in bank.kinds:
                kind = "note"
            meta = {
                "bank": [bank.name],
                "scope": [registry.instance],
                "kind": [kind],
                "status": ["draft"],
                "source": ["extract"],
                "written_by": ["conversation_memory"],
                "session_id": [self.session_id],
                "valid_from": [today],
                "last_verified": [today],
            }
            if cand.get("corrects"):
                # Correction candidate: subject = the corrected fact's subject
                # so the join key points a reviewer at the doc to supersede.
                meta["subject"] = [str(cand["corrects"])]
                meta["attribute"] = ["correction"]
            elif cand.get("subject"):
                meta["subject"] = [str(cand["subject"])]
            key = f"extract-{today}-{self.session_id}-{i}"
            try:
                await _mddb().add_document(
                    collection=bank.mddb_collection,
                    key=key, lang="en",
                    content_md=str(cand["text"]), meta=meta,
                )
            except Exception as exc:
                _report_failure("candidate_mddb", exc)
        if candidates:
            logger.info("session %s: extracted %d candidate memories",
                        self.session_id, min(len(candidates), 5))

    async def _extract_actions(self, transcript: str) -> None:
        """Distill conversation action items (appointments, reminders,
        todos) into pending proposals in ada-ha-actions-{instance}. Ada
        offers them at the next session; actual calendar/task writes still
        go through the confirmed=true tools."""
        if os.environ.get("ADA_EXTRACT_ACTIONS", "1") in ("0", "false", "no"):
            return
        if not _GEMINI_API_KEY:
            return
        try:
            prompt = (
                "Transcript of one voice session:\n"
                f"{transcript[-_SUMMARY_MAX_TRANSCRIPT_CHARS:]}\n\n"
                "Extract up to 3 concrete action items the user stated or "
                "implied — things to put on the calendar or task list. "
                "Examples: an appointment mentioned ('dentist Thursday 3pm'), "
                "a reminder request ('remind me to…'), a purchase or chore "
                "('need to buy X'). Skip vague talk and things already "
                "scheduled during the conversation. "
                "Return a JSON array of objects with keys "
                '"type" (event|task), "title", "when" (ISO date/time or '
                'natural language like "Thursday 15:00"), and optional '
                '"notes". Return [] if there is nothing actionable.'
            )
            resp = await gemini_pool.generate(
                model=_SUMMARY_MODEL, contents=prompt,
                tool="extract_actions")
            text = (resp.text or "").strip()
            m = re.search(r"\[.*\]", text, re.DOTALL)
            actions = json.loads(m.group(0)) if m else []
        except Exception as exc:
            _report_failure("extract_actions", exc)
            return
        today = datetime.now(timezone.utc).date().isoformat()
        for i, act in enumerate(actions[:3]):
            if not isinstance(act, dict) or not act.get("title"):
                continue
            atype = "event" if str(act.get("type")) == "event" else "task"
            when = str(act.get("when") or "").strip()
            line = f"{atype}: {act['title']}" + (f" — {when}" if when else "")
            if act.get("notes"):
                line += f" ({act['notes']})"
            meta = {
                "kind": ["action-proposal"],
                "status": ["pending"],
                "action_type": [atype],
                "source": ["extract"],
                "written_by": ["conversation_memory"],
                "session_id": [self.session_id],
                "date": [today],
            }
            if when:
                meta["when"] = [when]
            key = f"action-{today}-{self.session_id}-{i}"
            try:
                await _mddb().add_document(
                    collection=_actions_collection(),
                    key=key, lang="en", content_md=line, meta=meta,
                )
            except Exception as exc:
                _report_failure("action_mddb", exc)
        if actions:
            logger.info("session %s: extracted %d action proposal(s)",
                        self.session_id, min(len(actions), 3))

    def warm_summary(self) -> None:
        """Kick a background warm: load the persisted summary, or seed it."""
        if _summary_cache["text"] is not None:
            return
        task = _summary_cache.get("task")
        if task is not None and not task.done():
            return
        try:
            _summary_cache["task"] = asyncio.create_task(self._warm_summary())
        except RuntimeError:
            pass  # no running loop (e.g. unit tests)

    async def _warm_summary(self) -> None:
        text = await _load_summary_from_mddb()
        if text:
            _summary_cache["text"] = text
            _summary_cache["ts"] = time.time()
            return
        if self.client.configured:
            await _refresh_summary(self.client)

    def start_recall(
        self,
        question: str,
        on_complete: Callable[[str | None], Awaitable[None]],
        group: str | None = None,
        bank: str | None = None,
        on_slow: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        if not self.client.configured and not bank:
            return "My notes are not connected."
        if self._recall_task is not None and not self._recall_task.done():
            return "I'm still checking my notes."
        bank_obj: Any = None
        if bank:
            from backend.memory_banks import get_registry
            if str(bank).lower() in ("all", "*"):
                bank_obj = "all"
            else:
                try:
                    bank_obj = get_registry().bank(str(bank))
                except KeyError as exc:
                    return str(exc)
        question = self.expand_query(question)
        self.warm_summary()
        self._recall_task = asyncio.create_task(
            self._recall(question, on_complete, group, on_slow, bank=bank_obj)
        )
        summary = _summary_cache["text"]
        if summary and group in (None, "memory") and bank is None:
            return (
                "The detailed notes search is running in the background and "
                "the result will be spoken when ready. For your own context "
                "only, here is a summary of PAST sessions — do NOT switch to "
                "or offer those topics unless the user asks; answer the "
                f"CURRENT request first: {summary}"
            )
        return "One moment, I'm checking my notes."

    async def _recall(
        self,
        question: str,
        on_complete: Callable[[str | None], Awaitable[None]],
        group: str | None = None,
        on_slow: Callable[[], Awaitable[None]] | None = None,
        bank: Any | None = None,
    ) -> None:
        if bank is not None:
            # Fast tier: semantic search on the bank's MDDB collection, or
            # across every assigned bank when the model asked for 'all'.
            # memory_search applies the active/draft filters, the draft
            # score bar, and confidence tagging.
            from backend.memory_ops import memory_search as _bank_search
            from backend.memory_banks import get_registry
            registry = get_registry()
            bank_name = "all" if bank == "all" else bank.name
            res = await _bank_search(_mddb(), registry, bank_name, question, limit=3)
            hits = res.get("hits") or []
            if hits:
                logger.info(
                    "bank recall %r: %d hit(s), scores=%s",
                    bank_name, len(hits),
                    [round(h.get("score") or 0, 3) for h in hits],
                )
                where = ("your memory banks" if bank == "all"
                         else f"the {bank.title} memory bank")
                lines = [f"From {where}:"]
                for h in hits:
                    body = str(h.get("content") or "").strip()
                    if not body:
                        continue
                    if h.get("draft"):
                        tag = "[unverified draft] "
                    elif h.get("unverified"):
                        tag = "[unverified] "
                    else:
                        tag = ""
                    prefix = f"{h['bank']}: " if h.get("bank") else ""
                    lines.append(f"- {tag}{prefix}{body}")
                await on_complete("\n".join(lines) if len(lines) > 1 else
                                  f"I found a note in {where} but it was empty.")
                return
            # Low confidence: escalate to the bank's deep-tier notebook if it
            # has one, else the instance's memory notebook as a last resort.
            # The query is logged (json-quoted) so memory-gap-report.py can
            # cluster misses into "knowledge gap" drafts.
            logger.info(
                "bank recall %r: miss q=%s — escalating",
                bank_name, json.dumps(question[:200]),
            )
            mid = await self._recall_summaries(question)
            if mid:
                await on_complete(f"From recent memory:\n{mid}")
                return
            if bank == "all":
                return await self._recall_ask(question, on_complete, "memory", on_slow, None)
            notebook = bank.notebook(registry.notebook_ids)
            if not notebook:
                logger.info("bank %r has no notebook; falling back to memory group", bank.name)
                return await self._recall_ask(question, on_complete, "memory", on_slow, None)
            return await self._recall_ask(question, on_complete, None, on_slow, notebook)
        # No bank specified: try the mid-tier summary collection before the
        # ~26s NotebookLM path.
        mid = await self._recall_summaries(question)
        if mid:
            await on_complete(f"From recent memory:\n{mid}")
            return
        await self._recall_ask(question, on_complete, group, on_slow, None)

    async def _recall_summaries(
        self, question: str
    ) -> str | None:
        """Mid tier: semantic search over session/daily/weekly/monthly
        summaries + the rolling recent-sessions doc. Returns an answer
        string on a confident hit, else None (caller escalates to
        NotebookLM)."""
        threshold = float(os.environ.get("ADA_BANK_SEARCH_THRESHOLD", "0.45"))
        coll = _summary_collection()
        mddb = _mddb()
        try:
            if mddb.is_ops_routed(coll):
                # Ops store has no embeddings — a vector call is a
                # guaranteed 400. Listings there are oldest-first, so
                # bound by `date` meta, then keyword-rank candidates.
                from backend import memory_ops
                days = [
                    (datetime.now(timezone.utc).date() - timedelta(days=i)).isoformat()
                    for i in range(14)
                ]
                listed = await mddb.search_documents(
                    collection=coll, filter_meta={"date": days}, limit=100)
                docs = memory_ops._keyword_rank(listed, question)[:3] or None
            else:
                docs = await mddb.vector_search(
                    collection=coll,
                    query=question,
                    limit=3,
                    threshold=threshold,
                )
        except Exception as exc:
            _report_failure("summary_recall", exc)
            return None
        if not docs:
            return None
        logger.info(
            "summary recall: %d hit(s), scores=%s",
            len(docs), [round(d.get("score") or 0, 3) for d in docs],
        )
        lines = []
        for d in docs:
            body = str(d.get("contentMd") or d.get("content_md") or "").strip()
            if body:
                lines.append(f"- {body}")
        return "\n".join(lines) if lines else None

    async def _recall_ask(
        self,
        question: str,
        on_complete: Callable[[str | None], Awaitable[None]],
        group: str | None = None,
        on_slow: Callable[[], Awaitable[None]] | None = None,
        notebook: str | None = None,
    ) -> None:
        ctx = notebook or group or "memory"
        cached = await self._nlm_cache_get(question, ctx)
        if cached is not None:
            await on_complete(cached)
            return
        ask = asyncio.create_task(self.client.ask(question, group=group, notebook=notebook))
        if on_slow is not None:
            done, _ = await asyncio.wait({ask}, timeout=RECALL_SLOW_AFTER_S)
            if not done:
                try:
                    await on_slow()
                except Exception as exc:
                    logger.warning("recall slow filler failed: %s", exc)
        try:
            answer = await ask
        except Exception as exc:
            _report_failure("notebooklm", exc)
            await on_complete(
                "My notes search just failed — please ask me to try again "
                "in a moment."
            )
            return
        if answer:
            await self._nlm_cache_put(question, ctx, answer)
            await on_complete(answer)
        else:
            await on_complete("I couldn't find anything in my notes.")

    async def _nlm_cache_get(self, question: str, ctx: str) -> str | None:
        """Deep-tier answer cache: a semantically identical recent question
        in this context (bank/group/notebook) returns its stored answer."""
        try:
            docs = await _mddb().vector_search(
                collection=_nlm_cache_collection(),
                query=question,
                limit=1,
                filter_meta={"ctx": [ctx]},
                threshold=_NLM_CACHE_THRESHOLD,
            )
        except Exception as exc:
            _report_failure("nlm_cache", exc)
            return None
        if not docs:
            return None
        doc = docs[0]
        meta = doc.get("meta") or {}
        try:
            created = datetime.fromisoformat(str((meta.get("created") or [""])[0]))
            age = datetime.now(timezone.utc).date() - created.date()
        except (ValueError, TypeError, IndexError):
            return None
        if age.days > _NLM_CACHE_TTL_DAYS:
            return None
        answer = str(doc.get("contentMd") or doc.get("content_md") or "").strip()
        if answer:
            logger.info("nlm cache hit ctx=%r score=%s", ctx, doc.get("score"))
            return answer
        return None

    async def _nlm_cache_put(self, question: str, ctx: str, answer: str) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        import hashlib
        digest = hashlib.sha1(f"{ctx}\n{question}".encode()).hexdigest()[:12]
        try:
            await _mddb().add_document(
                collection=_nlm_cache_collection(),
                key=f"{ctx}-{today}-{digest}",
                lang="en",
                content_md=answer[:8000],
                meta={
                    "kind": ["nlm-answer"],
                    "ctx": [ctx],
                    "question": [question[:300]],
                    "created": [today],
                    "source": ["conversation_memory"],
                },
            )
        except Exception as exc:
            _report_failure("nlm_cache", exc)
