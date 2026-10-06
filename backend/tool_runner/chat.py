"""chat_send — async LINE/Telegram push.

Split out of backend/tool_runner.py (card tool-runner-split, 2026-10-06).
The wildcard import reproduces the original module's global namespace —
helpers, constants, contextvars and backend module handles — so method
bodies moved verbatim and patch("backend.tool_runner.<mod>") targets keep
working (the mixin shares the same imported module objects).
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403


class ChatMixin:

    # -- async chat push (LINE / Telegram) --

    async def chat_send(self, channel: str = "line", text: str = "",
                        image_url: str = "", camera: str = "",
                        to: str = "", photo: str = "", doc: str = "",
                        key: str = "", op: str = "",
                        session_id: str = "", screen: int = 0,
                        show: bool = True) -> dict[str, Any]:
        """Queue an outbound LINE/Telegram message in the background and
        return immediately. camera names a VMS channel (snapped on the
        shim); image_url is any fetchable image (camwall thumb, cast_url).
        Completion arrives as a system note via /api/notify.

        tools-merge-tasks-status absorbed the photo-picker pair and the
        doc-upload card actions: photo='pick' starts a Google Photos
        picker and delivers the picker_uri to the channel (was
        photos_pick), photo='picked' polls a picker session, casts the
        first pick to a screen (show=true) and sends it to the channel
        (was photos_picked, takes session_id/screen/show); doc='show' /
        'process' / 'card' covers sys_show_uploaded_document /
        process_document_upload / doc_upload_card_action — key= names a
        held /api/documents/intake key (default: newest), op= carries a
        card button ('archive'/'print' re-dispatch through execute() so
        the ada_doc_* confirm gates still apply)."""
        photo = str(photo or "").strip().lower()
        doc = str(doc or "").strip().lower()
        if doc.startswith("doc/"):
            # A bare intake key in the doc slot means "show that one".
            key = key or doc
            doc = "show"
        if photo:
            return await self._chat_send_photo(
                photo, channel=channel, text=text, to=to,
                session_id=str(session_id or ""), screen=int(screen or 0),
                show=bool(show))
        if doc:
            return await self._chat_send_doc(
                doc, channel=channel, text=text, to=to,
                key=str(key or ""), op=str(op or ""))
        return await self._chat_send_queue(
            channel=channel, text=text, image_url=image_url,
            camera=camera, to=to)

    async def _chat_send_queue(self, channel: str, text: str,
                               image_url: str, camera: str,
                               to: str) -> dict[str, Any]:
        import asyncio
        import secrets
        job_id = "chat-" + secrets.token_hex(4)
        asyncio.create_task(self._chat_send_run(
            job_id, channel=channel, text=text, image_url=image_url,
            camera=camera, to=to))
        return {"status": "queued", "job_id": job_id, "channel": channel,
                "note": "Running in the background. Tell the user the "
                        "message is being sent — a system note will report "
                        "success or failure when it finishes."}

    async def _chat_send_photo(self, action: str, *, channel: str,
                               text: str, to: str, session_id: str,
                               screen: int, show: bool) -> dict[str, Any]:
        """photo= sub-flows — absorbed photos_pick / photos_picked keep
        their picker semantics; chat_send additionally delivers results
        to the named channel."""
        if action == "pick":
            out = await self.photos_pick()
            uri = str(out.get("picker_uri") or "")
            if uri and str(channel or "").strip():
                send_text = (str(text).strip() + " " if str(
                    text or "").strip() else "") + uri
                out["send"] = await self._chat_send_queue(
                    channel=channel, text=send_text,
                    image_url="", camera="", to=to)
            return out
        if action == "picked":
            if not session_id:
                raise ValueError(
                    "session_id is required for photo='picked'")
            out = await self.photos_picked(
                session_id, screen=int(screen or 0), show=bool(show))
            items = out.get("items") or []
            base = str(items[0].get("baseUrl") or "") if items else ""
            if out.get("picked") and base and str(channel or "").strip():
                mime = str(items[0].get("mimeType") or "")
                url = base + ("=dv" if mime.startswith("video/")
                              else "=w2048")
                out["send"] = await self._chat_send_queue(
                    channel=channel, text=text, image_url=url,
                    camera="", to=to)
            return out
        raise ValueError(
            f"invalid photo action {action!r}: expected pick|picked")

    async def _chat_send_doc(self, action: str, *, channel: str,
                             text: str, to: str, key: str,
                             op: str) -> dict[str, Any]:
        """doc= sub-flows — the doc-upload card actions absorbed from the
        chaba side: 'show' posts the held upload's summary to the
        channel, 'process' returns the held intake assessment, 'card'
        takes op=<button> where archive/print re-dispatch through
        execute() so the ada_doc_* confirmation gates still apply."""
        from backend import document_check
        engine = document_check.engine()
        ref = str(key or "").strip()
        held_key, held = ref, (engine.held(ref) if ref else None)
        if held is None and not ref:
            latest = engine.latest()
            held_key, held = latest if latest else ("", None)
        op_l = str(op or "").strip().lower()
        if action == "card" and op_l == "archive":
            if held is None:
                raise RuntimeError(
                    "no held document to act on — upload it again")
            stem = re.sub(r"[^a-z0-9]+", "-", str(
                (held.meta or {}).get("filename") or
                "document").lower()).strip("-") or "document"
            return await self.execute("ada_doc_archive", {
                "slug": stem[:40], "intake_key": held_key})
        if action == "card" and op_l == "print":
            raise RuntimeError(
                "print needs an archived doc slug — archive the held "
                "intake first (op='archive'), then ada_doc_print")
        if action not in ("show", "process", "card"):
            raise ValueError(
                f"invalid doc action {action!r}: expected "
                "show|process|card")
        if held is None:
            raise RuntimeError(
                f"no held document{f' for key {ref!r}' if ref else ''}"
                " — intake results live in RAM only, upload again")
        meta = held.meta or {}
        summary = (
            f"Document '{meta.get('filename') or held_key}' "
            f"({meta.get('doc_type') or 'document'}) held as {held_key} "
            f"— print-ready PDF and preview at "
            f"/api/documents/{held_key}/pdf|preview")
        out = {"doc": held_key, "doc_action": action,
               "doc_type": meta.get("doc_type"),
               "filename": meta.get("filename"),
               "preview_url": f"/api/documents/{held_key}/preview",
               "pdf_url": f"/api/documents/{held_key}/pdf"}
        if op_l:
            out["op"] = op_l
        if str(channel or "").strip():
            out["send"] = await self._chat_send_queue(
                channel=channel, text=text or summary,
                image_url="", camera="", to=to)
        return out

    async def _chat_send_run(self, job_id: str, channel: str, text: str,
                             image_url: str, camera: str,
                             to: str) -> None:
        """Background worker: resolve the image (VMS snap or direct URL),
        POST to the relay /send endpoints, then surface the result to the
        live session via /api/notify."""
        import urllib.parse
        import urllib.request
        summary_bits: list[str] = []
        ok_any = False
        url = (image_url or "").strip()
        cam = (camera or "").strip()
        if cam and not url:
            vms = os.environ.get("ADA_VMS_SNAP_URL", "").rstrip("/")
            if vms:
                url = f"{vms}/snap?ch=" + urllib.parse.quote(cam)
        if cam and url and "snap?ch=" in url:
            # Warm the serial shim once — the relay would otherwise fetch
            # the raw endpoint and a 503 means a broken/blocked delivery.
            try:
                with urllib.request.urlopen(url, timeout=180) as r:
                    if r.status != 200:
                        summary_bits.append(
                            f"camera '{cam}' snap failed ({r.status})")
                        url = ""
            except Exception as exc:
                summary_bits.append(f"camera '{cam}' snap failed ({exc})")
                url = ""
        if not url and cam:
            # Live snap dead → fall back to the camwall puller's last-good
            # frame (data/<zone>/<cam>.jpg + manifest ts/ok per cam).
            url, fnote = await asyncio.to_thread(self._camwall_fallback, cam)
            if url:
                summary_bits.append(
                    f"camera '{cam}' live snap down — sending {fnote}")
            elif fnote:
                summary_bits.append(f"camera '{cam}': {fnote}")
        relays = [c for c in (channel or "line").lower().split(",")
                  if c.strip()]
        if "both" in relays or "all" in relays:
            relays = ["line", "telegram"]
        for svc in relays:
            svc = svc.strip()
            if svc == "line":
                ep = os.environ.get("ADA_LINE_SEND_URL",
                                    "http://127.0.0.1:8912/send")
            elif svc in ("telegram", "tg"):
                ep = os.environ.get("ADA_TG_SEND_URL",
                                    "http://127.0.0.1:8911/send")
            else:
                summary_bits.append(f"unknown channel '{svc}'")
                continue
            payload = {"text": text, "image_url": url,
                       "caption": text or cam, "to": to,
                       "chat_id": to}
            try:
                req = urllib.request.Request(
                    ep, data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=150) as r:
                    res = json.load(r)
                if res.get("ok"):
                    ok_any = True
                    summary_bits.append(f"{svc}: delivered")
                else:
                    summary_bits.append(
                        f"{svc}: failed ({res.get('error', '?')})")
            except Exception as exc:
                summary_bits.append(f"{svc}: failed ({exc})")
        note = (f"[job {job_id}] chat_send "
                f"{'DONE' if ok_any else 'FAILED'} — "
                + "; ".join(summary_bits))
        try:
            key = os.environ.get("ADA_API_KEY", "")
            req = urllib.request.Request(
                os.environ.get("ADA_NOTIFY_URL",
                               "http://127.0.0.1:8002/api/notify"),
                data=json.dumps({"text": note, "urgent": "0"}).encode(),
                headers={"Content-Type": "application/json",
                         "x-api-key": key})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:
            logger.warning("chat_send notify failed: %s", exc)
