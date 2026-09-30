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
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
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


def _key_for_chat(chat_id: int) -> str:
    """API key for this chat: mapped caller's issued key, else the default."""
    name = _chat_callers().get(chat_id)
    if not name:
        return ADA_API_KEY
    try:
        keys = json.loads(Path(ADA_KEYS_FILE).read_text())
        entry = keys.get(name) or {}
        key = entry.get("key")
        if key:
            return key
        logger.warning("chat %s caller %r has no issued key — using default",
                       chat_id, name)
    except Exception as exc:
        logger.warning("keys file read failed (%s) — using default", exc)
    return ADA_API_KEY


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
    """One Telegram chat <-> one Ada WS session."""

    def __init__(self, chat_id: int, relay: "TgRelay") -> None:
        self.chat_id = chat_id
        self.relay = relay
        self.ws: Any = None
        self.last_active = time.monotonic()
        self._lock = asyncio.Lock()
        self._rx_task: asyncio.Task | None = None
        self._current = asyncio.Queue()  # events for the in-flight turn

    async def ensure(self) -> None:
        if self.ws is not None:
            return
        url = f"{ADA_WS_URL}?api_key={_key_for_chat(self.chat_id)}"
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
        async with self._lock:
            self.last_active = time.monotonic()
            await self.ensure()
            # Drain stale events from a previous turn before sending.
            while not self._current.empty():
                self._current.get_nowait()
            await self.ws.send(json.dumps({"type": "text", "text": text}))
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
        self.sessions: dict[int, ChatSession] = {}
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

    async def send_text(self, chat_id: int, text: str) -> None:
        if not text:
            return
        for i in range(0, len(text), REPLY_CHUNK):
            await self.tg("sendMessage", chat_id=chat_id,
                          text=text[i:i + REPLY_CHUNK])

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

    async def handle(self, upd: dict[str, Any]) -> None:
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or not text:
            return
        if text == "/start":
            await self.send_text(chat_id, f"Ada relay online. chat_id={chat_id}")
            return
        allowed = _allowed_chats()
        if chat_id not in allowed:
            logger.info("unauthorized chat %s", chat_id)
            await self.send_text(
                chat_id,
                f"Not authorized. Your chat_id is {chat_id} — ask the owner "
                "to add it to TELEGRAM_ALLOWED_CHAT_IDS.")
            return
        sess = self.sessions.setdefault(
            chat_id, ChatSession(chat_id, self))
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
            sess = ChatSession(chat_id, self)
            self.sessions[chat_id] = sess
        if not reply:
            reply = "(no reply)"
        try:
            await self.send_text(chat_id, reply)
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

        app = Starlette(routes=[
            Route(TG_WEBHOOK_PATH, webhook, methods=["POST"]),
            Route("/webhook/health", health, methods=["GET"]),
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

    async def run(self) -> None:
        self.http = httpx.AsyncClient(timeout=POLL_TIMEOUT_S + 15)
        asyncio.create_task(self.sweeper())
        if TG_MODE == "webhook":
            await self._webhook_server()
            return
        logger.info("tg relay up — polling %s", TG_API)
        await self._poll_loop()


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
