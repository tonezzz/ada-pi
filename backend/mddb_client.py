"""Thin async client for the MDDB v1 HTTP API (shared by tool_runner and
conversation_memory — single place for the wire contract)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("tools")

MDDB_BASE_URL = os.environ.get("MDDB_BASE_URL", "http://127.0.0.1:11023/v1")
# Ops store: high-volume append-only telemetry lives on a separate
# no-embedding instance so the leader's vector index stays small.
# Unset -> everything routes to the leader (single-store behaviour).
MDDB_OPS_URL = (os.environ.get("MDDB_OPS_URL") or "").rstrip("/")

# Read replica: MDDB_READ_URL points at a read-only follower (e.g. the
# idc01 replica on :11123). Reads are lag-GATED — the follower's
# /v1/replication/status is checked (cached READ_CHECK_TTL_S) and reads
# only go there while healthy and under MDDB_READ_MAX_LAG_MS. Writes and
# write-path reads always hit the leader.
# MDDB_READ_MODE: "prefer" (default) reads the follower first — offload
# for batch/remote consumers. "fallback" reads the leader first and only
# fails over to the follower on transport/5xx errors — right choice for
# services co-located with the leader (leader-down resilience without
# paying replica latency on the happy path).
MDDB_READ_URL = (os.environ.get("MDDB_READ_URL") or "").rstrip("/")
MDDB_READ_MODE = os.environ.get("MDDB_READ_MODE", "prefer")
MDDB_READ_MAX_LAG_MS = float(os.environ.get("MDDB_READ_MAX_LAG_MS", "120000"))
_READ_CHECK_TTL_S = 30.0

# Prefix match — per-instance names (ada-ha-events-tony) route with
# their family. Keep in sync with docs/kb/mddb-ops-split.md.
OPS_COLLECTION_PREFIXES = (
    "host-logs",
    "ada-ha-events-",
    "ada-ha-actions-",
    "ada-ha-snapshots-",
    "ada-ha-recall-summary-",
    "ada-ha-scenario-reports",
    "yomi-digest",
)


def is_ops_collection(name: str) -> bool:
    return any(name.startswith(p) for p in OPS_COLLECTION_PREFIXES)


class MddbClient:
    """Thin async client for the MDDB v1 HTTP API."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or MDDB_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))


class MddbClient:
    """Thin async client for the MDDB v1 HTTP API.

    Ops routing: when constructed WITHOUT an explicit base_url and
    MDDB_OPS_URL is set, collections matching OPS_COLLECTION_PREFIXES
    automatically go to the ops store — per-call, so one client
    handles banks and telemetry transparently. An explicit base_url
    pins the client to that store (lab tests, followers)."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or MDDB_BASE_URL).rstrip("/")
        # ops routing only on the default leader client
        self._ops_url = (MDDB_OPS_URL if base_url is None else "")
        # follower reads only on the default leader client — an explicit
        # base_url pins the client (lab tests, direct follower access)
        self._read_url = (MDDB_READ_URL if base_url is None else "")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        self._follower_ok_at = float("-inf")
        self._follower_ok = False

    def is_ops_routed(self, collection: str) -> bool:
        """True when this client routes the collection to the ops store
        (no embedding provider there — vector_search will always fail)."""
        return bool(self._ops_url) and is_ops_collection(collection)

    async def _follower_usable(self) -> bool:
        """Refreshed every _READ_CHECK_TTL_S, fails closed to the leader.
        prefer: follower only while replication is healthy and inside the
        lag budget — a stale replica quietly serves old memory.
        fallback: follower is the outage path — a healthy /v1/health
        suffices; hours-stale recall beats none when the leader is down."""
        if not self._read_url:
            return False
        now = asyncio.get_running_loop().time()
        if now - self._follower_ok_at < _READ_CHECK_TTL_S:
            return self._follower_ok
        self._follower_ok_at = now
        try:
            if MDDB_READ_MODE == "fallback":
                resp = await self._client.get(
                    f"{self._read_url}/health", timeout=3.0)
                self._follower_ok = resp.json().get("status") == "healthy"
            else:
                resp = await self._client.get(
                    f"{self._read_url}/replication/status", timeout=3.0)
                st = resp.json()
                self._follower_ok = bool(
                    st.get("healthy")
                    and float(st.get("replication_lag_ms") or 0)
                    <= MDDB_READ_MAX_LAG_MS)
        except Exception:
            self._follower_ok = False
        return self._follower_ok

    def _url(self, collection: str) -> str:
        if self._ops_url and is_ops_collection(collection):
            return self._ops_url
        return self.base_url

    async def _read_bases(self, collection: str) -> list[str]:
        """Ordered read targets: ops collections keep their store; the
        rest try the follower (when it passes the lag gate) and the
        leader in MDDB_READ_MODE order."""
        if self._ops_url and is_ops_collection(collection):
            return [self._ops_url]
        follower = self._read_url if await self._follower_usable() else ""
        if MDDB_READ_MODE == "fallback":
            return [b for b in (self.base_url, follower) if b]
        return [b for b in (follower, self.base_url) if b]

    async def _read_post(
        self, collection: str, path: str, payload: dict[str, Any]
    ) -> httpx.Response:
        """POST a read against each base in order. Transport errors and
        5xx fall through to the next base; anything else (incl. 404) is
        returned so the caller keeps its own status handling."""
        bases = await self._read_bases(collection)
        for i, base in enumerate(bases):
            try:
                resp = await self._client.post(base + path, json=payload)
            except Exception:
                if i < len(bases) - 1:
                    continue
                raise
            if resp.status_code >= 500 and i < len(bases) - 1:
                continue
            return resp
        raise RuntimeError("unreachable")

    def _to_outbox(
        self, op: str, collection: str, key: str, lang: str,
        content_md: str | None, meta: dict[str, list[str]] | None,
        tool: str, session_id: str | None,
    ) -> dict[str, Any] | None:
        """Durable-write fallback: park the failed write in the local retry
        outbox (backend/write_outbox.py) so a transient mddb outage doesn't
        lose it. Returns the queued sentinel (is_queued) or None when the
        outbox itself refused."""
        from backend import write_outbox
        return write_outbox.enqueue(
            mddb=self, op=op, collection=collection, key=key, lang=lang,
            content_md=content_md, meta=meta, tool=tool,
            session_id=session_id)

    async def add_document(
        self,
        collection: str,
        key: str,
        lang: str,
        content_md: str,
        meta: dict[str, list[str]] | None = None,
        timeout: float | None = None,
        durable: bool = False,
        tool: str = "",
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "collection": collection,
            "key": key,
            "lang": lang,
            "contentMd": content_md,
        }
        if meta:
            payload["meta"] = meta
        try:
            # /add embeds inline — slow embedding providers need >20s.
            kwargs = {"timeout": timeout} if timeout else {}
            resp = await self._client.post(
                self._url(collection) + "/add", json=payload, **kwargs
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("mddb add_document failed: %s", exc)
            if durable:
                return self._to_outbox("add", collection, key, lang,
                                       content_md, meta, tool, session_id)
            return None

    async def search_documents(
        self,
        collection: str,
        query: str = "*",
        filter_meta: dict[str, list[str]] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Meta-filtered listing. NOTE: /v1/search has no text-query support —
        `query` is accepted for call-site compatibility but ignored by the
        server. Use vector_search() for real text/semantic queries."""
        payload: dict[str, Any] = {
            "collection": collection,
            "limit": limit,
        }
        if filter_meta:
            payload["filterMeta"] = filter_meta
        try:
            resp = await self._read_post(collection, "/search", payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("mddb search failed: %s", exc)
            return []

    async def vector_search(
        self,
        collection: str,
        query: str,
        limit: int = 5,
        filter_meta: dict[str, list[str]] | None = None,
        threshold: float = 0.0,
    ) -> list[dict[str, Any]] | None:
        """Semantic search via /v1/vector-search. Returns None on failure so
        callers can fall back to search_documents listing."""
        payload: dict[str, Any] = {
            "collection": collection,
            "query": query,
            "topK": limit,
            "includeContent": True,
        }
        if filter_meta:
            payload["filterMeta"] = filter_meta
        if threshold:
            payload["threshold"] = threshold
        # mddb 503s while its vector index reloads after a restart (minutes on
        # idc01) and when the embedding provider hiccups — retry twice with
        # backoff so a transient stall doesn't lose the memory hit.
        delays = (3.0, 8.0)
        for attempt in range(len(delays) + 1):
            try:
                resp = await self._read_post(
                    collection, "/vector-search", payload
                )
                if resp.status_code >= 500 and attempt < len(delays):
                    await asyncio.sleep(delays[attempt])
                    continue
                resp.raise_for_status()
                results = resp.json().get("results") or []
                return [
                    {**(item.get("document") or {}), "score": item.get("score")}
                    for item in results
                ]
            except (httpx.TransportError, asyncio.TimeoutError) as exc:
                if attempt < len(delays):
                    await asyncio.sleep(delays[attempt])
                    continue
                logger.error("mddb vector_search failed: %s", exc)
                return None
            except Exception as exc:
                logger.error("mddb vector_search failed: %s", exc)
                return None
        return None

    async def get_document(
        self, collection: str, key: str, lang: str = "en",
        prefer_leader: bool = False,
    ) -> dict[str, Any] | None:
        try:
            if prefer_leader:
                resp = await self._client.post(
                    self._url(collection) + "/get",
                    json={"collection": collection, "key": key, "lang": lang},
                )
            else:
                resp = await self._read_post(
                    collection, "/get",
                    {"collection": collection, "key": key, "lang": lang},
                )
            if resp.status_code == 404 or (
                resp.status_code == 400 and "not found" in resp.text.lower()
            ):
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("mddb get_document failed: %s", exc)
            return None

    async def update_document(
        self,
        collection: str,
        key: str,
        lang: str = "en",
        content_md: str | None = None,
        meta: dict[str, list[str]] | None = None,
        durable: bool = False,
        tool: str = "",
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        # mddb has no /update — /add upserts on (collection,key,lang), and a
        # meta-only /add wipes contentMd. Merge onto the existing doc.
        # The merge read must hit the leader — a lagging follower would
        # miss a just-written body and the write would store it stale.
        existing = await self.get_document(collection, key, lang,
                                           prefer_leader=True)
        if existing is None and content_md is None:
            # Read failed or doc absent — a meta-only write here would store
            # an empty body (or wipe the real one if the read merely
            # timed out). Refuse instead. When durable, park the update so
            # the outbox re-runs this read-merge-write once mddb is back.
            logger.error("mddb update_document: no existing doc and no "
                         "content_md — refusing meta-only write for %s/%s",
                         collection, key)
            if durable:
                return self._to_outbox("update", collection, key, lang,
                                       content_md, meta, tool, session_id)
            return None
        merged_meta = dict((existing or {}).get("meta") or {})
        if meta:
            merged_meta.update(meta)
        payload: dict[str, Any] = {"collection": collection, "key": key, "lang": lang,
                                   "meta": merged_meta}
        payload["contentMd"] = (content_md if content_md is not None
                                else (existing or {}).get("contentMd") or "")
        try:
            resp = await self._client.post(self._url(collection) + "/add", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            # httpx TimeoutException stringifies to "" — log the type so the
            # journal shows *what* failed, not an empty message.
            logger.error("mddb update_document failed for %s/%s: %s: %s",
                         collection, key, type(exc).__name__, exc)
            if durable:
                # Queue the ORIGINAL update args — the outbox replays
                # update_document so the retry re-merges onto the freshest
                # doc instead of pinning this attempt's snapshot.
                return self._to_outbox("update", collection, key, lang,
                                       content_md, meta, tool, session_id)
            return None

    async def delete_document(
        self, collection: str, key: str, lang: str = "en",
        durable: bool = False,
        tool: str = "",
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            resp = await self._client.post(
                self._url(collection) + "/delete",
                json={"collection": collection, "key": key, "lang": lang},
            )
            if resp.status_code == 404 or (
                resp.status_code == 400 and "not found" in resp.text.lower()
            ):
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("mddb delete_document failed: %s", exc)
            if durable:
                return self._to_outbox("delete", collection, key, lang,
                                       None, None, tool, session_id)
            return None
