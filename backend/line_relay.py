"""Ada <-> LINE two-way relay (webhook-only).

LINE has no polling API — the Messaging API delivers events by POSTing to a
configured webhook URL. This module serves that webhook on a local port
(Caddy proxies /webhook/line -> LINE_LISTEN on idc01) and relays text
messages to Ada over the same websocket protocol the TG relay uses.

Env:
    LINE_CHANNEL_ACCESS_TOKEN  — Messaging API channel token (outbound)
    LINE_CHANNEL_SECRET        — Basic-settings channel secret; verifies the
                                 X-Line-Signature HMAC on every webhook POST
    LINE_ALLOWED_USER_IDS      — comma list of LINE userIds that may chat
    LINE_USER_CALLERS          — "<userId>:<key-name>,..." per-user identity
    LINE_WEBHOOK_LISTEN        — host:port, default 127.0.0.1:8912
    LINE_WEBHOOK_PATH          — default /webhook/line
    ADA_WS_URL / ADA_API_KEY / ADA_KEYS_FILE — same as tg_relay

Replies go out via /message/reply using the event's replyToken (free, but
single-use and short-lived); if that fails the relay falls back to
/message/push so the user still gets Ada's answer.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
import websockets

logger = logging.getLogger("line_relay")

LINE_API = os.environ.get("LINE_API_BASE", "https://api.line.me")
CHANNEL_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
ADA_WS_URL = os.environ.get("ADA_WS_URL", "ws://127.0.0.1:8002/ws")
ADA_API_KEY = os.environ.get("ADA_API_KEY", "")
IDLE_SESSION_S = float(os.environ.get("LINE_IDLE_SESSION_S", "600"))

LINE_LISTEN = os.environ.get("LINE_WEBHOOK_LISTEN", "127.0.0.1:8912")
LINE_PATH = os.environ.get("LINE_WEBHOOK_PATH", "/webhook/line")
REPLY_CHUNK = 4900  # LINE text message limit is 5000 chars
# LINE image messages can only reference public HTTPS URLs — snapshots get
# hosted transiently on this relay and proxied out via Caddy /relay-img/*.
LINE_PUBLIC_IMG_BASE = os.environ.get(
    "LINE_PUBLIC_IMG_BASE",
    "https://157.85.110.99.sslip.io/relay-img")
IMG_TTL_S = float(os.environ.get("LINE_IMG_TTL_S", "3600"))

ADA_KEYS_FILE = os.environ.get(
    "ADA_KEYS_FILE",
    str(Path.home() / ".config" / "secrets" / "ada-ha-tony-keys.json"))


def _allowed_users() -> set[str]:
    return {c.strip() for c in
            os.environ.get("LINE_ALLOWED_USER_IDS", "").split(",")
            if c.strip()}


def _user_callers() -> dict[str, str]:
    """LINE_USER_CALLERS='Uabc:user-kk,Udef:user-tony' -> {'Uabc':'user-kk'}"""
    out: dict[str, str] = {}
    for part in os.environ.get("LINE_USER_CALLERS", "").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        uid, name = part.split(":", 1)
        if uid.strip() and name.strip():
            out[uid.strip()] = name.strip()
    return out


def _key_for_user(user_id: str) -> str:
    """API key for this user: mapped caller's issued key, else the default."""
    name = _user_callers().get(user_id)
    if not name:
        return ADA_API_KEY
    try:
        keys = json.loads(Path(ADA_KEYS_FILE).read_text())
        entry = keys.get(name) or {}
        key = entry.get("key")
        if key:
            return key
        logger.warning("user %s caller %r has no issued key — using default",
                       user_id, name)
    except Exception as exc:
        logger.warning("keys file read failed (%s) — using default", exc)
    return ADA_API_KEY


def _images_from(results: list[dict]) -> list[tuple[str, str]]:
    """(url, caption) pairs found in tool_result payloads — a camera
    snapshot's cast_url turns into an image message in the chat."""
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


class ChatSession:
    """One Ada WS session per LINE user, kept for continuity."""

    def __init__(self, user_id: str, relay: "LineRelay") -> None:
        self.user_id = user_id
        self.relay = relay
        self.ws = None
        self._rx_task: asyncio.Task | None = None
        self._current: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()
        self.last_active = time.monotonic()

    async def ensure(self) -> None:
        if self.ws is not None:
            return
        url = f"{ADA_WS_URL}?api_key={_key_for_user(self.user_id)}"
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
            logger.info("user %s ws reader ended: %s", self.user_id, exc)
        finally:
            await self._current.put({"type": "_ws_closed"})

    async def send_turn(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """Send one user turn; returns (transcript, image urls+captions)."""
        async with self._lock:
            self.last_active = time.monotonic()
            await self.ensure()
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
                    logger.warning("user %s ada error: %s", self.user_id, msg)
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


class LineRelay:
    def __init__(self) -> None:
        self.sessions: dict[str, ChatSession] = {}
        self.http: httpx.AsyncClient | None = None
        self._imgs: dict[str, tuple[bytes, float]] = {}

    def _verify(self, body: bytes, signature: str) -> bool:
        mac = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256)
        return hmac.compare_digest(
            base64.b64encode(mac.digest()).decode(), signature)

    async def line(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        assert self.http is not None
        return await self.http.post(
            f"{LINE_API}/v2/bot/message/{path}", json=payload,
            headers={"Authorization": f"Bearer {CHANNEL_TOKEN}"})

    async def send_text(self, user_id: str, text: str,
                        reply_token: str | None = None) -> None:
        if not text:
            return
        msgs = [{"type": "text", "text": text[i:i + REPLY_CHUNK]}
                for i in range(0, len(text), REPLY_CHUNK)][:5]
        await self._deliver(user_id, msgs, reply_token)

    async def _deliver(self, user_id: str, msgs: list[dict],
                       reply_token: str | None = None) -> None:
        if reply_token:
            try:
                r = await self.line("reply", {
                    "replyToken": reply_token, "messages": msgs[:5]})
                if r.status_code == 200:
                    return
                logger.info("replyToken failed (%s) — falling back to push",
                            r.status_code)
            except Exception as exc:
                logger.info("reply call failed (%s) — falling back to push",
                            exc)
        r = await self.line("push", {"to": user_id, "messages": msgs[:5]})
        if r.status_code != 200:
            logger.warning("push to %s failed: %s %s",
                           user_id, r.status_code, r.text[:200])

    def _preview_jpeg(self, png: bytes) -> bytes:
        """LINE previews want a small JPEG — downscale to <=240px."""
        try:
            import io
            from PIL import Image
            im = Image.open(io.BytesIO(png))
            im.thumbnail((240, 240))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=70)
            return buf.getvalue()
        except Exception:
            return png

    async def _image_msgs(self, url: str, caption: str) -> list[dict]:
        """Fetch the frame, host it publicly, return LINE image messages."""
        assert self.http is not None
        try:
            r = await self.http.get(url, timeout=60)
            if r.status_code != 200 or len(r.content) < 500:
                raise RuntimeError(f"fetch {r.status_code} {len(r.content)}B")
            import secrets
            nonce = secrets.token_urlsafe(12)
            self._imgs[nonce] = (r.content, time.time())
            pnonce = nonce + "-p"
            self._imgs[pnonce] = (self._preview_jpeg(r.content), time.time())
            msgs: list[dict] = [{"type": "image",
                                 "originalContentUrl":
                                     f"{LINE_PUBLIC_IMG_BASE}/{nonce}",
                                 "previewImageUrl":
                                     f"{LINE_PUBLIC_IMG_BASE}/{pnonce}"}]
            if caption:
                msgs.append({"type": "text", "text": caption[:4900]})
            return msgs
        except Exception as exc:
            logger.warning("image %s failed: %s", url, exc)
            return [{"type": "text",
                     "text": f"(image failed: {caption or url})"}]

    async def handle_event(self, ev: dict[str, Any]) -> None:
        src = ev.get("source") or {}
        user_id = src.get("userId")
        reply_token = ev.get("replyToken")
        if not user_id:
            return
        etype = ev.get("type")
        if etype == "follow":  # user added the bot
            await self.send_text(
                user_id, f"Ada LINE relay online. userId={user_id}",
                reply_token=reply_token)
            return
        if etype != "message":
            return
        msg = ev.get("message") or {}
        text = (msg.get("text") or "").strip() \
            if msg.get("type") == "text" else ""
        if msg.get("type") != "text":
            await self.send_text(
                user_id, f"(got {msg.get('type')}, text only for now)",
                reply_token=reply_token)
            return
        if not text:
            return
        if user_id not in _allowed_users():
            logger.info("unauthorized line user %s", user_id)
            await self.send_text(
                user_id,
                f"Not authorized. Your LINE userId is {user_id} — ask the "
                "owner to add it to LINE_ALLOWED_USER_IDS.",
                reply_token=reply_token)
            return
        sess = self.sessions.setdefault(
            user_id, ChatSession(user_id, self))
        try:
            reply, images = await sess.send_turn(text)
        except Exception as exc:
            logger.warning("user %s turn failed: %s", user_id, exc)
            reply, images = ("Ada didn't answer that turn — her session "
                             "dropped. Try again."), []
            sess = ChatSession(user_id, self)
            self.sessions[user_id] = sess
        msgs: list[dict] = [{"type": "text",
                             "text": (reply or "(no reply)")[:REPLY_CHUNK]}]
        for url, cap in images[:3]:
            msgs.extend(await self._image_msgs(url, cap))
        try:
            await self._deliver(user_id, msgs[:5],
                                reply_token=reply_token if len(text) else None)
        except Exception as exc:
            logger.warning("user %s reply send failed: %s", user_id, exc)

    async def sweeper(self) -> None:
        """Close WS sessions idle longer than IDLE_SESSION_S so Ada's
        session-end (summary, extraction) runs."""
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            for uid, sess in list(self.sessions.items()):
                if sess.ws is not None and \
                        now - sess.last_active > IDLE_SESSION_S:
                    logger.info("user %s idle close", uid)
                    await sess.close()
            for nonce, (_data, ts) in list(self._imgs.items()):
                if time.time() - ts > IMG_TTL_S:
                    self._imgs.pop(nonce, None)

    async def _webhook_server(self) -> None:
        if not CHANNEL_SECRET:
            raise SystemExit("webhook mode requires LINE_CHANNEL_SECRET")
        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def webhook(request: Request):
            body = await request.body()
            sig = request.headers.get("x-line-signature", "")
            if not self._verify(body, sig):
                # LINE console "Verify" probes may lack events; bad sigs get
                # 200 anyway so LINE doesn't retry noise — but never process.
                return JSONResponse({"ok": True}, status_code=200)
            try:
                payload = json.loads(body or b"{}")
            except Exception:
                return JSONResponse({"ok": True}, status_code=200)
            for ev in payload.get("events") or []:
                asyncio.create_task(self.handle_event(ev))
            return JSONResponse({"ok": True}, status_code=200)

        async def health(request):
            return JSONResponse({"ok": True, "mode": "webhook"})

        async def img(request):
            from starlette.responses import Response
            nonce = request.path_params.get("nonce", "")
            hit = self._imgs.get(nonce)
            if not hit:
                return JSONResponse({"ok": False}, status_code=404)
            data, _ = hit
            ct = "image/jpeg" if nonce.endswith("-p") else "image/png"
            return Response(data, media_type=ct)

        app = Starlette(routes=[
            Route(LINE_PATH, webhook, methods=["POST"]),
            Route("/webhook/line-health", health, methods=["GET"]),
            Route("/relay-img/{nonce}", img, methods=["GET"]),
        ])
        host, _, port = LINE_LISTEN.rpartition(":")
        server = uvicorn.Server(uvicorn.Config(
            app, host=host or "127.0.0.1", port=int(port or 8912),
            log_level="warning"))
        logger.info("line relay up — webhook %s%s", host, LINE_PATH)
        await server.serve()

    async def run(self) -> None:
        self.http = httpx.AsyncClient(timeout=40)
        asyncio.create_task(self.sweeper())
        await self._webhook_server()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s line_relay: %(message)s")
    if not CHANNEL_TOKEN:
        raise SystemExit("LINE_CHANNEL_ACCESS_TOKEN not set")
    if not ADA_API_KEY:
        raise SystemExit("ADA_API_KEY not set")
    await LineRelay().run()


if __name__ == "__main__":
    asyncio.run(main())
