"""Conversation memory and NotebookLM recall for Ada voice sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

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


def conversation_health() -> dict[str, Any]:
    """Snapshot of recall/memory subsystem health for status endpoints."""
    return {
        "summary_cached": _summary_cache["text"] is not None,
        "summary_ts": _summary_cache["ts"],
        "recall_running": bool(
            _summary_cache.get("task") and not _summary_cache["task"].done()
        ),
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
        from google import genai
        prompt = (
            "Existing summary of our recent voice sessions:\n"
            f"{prev or '(none yet)'}\n\n"
            "Transcript of the latest session:\n"
            f"{transcript[-_SUMMARY_MAX_TRANSCRIPT_CHARS:]}\n\n"
            "Update the summary to cover the recent sessions in two or three "
            "sentences. Keep it concise and factual."
        )
        resp = await genai.Client(api_key=_GEMINI_API_KEY).aio.models.generate_content(
            model=_SUMMARY_MODEL, contents=prompt
        )
        return (resp.text or "").strip() or None
    except Exception as exc:
        _report_failure("summary", exc)
        return None


async def _summarize_session(transcript: str) -> str | None:
    """Standalone summary of one session — the substrate for tiered rollups."""
    if not _GEMINI_API_KEY:
        return None  # _summarize already reported the missing key
    try:
        from google import genai
        prompt = (
            "Transcript of one voice session:\n"
            f"{transcript[-_SUMMARY_MAX_TRANSCRIPT_CHARS:]}\n\n"
            "Summarize this session in two or three sentences. Include any "
            "facts, decisions, preferences, or requests worth remembering."
        )
        resp = await genai.Client(api_key=_GEMINI_API_KEY).aio.models.generate_content(
            model=_SUMMARY_MODEL, contents=prompt
        )
        return (resp.text or "").strip() or None
    except Exception as exc:
        _report_failure("session_summary", exc)
        return None


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
        self.warm_summary()

    def add_user(self, text: str) -> None:
        if text.strip():
            self._turns.append({"role": "user", "text": text.strip(), "ts": time.time()})

    def add_assistant(self, text: str) -> None:
        if text.strip():
            self._turns.append({"role": "assistant", "text": text.strip(), "ts": time.time()})

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
            lines.append(f"## {role}")
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
                self.transcript(), encoding="utf-8"
            )
        except Exception as exc:
            _report_failure("transcript_file", exc)

    async def persist(self) -> None:
        if not self._turns:
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
        await self._extract_candidates(transcript)
        await self._extract_actions(transcript)

    async def _extract_candidates(self, transcript: str) -> None:
        """Auto-distill durable facts/decisions from the session into bank
        docs with status=draft — invisible to recall until a human promotes
        them (vault inbox -> consolidate flips draft -> active)."""
        if os.environ.get("ADA_EXTRACT_CANDIDATES", "1") in ("0", "false", "no"):
            return
        if not _GEMINI_API_KEY:
            return
        try:
            from google import genai
            from backend.memory_banks import get_registry
            registry = get_registry()
            bank_names = [b.name for b in registry.banks().values() if b.writable]
            prompt = (
                "Transcript of one voice session:\n"
                f"{transcript[-_SUMMARY_MAX_TRANSCRIPT_CHARS:]}\n\n"
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
            resp = await genai.Client(api_key=_GEMINI_API_KEY).aio.models.generate_content(
                model=_SUMMARY_MODEL, contents=prompt
            )
            text = (resp.text or "").strip()
            m = re.search(r"\[.*\]", text, re.DOTALL)
            candidates = json.loads(m.group(0)) if m else []
        except Exception as exc:
            _report_failure("extract_candidates", exc)
            return
        today = datetime.now(timezone.utc).date().isoformat()
        for i, cand in enumerate(candidates[:5]):
            if not isinstance(cand, dict) or not cand.get("text"):
                continue
            try:
                bank = registry.bank(str(cand.get("bank") or ""))
            except Exception:
                bank = None
            if bank is None or not bank.writable:
                bank = registry.bank("personal")  # safest: per-instance
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
            from google import genai
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
            resp = await genai.Client(api_key=_GEMINI_API_KEY).aio.models.generate_content(
                model=_SUMMARY_MODEL, contents=prompt
            )
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
                "the result will be spoken when ready. Meanwhile, tell the "
                "user this summary of recent sessions: "
                f"{summary}"
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
        try:
            docs = await _mddb().vector_search(
                collection=_summary_collection(),
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
