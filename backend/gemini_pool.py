"""Shared Gemini key pool + free-tier quota surface for side-tool calls.

(card ada-gemini-free-tier-quota, 2026-10-10) Every non-live
generate_content path — web_search grounding, document classify, CMS
edit LLM ops, session summaries/rollups, decision/devteam checks,
posture/clutter verify, vcast TTS — shares the one configured API key.
Free-tier quota is per-project-per-model-day (~20 req/day on the flash
tier), so one 429 used to leave every later call silently degraded or
retrying doomed requests.

Pool (card option B, ready for N=1..n): keys come from GEMINI_API_KEY,
then the comma list GEMINI_API_KEYS, then numbered GEMINI_API_KEY_2..
GEMINI_API_KEY_9 — dropping extra projects or a billing-enabled key
(card option A) into the env is all the rotation needs; no code change.
Calls round-robin across keys that still have quota for the model.

Exhaustion: a 429/quota-shaped error marks (key, model) exhausted until
the free-tier daily reset (midnight US/Pacific). next_key() skips
exhausted pairs; a drained pool returns None so callers fail loud or
engage their fallback instead of burning latency on doomed requests.
ADA_GEMINI_SIMULATE_EXHAUSTED=1 forces the drained state for the
forced-exhaust verification test.

Surface (card option D): emit_ops_event() posts a throttled ops-event
doc to ada-ha-events-<instance> — same feed shape as the provider's
_emit_ops_event and write_outbox dead-letters — so quota burn lands in
the hourly digest before it reaches zero. status() feeds
conversation_health so pool state is visible, not silent.

Cache (card option C): cache_get/cache_put are a small in-process TTL
cache for the repeat-heavy callers — grounded web_search answers and
document classify — so repeats never reach the API at all.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

logger = logging.getLogger("tools")

# 429/quota-shaped error text — the genai client raises APIError(code=429,
# status='RESOURCE_EXHAUSTED') but older transports surface bare message
# strings, so match both the structured fields and the message.
_QUOTA_SHAPED_RE = re.compile(
    r"\b429\b|resource_exhausted|rate.?limit|too many requests|quota",
    re.I)


class QuotaExhaustedError(RuntimeError):
    """Raised when every configured key is out of quota for the model.

    Deliberately quota-shaped (the message matches _QUOTA_SHAPED_RE) so
    callers that annotate on is_quota_error treat a drained pool exactly
    like a live 429.
    """


def is_quota_error(exc: BaseException) -> bool:
    """True when the exception looks like a 429/quota exhaustion, not an
    ordinary failure (timeout, empty answer, bad request)."""
    for attr in ("code", "status_code", "status"):
        v = getattr(exc, attr, None)
        if v == 429 or str(v).upper() == "RESOURCE_EXHAUSTED":
            return True
    return bool(_QUOTA_SHAPED_RE.search(str(exc)))


# ---------------------------------------------------------------- key pool

_EXTRA_KEY_VARS = [f"GEMINI_API_KEY_{i}" for i in range(2, 10)]

_lock = threading.Lock()
_rr_index = 0
# (key, model) -> monotonic ts when the pair stops being served.
_exhausted: dict[tuple[str, str], float] = {}
# Last quota error per tool — feeds status() for conversation_health.
_last_quota: dict[str, dict[str, Any]] = {}


def configured_keys() -> list[str]:
    """Env-wired keys, deduped in order: GEMINI_API_KEY first (the live
    session also uses it), then GEMINI_API_KEYS comma list, then numbered
    GEMINI_API_KEY_2..9, then the GOOGLE_API_KEY legacy fallback."""
    seen: dict[str, None] = {}
    for raw in (
            os.environ.get("GEMINI_API_KEY"),
            *(os.environ.get("GEMINI_API_KEYS") or "").split(","),
            *(os.environ.get(v) for v in _EXTRA_KEY_VARS),
            os.environ.get("GOOGLE_API_KEY")):
        k = str(raw or "").strip()
        if k:
            seen[k] = None
    return list(seen)


def _reset_at() -> float:
    """Epoch ts of the next free-tier daily reset (midnight US/Pacific;
    Google resets per-day quotas on the Pacific boundary)."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if midnight <= now:
            from datetime import timedelta
            midnight += timedelta(days=1)
        return midnight.timestamp()
    except Exception:
        return time.time() + 24 * 3600


def _prune() -> None:
    now = time.time()
    for pair, until in list(_exhausted.items()):
        if until <= now:
            del _exhausted[pair]


def next_key(model: str = "") -> str | None:
    """Round-robin pick among keys that still have quota for `model`.
    None when the pool is drained (or ADA_GEMINI_SIMULATE_EXHAUSTED)."""
    if os.environ.get("ADA_GEMINI_SIMULATE_EXHAUSTED", "").lower() in (
            "1", "true", "yes"):
        return None
    keys = configured_keys()
    if not keys:
        return None
    global _rr_index
    with _lock:
        _prune()
        live = [k for k in keys
                if _exhausted.get((k, model), 0) <= time.time()]
        if not live:
            return None
        key = live[_rr_index % len(live)]
        _rr_index += 1
        return key


def mark_exhausted(key: str, model: str = "", exc: BaseException | None = None,
                   tool: str = "") -> None:
    """Mark (key, model) out of quota until the free-tier daily reset and
    record the error for status(). Idempotent — remarking just refreshes
    the reset stamp."""
    if not key:
        return
    with _lock:
        _prune()
        _exhausted[(key, model)] = _reset_at()
        if tool:
            _last_quota[tool] = {
                "model": model,
                "error": str(exc)[:200] if exc else "quota exhausted",
                "at": datetime.now().astimezone().isoformat(
                    timespec="seconds"),
            }
    logger.warning("gemini key …%s exhausted for model %s until reset",
                   key[-4:], model)


def pool_exhausted(model: str = "") -> bool:
    """True when no configured key still has quota for `model`."""
    return bool(configured_keys()) and next_key(model) is None


def status() -> dict[str, Any]:
    """Pool snapshot for conversation_health — key counts and the last
    quota error per tool. Never carries key material."""
    keys = configured_keys()
    with _lock:
        _prune()
        exhausted_pairs = len(_exhausted)
    return {
        "keys_configured": len(keys),
        "exhausted_key_models": exhausted_pairs,
        "reset_at": (datetime.fromtimestamp(_reset_at()).astimezone()
                     .isoformat(timespec="seconds")),
        "simulate_exhausted": os.environ.get(
            "ADA_GEMINI_SIMULATE_EXHAUSTED", "").lower()
            in ("1", "true", "yes"),
        "last_quota_errors": dict(_last_quota),
    }


def reset() -> None:
    """Test hook — clear exhaustion marks, throttle stamps, caches and
    the round-robin cursor."""
    global _rr_index
    with _lock:
        _exhausted.clear()
        _last_quota.clear()
        _event_at.clear()
        _caches.clear()
        _rr_index = 0


# ------------------------------------------------------------- ops events

# Quota-burn ops events are throttled — a 429 storm must not spam the
# ada-ha-events digest; one event per tool per window shows the burn.
_EVENT_MIN_S = float(os.environ.get("ADA_QUOTA_EVENT_MIN_S", "300"))
_event_at: dict[str, float] = {}


def emit_ops_event(mddb: Any, tool: str, exc: BaseException,
                   session_id: str = "", instance: str | None = None,
                   ev_type: str | None = None, detail: str = "") -> None:
    """Fire-and-forget ops event to ada-ha-events-<instance> — the hourly
    chaba report feed surfaces these, so quota burn lands in the digest
    before it reaches zero. Throttled per tool; no-op when mddb is absent
    (chaba mode) or no event loop is running."""
    if mddb is None:
        return
    try:
        from backend.instance import ada_instance_id
        instance = instance or ada_instance_id()
    except Exception:
        return
    now_mono = time.monotonic()
    prev_at = _event_at.get(tool)
    if prev_at is not None and now_mono - prev_at < _EVENT_MIN_S:
        return
    _event_at[tool] = now_mono
    collection = f"ada-ha-events-{instance}"
    session = str(session_id or "runner")
    ev = ev_type or f"{tool}_quota"
    content = detail or (
        f"{tool}: gemini call hit a quota/rate-limit error — "
        f"key marked exhausted until the daily reset: "
        f"{type(exc).__name__}: {exc}"[:200])

    async def _post() -> None:
        try:
            now = datetime.now().astimezone()
            await mddb.add_document(
                collection=collection,
                key=f"ops-{session}-{ev}-{now:%Y%m%d%H%M%S%f}",
                lang="en",
                content_md=content,
                meta={
                    "kind": ["ops-event"],
                    "type": [ev],
                    "instance": [instance],
                    "tool": [tool],
                    "session_id": [session],
                    "ts": [now.isoformat(timespec="seconds")],
                },
                timeout=30,
                tool=tool,
                session_id=session,
            )
        except Exception:
            logger.debug("%s quota ops event emit failed", tool,
                         exc_info=True)

    try:
        asyncio.get_running_loop().create_task(_post())
    except RuntimeError:
        return  # no loop (unit tests, shutdown) — nothing to schedule


# ------------------------------------------------------------------ cache

_caches: dict[str, OrderedDict[str, tuple[float, Any]]] = {}
_CACHE_CAP = int(os.environ.get("ADA_GEMINI_CACHE_CAP", "64"))


def cache_get(namespace: str, key: str) -> Any | None:
    """Return the cached value for (namespace, key) or None on miss/expiry."""
    ns = _caches.get(namespace)
    if ns is None:
        return None
    hit = ns.get(key)
    if hit is None:
        return None
    until, value = hit
    if until <= time.time():
        ns.pop(key, None)
        return None
    ns.move_to_end(key)
    return value


def cache_put(namespace: str, key: str, value: Any, ttl_s: float) -> None:
    """Store `value` under (namespace, key) for ttl_s seconds. Bounded per
    namespace — oldest entries evict first."""
    if ttl_s <= 0:
        return
    ns = _caches.setdefault(namespace, OrderedDict())
    ns[key] = (time.time() + ttl_s, value)
    ns.move_to_end(key)
    while len(ns) > _CACHE_CAP:
        ns.popitem(last=False)


# ---------------------------------------------------------------- generate

async def generate(model: str, contents: Any, config: Any = None,
                   client: Any = None, timeout: float | None = None,
                   tool: str = "", mddb: Any = None,
                   session_id: str = "") -> Any:
    """One generate_content call over the key pool.

    Walks non-exhausted keys round-robin; a quota-shaped error marks the
    (key, model) pair exhausted and moves to the next key. Raises the
    original error on non-quota failures, QuotaExhaustedError when every
    key is drained. `client` short-circuits the pool entirely (test
    injection — engine constructors keep passing their fake through).
    Emits one throttled ops event per call when mddb is provided.
    """
    if client is not None:
        return await client.aio.models.generate_content(
            model=model, contents=contents, config=config)
    from google import genai
    keys = configured_keys()
    if not keys:
        raise RuntimeError("GEMINI_API_KEY is not set")
    last_exc: BaseException | None = None
    for _ in range(len(keys)):
        key = next_key(model)
        if key is None:
            break
        try:
            call = genai.Client(api_key=key).aio.models.generate_content(
                model=model, contents=contents, config=config)
            if timeout:
                return await asyncio.wait_for(call, timeout=timeout)
            return await call
        except Exception as exc:
            if not is_quota_error(exc):
                raise
            mark_exhausted(key, model, exc, tool=tool)
            last_exc = exc
    if mddb is not None and last_exc is not None:
        emit_ops_event(mddb, tool or "gemini", last_exc,
                       session_id=session_id)
    if last_exc is not None:
        raise QuotaExhaustedError(
            f"gemini free-tier quota exhausted for {model or 'model'} on "
            f"all {len(keys)} configured key(s) — resets at the daily "
            "boundary (midnight US/Pacific); tell the user plainly that "
            f"the quota is spent: {type(last_exc).__name__}: {last_exc}"[:300])
    raise QuotaExhaustedError(
        f"gemini free-tier quota already exhausted for {model or 'model'} "
        f"on all {len(keys)} configured key(s) — resets at the daily "
        "boundary (midnight US/Pacific); tell the user plainly that the "
        "quota is spent")
