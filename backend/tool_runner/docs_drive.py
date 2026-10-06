"""Document archive + Google Drive/Photos tools.

Split out of backend/tool_runner.py (card tool-runner-split, 2026-10-06).
The wildcard import reproduces the original module's global namespace —
helpers, constants, contextvars and backend module handles — so method
bodies moved verbatim and patch("backend.tool_runner.<mod>") targets keep
working (the mixin shares the same imported module objects).
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403


class DocsDriveMixin:

    def _check_doc_confirmed(
        self, name: str, args: dict[str, Any], confirmed: Any,
        confirm_token: Any = None,
    ) -> None:
        """Server-side gate for doc archive/print. Raises PermissionError on denial."""
        if name == "docs":
            # Per-action split after the tools-merge-docs-drive collapse —
            # alias resolution ran before this gate, so name is canonical:
            # search/get stay free reads; archive, print, and anything
            # unrecognized fall through to confirmation.
            if str(args.get("action") or "").lower() in self._DOC_READ_ACTIONS:
                return
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("document tools are disabled (ADA_READ_ONLY=true)")
        self._require_confirmation(
            name, args, confirmed, confirm_token,
            f"{name} requires confirmation. Restate the archive/print "
            "target, get an explicit yes, then call again with confirmed=true.",
        )

    def _check_drive_confirmed(
        self, name: str, args: dict[str, Any], confirmed: Any,
        confirm_token: Any = None,
    ) -> None:
        """Server-side gate for drive update. Raises PermissionError on denial."""
        if name == "drive":
            # Per-action split after the tools-merge-docs-drive collapse:
            # only action='update' mutated before the merge — search/get/
            # show were free (show's cast runs inside cast_to_screen/
            # tv_action, which carry their own gates).
            if str(args.get("action") or "").lower() in (
                    "search", "get", "show"):
                return
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError("drive tools are disabled (ADA_READ_ONLY=true)")
        self._require_confirmation(
            name, args, confirmed, confirm_token,
            f"{name} modifies a Drive file. Restate the file "
            "and change, get an explicit yes, then call again "
            "with confirmed=true.",
        )

    # -- Document archive tools (doc-archive service on idc01 + MDDB
    #    `documents` collection — the shared Ada/Devin document index) --

    async def docs(
        self, action: str, query: str = "", slug: str = "",
        limit: int = 5, doc_type: str = "document",
        intake_key: str | None = None, intake_keys: list[str] | None = None,
        source_dir: str | None = None, pages: str | None = None,
        true_size_mm: str | None = None,
    ) -> Any:
        """Personal document archive surface (tools-merge-docs-drive):
        action='search'|'get' read the archive index (absorbed
        ada_doc_search/ada_doc_get); 'archive'|'print' write — the
        confirm gate runs in _execute_gated before dispatch."""
        action = (action or "").strip().lower()
        if action == "search":
            return await self.ada_doc_search(query, limit=limit)
        if action == "get":
            return await self.ada_doc_get(slug)
        if action == "archive":
            return await self.ada_doc_archive(
                slug, doc_type=doc_type, intake_key=intake_key,
                intake_keys=intake_keys, source_dir=source_dir)
        if action == "print":
            return await self.ada_doc_print(
                slug, pages=pages, true_size_mm=true_size_mm)
        raise ValueError(
            f"invalid action {action!r}: expected search|get|print|archive")

    def _doc_record(self, action: str, **fields: Any) -> None:
        """Append to the session's L0 doc-work timeline (conversation
        report substrate); no-op when no conversation is attached."""
        if self.doc_log is None:
            return
        self.doc_log.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action, **fields,
        })

    async def ada_doc_search(self, query: str, limit: int = 5) -> Any:
        """Search archived document sets (documents bank / MDDB index)."""
        hits = await doc_archive_client.doc_search(query, limit=limit)
        self._doc_record("search", query=query,
                         found=[h.get("slug") for h in hits])
        return hits

    async def ada_doc_get(self, slug: str) -> dict[str, Any]:
        """Manifest + index metadata for one archived document set."""
        out = await doc_archive_client.doc_get(slug)
        self._doc_record("get", slug=slug)
        return out

    async def ada_doc_archive(
        self, slug: str, doc_type: str = "document",
        intake_key: str | None = None,
        intake_keys: list[str] | None = None,
        source_dir: str | None = None,
    ) -> dict[str, Any]:
        """Archive a document set: from held intake results (intake_key or
        intake_keys for multi-page sets) or a directory (source_dir)."""
        pages: list[tuple[str, bytes]] = []
        keys = list(intake_keys or [])
        if intake_key:
            keys.insert(0, intake_key)
        if keys:
            from backend import document_check
            engine = document_check.engine()
            for i, k in enumerate(keys, 1):
                held = engine.held(k)
                if held is None:
                    raise RuntimeError(
                        f"unknown or expired intake key '{k}' — "
                        "upload the document again")
                blob = (held.meta or {}).get("archive_jpg")
                if not blob:
                    raise RuntimeError(f"held intake {k} has no archive image")
                name = (held.meta or {}).get("filename") or f"{slug}-p{i}.jpg"
                if len(keys) > 1:
                    stem, dot, ext = name.rpartition(".")
                    name = f"{stem or name}-p{i}.{ext or 'jpg'}"
                pages.append((name, bytes(blob)))
        elif source_dir:
            d = os.path.expanduser(source_dir)
            if not os.path.isdir(d):
                raise RuntimeError(f"no such directory: {source_dir}")
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith((".jpg", ".jpeg", ".png")):
                    with open(os.path.join(d, fn), "rb") as fh:
                        pages.append((fn, fh.read()))
            if not pages:
                raise RuntimeError(f"no images found in {source_dir}")
        else:
            raise RuntimeError("need intake_key or source_dir")
        out = await doc_archive_client.doc_archive(slug, doc_type, pages)
        self._doc_record("archive", slug=slug, doc_type=doc_type,
                         pages=len(pages), keys=keys,
                         duplicates=(out.get("duplicates") or
                                     out.get("dupe_pages")))
        return out

    async def ada_doc_print(
        self, slug: str, pages: str | None = None,
        true_size_mm: str | None = None,
    ) -> dict[str, Any]:
        """Print archived pages on the DeskJet via tony-dell CUPS.
        pages: 'all' | '1-3' | '2'; true_size_mm: '85.6x54' for ID-1."""
        ts = None
        if true_size_mm:
            try:
                a, b = str(true_size_mm).lower().split("x", 1)
                ts = (float(a), float(b))
            except ValueError as exc:
                raise RuntimeError(
                    f"bad true_size_mm '{true_size_mm}' — use '85.6x54'") from exc
        out = await doc_archive_client.doc_print_pdf(slug, pages, ts)
        self._doc_record("print", slug=slug, pages=out.get("pages"),
                         queue=out.get("queue"))
        return out

    # -- Google Drive / Photos tools (doc-archive /v1/drive + /v1/photos) --

    async def drive(
        self, action: str, query: str = "", mime: str | None = None,
        limit: int = 10, file_id: str = "", content: str = "",
        screen: int = 0, target: str = "screen",
    ) -> Any:
        """Google Drive surface (tools-merge-docs-drive): action=
        'search'|'get'|'show' read (absorbed drive_search/drive_get/
        drive_show); 'update' rewrites a file's content — the confirm
        gate runs in _execute_gated before dispatch."""
        action = (action or "").strip().lower()
        if action == "search":
            return await self.drive_search(query, mime=mime, limit=limit)
        if action == "get":
            return await self.drive_get(file_id)
        if action == "show":
            return await self.drive_show(
                file_id, screen=screen, target=target)
        if action == "update":
            return await self.drive_update(file_id, content)
        raise ValueError(
            f"invalid action {action!r}: expected search|show|get|update")

    async def drive_search(self, query: str, mime: str | None = None,
                           limit: int = 10) -> Any:
        """Search the whole Drive by name or content. mime narrows it:
        'image/', 'video/', 'application/pdf', 'text/'."""
        files = await doc_archive_client.drive_search(
            query, mime=mime, limit=limit)
        return {"files": files, "count": len(files)}

    async def drive_get(self, file_id: str) -> dict[str, Any]:
        """Read one Drive file by id (from drive_search). Text and Google
        docs come back inline; binary types return a castable media_url."""
        return await doc_archive_client.drive_get(file_id)

    async def drive_update(self, file_id: str, content: str,
                           confirmed: bool = False) -> dict[str, Any]:
        """Replace a regular Drive file's content in place (text/md/json/
        csv). Google-native docs can't be media-updated — the service
        returns 400 with the export/re-upload guidance."""
        out = await doc_archive_client.drive_update(file_id, content)
        self._doc_record("drive-update", file_id=file_id)
        return out

    async def drive_show(self, file_id: str, screen: int = 0,
                         target: str = "screen") -> dict[str, Any]:
        """Show a Drive photo/video/file on a vcast screen (default) or the
        TV (target='tv'). Picks image/play/nav from the file's mimeType —
        the media URL is minted server-side so the display needs no auth."""
        info = await doc_archive_client.drive_get(file_id)
        mime = str(info.get("mimeType") or "")
        media_url = info.get("media_url")
        if media_url:
            url = doc_archive_client.DOC_ARCHIVE_URL + str(media_url)
        else:
            # text/inline types: mint a media URL anyway so the display
            # can fetch the raw file
            url = await doc_archive_client.drive_media_url(file_id)
        if str(target or "screen").lower() == "tv":
            out = await self.tv_action(cmd="nav", text=url)
            if isinstance(out, dict):
                out.update({"name": info.get("name"), "mimeType": mime})
            return out
        if mime.startswith("video/") or mime.startswith("audio/"):
            action = "play"
        elif mime.startswith("image/"):
            action = "image"
        else:
            action = "nav"
        n = int(screen or 1)
        out = await self.cast_to_screen(screen=n, action=action, url=url)
        if isinstance(out, dict):
            out.update({"name": info.get("name"), "mimeType": mime})
        return out

    async def photos_pick(self) -> dict[str, Any]:
        """Start a Google Photos picker session — returns a picker_uri the
        user opens on their signed-in phone/browser to choose items, plus a
        session_id to pass to photos_picked. Google's Library API was
        limited to app-created data in 2025, so picking is the only way to
        reach library photos."""
        out = await doc_archive_client.photos_picker_create()
        out["note"] = ("Send the user picker_uri — it must be opened where "
                       "their Google account is signed in (phone/laptop). "
                       "Then call chat_send photo='picked' with session_id.")
        return out

    async def photos_picked(self, session_id: str, screen: int = 0,
                            show: bool = True) -> dict[str, Any]:
        """Poll a picker session. When the user has picked items, returns
        them and (show=true) casts the first item to the screen — images
        as image, videos as play, using the picker baseUrl directly."""
        out = await doc_archive_client.photos_picker_poll(session_id)
        if not out.get("picked"):
            return out
        items = out.get("items") or []
        if items and show:
            first = items[0]
            base = str(first.get("baseUrl") or "")
            if base:
                n = int(screen or 1)
                mime = str(first.get("mimeType") or "")
                action = ("play" if mime.startswith("video/")
                          else "image")
                # baseUrl modifiers: =dv fetches playable video bytes,
                # =w<N> resizes images for a screen.
                url = base + ("=dv" if mime.startswith("video/") else "=w2048")
                cast = await self.cast_to_screen(
                    screen=n, action=action, url=url)
                out["cast"] = cast
                out["shown"] = first.get("filename") or first.get("id")
        return out
