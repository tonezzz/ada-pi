"""web_search — Gemini grounding with DDG fallback.

Split out of backend/tool_runner.py (card tool-runner-split, 2026-10-06).
The wildcard import reproduces the original module's global namespace —
helpers, constants, contextvars and backend module handles — so method
bodies moved verbatim and patch("backend.tool_runner.<mod>") targets keep
working (the mixin shares the same imported module objects).
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403


# 429/quota-shaped error text — the genai client raises APIError(code=429,
# status='RESOURCE_EXHAUSTED') but older transports surface bare message
# strings, so match both the structured fields and the message.
_QUOTA_SHAPED_RE = re.compile(
    r"\b429\b|resource_exhausted|rate.?limit|too many requests|quota",
    re.I)

# Quota-burn ops events are throttled runner-side — a 429 storm must not
# spam the ada-ha-events digest; one event per window shows the burn.
_QUOTA_EVENT_MIN_S = float(os.environ.get("ADA_QUOTA_EVENT_MIN_S", "300"))


def _is_quota_error(exc: BaseException) -> bool:
    """True when the exception looks like a 429/quota exhaustion, not an
    ordinary failure (timeout, empty answer, bad request)."""
    for attr in ("code", "status_code", "status"):
        v = getattr(exc, attr, None)
        if v == 429 or str(v).upper() == "RESOURCE_EXHAUSTED":
            return True
    return bool(_QUOTA_SHAPED_RE.search(str(exc)))


class WebMixin:

    async def web_search(self, query: str, provider: str | None = None) -> dict[str, Any]:
        """Web search with provider selection.

        provider='gemini'  — grounded answer via Gemini google_search
                             (billed/quota-limited; the live audio model
                             cannot ground itself).
        provider='duckduckgo' — free HTML endpoint; returns top results,
                             no quota. Use when grounding is exhausted
                             or the user asks for DuckDuckGo.
        provider='auto' (default) — gemini, falling back to duckduckgo
                             on quota/error. A quota-shaped (429) gemini
                             failure also posts a web_search_quota ops
                             event so the burn shows in the digest.

        Every result carries provider_used + quota_exhausted; a fallback
        answer additionally carries degraded + fallback_error so Ada can
        tell the user live search is degraded instead of silently
        passing stale or shallower hits off as grounded.
        """
        want = (provider or "auto").lower()
        if want not in ("auto", "gemini", "duckduckgo"):
            raise ValueError(f"unknown web_search provider {provider!r} "
                             "(auto|gemini|duckduckgo)")
        quota_hit = False
        if want in ("auto", "gemini"):
            try:
                out = await self._web_search_gemini(query)
            except Exception as e:
                quota_hit = _is_quota_error(e)
                if quota_hit:
                    self._emit_quota_ops_event(e)
                if want == "gemini":
                    raise
                # auto: quota/error → free fallback, retried once and
                # annotated so the degraded answer is never passed off
                # as the grounded one.
                try:
                    out = await self._web_search_ddg(query)
                except Exception:
                    raise e
                out["degraded"] = (
                    "live search degraded — grounded gemini search "
                    f"failed ({'quota exhausted' if quota_hit else type(e).__name__}); "
                    "this answer is the free duckduckgo fallback — "
                    "briefly tell the user live search is degraded.")
                out["fallback_error"] = f"{type(e).__name__}: {e}"[:160]
        else:
            out = await self._web_search_ddg(query)
        out["provider_used"] = out.get("provider") or want
        out["quota_exhausted"] = quota_hit
        # Save-back standard — every outside-source answer carries the
        # reminder so the finding lands back in CMS and repeat questions
        # never need a re-search (card ada-cms-first-answers).
        out["note"] = (
            "outside-source answer — save-back standard: if this updates a "
            "tracked topic or may be asked again, write it to CMS this turn "
            "(cms_note_update on the matching page, or offer "
            "cms_publish_page for a new one)."
        )
        return out

    def _emit_quota_ops_event(self, exc: BaseException) -> None:
        """Fire-and-forget ops event to ada-ha-events-<instance> — the
        hourly chaba report feed surfaces these, so grounded-search quota
        burn lands in the digest before it reaches zero. Runner-side
        (same write_outbox pattern); throttled per-process so a 429 storm
        cannot flood the index."""
        if self.mddb is None:
            return
        try:
            instance = self._instance_id or ada_instance_id()
        except Exception:
            return
        now_mono = time.monotonic()
        if now_mono - self._web_quota_event_at < _QUOTA_EVENT_MIN_S:
            return
        self._web_quota_event_at = now_mono
        collection = f"ada-ha-events-{instance}"
        session = str(self.session_id or "runner")
        mddb = self.mddb

        async def _post() -> None:
            try:
                now = datetime.now().astimezone()
                await mddb.add_document(
                    collection=collection,
                    key=(f"ops-{session}-web_search_quota-"
                         f"{now:%Y%m%d%H%M%S%f}"),
                    lang="en",
                    content_md=(
                        "web_search: grounded gemini search hit a "
                        "quota/rate-limit error — free duckduckgo fallback "
                        f"engaged: {type(exc).__name__}: {exc}"[:200]),
                    meta={
                        "kind": ["ops-event"],
                        "type": ["web_search_quota"],
                        "instance": [instance],
                        "tool": ["web_search"],
                        "session_id": [session],
                        "ts": [now.isoformat(timespec="seconds")],
                    },
                    timeout=30,
                    tool="web_search",
                    session_id=session,
                )
            except Exception:
                logger.debug("web_search quota ops event emit failed",
                             exc_info=True)

        try:
            asyncio.get_running_loop().create_task(_post())
        except RuntimeError:
            return  # no loop (unit tests, shutdown) — nothing to schedule

    async def _web_search_gemini(self, query: str) -> dict[str, Any]:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("web search unavailable: GEMINI_API_KEY not set")
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        model = os.environ.get("ADA_WEB_SEARCH_MODEL", "gemini-2.5-flash")
        resp = await client.aio.models.generate_content(
            model=model,
            contents=str(query),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]),
        )
        text = (resp.text or "").strip()
        if not text:
            raise RuntimeError("web search returned no answer")
        sources = []
        try:
            gm = resp.candidates[0].grounding_metadata
            for ch in (gm.grounding_chunks or [])[:5]:
                w = getattr(ch, "web", None)
                if w is not None:
                    sources.append({
                        "title": getattr(w, "title", "") or "",
                        "uri": getattr(w, "uri", "") or "",
                    })
        except Exception:
            pass
        return {"answer": text, "sources": sources,
                "provider": "gemini", "model": model}

    async def _web_search_ddg(self, query: str) -> dict[str, Any]:
        """DuckDuckGo lite HTML — no API key, no quota. Returns top hits
        as a synthesized answer + source list."""
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (Ada-voice-assistant)"})
            r.raise_for_status()
        # results: <a rel="nofollow" class="result__a" href="redirect">title</a>
        hits = re.findall(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text)
        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
        strip = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()
        sources = []
        for url, title in hits[:5]:
            m = re.search(r"uddg=([^&]+)", url)
            if m:
                from urllib.parse import unquote
                url = unquote(m.group(1))
            sources.append({"title": strip(title), "uri": url})
        if not sources:
            raise RuntimeError("duckduckgo returned no results")
        top = strip(snippets[0]) if snippets else sources[0]["title"]
        answer = (top + " " if top else "") + "Top result: " + sources[0]["title"]
        return {"answer": answer.strip(), "sources": sources,
                "provider": "duckduckgo"}
