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
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))

    def is_ops_routed(self, collection: str) -> bool:
        """True when this client routes the collection to the ops store
        (no embedding provider there — vector_search will always fail)."""
        return bool(self._ops_url) and is_ops_collection(collection)

    def _url(self, collection: str) -> str:
        if self._ops_url and is_ops_collection(collection):
            return self._ops_url
        return self.base_url

    async def add_document(
        self,
        collection: str,
        key: str,
        lang: str,
        content_md: str,
        meta: dict[str, list[str]] | None = None,
        timeout: float | None = None,
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
            resp = await self._client.post(self._url(collection) + "/search", json=payload)
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
                resp = await self._client.post(
                    self._url(collection) + "/vector-search", json=payload
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
        self, collection: str, key: str, lang: str = "en"
    ) -> dict[str, Any] | None:
        try:
            resp = await self._client.post(
                self._url(collection) + "/get",
                json={"collection": collection, "key": key, "lang": lang},
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
    ) -> dict[str, Any] | None:
        # mddb has no /update — /add upserts on (collection,key,lang), and a
        # meta-only /add wipes contentMd. Merge onto the existing doc.
        existing = await self.get_document(collection, key, lang)
        if existing is None and content_md is None:
            # Read failed or doc absent — a meta-only write here would store
            # an empty body (or wipe the real one if the read merely
            # timed out). Refuse instead.
            logger.error("mddb update_document: no existing doc and no "
                         "content_md — refusing meta-only write for %s/%s",
                         collection, key)
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
            return None

    async def delete_document(
        self, collection: str, key: str, lang: str = "en"
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
            return None
