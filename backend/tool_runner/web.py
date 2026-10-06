"""web_search — Gemini grounding with DDG fallback.

Split out of backend/tool_runner.py (card tool-runner-split, 2026-10-06).
The wildcard import reproduces the original module's global namespace —
helpers, constants, contextvars and backend module handles — so method
bodies moved verbatim and patch("backend.tool_runner.<mod>") targets keep
working (the mixin shares the same imported module objects).
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403


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
                             on quota/error.
        """
        want = (provider or "auto").lower()
        if want not in ("auto", "gemini", "duckduckgo"):
            raise ValueError(f"unknown web_search provider {provider!r} "
                             "(auto|gemini|duckduckgo)")
        if want in ("auto", "gemini"):
            try:
                out = await self._web_search_gemini(query)
            except Exception as e:
                if want == "gemini":
                    raise
                # auto: quota/error → free fallback
                try:
                    out = await self._web_search_ddg(query)
                except Exception:
                    raise e
        else:
            out = await self._web_search_ddg(query)
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
