"""Ada <-> LINE two-way relay (webhook-only).

LINE has no polling API — the Messaging API delivers events by POSTing to a
configured webhook URL. This module serves that webhook on a local port
(Caddy proxies /webhook/line -> LINE_LISTEN on idc03) and relays text
messages to Ada over the same websocket protocol the TG relay uses.

Group rooms (docs/kb/msg-lane-multihome-routing.md): an allowed group's
messages are MENTION-routed — a member addresses a persona by name
("ada, ..." / "@kaewta ...") and the turn goes to that persona's own
backend over LINE_NAME_BACKENDS, answered "[persona->sender] ...".
Unaddressed group chatter is ignored unless LINE_DEFAULT_PERSONA names a
catch-all. Sender identity is the per-member LINE_USER_CALLERS key —
there is no admin fallback in a room.

Env:
    LINE_CHANNEL_ACCESS_TOKEN  — Messaging API channel token (outbound)
    LINE_CHANNEL_SECRET        — Basic-settings channel secret; verifies the
                                 X-Line-Signature HMAC on every webhook POST
    LINE_ALLOWED_USER_IDS      — comma list of LINE userIds that may DM
    LINE_ALLOWED_GROUP_IDS     — comma list of groupIds/roomIds that may chat
    LINE_USER_CALLERS          — "<userId>:<key-name>,..." per-user identity
    LINE_NAME_BACKENDS         — "<persona>:<ws-base>,..." mention routing
                                 (e.g. "ada:ws://127.0.0.1:8002,kaewta:
                                 ws://127.0.0.1:8007"); empty URL ("chaba:")
                                 = known-but-offline persona. 'ada' defaults
                                 to ADA_WS_URL when unlisted.
    LINE_DEFAULT_PERSONA       — persona for unaddressed group messages
                                 (unset = ignore them)
    LINE_VOICE_CORPUS_DIR      — default ~/.local/share/ada-voice-corpus/line;
                                 audio clips from enrolled senders land here
                                 (local only — like tg_relay's voice rule)
    LINE_VOICE_TRANSCRIBE      — "1" transcribes clips (faster-whisper) and
                                 routes the transcript like a text turn
    LINE_WHISPER_MODEL         — default base
    LINE_VOICE_DM_MIRROR       — default 1: group voice ack/transcript is
                                 also pushed to the speaker's DM lane
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
import re
import time
from datetime import datetime
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
    "https://157.85.102.125.sslip.io/relay-img")
IMG_TTL_S = float(os.environ.get("LINE_IMG_TTL_S", "3600"))
# Delivered photos must outlive relay restarts — cache on disk too.
IMG_CACHE_DIR = Path(os.environ.get(
    "LINE_IMG_CACHE_DIR",
    str(Path.home() / ".local/state/ada-line-relay/img-cache")))

# Voice corpus: audio clips from enrolled senders (allowed DM users /
# mapped room members — the same gate a reply sits behind) are saved for
# speaker-id training. Private by design: local disk only, no forwarding.
MEDIA_ARCHIVE_DIR = Path(os.environ.get(
    "LINE_MEDIA_ARCHIVE_DIR",
    os.path.expanduser("~/.local/share/ada/line-media")))

VOICE_CORPUS_DIR = Path(os.environ.get(
    "LINE_VOICE_CORPUS_DIR",
    str(Path.home() / ".local" / "share" / "ada-voice-corpus" / "line")))

_MIME_EXT = {
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-m4a": ".m4a",
    "video/mp4": ".mp4",
}

_REFUSAL_GAP_S = 3600  # don't spam a room — one refusal per sender per hour

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


def _allowed_groups() -> set[str]:
    return {c.strip() for c in
            os.environ.get("LINE_ALLOWED_GROUP_IDS", "").split(",")
            if c.strip()}


def _name_backends() -> dict[str, str]:
    """LINE_NAME_BACKENDS -> {persona: ws_url}. An empty URL marks a
    known-but-offline persona; 'ada' falls back to ADA_WS_URL. A bare
    ws://host:port gets the conventional /ws path appended."""
    out: dict[str, str] = {}
    for part in os.environ.get("LINE_NAME_BACKENDS", "").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        name, url = part.split(":", 1)
        name, url = name.strip().lower(), url.strip()
        if name:
            if url and re.match(r"^wss?://[^/]+$", url):
                url += "/ws"
            out[name] = url
    out.setdefault("ada", ADA_WS_URL)
    return out


def _lane_env_map(name: str) -> dict[str, str]:
    """LINE_LANE_*="kk:C822..,kk-dev:C822.." -> {instance: chat_id}."""
    out: dict[str, str] = {}
    for pair in os.environ.get(name, "").split(","):
        if ":" in pair:
            inst, chat = pair.split(":", 1)
            out[inst.strip()] = chat.strip()
    return out


def _default_persona() -> str | None:
    return os.environ.get("LINE_DEFAULT_PERSONA", "").strip().lower() or None


def _mention_persona(text: str) -> str | None:
    """First configured persona name addressed in the text — word-bounded,
    case-insensitive, optional leading '@'. Returns the canonical
    (lowercased) persona name or None."""
    best: tuple[int, str] | None = None
    for name in _name_backends():
        m = re.search(r"(?<![\w])@?" + re.escape(name) + r"(?![\w])",
                      text, flags=re.IGNORECASE)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    return best[1] if best else None


def _strip_lead_mention(text: str, name: str | None) -> str:
    """Drop a leading 'ada,' / '@ada:' address token — routing noise the
    persona doesn't need to see."""
    if not name:
        return text
    m = re.match(r"\s*@?" + re.escape(name) + r"\s*[:,;\-–—]?\s+",
                 text, flags=re.IGNORECASE)
    return text[m.end():].strip() if m else text


_THAI_RE = re.compile(r"[\u0e00-\u0e7f]")


def _name_th() -> dict[str, str]:
    """LINE_NAME_TH='kaewta:แก้วตา,chaba:ชบา' -> {persona: thai display
    name}. Thai-script messages get the Thai persona name in reply tags."""
    out: dict[str, str] = {}
    for part in os.environ.get("LINE_NAME_TH", "").split(","):
        if ":" not in part:
            continue
        name, _, th = part.partition(":")
        name, th = name.strip().lower(), th.strip()
        if name and th:
            out[name] = th
    return out


def _display_name(persona: str, text: str) -> str:
    """Reply-tag persona name: Thai form when the incoming message is
    Thai-script and a name is configured, else the ascii persona id."""
    if text and _THAI_RE.search(text):
        return _name_th().get(persona, persona)
    return persona


def _route(text: str, is_group: bool, room: str = "",
           pinned: dict | None = None) -> tuple[str | None, str]:
    """(persona, turn_text) for a message — None persona means ignore.
    Groups need a mention, a room pin, or LINE_DEFAULT_PERSONA; DMs
    default to ada. Interview-room rule: mentioning a persona pins it
    to that room — subsequent unaddressed messages keep routing to the
    pinned persona until another name is mentioned (switch). A default
    naming no configured persona resolves to ignore — an env typo must
    not crash the turn on _name_backends()[name]."""
    persona = _mention_persona(text)
    if persona is None:
        persona = ((pinned or {}).get(room)
                   or _default_persona()) if is_group else "ada"
        if persona is None:
            return None, text
        if persona not in _name_backends():
            logger.warning("default persona %r not in LINE_NAME_BACKENDS"
                           " — ignoring", persona)
            return None, text
    elif is_group and room and pinned is not None:
        if pinned.get(room) != persona:
            logger.info("room %s persona pin -> %s", room, persona)
        pinned[room] = persona
    return persona, _strip_lead_mention(text, persona)


def _key_by_name(name: str | None) -> str:
    """Issued API key for a caller name, else the admin default."""
    if not name:
        return ADA_API_KEY
    try:
        keys = json.loads(Path(ADA_KEYS_FILE).read_text())
        key = (keys.get(name) or {}).get("key")
        if key:
            return key
        logger.warning("caller %r has no issued key — using default", name)
    except Exception as exc:
        logger.warning("keys file read failed (%s) — using default", exc)
    return ADA_API_KEY


_whisper_model = None


def _transcribe(path) -> str:
    """Free-first voice transcription — faster-whisper locally, model via
    LINE_WHISPER_MODEL (default base). Gated by LINE_VOICE_TRANSCRIBE=1."""
    if os.environ.get("LINE_VOICE_TRANSCRIBE") != "1":
        return ""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        size = os.environ.get("LINE_WHISPER_MODEL", "base")
        _whisper_model = WhisperModel(
            size, device="cpu", compute_type="int8")
        logger.info("whisper model %s loaded", size)
    segments, _ = _whisper_model.transcribe(str(path), beam_size=1)
    return " ".join(seg.text.strip() for seg in segments).strip()


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
    """One persona WS session per (chat, sender, persona) — in a group each
    member gets their own caller key and their own session per persona."""

    def __init__(self, user_id: str, relay: "LineRelay",
                 ws_url: str | None = None,
                 caller: str | None = None) -> None:
        self.user_id = user_id
        self.ws_url = ws_url or ADA_WS_URL
        self.caller = caller  # LINE_USER_CALLERS name; None -> default key
        self.relay = relay
        self.ws = None
        self._rx_task: asyncio.Task | None = None
        self._current: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()
        self.last_active = time.monotonic()

    async def ensure(self) -> None:
        if self.ws is not None:
            return
        url = (f"{self.ws_url}?api_key={_key_by_name(self.caller)}"
               "&channel=line")
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
        # session key: (chat_id, sender_id, persona) in a room,
        # (user_id, persona) in a DM
        self.sessions: dict[Any, ChatSession] = {}
        self.http: httpx.AsyncClient | None = None
        self._imgs: dict[str, tuple[bytes, float, str]] = {}
        # Most recent chat a turn was delivered into — the default target
        # for /send when 'to' is empty. The old fallback (first allowed
        # user) pushed persona sends into the owner's DM.
        self._recent_chat: dict[str, tuple[str, float]] = {}
        # Interview-room pins: room chat_id -> last-mentioned persona.
        self._room_pin: dict[str, str] = {}
        self._names: dict[str, str] = {}  # userId -> display label cache
        self._refused: dict[tuple[str, str], float] = {}

    def _verify(self, body: bytes, signature: str) -> bool:
        mac = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256)
        return hmac.compare_digest(
            base64.b64encode(mac.digest()).decode(), signature)

    async def line(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        assert self.http is not None
        return await self.http.post(
            f"{LINE_API}/v2/bot/message/{path}", json=payload,
            headers={"Authorization": f"Bearer {CHANNEL_TOKEN}"})

    async def _fetch_content(self, message_id: str) -> tuple[bytes, str]:
        """Pull an inbound LINE image's bytes (api-data.line.me content
        endpoint — separate host from api.line.me)."""
        assert self.http is not None
        r = await self.http.get(
            f"https://api-data.line.me/v2/bot/message/{message_id}/content",
            headers={"Authorization": f"Bearer {CHANNEL_TOKEN}"})
        r.raise_for_status()
        mime = r.headers.get("content-type") or "image/jpeg"
        return r.content, mime.split(";")[0]

    def _chat_dir(self, chat_id: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", chat_id).strip("-") \
            or "chat"
        d = MEDIA_ARCHIVE_DIR / slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _journal(self, chat_id: str, entry: dict) -> None:
        """One line per event for the conversation-report builder --
        <archive>/<chat>/journal-YYYY-MM-DD.jsonl. Best-effort: a write
        failure must never break message flow."""
        try:
            entry.setdefault("ts",
                             datetime.now().isoformat(timespec="seconds"))
            day = datetime.now().strftime("%Y-%m-%d")
            with (self._chat_dir(chat_id)
                  / f"journal-{day}.jsonl").open("a") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.info("journal write failed: %s", exc)

    def _archive_media(self, msg: dict, data: bytes, mime: str,
                       kind: str, chat_id: str, label: str) -> str:
        """Persist inbound media bytes under
        <archive>/<chat>/<messageId>.<ext> + manifest line, and return a
        durable public /relay-img/arch/ URL -- received media outlives
        LINE's content retention and stays resendable. Mirrors the
        voice-corpus scheme."""
        import secrets
        d = self._chat_dir(chat_id)
        mid = str(msg.get("id") or secrets.token_hex(6))
        ext = _MIME_EXT.get(mime, ".bin")
        dest = d / f"{mid}{ext}"
        if not dest.exists():
            dest.write_bytes(data)
        url = f"{LINE_PUBLIC_IMG_BASE}/arch/{d.name}/{mid}{ext}"
        try:
            with (MEDIA_ARCHIVE_DIR / "manifest.jsonl").open("a") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "sender": label, "chat_id": chat_id, "kind": kind,
                    "message_id": mid, "mime": mime,
                    "path": str(dest), "url": url},
                    ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.info("media manifest write failed: %s", exc)
        return url

    async def _mark_read(self, ev: dict) -> None:
        """Show the read tick as soon as a persona starts on the message —
        the event's markAsReadToken marks the whole chat read up to it.
        Absent when the OA's Chat mode is off (LINE auto-marks then)."""
        token = ev.get("markAsReadToken")
        if not token:
            return
        try:
            r = await self.line("markAsRead",
                                {"markAsReadToken": token})
            if r.status_code != 200:
                logger.info("markAsRead %s: %s", r.status_code, r.text[:120])
        except Exception as exc:
            logger.info("markAsRead failed: %s", exc)

    async def send_text(self, user_id: str, text: str,
                        reply_token: str | None = None) -> None:
        if not text:
            return
        msgs = [{"type": "text", "text": text[i:i + REPLY_CHUNK]}
                for i in range(0, len(text), REPLY_CHUNK)][:5]
        await self._deliver(user_id, msgs, reply_token)

    async def _deliver(self, user_id: str, msgs: list[dict],
                       reply_token: str | None = None) -> None:
        logger.info("deliver to %s: [%s]",
                    user_id, ",".join(m.get("type", "?") for m in msgs[:5]))
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
        """Fetch the frame, host it publicly, return LINE image messages.
        The served content-type must match the real bytes — LINE renders
        a blank image when a JPEG thumb is labelled image/png."""
        assert self.http is not None
        # CMS docs embed site-relative paths (/api/documents/x.jpg) —
        # resolve them against LINE_URL_BASE so lanes can send them
        # verbatim. Absolute http(s) URLs pass through untouched.
        if url.startswith("/"):
            # Malformed path-embedded URLs first — lanes sometimes emit
            # '/image_url=https://cdn/x.jpg'; recover the inner URL.
            m = re.search(r"https?://\S+", url)
            if m:
                url = m.group(0)
            else:
                base = os.environ.get(
                    "LINE_URL_BASE", "http://100.102.134.91:11023")
                url = base.rstrip("/") + url
        try:
            r = await self.http.get(url, timeout=120, follow_redirects=True)
            if r.status_code != 200 or len(r.content) < 500:
                raise RuntimeError(f"fetch {r.status_code} {len(r.content)}B")
            mime = (r.headers.get("content-type")
                    or "image/jpeg").split(";")[0]
            # Not-an-image guard: a fetched HTML page (e.g. the CMS SPA
            # cast_url pasted as image_url) used to be re-labelled jpeg —
            # LINE rendered a broken tile. Verify the bytes are a real
            # image before hosting; anything else is a fetch failure.
            if not mime.startswith("image/") and \
                    not (r.content[:3] == b"\xff\xd8\xff"
                         or r.content[:8].startswith(b"\x89PNG")
                         or r.content[:4] in (b"RIFF", b"GIF8")):
                raise RuntimeError(
                    f"fetched {mime}, not an image ({len(r.content)}B)")
            if not mime.startswith("image/"):
                mime = "image/jpeg"
            import secrets
            nonce = secrets.token_urlsafe(12)
            self._imgs[nonce] = (r.content, time.time(), mime)
            pnonce = nonce + "-p"
            self._imgs[pnonce] = (self._preview_jpeg(r.content),
                                  time.time(), "image/jpeg")
            self._img_persist(nonce, r.content, mime)
            self._img_persist(pnonce, self._imgs[pnonce][0], "image/jpeg")
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

    def _audio_msgs(self, b64: str, duration_ms: int) -> list[dict]:
        """audio_b64 (WAV bytes, base64) -> LINE audio push message.

        LINE audio bubbles accept only AAC/m4a — transcode via PyAV
        (bundled ffmpeg libs, backend/requirements.txt), then host the
        file on the same public /relay-img lane images use so LINE's CDN
        can pull it. Falls back to a text notice on any encode failure.
        """
        import base64
        import io
        import secrets
        try:
            import av
            data = base64.b64decode(b64)
            inp = av.open(io.BytesIO(data))
            ist = inp.streams.audio[0]
            buf = io.BytesIO()
            out = av.open(buf, mode="w", format="ipod")
            ost = out.add_stream("aac", rate=24000)
            ost.layout = "mono"
            resampler = av.audio.resampler.AudioResampler(
                format="fltp", layout="mono", rate=24000)
            for frame in inp.decode(ist):
                for rf in resampler.resample(frame):
                    for pkt in ost.encode(rf):
                        out.mux(pkt)
            for rf in resampler.resample(None):
                for pkt in ost.encode(rf):
                    out.mux(pkt)
            for pkt in ost.encode(None):
                out.mux(pkt)
            out.close()
            inp.close()
            m4a = buf.getvalue()
            nonce = "aud-" + secrets.token_urlsafe(12)
            self._imgs[nonce] = (m4a, time.time(), "audio/mp4")
            self._img_persist(nonce, m4a, "audio/mp4")
            return [{"type": "audio",
                     "originalContentUrl":
                         f"{LINE_PUBLIC_IMG_BASE}/{nonce}",
                     "duration": max(1, int(duration_ms or 0))}]
        except Exception as exc:
            logger.warning("audio encode/host failed: %s", exc)
            return [{"type": "text",
                     "text": "(voice clip could not be delivered)"}]

    async def _sender_label(self, user_id: str, group_id: str | None,
                            caller: str | None,
                            kind: str = "group") -> str:
        """Display label for the [persona->sender] reply tag: the room
        profile's displayName when LINE will tell us (kind is the event
        source type — 'room' sources use the /room/ member endpoint),
        else the caller key minus its 'user-' prefix, else the userId
        tail."""
        if user_id in self._names:
            return self._names[user_id]
        label = ""
        if group_id:
            if kind not in ("group", "room"):
                kind = "group"
            try:
                assert self.http is not None
                r = await self.http.get(
                    f"{LINE_API}/v2/bot/{kind}/{group_id}"
                    f"/member/{user_id}",
                    headers={"Authorization": f"Bearer {CHANNEL_TOKEN}"})
                if r.status_code == 200:
                    label = str(r.json().get("displayName") or "")
            except Exception as exc:
                logger.info("group profile %s/%s failed: %s",
                            group_id, user_id, exc)
        if not label:
            label = re.sub(r"^user-", "", caller or "") or user_id[-6:]
        self._names[user_id] = label
        return label

    async def _refuse_group_member(self, chat_id: str, user_id: str,
                                   reply_token: str | None) -> None:
        """Rate-limited 'not authorized' for an unmapped room member —
        echoes the userId once an hour so the owner can whitelist it."""
        last = self._refused.get((chat_id, user_id), 0.0)
        if time.monotonic() - last < _REFUSAL_GAP_S:
            return
        self._refused[(chat_id, user_id)] = time.monotonic()
        await self.send_text(
            chat_id,
            f"Not authorized in this room. LINE userId: {user_id} — the "
            "owner can whitelist it in LINE_USER_CALLERS.",
            reply_token=reply_token)

    def _session(self, skey: Any, user_id: str, persona_url: str,
                 caller: str | None) -> ChatSession:
        sess = self.sessions.get(skey)
        if sess is None or sess.ws_url != persona_url:
            sess = ChatSession(user_id, self, ws_url=persona_url,
                               caller=caller)
            self.sessions[skey] = sess
        return sess

    async def _deliver_turn(self, skey: Any, chat_id: str, user_id: str,
                            persona: str, persona_url: str,
                            caller: str | None, label: str,
                            turn_text: str, is_group: bool,
                            reply_token: str | None) -> None:
        """One text turn to a persona backend; reply tagged
        [persona->sender] in rooms."""
        self._recent_chat[persona] = (chat_id, time.monotonic())
        sess = self._session(skey, user_id, persona_url, caller)
        disp = _display_name(persona, turn_text)
        try:
            reply, images = await sess.send_turn(turn_text)
        except Exception as exc:
            logger.warning("user %s turn failed: %s", user_id, exc)
            reply, images = (f"{disp} didn't answer that turn — the "
                             "session dropped. Try again."), []
            self.sessions.pop(skey, None)
        self._journal(chat_id, {"dir": "in", "from": label,
                                "persona": disp, "kind": "text",
                                "text": turn_text})
        self._journal(chat_id, {"dir": "out", "from": disp,
                                "kind": "text", "text": reply or ""})
        tag = f"[{disp}->{label}] " if is_group else ""
        msgs: list[dict] = [{"type": "text",
                             "text": (tag + (reply or "(no reply)"))
                             [:REPLY_CHUNK]}]
        for url, cap in images[:3]:
            msgs.extend(await self._image_msgs(url, cap))
        try:
            await self._deliver(chat_id, msgs[:5], reply_token=reply_token)
        except Exception as exc:
            logger.warning("user %s reply send failed: %s", user_id, exc)

    async def _save_voice_clip(
            self, msg: dict, data: bytes, mime: str, kind: str,
            chat_id: str, user_id: str, caller: str | None,
            label: str) -> tuple[Path, int]:
        """Save an audio clip into the local voice corpus + one manifest
        line — the same rule tg_relay applies. Returns (path, secs)."""
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-",
                      caller or label or user_id).strip("-").lower() \
            or "unknown"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        ext = _MIME_EXT.get(mime, ".m4a")
        dest_dir = VOICE_CORPUS_DIR / slug
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{stamp}{ext}"
        n = 1
        while dest.exists():
            dest = dest_dir / f"{stamp}-{n}{ext}"
            n += 1
        dest.write_bytes(data)
        duration = int((msg.get("duration") or 0) / 1000)  # LINE ms -> s
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "sender": slug,
            "sender_id": user_id,
            "chat_id": chat_id,
            "kind": kind,
            "duration_s": duration,
            "message_id": msg.get("id"),
            "mime": mime or None,
            "path": str(dest),
        }
        with (VOICE_CORPUS_DIR / "manifest.jsonl").open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("chat %s saved %s clip %s (%ds)",
                    chat_id, kind, dest, duration)
        return dest, duration

    async def _handle_audio(self, ev: dict, msg: dict, chat_id: str,
                            user_id: str, group_id: str | None,
                            is_group: bool, caller: str | None,
                            reply_token: str | None) -> None:
        """Voice rule in-channel: enrolled senders' clips are saved to the
        local corpus and acked; with LINE_VOICE_TRANSCRIBE=1 the transcript
        routes like a text turn. Group clips also mirror the ack/transcript
        to the speaker's home channel (their DM lane)."""
        if is_group and caller is None:
            return  # unmapped member — same gate as a routed text turn
        label = await self._sender_label(
            user_id, group_id if is_group else None, caller,
            kind=(ev.get("source") or {}).get("type") or "group")
        try:
            data, mime = await self._fetch_content(str(msg.get("id") or ""))
        except Exception as exc:
            logger.warning("user %s audio fetch failed: %s", user_id, exc)
            await self.send_text(
                chat_id, "Couldn't fetch that voice clip — try again.",
                reply_token=reply_token)
            return
        try:
            clip, duration = await self._save_voice_clip(
                msg, data, mime, "audio", chat_id, user_id, caller, label)
        except Exception as exc:
            logger.warning("user %s audio save failed: %s", user_id, exc)
            await self.send_text(
                chat_id, "Couldn't save that clip — try again.",
                reply_token=reply_token)
            return
        transcript = await asyncio.get_running_loop().run_in_executor(
            None, _transcribe, clip)
        if transcript:
            await self.send_text(chat_id, f'\U0001f399 "{transcript}"',
                                 reply_token=reply_token)
        else:
            await self.send_text(
                chat_id, f"Saved {duration}s voice clip to the corpus.",
                reply_token=reply_token)
        if is_group and os.environ.get("LINE_VOICE_DM_MIRROR", "1") != "0":
            try:
                mirror = (f'\U0001f399 "{transcript}"' if transcript
                          else f"(voice clip saved — {duration}s)")
                await self.send_text(user_id, mirror)
            except Exception as exc:
                logger.info("voice dm-mirror to %s failed: %s",
                            user_id, exc)
        if not transcript:
            return
        await self._mark_read(ev)
        persona, turn = _route(transcript, is_group, chat_id,
                                   self._room_pin)
        if persona is None:
            return
        disp = _display_name(persona, transcript)
        persona_url = _name_backends().get(persona)
        if not persona_url:
            await self.send_text(
                chat_id, f"[{disp}->{label}] {disp} isn't home "
                         "yet — try another name.")
            return
        skey = (chat_id, user_id, persona) if is_group \
            else (user_id, persona)
        await self._deliver_turn(
            skey, chat_id, user_id, persona, persona_url, caller, label,
            f"[LINE group from {label}] {turn}" if is_group else turn,
            is_group, None)

    async def handle_event(self, ev: dict[str, Any]) -> None:
        src = ev.get("source") or {}
        user_id = src.get("userId")
        reply_token = ev.get("replyToken")
        src_type = src.get("type") or "user"
        group_id = src.get("groupId") or src.get("roomId")
        is_group = src_type in ("group", "room") and bool(group_id)
        chat_id = group_id if is_group else user_id  # reply/push target
        etype = ev.get("type")
        if etype == "join" and is_group:
            # Bot added to a room — surface the id for allowlisting.
            logger.info("joined %s %s", src_type, group_id)
            await self.send_text(
                chat_id,
                f"Ada relay here. groupId={group_id} — the owner can "
                "enable this room via LINE_ALLOWED_GROUP_IDS.",
                reply_token=reply_token)
            return
        if is_group and group_id not in _allowed_groups():
            logger.info("ignoring event in unallowed %s %s",
                        src_type, group_id)
            return
        if not user_id or not chat_id:
            return
        if etype == "follow":  # user added the bot
            await self.send_text(
                chat_id, f"Ada LINE relay online. userId={user_id}",
                reply_token=reply_token)
            return
        if etype != "message":
            return
        msg = ev.get("message") or {}
        mtype = msg.get("type")
        text = (msg.get("text") or "").strip() if mtype == "text" else ""
        caller = _user_callers().get(user_id)
        # Auth: a DM needs LINE_ALLOWED_USER_IDS; a room member needs a
        # LINE_USER_CALLERS entry — there is no admin fallback in a room,
        # because that would hand the owner key to every member.
        if not is_group and user_id not in _allowed_users():
            logger.info("unauthorized line user %s", user_id)
            await self.send_text(
                chat_id,
                f"Not authorized. Your LINE userId is {user_id} — ask the "
                "owner to add it to LINE_ALLOWED_USER_IDS.",
                reply_token=reply_token)
            return
        if mtype == "audio":
            await self._handle_audio(ev, msg, chat_id, user_id, group_id,
                                     is_group, caller, reply_token)
            return
        if mtype not in ("text", "image"):
            if not is_group:
                await self.send_text(
                    chat_id, f"(got {mtype}, text, voice or photos only)",
                    reply_token=reply_token)
            return
        if mtype == "text" and not text:
            return
        # Route: groups need a mention (or LINE_DEFAULT_PERSONA), DMs
        # default to ada. Group images have no caption — default persona.
        persona, turn = _route(text if mtype == "text" else "",
                                   is_group, chat_id, self._room_pin)
        if persona is None:
            logger.info("ignored unaddressed %s msg from %s (no persona mention)",
                        src_type, user_id)
            return  # unaddressed room chatter — ignore per spec
        disp = _display_name(persona, text)
        if is_group and caller is None:
            await self._refuse_group_member(chat_id, user_id, reply_token)
            return
        persona_url = _name_backends().get(persona)
        label = await self._sender_label(
            user_id, group_id if is_group else None, caller,
            kind=src_type)
        if not persona_url:
            await self.send_text(
                chat_id,
                f"[{disp}->{label}] {disp} isn't home yet — try "
                "another name.",
                reply_token=reply_token)
            return
        skey = (chat_id, user_id, persona) if is_group \
            else (user_id, persona)
        sess = self._session(skey, user_id, persona_url, caller)
        await self._mark_read(ev)
        if mtype == "image":
            try:
                img, mime = await self._fetch_content(
                    str(msg.get("id") or ""))
                media_url = self._archive_media(
                    msg, img, mime, "image", chat_id, label)
                self._journal(chat_id, {"dir": "in", "from": label,
                                        "kind": "image",
                                        "url": media_url})
                where = (f"[via LINE group from {label}]"
                         if is_group else f"[via LINE from user {user_id}]")
                reply, images = await sess.send_image_turn(
                    img, mime,
                    f"{where} The user sent this photo — describe it and "
                    "respond naturally.")
            except Exception as exc:
                logger.warning("user %s image turn failed: %s",
                               user_id, exc)
                reply, images = (
                    f"Couldn't pass that photo to {disp} — try "
                    "again.", [])
            self._journal(chat_id, {"dir": "out", "from": disp,
                                    "kind": "text",
                                    "text": reply or ""})
            tag = f"[{disp}->{label}] " if is_group else ""
            msgs = [{"type": "text",
                     "text": (tag + (reply or "(no reply)"))
                     [:REPLY_CHUNK]}]
            for url, cap in images[:3]:
                msgs.extend(await self._image_msgs(url, cap))
            try:
                await self._deliver(chat_id, msgs[:5],
                                    reply_token=reply_token)
            except Exception as exc:
                logger.warning("user %s img-reply failed: %s",
                               user_id, exc)
            return
        turn_text = (f"[LINE group from {label}] {turn}"
                     if is_group else turn)
        try:
            reply, images = await sess.send_turn(turn_text)
        except Exception as exc:
            logger.warning("user %s turn failed: %s", user_id, exc)
            reply, images = (f"{persona} didn't answer that turn — the "
                             "session dropped. Try again."), []
            self.sessions.pop(skey, None)
        self._journal(chat_id, {"dir": "in", "from": label,
                                "persona": disp, "kind": "text",
                                "text": turn})
        self._journal(chat_id, {"dir": "out", "from": disp,
                                "kind": "text", "text": reply or ""})
        tag = f"[{disp}->{label}] " if is_group else ""
        msgs = [{"type": "text",
                 "text": (tag + (reply or "(no reply)"))[:REPLY_CHUNK]}]
        for url, cap in images[:3]:
            msgs.extend(await self._image_msgs(url, cap))
        try:
            await self._deliver(chat_id, msgs[:5], reply_token=reply_token)
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
            for nonce, hit in list(self._imgs.items()):
                ts = hit[1]
                if time.time() - ts > IMG_TTL_S:
                    self._imgs.pop(nonce, None)
            for f in IMG_CACHE_DIR.glob("*"):
                try:
                    if time.time() - f.stat().st_mtime > IMG_TTL_S:
                        f.unlink(missing_ok=True)
                except OSError:
                    pass

    def _img_persist(self, nonce: str, data: bytes, mime: str) -> None:
        try:
            IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (IMG_CACHE_DIR / nonce).write_bytes(data)
            (IMG_CACHE_DIR / (nonce + ".mime")).write_text(mime)
        except OSError as exc:
            logger.warning("relay-img persist %s failed: %s", nonce, exc)

    def _img_load(self, nonce: str):
        """Disk fallback after a restart — repopulates _imgs on hit."""
        try:
            f = IMG_CACHE_DIR / nonce
            if not f.is_file():
                return None
            mime = (IMG_CACHE_DIR / (nonce + ".mime")).read_text().strip()
            data = f.read_bytes()
            hit = (data, f.stat().st_mtime, mime)
            self._imgs[nonce] = hit
            return hit
        except OSError:
            return None

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

        async def send(request):
            """Outbound push API for Ada's chat_send tool — loopback only
            (127.0.0.1 listen). {to?, text?, image_url?, caption?} — the
            image is fetched + publicly hosted via _image_msgs so LINE's
            CDN can pull it."""
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False,
                                     "error": "bad json"}, status_code=422)
            uid = str(body.get("to") or body.get("chat_id") or "").strip()
            # Display-name shortcut: personas pass 'to=กุ้ง' meaning a room
            # member — resolve through the profile-name cache, else leave
            # it (a bad id makes LINE 400 either way, but resolving keeps
            # the message deliverable).
            if uid and not re.match(r"^[UCR][0-9a-f]{10,}$", uid):
                lookup = uid.lower()
                if lookup.startswith("person."):
                    lookup = lookup.split(".", 1)[1]
                if lookup in ("owner", "me"):
                    # 'owner' is a standing alias — lanes saying
                    # to=owner mean the relay owner, not a literal id.
                    hit = next(iter(_allowed_users()), "")
                    if hit:
                        uid = hit
                hit = next((k for k, v in self._names.items()
                            if v.lower() == lookup), "")
                if not hit:
                    hit = next((u for u, n in _user_callers().items()
                                if n.lower() in
                                (lookup, f"user-{lookup}")), "")
                if not hit:
                    # LINE_PERSON_ALIASES="tony=user-tonezzzz,kk=user-kk"
                    aliases = dict(p.split("=", 1) for p in
                                   os.environ.get("LINE_PERSON_ALIASES",
                                                  "").split(",") if "=" in p)
                    target = aliases.get(lookup, "")
                    if target:
                        hit = next((u for u, n in _user_callers().items()
                                    if n.lower() == target.lower()), "")
                if hit:
                    uid = hit
            inst = str(body.get("instance") or "").strip()
            if not uid and inst:
                hit = self._recent_chat.get(inst)
                if hit and time.monotonic() - hit[1] < 600:
                    uid = hit[0]
            if not uid and inst:
                uid = _lane_env_map("LINE_LANE_DEFAULT").get(inst, "")
            if not uid:
                uid = next(iter(_allowed_users()), "")
            if not uid:
                return JSONResponse({"ok": False,
                                     "error": "no target"}, status_code=400)
            # Lane lock — a locked lane ALWAYS lands in its default room;
            # any resolved target (to=owner, display names, stray ids) is
            # rewritten instead of refused, so nothing silently dies.
            locked = {x.strip() for x in
                      os.environ.get("LINE_LANE_LOCK", "").split(",")
                      if x.strip()}
            if inst in locked:
                forced = (_lane_env_map("LINE_LANE_DEFAULT").get(inst)
                          or "")
                if forced and uid != forced:
                    logger.info("lane lock: %s send to %s rewritten -> %s",
                                inst, uid, forced)
                    uid = forced
            # Hard gate — LINE_LANE_ALLOW scopes a lane's reachable rooms:
            # a member lane can never address a chat outside its set,
            # explicit to= included (kk never reaches tony's DM).
            allow = _lane_env_map("LINE_LANE_ALLOW")
            allowed_set = {c.strip() for c in
                           allow.get(inst, "").split("+") if c.strip()}
            if inst in allow and uid not in allowed_set:
                logger.warning("send denied: instance %s -> %s not in "
                               "LINE_LANE_ALLOW", inst, uid)
                return JSONResponse(
                    {"ok": False,
                     "error": f"target {uid} outside lane allowlist"},
                    status_code=403)
            msgs: list[dict] = []
            if body.get("text"):
                text = str(body["text"])
                # Who's-talking label: queued sends carry no persona tag
                # unlike turn replies — prefix [instance] on room targets.
                if inst and uid.startswith("C"):
                    text = f"[{inst}] " + text
                msgs += [{"type": "text", "text": text[:4900]}]
            if body.get("image_url"):
                msgs += await self._image_msgs(
                    str(body["image_url"]), str(body.get("caption") or ""))
            if body.get("audio_b64"):
                msgs += self._audio_msgs(
                    str(body["audio_b64"]),
                    int(body.get("duration_ms") or 0))
            if not msgs:
                return JSONResponse({"ok": False,
                                     "error": "nothing to send"},
                                    status_code=400)
            try:
                await self._deliver(uid, msgs[:5], None)
                return JSONResponse({"ok": True, "to": uid})
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)},
                                    status_code=502)

        async def img(request):
            from starlette.responses import Response
            nonce = request.path_params.get("nonce", "")
            hit = self._imgs.get(nonce)
            if not hit:
                hit = self._img_load(nonce)  # post-restart disk fallback
            ua = request.headers.get("user-agent", "")[:60]
            if not hit:
                logger.warning("relay-img MISS %s (ua=%s)", nonce, ua)
                return JSONResponse({"ok": False}, status_code=404)
            data, _ts, mime = (hit + ("image/jpeg",))[:3] \
                if len(hit) == 2 else hit
            logger.info("relay-img serve %s %dB %s (ua=%s)",
                        nonce, len(data), mime, ua)
            return Response(data, media_type=mime)

        async def arch_img(request):
            """Serve archived inbound media -- durable URLs the conv
            reports + resends reference. Path is <chat>/<file>; same
            unguessable-address trust level as the nonce lane."""
            from starlette.responses import Response
            rest = request.path_params.get("rest", "")
            if ".." in rest.split("/"):
                return JSONResponse({"ok": False}, status_code=400)
            f = MEDIA_ARCHIVE_DIR / rest
            if not f.is_file():
                return JSONResponse({"ok": False}, status_code=404)
            import mimetypes
            mime = mimetypes.guess_type(f.name)[0] or "image/jpeg"
            return Response(f.read_bytes(), media_type=mime)

        app = Starlette(routes=[
            Route(LINE_PATH, webhook, methods=["POST"]),
            Route("/webhook/line-health", health, methods=["GET"]),
            Route("/send", send, methods=["POST"]),
            Route("/relay-img/{nonce}", img, methods=["GET"]),
            Route("/relay-img/arch/{rest:path}", arch_img,
                  methods=["GET"]),
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
