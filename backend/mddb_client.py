"""Thin async client for the MDDB v1 HTTP API (shared by tool_runner and
conversation_memory — single place for the wire contract)."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("tools")

MDDB_BASE_URL = os.environ.get("MDDB_BASE_URL", "http://127.0.0.1:11023/v1")


class MddbClient:
    """Thin async client for the MDDB v1 HTTP API."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or MDDB_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))

    async def add_document(
        self,
        collection: str,
        key: str,
        lang: str,
        content_md: str,
        meta: dict[str, list[str]] | None = None,
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
            resp = await self._client.post(f"{self.base_url}/add", json=payload)
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
            resp = await self._client.post(f"{self.base_url}/search", json=payload)
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
        try:
            resp = await self._client.post(
                f"{self.base_url}/vector-search", json=payload
            )
            resp.raise_for_status()
            results = resp.json().get("results") or []
            return [
                {**(item.get("document") or {}), "score": item.get("score")}
                for item in results
            ]
        except Exception as exc:
            logger.error("mddb vector_search failed: %s", exc)
            return None

    async def get_document(
        self, collection: str, key: str, lang: str = "en"
    ) -> dict[str, Any] | None:
        try:
            resp = await self._client.post(
                f"{self.base_url}/get",
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
        payload: dict[str, Any] = {"collection": collection, "key": key, "lang": lang}
        if content_md is not None:
            payload["contentMd"] = content_md
        if meta is not None:
            payload["meta"] = meta
        try:
            resp = await self._client.patch(f"{self.base_url}/update", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("mddb update_document failed: %s", exc)
            return None
