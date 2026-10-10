"""Telegram two-way chat relay for Ada.

Long-polls api.telegram.org (no public webhook needed) and forwards each
allowed chat's messages to Ada over the PWA WebSocket, streaming the
assistant transcript back to Telegram.

Session model: one WS per chat_id, kept open while the chat is active and
closed after TELEGRAM_IDLE_SESSION_S of quiet — closing ends the Ada
session so session-end memory extraction/session reports still run, and
the next message starts a fresh session (like leaving and reopening the
card).

Identity: each chat maps to an Ada caller key via
TELEGRAM_CHAT_CALLERS ("<chat_id>:<key-name>,..." — e.g.
"123456:user-kk"). Names resolve through ADA_KEYS_FILE (the issued-keys
json); unmapped-but-allowed chats fall back to ADA_API_KEY (admin). Only
chat ids in TELEGRAM_ALLOWED_CHAT_IDS reach Ada; an unknown chat gets a
polite refusal with its numeric chat id so the owner can whitelist it.

Env:
  TELEGRAM_BOT_TOKEN        required — BotFather token
  TELEGRAM_ALLOWED_CHAT_IDS comma-separated numeric chat ids (empty =
                            nobody allowed; unknowns get their id echoed)
  ADA_API_KEY               required — Ada WS auth key
  ADA_WS_URL                default ws://127.0.0.1:8002/ws
  TELEGRAM_IDLE_SESSION_S   default 600 — WS close after idle
  TELEGRAM_STATE_FILE       default ~/.local/state/ada-tg-relay.json
  TELEGRAM_API_BASE         default https://api.telegram.org
  TG_VOICE_CORPUS_DIR       default ~/.local/share/ada-voice-corpus/tg —
                            voice/audio/video_note clips from allowed
                            senders land here (one dir per sender slug,
                            manifest.jsonl at the root). Local disk only:
                            clips are never forwarded to Ada or elsewhere.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import websockets

logger = logging.getLogger("tg_relay")

TG_API = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADA_WS_URL = os.environ.get("ADA_WS_URL", "ws://127.0.0.1:8002/ws")
ADA_API_KEY = os.environ.get("ADA_API_KEY", "")
IDLE_SESSION_S = float(os.environ.get("TELEGRAM_IDLE_SESSION_S", "600"))
STATE_FILE = Path(os.environ.get(
    "TELEGRAM_STATE_FILE",
    str(Path.home() / ".local" / "state" / "ada-tg-relay.json")))
POLL_TIMEOUT_S = 25
REPLY_CHUNK = 3900  # TG hard limit is 4096

# Receive mode: 'poll' (default — getUpdates long-poll, works behind NAT but
# any other poller with the token steals updates) or 'webhook' (Telegram
# pushes to a public URL — exclusive delivery, needs Caddy route). In
# webhook mode every POST must carry X-Telegram-Bot-Api-Secret-Token equal
# to TG_SECRET_TOKEN.
TG_MODE = os.environ.get("TELEGRAM_MODE", "poll").strip().lower()
TG_LISTEN = os.environ.get("TG_WEBHOOK_LISTEN", "127.0.0.1:8911")
TG_WEBHOOK_PATH = os.environ.get("TG_WEBHOOK_PATH", "/webhook/tg")
TG_SECRET_TOKEN = os.environ.get("TG_SECRET_TOKEN", "")

# Voice corpus: clips from enrolled senders (allowed chats / mapped room
# members — the same gate Ada replies behind) are saved for speaker-id
# training. Private by design: local disk only, no forwarding.
VOICE_CORPUS_DIR = Path(os.environ.get(
    "TG_VOICE_CORPUS_DIR",
    str(Path.home() / ".local" / "share" / "ada-voice-corpus" / "tg")))

# chat_id -> ada key name; resolved against the issued-keys file.
ADA_KEYS_FILE = os.environ.get(
    "ADA_KEYS_FILE",
    str(Path.home() / ".config" / "secrets" / "ada-ha-tony-keys.json"))


def _chat_callers() -> dict[int, str]:
    """TELEGRAM_CHAT_CALLERS='123:user-kk,456:user-tony' -> {123:'user-kk'}"""
    out: dict[int, str] = {}
    for part in os.environ.get("TELEGRAM_CHAT_CALLERS", "").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        cid, name = part.split(":", 1)
        if cid.strip().lstrip("-").isdigit() and name.strip():
            out[int(cid)] = name.strip()
    return out


def _user_callers() -> dict[int, str]:
    """TELEGRAM_USER_CALLERS='<tg_uid>:<key-name>,...' — per-SENDER map for
    group rooms. A group's chat_id is shared by every member, so identity
    must resolve on message.from.id, not the chat. Senders absent from the
    map are refused (with their id echoed) — there is no admin fallback in
    a room, because that would hand the owner key to every member."""
    out: dict[int, str] = {}
    for part in os.environ.get("TELEGRAM_USER_CALLERS", "").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        uid, name = part.split(":", 1)
        if uid.strip().lstrip("-").isdigit() and name.strip():
            out[int(uid)] = name.strip()
    return out


def _key_by_name(name: str | None) -> str:
    """Issued API key for a caller name, else the admin default."""
    if not name:
        return ADA_API_KEY
    try:
        keys = json.loads(Path(ADA_KEYS_FILE).read_text())
        entry = keys.get(name) or {}
        key = entry.get("key")
        if key:
            return key
        logger.warning("caller %r has no issued key — using default", name)
    except Exception as exc:
        logger.warning("keys file read failed (%s) — using default", exc)
    return ADA_API_KEY


def _key_for_chat(chat_id: int) -> str:
    """API key for this chat: mapped caller's issued key, else the default."""
    return _key_by_name(_chat_callers().get(chat_id))


_REFUSAL_GAP_S = 3600  # don't spam a room — one refusal per sender per hour

# audio/* clip extensions when Telegram's file_path has no usable suffix.
_MIME_EXT = {
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/webm": ".weba",
    "video/mp4": ".mp4",
}


def _sender_slug(msg: dict, caller: str | None,
                 sender_id: int | None) -> str:
    """Filesystem-safe dir name for a clip's sender: caller key name if
    the sender is mapped, else their TG username/name, else the numeric
    id. Caller names read like 'user-kk' — keep them verbatim so the
    corpus dir matches the keys file."""
    sender = msg.get("from") or {}
    raw = (caller or sender.get("username")
           or sender.get("first_name") or str(sender_id or "unknown"))
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(raw)).strip("-").lower()
    return slug or str(sender_id or "unknown")


def _images_from(results: list[dict]) -> list[tuple[str, str]]:
    """(url, caption) pairs found in tool_result payloads — a camera
    snapshot's cast_url turns into a photo in the chat."""
    out: list[tuple[str, str]] = []
    for res in results:
        if not isinstance(res, dict):
            continue
        url = res.get("cast_url") or res.get("image_url") or res.get("url")
        if not (isinstance(url, str) and url.startswith("http")):
            continue
        cap = str(res.get("channel") or res.get("camera")
                  or res.get("title") or "")
        out.append((url, cap))
    return out


def _allowed_chats() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


class ChatSession:
    """One Telegram peer <-> one Ada WS session. In a group room the peer
    is (chat_id, sender_id) — each member gets their own caller key."""

    def __init__(self, chat_id: int, relay: "TgRelay",
                 caller: str | None = None) -> None:
        self.chat_id = chat_id
        self.caller = caller  # None = chat-scoped default key
        self.relay = relay
        self.ws: Any = None
        self.last_active = time.monotonic()
        self._lock = asyncio.Lock()
        self._rx_task: asyncio.Task | None = None
        self._current = asyncio.Queue()  # events for the in-flight turn

    async def ensure(self) -> None:
        if self.ws is not None:
            return
        key = (_key_by_name(self.caller) if self.caller
               else _key_for_chat(self.chat_id))
        url = f"{ADA_WS_URL}?api_key={key}&channel=telegram"
        self.ws = await websockets.connect(
            url, max_size=8 * 1024 * 1024, ping_interval=20)
        self._rx_task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._current.put(msg)
        except Exception as exc:
            logger.info("chat %s ws reader ended: %s", self.chat_id, exc)
        finally:
            await self._current.put({"type": "_ws_closed"})

    async def send_turn(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """Send one user turn; returns (transcript, image urls+captions)."""
        return await self._send_and_collect({"type": "text", "text": text})

    async def send_image_turn(
            self, img: bytes, mime: str, caption: str
    ) -> tuple[str, list[tuple[str, str]]]:
        """Forward a user photo into the session as a real image turn —
        Ada sees the picture (her describe path), replies with text."""
        return await self._send_and_collect({
            "type": "image",
            "data": base64.b64encode(img).decode(),
            "mime": mime,
            "text": caption,
        })

    async def _send_and_collect(
            self, payload: dict) -> tuple[str, list[tuple[str, str]]]:
        async with self._lock:
            self.last_active = time.monotonic()
            await self.ensure()
            # Drain stale events from a previous turn before sending.
            while not self._current.empty():
                self._current.get_nowait()
            await self.ws.send(json.dumps(payload))
            parts: list[str] = []
            results: list[dict] = []
            deadline = time.monotonic() + 180
            completed_at = 0.0
            while time.monotonic() < deadline:
                wait = min(
                    8.0 if completed_at else deadline - time.monotonic(),
                    max(deadline - time.monotonic(), 0.1))
                try:
                    msg = await asyncio.wait_for(self._current.get(), wait)
                except asyncio.TimeoutError:
                    break
                t = msg.get("type")
                if t == "assistant_transcript_delta":
                    parts.append(str(msg.get("text") or msg.get("delta") or ""))
                elif t == "tool_result":
                    results.append(msg.get("result") or {})
                elif t == "response_completed":
                    completed_at = time.monotonic()
                elif t == "_ws_closed":
                    self.ws = None
                    raise RuntimeError("Ada session closed mid-turn")
                elif t == "error":
                    logger.warning("chat %s ada error: %s", self.chat_id, msg)
                # Allow a short settle after completion for a late delta.
                if completed_at and time.monotonic() - completed_at > 6:
                    break
            return "".join(parts).strip(), _images_from(results)

    async def close(self) -> None:
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._rx_task is not None:
            self._rx_task.cancel()


class TgRelay:
    def __init__(self) -> None:
        self.sessions: dict[Any, ChatSession] = {}
        self._refused: dict[tuple[int, int], float] = {}
        self.http: httpx.AsyncClient | None = None
        self.offset = self._load_offset()

    def _load_offset(self) -> int:
        try:
            return int(json.loads(STATE_FILE.read_text()).get("offset", 0))
        except Exception:
            return 0

    def _save_offset(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({"offset": self.offset}))
        except Exception as exc:
            logger.warning("state save failed: %s", exc)

    async def tg(self, method: str, **params: Any) -> Any:
        assert self.http is not None
        url = f"{TG_API}/bot{BOT_TOKEN}/{method}"
        r = await self.http.post(url, json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {data}")
        return data.get("result")

    async def send_text(self, chat_id: int, text: str,
                        reply_to: int | None = None) -> None:
        if not text:
            return
        tag = os.environ.get("TG_SPEAKER_TAG", "").strip().lower()
        if tag and not text.lstrip().startswith("["):
            text = f"[{tag}] {text}"
        params: dict[str, Any] = {"chat_id": chat_id}
        if reply_to:
            params["reply_to_message_id"] = reply_to
        for i in range(0, len(text), REPLY_CHUNK):
            await self.tg("sendMessage",
                          **{**params, "text": text[i:i + REPLY_CHUNK]})

    async def send_photo(self, chat_id: int, url: str, caption: str) -> None:
        """Fetch image bytes (tailnet-reachable) and sendPhoto them."""
        assert self.http is not None
        try:
            r = await self.http.get(url, timeout=120)
            if r.status_code != 200 or len(r.content) < 500:
                raise RuntimeError(f"fetch {r.status_code} {len(r.content)}B")
            ct = r.headers.get("content-type", "image/png")
            ext = ".jpg" if "jpeg" in ct else ".png"
            resp = await self.http.post(
                f"{TG_API}/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": str(chat_id), "caption": caption[:1000]},
                files={"photo": (f"cam{ext}", r.content, ct)})
            if not resp.json().get("ok"):
                raise RuntimeError(str(resp.json()))
        except Exception as exc:
            logger.warning("chat %s photo %s failed: %s", chat_id, url, exc)
            await self.send_text(chat_id, f"(image failed: {caption or url})")

    async def _download_file(self, file_id: str) -> tuple[bytes, str]:
        """TG media messages carry file_ids — resolve via getFile, then
        download from the file host. Returns (bytes, file_path)."""
        info = await self.tg("getFile", file_id=file_id)
        path = info.get("file_path")
        if not path:
            raise RuntimeError("getFile returned no file_path")
        r = await self.http.get(
            f"{TG_API}/file/bot{BOT_TOKEN}/{path}", timeout=60)
        r.raise_for_status()
        return r.content, path

    async def _fetch_photo(self, file_id: str) -> tuple[bytes, str]:
        data, path = await self._download_file(file_id)
        mime = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) \
            else "image/png"
        return data, mime

    async def _save_voice_clip(
            self, msg: dict, media: dict, kind: str,
            chat_id: int, sender_id: int | None,
            caller: str | None) -> tuple[Path, int]:
        """Download a voice/audio/video_note clip into the local voice
        corpus and append one manifest line. Returns (path, duration_s)."""
        data, tg_path = await self._download_file(media["file_id"])
        slug = _sender_slug(msg, caller, sender_id)
        stamp = datetime.fromtimestamp(
            msg.get("date") or time.time()).strftime("%Y%m%d-%H%M%S")
        mime = str(media.get("mime_type") or "")
        ext = {"voice": ".ogg", "video_note": ".mp4"}.get(kind)
        if not ext:
            ext = Path(tg_path).suffix.lower() or _MIME_EXT.get(mime, ".bin")
        dest_dir = VOICE_CORPUS_DIR / slug
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{stamp}{ext}"
        n = 1
        while dest.exists():
            dest = dest_dir / f"{stamp}-{n}{ext}"
            n += 1
        dest.write_bytes(data)
        duration = int(media.get("duration") or 0)
        entry = {
            "ts": datetime.fromtimestamp(
                msg.get("date") or time.time()).isoformat(
                    timespec="seconds"),
            "sender": slug,
            "sender_id": sender_id,
            "chat_id": chat_id,
            "kind": kind,
            "duration_s": duration,
            "file_id": media["file_id"],
            "mime": mime or None,
            "path": str(dest),
        }
        with (VOICE_CORPUS_DIR / "manifest.jsonl").open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("chat %s saved %s clip %s (%ds)",
                    chat_id, kind, dest, duration)
        return dest, duration

    async def handle(self, upd: dict[str, Any]) -> None:
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        chat_type = chat.get("type") or "private"
        from_id = (msg.get("from") or {}).get("id")
        msg_id = msg.get("message_id")
        text = (msg.get("text") or "").strip()
        photos = msg.get("photo") or []
        if chat_id is None:
            return
        is_group = chat_type in ("group", "supergroup")
        if chat_id not in _allowed_chats():
            logger.info("unauthorized chat %s", chat_id)
            await self.send_text(
                chat_id,
                f"Not authorized. Your chat_id is {chat_id} — ask the owner "
                "to add it to TELEGRAM_ALLOWED_CHAT_IDS.")
            return
        # Group rooms: identity is the SENDER, not the chat. No admin
        # fallback — unmapped senders are refused (their id is echoed once
        # per hour so the owner can whitelist via TELEGRAM_USER_CALLERS).
        caller: str | None = None
        skey: Any = chat_id
        reply_to = msg_id if is_group else None
        if is_group:
            caller = _user_callers().get(from_id)
            if caller is None:
                if from_id is None:
                    return  # no sender (channel post etc.) — ignore
                last = self._refused.get((chat_id, from_id), 0.0)
                if time.monotonic() - last > _REFUSAL_GAP_S:
                    self._refused[(chat_id, from_id)] = time.monotonic()
                    await self.send_text(
                        chat_id,
                        f"Not authorized in this room. Telegram user id: "
                        f"{from_id} — the owner can whitelist it in "
                        "TELEGRAM_USER_CALLERS.",
                        reply_to=reply_to)
                return
            skey = (chat_id, from_id)
        # Voice/audio/video_note: only reached by enrolled senders (the
        # allowlist checks above return early otherwise). Save to the
        # local voice corpus and ack — nothing goes to Ada.
        media = msg.get("voice") or msg.get("audio") or msg.get("video_note")
        if media and media.get("file_id"):
            kind = ("voice" if msg.get("voice")
                    else "video_note" if msg.get("video_note") else "audio")
            try:
                _, duration = await self._save_voice_clip(
                    msg, media, kind, chat_id, from_id, caller)
                await self.send_text(
                    chat_id,
                    f"Saved {duration}s {kind} clip to the voice corpus.",
                    reply_to=reply_to)
            except Exception as exc:
                logger.warning("chat %s %s save failed: %s",
                               chat_id, kind, exc)
                await self.send_text(
                    chat_id, "Couldn't save that clip — try again.",
                    reply_to=reply_to)
            return
        if photos:
            sess = self.sessions.setdefault(
                skey, ChatSession(chat_id, self, caller=caller))
            try:
                await self.tg("sendChatAction", chat_id=chat_id,
                              action="upload_photo")
            except Exception:
                pass
            try:
                img, mime = await self._fetch_photo(photos[-1]["file_id"])
                cap = (msg.get("caption") or "").strip()
                reply, images = await sess.send_image_turn(
                    img, mime,
                    f"[via Telegram chat {chat_id}] The user sent this "
                    "photo — describe it and respond naturally."
                    + (f" Their caption: {cap}" if cap else ""))
            except Exception as exc:
                logger.warning("chat %s photo turn failed: %s",
                               chat_id, exc)
                reply, images = (
                    "Couldn't pass that photo to Ada — try again.", [])
            try:
                await self.send_text(chat_id, reply or "(no reply)",
                                     reply_to=reply_to)
            except Exception as exc:
                logger.warning("chat %s reply send failed: %s",
                               chat_id, exc)
            for url, cap2 in images[:3]:
                await self.send_photo(chat_id, url, cap2)
            return
        if not text:
            return
        if text == "/start":
            await self.send_text(chat_id, f"Ada relay online. chat_id={chat_id}")
            return
        sess = self.sessions.setdefault(
            skey, ChatSession(chat_id, self, caller=caller))
        try:
            await self.tg("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            reply, images = await sess.send_turn(text)
        except Exception as exc:
            logger.warning("chat %s turn failed: %s", chat_id, exc)
            reply, images = ("Ada didn't answer that turn — her session "
                             "dropped. Try again."), []
            sess = ChatSession(chat_id, self, caller=caller)
            self.sessions[skey] = sess
        if not reply:
            reply = "(no reply)"
        try:
            await self.send_text(chat_id, reply, reply_to=reply_to)
        except Exception as exc:
            logger.warning("chat %s reply send failed: %s", chat_id, exc)
        for url, cap in images[:3]:
            await self.send_photo(chat_id, url, cap)

    async def sweeper(self) -> None:
        """Close WS sessions idle longer than IDLE_SESSION_S so Ada's
        session-end (summary, extraction) runs — like the user closing the
        mic card."""
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            for cid, sess in list(self.sessions.items()):
                if sess.ws is not None and now - sess.last_active > IDLE_SESSION_S:
                    logger.info("chat %s idle close", cid)
                    await sess.close()

    async def _poll_loop(self) -> None:
        while True:
            try:
                updates = await self.tg(
                    "getUpdates", offset=self.offset,
                    timeout=POLL_TIMEOUT_S, limit=50)
                for upd in updates or []:
                    self.offset = int(upd["update_id"]) + 1
                    self._save_offset()
                    asyncio.create_task(self.handle(upd))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("poll error: %s", exc)
                await asyncio.sleep(5)

    async def _webhook_server(self) -> None:
        """Receive pushed updates on TG_LISTEN. Requires TG_SECRET_TOKEN —
        Telegram sends it as X-Telegram-Bot-Api-Secret-Token on every POST."""
        if not TG_SECRET_TOKEN:
            raise SystemExit("webhook mode requires TG_SECRET_TOKEN")
        import uvicorn
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        queue: asyncio.Queue = asyncio.Queue()

        async def webhook(request):
            if request.headers.get("x-telegram-bot-api-secret-token") != TG_SECRET_TOKEN:
                return JSONResponse({"ok": True}, status_code=200)
            try:
                upd = await request.json()
            except Exception:
                return JSONResponse({"ok": True}, status_code=200)
            await queue.put(upd)
            return JSONResponse({"ok": True}, status_code=200)

        async def health(request):
            return JSONResponse({"ok": True, "mode": "webhook"})

        async def send(request):
            """Outbound push API for Ada's chat_send tool — loopback only.
            {chat_id?, text?, photo_url|image_url?, caption?}"""
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False,
                                     "error": "bad json"}, status_code=422)
            try:
                cid = int(body.get("chat_id") or 0) or \
                    next(iter(_allowed_chats()), 0)
            except (TypeError, ValueError):
                cid = next(iter(_allowed_chats()), 0)
            if not cid:
                return JSONResponse({"ok": False,
                                     "error": "no target"}, status_code=400)
            # chat_send posts image_url; keep photo_url as the legacy alias.
            photo = body.get("photo_url") or body.get("image_url")
            if not (body.get("text") or photo):
                return JSONResponse({"ok": False,
                                     "error": "nothing to send"},
                                    status_code=400)
            try:
                if body.get("text"):
                    await self.send_text(cid, str(body["text"])[:4000])
                if photo:
                    await self.send_photo(cid, str(photo),
                                          str(body.get("caption") or ""))
                return JSONResponse({"ok": True, "chat_id": cid})
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)},
                                    status_code=502)

        app = Starlette(routes=[
            Route(TG_WEBHOOK_PATH, webhook, methods=["POST"]),
            Route("/webhook/health", health, methods=["GET"]),
            Route("/send", send, methods=["POST"]),
        ])
        host, _, port = TG_LISTEN.rpartition(":")
        server = uvicorn.Server(uvicorn.Config(
            app, host=host or "127.0.0.1", port=int(port or 8911),
            log_level="warning"))

        async def consume() -> None:
            while True:
                upd = await queue.get()
                asyncio.create_task(self.handle(upd))

        asyncio.create_task(consume())
        logger.info("tg relay up — webhook %s on %s%s",
                    "listening", host, TG_WEBHOOK_PATH)
        await server.serve()

    async def _serve_push(self) -> None:
        """Loopback-only outbound push API for polling relays — same /send
        contract as webhook mode so chat_send works on every lane."""
        import uvicorn
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def send(request):
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False,
                                     "error": "bad json"}, status_code=422)
            try:
                cid = int(body.get("chat_id") or 0) or \
                    next(iter(_allowed_chats()), 0)
            except (TypeError, ValueError):
                cid = next(iter(_allowed_chats()), 0)
            if not cid:
                return JSONResponse({"ok": False,
                                     "error": "no target"}, status_code=400)
            photo = body.get("photo_url") or body.get("image_url")
            if not (body.get("text") or photo):
                return JSONResponse({"ok": False,
                                     "error": "nothing to send"},
                                    status_code=400)
            try:
                if body.get("text"):
                    await self.send_text(cid, str(body["text"])[:4000])
                if photo:
                    await self.send_photo(cid, str(photo),
                                          str(body.get("caption") or ""))
                return JSONResponse({"ok": True, "chat_id": cid})
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)},
                                    status_code=502)

        async def health(request):
            return JSONResponse({"ok": True, "mode": "polling"})

        app = Starlette(routes=[
            Route("/send", send, methods=["POST"]),
            Route("/webhook/health", health, methods=["GET"]),
        ])
        host, _, port = TG_LISTEN.rpartition(":")
        server = uvicorn.Server(uvicorn.Config(
            app, host=host or "127.0.0.1", port=int(port or 8911),
            log_level="warning"))
        await server.serve()

    async def run(self) -> None:
        self.http = httpx.AsyncClient(timeout=POLL_TIMEOUT_S + 15)
        asyncio.create_task(self.sweeper())
        if TG_MODE == "webhook":
            await self._webhook_server()
            return
        logger.info("tg relay up — polling %s (send listener %s)", TG_API,
                    TG_LISTEN)
        await asyncio.gather(self._poll_loop(), self._serve_push())


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s tg_relay: %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")
    if not ADA_API_KEY:
        raise SystemExit("ADA_API_KEY not set")
    await TgRelay().run()


if __name__ == "__main__":
    asyncio.run(main())
