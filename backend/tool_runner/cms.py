"""Miniapp/CMS page tools — one MDDB document per page.

Split out of backend/tool_runner.py (card tool-runner-split, 2026-10-06).
The wildcard import reproduces the original module's global namespace —
helpers, constants, contextvars and backend module handles — so method
bodies moved verbatim and patch("backend.tool_runner.<mod>") targets keep
working (the mixin shares the same imported module objects).
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403


class CmsMixin:

    # -- Miniapp/CMS page tools: one MDDB document per page in CMS_COLLECTION --
    # Meta carries the page contract the miniapp shell renders: slug, title,
    # format (markdown/html/yaml/slides), and the last-updated timestamp.

    @staticmethod
    def _cms_slug(slug: str) -> str:
        s = (slug or "").strip().lower().replace(" ", "-")
        if not _SLUG_RE.match(s):
            raise ValueError(
                f"invalid page slug {slug!r}: use 1-64 chars of a-z, 0-9, '-' or '_'"
            )
        return s

    @staticmethod
    def _cms_page_summary(doc: dict[str, Any]) -> dict[str, Any]:
        meta = doc.get("meta") or {}

        def first(name: str) -> str | None:
            v = meta.get(name)
            if isinstance(v, list) and v:
                return str(v[0])
            return str(v) if isinstance(v, str) else None

        page = {
            "slug": first("slug") or doc.get("key"),
            "title": first("title") or doc.get("key"),
            "format": first("format") or "markdown",
            "updated": first("updated"),
        }
        # Provenance passthrough — generated pages carry these; the CMS
        # viewer uses generated_by to offer a Regenerate control. Report
        # fields too: Ada needs summary/updated/freshness BEFORE deciding
        # to drill deeper (the report-first ritual). kind lets a 'list'
        # row be told apart (page vs report) now that reports list too.
        for name in ("kind", "generated_by", "report_role", "parent",
                     "summary", "domain", "fresh_for", "confidence",
                     "supersedes", "timeline"):
            v = first(name)
            if v:
                page[name] = v
        links = meta.get("links")
        if isinstance(links, list) and links:
            page["links"] = [str(x) for x in links]
        children = meta.get("children")
        if isinstance(children, list) and children:
            page["children"] = [str(c) for c in children]
        return page

    # -- cms_read: the merged read side (tools-merge-cms) -------------------
    # Absorbs cms_list_pages/cms_get_page/cms_verify_page — the absorbed
    # methods below keep their names as the per-action implementations;
    # only the declared surface changed (aliases route old call sites).

    async def cms_read(
        self, action: str, key: str = "", slug: str = "",
        lang: str = "en", limit: int = 50,
    ) -> Any:
        """Read the miniapp CMS. action='list' returns every page and
        report's slug/kind/title/format/updated, most recently updated
        first; 'get' returns one page's full content
        by key (the page slug) + lang; 'verify' re-reads a page and checks
        its content parses for the declared format. Free reads — no
        confirmation."""
        action = (action or "").strip().lower()
        if action not in ("get", "list", "verify"):
            raise ValueError(
                f"invalid action {action!r}: expected get|list|verify")
        if action == "list":
            return await self.cms_list_pages(limit=limit)
        k = self._cms_slug(key or slug)
        if action == "get":
            return await self.cms_get_page(k, lang=lang)
        return await self.cms_verify_page(k)

    async def cms_list_pages(self, limit: int = 50) -> list[dict[str, Any]]:
        """List pages AND reports in the miniapp CMS collection, grouped
        by slug with the language variants present (lang is a per-doc
        field in MDDB), most recently updated first. kind=report docs
        were invisible here until 2026-10-08 (ada-video-query-routing)
        which left cached-videos-report undiscoverable."""
        # Over-fetch: /search has no ordering, so fetching exactly `limit`
        # raw docs returns an arbitrary subset — the cam-page flood (~300
        # docs) would starve every other slug out of the grouped result.
        fetch = max(limit * 8, 400)
        docs = await self.mddb.search_documents(
            CMS_COLLECTION, filter_meta={"kind": ["page", "report"]},
            limit=fetch,
        )
        # Lifecycle gate (2026-10-07): generated pages keep kind=page after
        # supersede — e.g. cam-wall-cms marks renamed cams status:superseded
        # until the archive pass flips kind. Listing without this check
        # shows dead twins of every live page (36 ghost rows observed).
        drop = {"superseded", "archived", "retracted", "expired"}

        def _status(d: dict[str, Any]) -> str:
            s = (d.get("meta") or {}).get("status") or ""
            return s[0] if isinstance(s, list) and s else str(s)

        docs = [d for d in docs if _status(d) not in drop]
        by_slug: dict[str, dict[str, Any]] = {}
        for d in docs:
            page = self._cms_page_summary(d)
            slug = page["slug"]
            dlang = d.get("lang") or "en"
            if slug in by_slug:
                by_slug[slug]["langs"].add(dlang)
                by_slug[slug].setdefault("titles", {})[dlang] = page["title"]
                continue
            page["langs"] = {dlang}
            page["titles"] = {dlang: page["title"]}
            by_slug[slug] = page
        for p in by_slug.values():
            p["langs"] = sorted(p["langs"])
        pages = sorted(by_slug.values(),
                       key=lambda p: p.get("updated") or "",
                       reverse=True)
        return pages[:limit]

    async def cms_get_page(
        self, slug: str, lang: str = "en"
    ) -> dict[str, Any]:
        """Fetch one page's content by slug + language; falls back to 'en'
        when the requested variant doesn't exist."""
        key = self._cms_slug(slug)
        lang = (lang or "en").strip().lower()
        doc = await self.mddb.get_document(CMS_COLLECTION, key, lang)
        fallback = False
        if not doc and lang != "en":
            doc = await self.mddb.get_document(CMS_COLLECTION, key, "en")
            fallback = bool(doc)
        if not doc:
            # ok:False, not a silent None — Ada opined on a page she never
            # got because a miss read as ok:True/output:None (2026-10-08,
            # report_opinion_loop smoke fail).
            return {"ok": False,
                    "error": f"no page {key!r} in {CMS_COLLECTION}",
                    "note": "try cms_read action='list' to find the slug "
                            "before reading"}
        page = self._cms_page_summary(doc)
        page["content"] = doc.get("contentMd") or doc.get("content") or ""
        page["lang"] = doc.get("lang") or "en"
        if fallback:
            page["fallback"] = True
        return page

    async def cms_publish_page(
        self,
        slug: str,
        title: str,
        content: str,
        format: str = "markdown",
        lang: str = "en",
        summary: str = "",
        domain: str = "",
        fresh_for: str = "",
        links: str = "",
        supersedes: str = "",
        confidence: str = "",
    ) -> dict[str, Any]:
        """Create or update a miniapp page. Upserts by (slug, lang) — 'en'
        and 'th' variants of the same slug coexist; the viewer toggles.
        summary/domain/fresh_for feed reports-index: a one-line brief Ada
        can answer from without cms_get_page, the grouping domain, and a
        staleness hint ('1h', '6h', '1d') the index flags when exceeded."""
        slug = self._cms_slug(slug)
        fmt = (format or "markdown").strip().lower()
        lang = (lang or "en").strip().lower()
        # The model tends to bake the language into the slug
        # (gold-report-th + Thai content stored as 'en') — split it.
        if lang == "en" and slug.endswith("-th"):
            slug = slug[:-3]
            lang = "th"
        if lang not in ("en", "th"):
            raise ValueError(
                f"invalid lang {lang!r}: expected 'en' or 'th'")
        if fmt not in CMS_FORMATS:
            raise ValueError(
                f"invalid format {format!r}: expected one of {sorted(CMS_FORMATS)}"
            )
        if not (title or "").strip():
            raise ValueError("title is required")
        now = datetime.now(timezone.utc)
        updated = now.isoformat(timespec="seconds")
        # Merge the existing doc's meta instead of replacing it — generated
        # pages carry provenance (generated_by/sources/parent/children) and
        # memory-schema fields a wholesale replace would silently strip.
        try:
            existing = await self.mddb.get_document(CMS_COLLECTION, slug, lang)
        except Exception:
            existing = None
        if not isinstance(existing, dict):
            existing = None
        meta = {
            k: (v if isinstance(v, list) else [v])
            for k, v in ((existing or {}).get("meta") or {}).items()
        }
        meta.setdefault("bank", ["cms"])
        meta.setdefault("scope", ["tony"])
        meta.setdefault("status", ["active"])
        meta.setdefault("source", ["api"])
        meta["written_by"] = ["cms_publish_page"]
        meta.setdefault("subject", [slug])
        meta.setdefault("attribute", ["page"])
        meta.setdefault("valid_from", [now.date().isoformat()])
        meta["last_verified"] = [now.date().isoformat()]
        meta.update({
            "kind": ["page"],
            "slug": [slug],
            "title": [title.strip()],
            "format": [fmt],
            "lang": [lang],
            "updated": [updated],
            "instance": [self._instance_id or "ada"],
        })
        # Report-structure fields — summary is the one-line brief the
        # reports-index shows so Ada answers without re-reading the page;
        # fresh_for is a staleness hint the index renders as stale/FRESH.
        if summary.strip():
            meta["summary"] = [summary.strip()[:240]]
        if domain.strip():
            meta["domain"] = [domain.strip().lower()]
        if fresh_for.strip():
            meta["fresh_for"] = [fresh_for.strip()]
        # Linking — comma-separated slugs this report derives from or
        # supersedes. links = parent/child/source references; supersedes
        # marks the older report this one replaces (staleness signal).
        if isinstance(links, str) and links.strip():
            meta["links"] = [x.strip() for x in links.split(",") if x.strip()]
        elif isinstance(links, list) and links:
            meta["links"] = [str(x).strip() for x in links if str(x).strip()]
        if supersedes.strip():
            meta["supersedes"] = [supersedes.strip()]
        if confidence.strip():
            meta["confidence"] = [confidence.strip()]
        # Timeline — append-only audit of what changed and when. Cap at 40
        # entries; visible via cms_get_page meta and the report pages.
        tl = [x for x in meta.get("timeline", []) if isinstance(x, str)]
        tl.append(f"{updated[:16]} published: {title.strip()[:80]}")
        meta["timeline"] = tl[-40:]
        # Post-merge contract check — the gate already rejects missing
        # model args, so gaps here mean an internal caller republished a
        # legacy page; warn rather than break the merge path.
        meta_check = validate_report_meta(meta)
        if not meta_check["ok"] or meta_check["warnings"]:
            logger.warning(
                "cms_publish_page %s meta_contract gaps: %s", slug, meta_check)
        result = await self.mddb.add_document(
            CMS_COLLECTION,
            slug,
            lang,
            content,
            meta=meta,
            durable=True, tool="cms_publish_page",
            session_id=self.session_id,
        )
        if write_outbox.is_queued(result):
            self._log_session_event(
                "write_queued", tool="cms_publish_page", key=slug)
            return {
                "status": "queued",
                "slug": slug,
                "note": ("mddb is unreachable — the page is parked in the "
                         "local write outbox and will publish automatically "
                         "within the retry window; the user will hear about "
                         "it if it ultimately fails. Do NOT describe it as "
                         "published yet."),
            }
        if result is None:
            return {
                "status": "error",
                "error": "mddb write failed",
                "slug": slug,
                "note": ("This is a storage failure, NOT a confirmation "
                         "problem — do not ask the user to re-confirm. "
                         "Report the failure plainly and suggest checking "
                         "the mddb service."),
            }
        # Give the model the REAL URLs — she has invented /cms/<slug> paths
        # on tony-dell before (404). view_url is the CMS viewer; cast_url adds
        # the api key so a vcast display can render it without a stored key.
        base = os.environ.get(
            "ADA_CMS_BASE", "https://idc01.taila0626a.ts.net/cms/")
        view_url = f"{base}#/{slug}"
        key = os.environ.get("ADA_API_KEY", "")
        cast_url = f"{base}?api_key={key}#/{slug}" if key else view_url
        # Refresh the reports index in the background — cheap page Ada reads
        # instead of re-querying per report.
        asyncio.create_task(self._cms_reports_index())
        out = {
            "status": "published",
            "slug": slug,
            "title": title.strip(),
            "format": fmt,
            "lang": lang,
            "updated": updated,
            "view_url": view_url,
            "cast_url": cast_url,
            "note": "To show this page on a display: cast_to_screen("
                    "action='nav', url=<cast_url>) — only after this result "
                    "shows status=published.",
        }
        if not meta_check["ok"] or meta_check["warnings"]:
            out["meta_contract"] = meta_check
        return out

    async def cms_delete_page(self, slug: str) -> dict[str, Any]:
        """Delete a miniapp page by slug."""
        slug = self._cms_slug(slug)
        result = await self.mddb.delete_document(
            CMS_COLLECTION, slug,
            durable=True, tool="cms_delete_page",
            session_id=self.session_id)
        if write_outbox.is_queued(result):
            self._log_session_event(
                "write_queued", tool="cms_delete_page", key=slug)
            return {"status": "queued", "slug": slug,
                    "note": ("mddb is unreachable — the delete is parked in "
                             "the local write outbox and will land within "
                             "the retry window.")}
        if result is None:
            return {"status": "not_found", "slug": slug}
        asyncio.create_task(self._cms_reports_index())
        return {"status": "deleted", "slug": slug}

    async def cms_note_update(
        self, slug: str, note: str, lang: str = "en", summary: str = ""
    ) -> dict[str, Any]:
        """Append a timeline note to an existing page — the lightweight
        'this report learned something new' path. Unlike cms_publish_page it
        merges content (adds a Timeline section entry) and never replaces,
        so it needs no confirmation. Optionally refreshes the page's
        one-line summary shown in reports-index."""
        slug = self._cms_slug(slug)
        lang = (lang or "en").strip().lower()
        if not (note or "").strip():
            raise ValueError("note is required")
        doc = await self.mddb.get_document(CMS_COLLECTION, slug, lang)
        if doc is None and lang != "en":
            doc = await self.mddb.get_document(CMS_COLLECTION, slug, "en")
        if not isinstance(doc, dict):
            return {"status": "not_found", "slug": slug,
                    "error": "no such page — use cms_publish_page to create it"}
        meta = {
            k: (v if isinstance(v, list) else [v])
            for k, v in (doc.get("meta") or {}).items()
        }
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = f"{now[:16]} {note.strip()[:200]}"
        tl = [x for x in meta.get("timeline", []) if isinstance(x, str)]
        tl.append(entry)
        meta["timeline"] = tl[-40:]
        meta["updated"] = [now]
        meta["written_by"] = ["cms_note_update"]
        if summary.strip():
            meta["summary"] = [summary.strip()[:240]]
        # Contract check is warn-only here — note is a merge-only append;
        # gaps mean the underlying page predates the contract.
        meta_check = validate_report_meta(meta)
        if not meta_check["ok"] or meta_check["warnings"]:
            logger.warning(
                "cms_note_update %s meta_contract gaps: %s", slug, meta_check)
        content = doc.get("contentMd") or ""
        marker = "## Timeline"
        line = f"- {entry}"
        if marker in content:
            content = content.rstrip() + "\n" + line + "\n"
        else:
            content = content.rstrip() + f"\n\n{marker}\n\n{line}\n"
        result = await self.mddb.add_document(
            CMS_COLLECTION, slug, doc.get("lang") or lang, content, meta=meta,
            durable=True, tool="cms_note_update",
            session_id=self.session_id)
        if write_outbox.is_queued(result):
            self._log_session_event(
                "write_queued", tool="cms_note_update", key=slug)
            return {"status": "queued", "slug": slug,
                    "note": ("mddb is unreachable — the note is parked in "
                             "the local write outbox and will land within "
                             "the retry window.")}
        if result is None:
            return {"status": "error", "error": "mddb write failed", "slug": slug}
        asyncio.create_task(self._cms_reports_index())
        # Diff so Ada can report what changed: note text + new summary
        # when refreshed. Times are the time-of-event stamps already in
        # the timeline entries.
        diff = {"note": entry}
        if summary.strip():
            diff["summary"] = summary.strip()[:240]
        out = {"status": "noted", "slug": slug,
               "timeline_entries": len(meta["timeline"]),
               "updated": now, "diff": diff}
        if not meta_check["ok"] or meta_check["warnings"]:
            out["meta_contract"] = meta_check
        return out

    # -- cms_edit: the merged edit side (tools-merge-cms) -------------------
    # Absorbs cms_note_update/cms_delete_page/cms_automation — per-action
    # dispatch onto the absorbed methods below; 'op' carries the automation
    # registry action for action='automate' (list|get|set|enable|disable|run).

    async def cms_edit(
        self,
        action: str,
        slug: str = "",
        note: str = "",
        summary: str = "",
        lang: str = "en",
        op: str = "",
        enabled: bool | None = None,
        interval_min: int | None = None,
        run_now: bool | None = None,
        max_items: int | None = None,
        since_hours: int | None = None,
        require: str | None = None,
        feeds: list | None = None,
        langs: list | None = None,
        parent: str | None = None,
        children: list | None = None,
    ) -> dict[str, Any]:
        """Edit-side CMS ops. action='note' appends a timeline note to an
        existing page (ungated); 'delete' removes the page (confirmed);
        'automate' drives the page's automation registry — op carries the
        registry action (list/get are free reads; set/enable/disable/run
        are confirmed writes)."""
        action = (action or "").strip().lower()
        if action == "note":
            return await self.cms_note_update(
                slug, note, lang=lang, summary=summary)
        if action == "delete":
            return await self.cms_delete_page(slug)
        if action == "automate":
            return await self.cms_automation(
                op, slug=slug, enabled=enabled, interval_min=interval_min,
                run_now=run_now, max_items=max_items,
                since_hours=since_hours, require=require, feeds=feeds,
                langs=langs, parent=parent, children=children)
        raise ValueError(
            f"invalid action {action!r}: expected note|delete|automate")

    # -- _cms_edit_sections: page/section ops (PWA edit drawer; not a
    #    declared tool — the model-facing cms_edit above carries the
    #    note/delete/automate actions) -------------------------------------
    # Ops: add (append/insert text), update (replace section or whole page),
    # improve/expand/consolidate (LLM transforms), split (section -> new
    # page), merge (other page's content folded in). Sections are addressed
    # by their '## heading' text — stable across edits, unlike line numbers.
    CMS_EDIT_OPS = ("add", "update", "improve", "expand", "consolidate",
                    "split", "merge")
    _CMS_LLM_OPS = ("improve", "expand", "consolidate")

    @staticmethod
    def _cms_sections(content: str) -> list[tuple[str, int, int]]:
        """[(heading, start, end)] — sections = '## ' blocks; 'preamble' is
        text before the first heading."""
        lines = content.split("\n")
        marks = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
        out = []
        if not marks:
            return [("preamble", 0, len(lines))]
        if marks[0] > 0:
            out.append(("preamble", 0, marks[0]))
        for j, m in enumerate(marks):
            end = marks[j + 1] if j + 1 < len(marks) else len(lines)
            out.append((lines[m][3:].strip(), m, end))
        return out

    async def _cms_llm(self, prompt: str) -> str:
        from google import genai
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        resp = await client.aio.models.generate_content(
            model=os.environ.get("ADA_CMS_EDIT_MODEL",
                                 "gemini-3.5-flash-lite"),
            contents=prompt)
        return (resp.text or "").strip()

    async def _cms_edit_sections(
        self, slug: str, op: str, text: str = "", section: str = "",
        instruction: str = "", lang: str = "en", target_slug: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        """Structured page edit. section='' means the whole page. LLM ops
        take 'instruction' (what to change); mechanical ops take 'text'
        (new content) or 'target_slug' (split/merge)."""
        slug = self._cms_slug(slug)
        op = (op or "").strip().lower()
        if op not in self.CMS_EDIT_OPS:
            raise ValueError(
                f"invalid op {op!r}: expected one of {self.CMS_EDIT_OPS}")
        doc = await self.mddb.get_document(CMS_COLLECTION, slug, lang)
        if not isinstance(doc, dict):
            return {"status": "not_found", "slug": slug}
        content = doc.get("contentMd") or ""
        sections = self._cms_sections(content)
        lines = content.split("\n")

        target = None
        if section.strip():
            want = section.strip().lower()
            for name, s, e in sections:
                if name.lower() == want:
                    target = (name, s, e)
                    break
            if target is None:
                return {"status": "error", "slug": slug,
                        "error": f"no section {section!r} — "
                                 f"sections: {[n for n, _, _ in sections]}"}

        new_content, created = None, None
        if op == "add":
            block = text.strip()
            if not block:
                raise ValueError("add requires text")
            if target:
                # insert before the next section starts (end of target's block)
                lines.insert(target[2], "\n" + block if block.startswith("#")
                             else block)
                new_content = "\n".join(lines)
            else:
                new_content = content.rstrip() + "\n\n" + block + "\n"
        elif op == "update":
            block = text.strip()
            if not block:
                raise ValueError("update requires text")
            if target:
                name, s, e = target
                new = lines[:s + 1] + block.split("\n") + lines[e:]
                new_content = "\n".join(new)
            else:
                new_content = block
        elif op in self._CMS_LLM_OPS:
            scope = ("\n\n".join(lines[target[1]:target[2]])
                     if target else content)
            if not scope.strip():
                return {"status": "error", "error": "nothing to transform"}
            instr = instruction.strip() or (
                {"improve": "tighten the writing, keep every fact",
                 "expand": "add depth and detail, keep every fact",
                 "consolidate": "merge overlapping/duplicate sections"}[op])
            out = await self._cms_llm(
                f"Rewrite this {'section' if target else 'page'} — {instr}.\n"
                "Return ONLY the new markdown, no commentary.\n\n" + scope)
            if not out:
                return {"status": "error", "error": "llm returned empty"}
            if target:
                name, s, e = target
                out_lines = out.split("\n")
                if out_lines and not out_lines[0].startswith("## "):
                    out_lines.insert(0, f"## {name}")
                new_content = "\n".join(lines[:s] + out_lines + lines[e:])
            else:
                new_content = out
        elif op == "split":
            if not target:
                raise ValueError("split requires a section")
            tslug = self._cms_slug(target_slug or "")
            name, s, e = target
            moved = "\n".join(lines[s:e])
            pub = await self.cms_publish_page(
                tslug, title.strip() or name, moved, lang=lang,
                links=slug)
            if pub.get("status") != "published":
                return {"status": "error", "error": "split publish failed",
                        "detail": pub}
            created = tslug
            new_content = "\n".join(
                lines[:s]
                + [f"## {name}", f"*Moved to [{name}](/#{tslug}).*"]
                + lines[e:])
        elif op == "merge":
            tslug = self._cms_slug(target_slug or "")
            other = await self.mddb.get_document(CMS_COLLECTION, tslug, lang)
            if not isinstance(other, dict):
                return {"status": "error", "error": f"no page {tslug!r}"}
            body = (other.get("contentMd") or "").strip()
            new_content = content.rstrip() + "\n\n" + body + "\n"
            # mark the absorbed page superseded
            ometa = {k: (v if isinstance(v, list) else [v])
                     for k, v in (other.get("meta") or {}).items()}
            ometa["status"] = ["superseded"]
            ometa["superseded_by"] = [slug]
            await self.mddb.add_document(
                CMS_COLLECTION, tslug, lang, body,
                meta=ometa)
        # persist via publish so meta/timeline/index all stay honest
        meta_title = title.strip() or next(
            iter((doc.get("meta") or {}).get("title") or []), slug)
        res = await self.cms_publish_page(
            slug, str(meta_title), new_content, lang=lang)
        res["op"] = op
        if target:
            res["section"] = target[0]
        if created:
            res["created"] = created
        return res

    async def _cms_reports_index(self) -> None:
        """Regenerate the reports-index page — one row per report/tagged
        page with slug, domain, one-line summary, updated, and a stale flag
        when 'updated' exceeds its fresh_for hint. Ada reads THIS to answer
        'what reports exist / what changed' without per-page cms_get_page."""
        try:
            # Fetch past the raw-doc count — /search returns an arbitrary
            # subset at the cap and the collection already exceeds 200.
            docs = await self.mddb.search_documents(
                CMS_COLLECTION, limit=600)
        except Exception as exc:
            logger.warning("reports-index list failed: %s", exc)
            return
        now = datetime.now(timezone.utc)
        def stale(meta: dict) -> bool:
            ff = (meta.get("fresh_for") or [""])[0]
            upd = (meta.get("updated") or [""])[0]
            secs = fresh_for_seconds(ff)
            if secs is None or not upd:
                return False
            try:
                dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
                return (now - dt).total_seconds() > secs
            except Exception:
                return False
        drop = {"superseded", "archived", "retracted", "expired"}
        best: dict[str, dict[str, Any]] = {}
        for d in docs:
            meta = d.get("meta") or {}
            kind = (meta.get("kind") or [""])[0]
            if kind not in ("report", "page"):
                continue
            slug = (meta.get("slug") or [d.get("key") or "?"])[0]
            if slug == "reports-index":
                continue
            status = meta.get("status") or ""
            status = status[0] if isinstance(status, list) and status else str(status)
            if status in drop:
                continue
            row = {
                "slug": slug,
                "kind": kind,
                "title": (meta.get("title") or [slug])[0],
                "domain": (meta.get("domain") or ["-"])[0],
                "summary": (meta.get("summary") or [""])[0],
                "updated": (meta.get("updated") or ["-"])[0][:16],
                "fresh": (meta.get("fresh_for") or ["-"])[0],
                "stale": stale(meta),
            }
            prev = best.get(slug)
            if prev is None or row["updated"] > prev["updated"]:
                best[slug] = row
        rows = sorted(best.values(), key=lambda r: r["updated"],
                      reverse=True)
        # Reports lead the index — it exists to enumerate them, and pure
        # recency lets the cam-page refresh flood push every report past
        # the row cap (observed 2026-10-07: zero video pages listed).
        rows = ([r for r in rows if r["kind"] == "report"] +
                [r for r in rows if r["kind"] != "report"])
        lines = [f"# Reports index — {now:%Y-%m-%d %H:%M}Z\n",
                 "Brief summaries of every report page — read the linked page",
                 "only when the summary isn't enough.\n",
                 "| slug | kind | domain | updated | fresh | summary |",
                 "|---|---|---|---|---|---|"]
        for r in rows[:60]:
            flag = " ⚠STALE" if r["stale"] else ""
            summ = (r["summary"] or r["title"])[:80]
            lines.append(f"| {r['slug']} | {r['kind']} | {r['domain']} | "
                         f"{r['updated']}{flag} | {r['fresh']} | {summ} |")
        md = "\n".join(lines)
        await self.mddb.add_document(
            CMS_COLLECTION, "reports-index", "en", md,
            meta={"kind": ["page"], "slug": ["reports-index"],
                  "title": [f"Reports index — {now:%Y-%m-%d %H:%M}Z"],
                  "format": ["markdown"], "domain": ["meta"],
                  "summary": ["Auto-generated index of report pages — "
                              "slug, domain, staleness, one-line brief."],
                  "fresh_for": ["6h"], "confidence": ["high"],
                  "updated": [now.isoformat(timespec="seconds")],
                  "timeline": [f"{now.isoformat(timespec='seconds')[:16]} "
                               "index regenerated"],
                  "instance": [self._instance_id or "ada"],
                  "written_by": ["_cms_reports_index"]})

    # -- CMS automation registry (ada-cms-automation) -----------------------
    # One doc per page slug, contentMd = JSON of the page's switches/knobs
    # and the worker's write-back state. The flood-news worker merges this
    # over its seed config, so Ada flips switches by editing the doc here.

    @staticmethod
    def _cms_automation_cfg(doc: dict[str, Any] | None) -> dict[str, Any]:
        if not doc:
            return {}
        try:
            cfg = json.loads(doc.get("contentMd") or "{}")
        except (TypeError, ValueError):
            return {}
        return cfg if isinstance(cfg, dict) else {}

    async def _cms_automation_doc(self, slug: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        doc = await self.mddb.get_document(CMS_AUTOMATION_COLLECTION, slug, "en")
        return doc, self._cms_automation_cfg(doc)

    async def _cms_automation_save(self, slug: str, cfg: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        await self.mddb.add_document(
            CMS_AUTOMATION_COLLECTION,
            slug,
            "en",
            json.dumps(cfg, ensure_ascii=False, indent=2),
            meta={
                "kind": ["automation-config"], "bank": ["cms"],
                "scope": ["tony"], "status": ["active"], "source": ["api"],
                "written_by": ["ada:cms_automation"], "subject": [slug],
                "attribute": ["automation"], "slug": [slug],
                "title": [f"CMS automation: {slug}"], "format": ["json"],
                "lang": ["en"], "updated": [now.isoformat(timespec="seconds")],
                "last_verified": [now.date().isoformat()],
            },
        )

    @staticmethod
    def _cms_automation_state(cfg: dict[str, Any], slug: str) -> dict[str, Any]:
        return {
            "slug": slug,
            "enabled": cfg.get("enabled", True),
            "interval_min": cfg.get("interval_min", 0),
            "run_now": cfg.get("run_now", False),
            "last_run": cfg.get("last_run"),
            "last_status": cfg.get("last_status"),
            "last_count": cfg.get("last_count"),
            "last_error": cfg.get("last_error"),
            "last_duration_s": cfg.get("last_duration_s"),
        }

    def _cms_automation_knobs(
        self, slug: str, *, enabled=None, interval_min=None, run_now=None,
        max_items=None, since_hours=None, require=None, feeds=None,
        langs=None, parent=None, children=None,
    ) -> dict[str, Any]:
        """Validate and collect the writable knobs. Raises ValueError on
        bad input; returns only the keys the caller actually passed."""
        out: dict[str, Any] = {}
        if enabled is not None:
            out["enabled"] = bool(enabled)
        if run_now is not None:
            out["run_now"] = bool(run_now)
        if interval_min is not None:
            try:
                v = int(interval_min)
            except (TypeError, ValueError):
                raise ValueError("interval_min must be an integer")
            if not 0 <= v <= 7 * 24 * 60:
                raise ValueError("interval_min must be 0-10080 (max 7 days)")
            out["interval_min"] = v
        if max_items is not None:
            try:
                v = int(max_items)
            except (TypeError, ValueError):
                raise ValueError("max_items must be an integer")
            if not 1 <= v <= 50:
                raise ValueError("max_items must be 1-50")
            out["max_items"] = v
        if since_hours is not None:
            try:
                v = int(since_hours)
            except (TypeError, ValueError):
                raise ValueError("since_hours must be an integer")
            if not 1 <= v <= 720:
                raise ValueError("since_hours must be 1-720 (max 30 days)")
            out["since_hours"] = v
        if require is not None:
            require = str(require).strip()
            if require:
                if len(require) > 200:
                    raise ValueError("require regex too long (max 200 chars)")
                try:
                    re.compile(require, re.IGNORECASE)
                except re.error as exc:
                    raise ValueError(f"invalid require regex: {exc}")
            out["require"] = require
        if feeds is not None:
            if not isinstance(feeds, list):
                raise ValueError(
                    "feeds must be a list of [name, url] pairs")
            norm = []
            for f in feeds:
                if (not isinstance(f, (list, tuple)) or len(f) != 2):
                    raise ValueError("each feed must be a [name, url] pair")
                fname, url = str(f[0]).strip().lower(), str(f[1]).strip()
                if not _SLUG_RE.match(fname):
                    raise ValueError(f"invalid feed name {fname!r}")
                if not re.match(r"https?://", url):
                    raise ValueError(f"feed url must be http(s): {url!r}")
                norm.append([fname, url])
            if not norm:
                raise ValueError("feeds list cannot be empty")
            out["feeds"] = norm
        if langs is not None:
            if not isinstance(langs, list) or not langs:
                raise ValueError("langs must be a non-empty list")
            bad = [l for l in langs if str(l) not in ("en", "th")]
            if bad:
                raise ValueError(
                    f"invalid langs {bad!r}: supported are 'en' and 'th'")
            out["langs"] = [str(l) for l in langs]
        if parent is not None:
            out["parent"] = self._cms_slug(parent) if str(parent).strip() else None
        if children is not None:
            if not isinstance(children, list):
                raise ValueError("children must be a list of page slugs")
            out["children"] = [self._cms_slug(str(c)) for c in children]
        return out

    async def cms_automation(
        self,
        action: str,
        slug: str = "",
        enabled: bool | None = None,
        interval_min: int | None = None,
        run_now: bool | None = None,
        max_items: int | None = None,
        since_hours: int | None = None,
        require: str | None = None,
        feeds: list | None = None,
        langs: list | None = None,
        parent: str | None = None,
        children: list | None = None,
    ) -> dict[str, Any]:
        """Inspect and adjust a miniapp page's automation switches and knobs.

        Each generated page has a registry doc in ada-cms-automation that the
        scheduled news worker honors: 'enabled' pauses updates, 'interval_min'
        throttles them, 'run_now' queues a one-shot regeneration, 'feeds' and
        'require' control what is fetched, 'parent'/'children' link reports
        into a hierarchy. Actions: list (all pages), get (one page),
        set/enable/disable/run (writes — confirmation-gated).
        """
        action = (action or "").strip().lower()
        if action == "list":
            docs = await self.mddb.search_documents(
                CMS_AUTOMATION_COLLECTION, limit=200)
            pages = []
            for d in docs or []:
                slug = d.get("key") or ""
                pages.append(
                    self._cms_automation_state(
                        self._cms_automation_cfg(d), slug))
            pages.sort(key=lambda p: p["slug"])
            return {"pages": pages, "count": len(pages)}

        key = self._cms_slug(slug)
        doc, cfg = await self._cms_automation_doc(key)

        if action == "get":
            if not doc:
                return {"status": "not_found", "slug": key,
                        "note": "no automation config yet — the worker "
                                "creates one on its first run"}
            return {"status": "ok", "config": cfg,
                    **self._cms_automation_state(cfg, key)}

        # Write actions: merge validated knobs into the stored config.
        knobs = self._cms_automation_knobs(
            key, enabled=enabled, interval_min=interval_min,
            run_now=run_now, max_items=max_items, since_hours=since_hours,
            require=require, feeds=feeds, langs=langs, parent=parent,
            children=children)
        if action == "enable":
            knobs["enabled"] = True
        elif action == "disable":
            knobs["enabled"] = False
        elif action == "run":
            knobs["run_now"] = True
        elif action != "set":
            raise ValueError(
                f"invalid action {action!r}: expected list/get/set/"
                "enable/disable/run")
        if not knobs:
            raise ValueError(f"action {action!r}: nothing to change")
        cfg.update(knobs)
        await self._cms_automation_save(key, cfg)
        out = {"status": "updated", "slug": key, "changes": knobs}
        if action == "run":
            out["queued"] = True
            out["note"] = ("regeneration queued — the worker picks it up on "
                           "its next pass and clears run_now when done")
        return out

    async def cms_verify_page(self, slug: str) -> dict[str, Any]:
        """Re-read a page and check its content parses for its declared format.

        The assistant can't see the rendered miniapp — this is the feedback
        loop after publish: returns a structural summary of what the viewer
        renders, or the parse error to fix.
        """
        key = self._cms_slug(slug)
        doc = await self.mddb.get_document(CMS_COLLECTION, key, "en")
        if not isinstance(doc, dict):
            return {"ok": False, "status": "not_found", "slug": slug}
        page = self._cms_page_summary(doc)
        content = doc.get("contentMd") or doc.get("content") or ""
        fmt = page["format"]
        report: dict[str, Any] = {
            "ok": True,
            "slug": page["slug"],
            "title": page["title"],
            "format": fmt,
            "chars": len(content),
            # Report meta contract (ssot.apps.ada-cms-reports.yml) — ok
            # stays about content parse; meta_contract reports the field
            # check Ada can act on (republish with the missing fields).
            "meta_contract": validate_report_meta(doc.get("meta") or {}),
        }
        if not content.strip():
            report.update(ok=False, error="page content is empty")
            return report
        if fmt == "yaml":
            try:
                import yaml
                data = yaml.safe_load(content)
            except ImportError:
                report["summary"] = {"note": "pyyaml not installed — parse check skipped"}
                return report
            except Exception as exc:
                report.update(
                    ok=False,
                    error=f"yaml parse error: {str(exc).splitlines()[0]}",
                )
                return report
            if not isinstance(data, dict):
                report.update(
                    ok=False,
                    error="yaml page must be a mapping (title/sections/items)",
                )
                return report
            sections = data.get("sections")
            report["summary"] = {
                "title": data.get("title"),
                "subtitle": data.get("subtitle"),
                "section_count": len(sections) if isinstance(sections, list) else 0,
                "sections": [
                    {
                        "label": (s or {}).get("label") or (s or {}).get("title"),
                        "items": len((s or {}).get("items") or []),
                    }
                    for s in sections
                ] if isinstance(sections, list) else [],
            }
        elif fmt == "slides":
            slides = [
                s for s in re.split(r"^---+\s*$", content, flags=re.M) if s.strip()
            ]
            report["summary"] = {"slide_count": len(slides)}
        elif fmt == "html":
            title = re.search(r"<title[^>]*>(.*?)</title>", content, re.I | re.S)
            report["summary"] = {
                "title": title.group(1).strip() if title else None,
                "has_doctype": content.lstrip().lower().startswith("<!doctype"),
                "script_tags": len(re.findall(r"<script\b", content, re.I)),
            }
        else:  # markdown — validate fenced rich blocks too
            headings = [
                ln.strip() for ln in content.splitlines() if ln.lstrip().startswith("#")
            ]
            report["summary"] = {"headings": headings[:20]}
            blocks = re.findall(
                r"```(chart3?|mermaid|media)\s*\n(.*?)```", content, re.S
            )
            block_report = []
            for kind, body in blocks:
                entry: dict[str, Any] = {"type": kind}
                if kind == "mermaid":
                    entry["lines"] = len(body.splitlines())
                else:
                    try:
                        import yaml
                        yaml.safe_load(body)
                        entry["yaml_ok"] = True
                    except Exception as exc:
                        entry["yaml_ok"] = False
                        entry["error"] = str(exc).splitlines()[0]
                        report["ok"] = False
                block_report.append(entry)
            if block_report:
                report["summary"]["blocks"] = block_report
        return report
