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
    LINE_GROUP_PERSONA         — "<groupId>:<persona>,..." hard lane pin:
                                 every message in that room routes to its
                                 persona; mentions of other personas get a
                                 "isn't on this lane" reply instead of a
                                 foreign backend turn
    LINE_IMAGE_CAPTION_WAIT_S  — default 8: hold an image turn this long
                                 for a following text from the same sender
                                 routed to the same persona, then merge it
                                 in as the photo caption (one reply)
    LINE_PARKED_DIR            — default ~/.local/share/ada/line-parked;
                                 outbound msgs that hit LINE push quota
                                 (429) park here and ride the next free
                                 replyToken on that chats next inbound
                                 (OPS-0651)
    LINE_BULK_LANES            — comma list of /send `instance` values
                                 forced to class=bulk regardless of the
                                 body's class field
    LINE_BULK_COALESCE_S       — default 300: class=bulk /send payloads
                                 park durably, then flush as ONE bundled
                                 push after this debounce — adjacent text
                                 merges so N caller msgs can cost 1 quota
                                 message; an inbound replyToken in the
                                 window delivers them free instead
    LINE_QUOTA_RETRY_S         — default 21600 (6h): after a confirmed 429
                                 'monthly limit', bulk flushes skip the
                                 LINE call for this long (msgs stay
                                 parked); urgent pushes still attempt and
                                 a 200 clears the block early
    LINE_VOICE_CORPUS_DIR      — default ~/.local/share/ada-voice-corpus/line;
                                 audio clips from enrolled senders land here
                                 (local only — like tg_relay's voice rule)
    LINE_VOICE_TRANSCRIBE      — "1" transcribes clips (faster-whisper) and
                                 routes the transcript like a text turn
    LINE_WHISPER_MODEL         — default base
    LINE_VOICE_ENROLL          — default 1: an unknown speaker on a
                                 caller-mapped lane arms a pending
                                 enrollment — the next
                                 LINE_VOICE_ENROLL_CLIPS clips become
                                 voiceprints under the caller's person
    LINE_VOICE_ENROLL_CLIPS    — default 3 prints to complete enrollment
    LINE_VOICE_ENROLL_TTL_S    — default 900s before a pending enrollment
                                 expires
    LINE_VOICE_MATCH           — default 1: score every saved clip with
                                 speaker_id.match_file; the persona's
                                 turn gets a [voice: speaker=..] context
                                 line
    LINE_VOICE_DM_MIRROR       — default 1: group voice ack/transcript is
                                 also pushed to the speaker's DM lane
    LINE_WEBHOOK_LISTEN        — host:port, default 127.0.0.1:8912
    LINE_WEBHOOK_PATH          — default /webhook/line
    LINE_TEXT_IMG_MINE         — default 1: image-bearing links inside a
                                 reply or /send text (Drive share links,
                                 photo URLs, /v1/drive/media/ and
                                 /relay-img/ paths) are upgraded to real
                                 image messages — the link also stays in
                                 the text; failures stay silent
    DOC_ARCHIVE_URL / DOC_ARCHIVE_API_KEY — the doc-archive service mints
                                 /v1/drive/media/<id>?t= fetch URLs, so a
                                 pasted drive.google.com share link can be
                                 delivered as an actual image
    ADA_WS_URL / ADA_API_KEY / ADA_KEYS_FILE — same as tg_relay

Replies go out via /message/reply using the event's replyToken (free, but
single-use and short-lived); if that fails the relay falls back to
/message/push so the user still gets Ada's answer.

Reply-context binding (card line-tool-reply-routing): every backend
session opens with &chat_id=<originating chat> so a chat_send the
persona fires mid-turn lands in the chat that asked — not the lane
default room or the owner's DM. Explicit to= still wins, and the
LINE_LANE_ALLOW/LOCK gates vet the bound target like any other.
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

# LINE has no native photo caption: users send the photo, then type the
# caption as the next message. Holding an image turn briefly lets a
# following text from the same sender merge in as its caption — one turn,
# one reply (card ada-line-caption-coalesce).
ADA_API_KEY = os.environ.get("ADA_API_KEY", "")
IDLE_SESSION_S = float(os.environ.get("LINE_IDLE_SESSION_S", "600"))

LINE_LISTEN = os.environ.get("LINE_WEBHOOK_LISTEN", "127.0.0.1:8912")
LINE_PATH = os.environ.get("LINE_WEBHOOK_PATH", "/webhook/line")
REPLY_CHUNK = 4900  # LINE text message limit is 5000 chars

LINE_IMAGE_CAPTION_WAIT_S = float(
    os.environ.get("LINE_IMAGE_CAPTION_WAIT_S", "8"))
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

# doc-archive mints short-lived /v1/drive/media/<id>?t=<tok> fetch URLs —
# that is how a pasted Drive share link (webViewLink is an HTML viewer,
# not bytes) becomes an image LINE can actually deliver. Same env pair
# doc_archive_client uses; absent key = share links stay plain text.
DOC_ARCHIVE_URL = os.environ.get(
    "DOC_ARCHIVE_URL", "http://idc03.taila0626a.ts.net:11025").rstrip("/")
DOC_ARCHIVE_KEY = os.environ.get("DOC_ARCHIVE_API_KEY", "")

# A Drive share link in any of the common spellings -> file id. Covers
# file/d/, <app>/d/ (docs/slides) and the ?id= query forms (open, uc).
_DRIVE_LINK_RE = re.compile(
    r"(?:drive|docs)\.google\.com/(?:file/d/|[a-z-]+/d/)"
    r"([A-Za-z0-9_-]{15,})"
    r"|(?:drive|docs)\.google\.com/\w*\?[^\s\"']*?\bid="
    r"([A-Za-z0-9_-]{15,})")

# URLs inside reply/send text worth upgrading to real image bubbles.
_TEXT_URL_RE = re.compile(
    r"(?:https?://|/v1/drive/media/)[^\s<>\)\]\}\"'“”]+")
_IMG_URL_TAIL = re.compile(r"\.(?:jpe?g|png|webp|gif|heic)$", re.I)

# Voice corpus: audio clips from enrolled senders (allowed DM users /
# mapped room members — the same gate a reply sits behind) are saved for
# speaker-id training. Private by design: local disk only, no forwarding.
OPS_EVENTS_FILE = Path(os.environ.get(
    "LINE_OPS_EVENTS", "~/.local/share/ada/line-media/ops-events.jsonl")
    ).expanduser()

MEDIA_ARCHIVE_DIR = Path(os.environ.get(
    "LINE_MEDIA_ARCHIVE_DIR",
    os.path.expanduser("~/.local/share/ada/line-media")))

VOICE_CORPUS_DIR = Path(os.environ.get(
    "LINE_VOICE_CORPUS_DIR",
    str(Path.home() / ".local" / "share" / "ada-voice-corpus" / "line")))

PARKED_DIR = Path(os.environ.get(
    "LINE_PARKED_DIR",
    str(Path.home() / ".local" / "share" / "ada" / "line-parked")))

# Push classes (card linepush-quota-audit): LINE meters *messages* on
# push — replies are free. class=urgent payloads always attempt the push
# API; class=bulk payloads park first and flush bundled after
# LINE_BULK_COALESCE_S (or free, on the chat's next replyToken). A
# confirmed 429 monthly-limit sets _quota_blocked for LINE_QUOTA_RETRY_S
# so a spent month stops generating doomed bulk calls.
LINE_BULK_COALESCE_S = float(os.environ.get("LINE_BULK_COALESCE_S", "300"))
LINE_QUOTA_RETRY_S = float(os.environ.get("LINE_QUOTA_RETRY_S", "21600"))


def _bulk_lanes() -> set[str]:
    return {x.strip() for x in
            os.environ.get("LINE_BULK_LANES", "").split(",") if x.strip()}


def _is_monthly_limit(resp: httpx.Response) -> bool:
    """429 'monthly limit' (plan cap) vs a transient rate-limit 429."""
    return resp.status_code == 429 and \
        "monthly" in (resp.text or "").lower()


def _push_class(body: dict) -> str:
    """Resolve a /send payload's push class: body class=|priority=
    'bulk' marks quota-optional traffic (digests, rollups, watch
    deltas); LINE_BULK_LANES forces an instance's sends bulk. Anything
    else is urgent — it always attempts the push API."""
    cls = str(body.get("class") or body.get("priority")
              or "urgent").strip().lower()
    inst = str(body.get("instance") or "").strip()
    if cls != "bulk" and inst in _bulk_lanes():
        cls = "bulk"
    return "bulk" if cls == "bulk" else "urgent"


def _merge_msgs(msgs: list[dict], cap: int = 5
                ) -> tuple[list[dict], list[dict]]:
    """LINE allows <=5 bubbles per call — merge adjacent text bubbles
    (<=4900 chars each) and return (send, overflow)."""
    out: list[dict] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append({"type": "text", "text": "\n\n".join(buf)[:4900]})
            buf.clear()

    for m in msgs:
        if m.get("type") == "text":
            t = str(m.get("text") or "")
            if buf and sum(len(x) + 2 for x in buf) + len(t) > 4900:
                flush()
            buf.append(t)
        else:
            flush()
            out.append(m)
    flush()
    return out[:cap], out[cap:]


def _parked_path(chat_id: str) -> Path:
    return PARKED_DIR / (re.sub(r"[^A-Za-z0-9_-]", "_", chat_id) + ".jsonl")


def _parked_add(chat_id: str, msgs: list[dict], front: bool = False) -> None:
    """Park outbound msgs for chat_id (one JSON msg per line). front=True
    restores previously-taken items ahead of anything still queued."""
    if not msgs:
        return
    try:
        PARKED_DIR.mkdir(parents=True, exist_ok=True)
        p = _parked_path(chat_id)
        lines = [json.dumps(m, ensure_ascii=False) for m in msgs]
        if front:
            if p.exists():
                lines += p.read_text().splitlines()
            p.write_text("\n".join(lines) + "\n")
        else:
            with p.open("a") as fh:
                fh.write("\n".join(lines) + "\n")
    except Exception:
        logger.exception("parked-write failed for %s", chat_id)


def _parked_take(chat_id: str) -> list[dict]:
    p = _parked_path(chat_id)
    if not p.exists():
        return []
    try:
        msgs = [json.loads(l) for l in p.read_text().splitlines()
                if l.strip()]
        p.unlink()
        return msgs
    except Exception:
        logger.exception("parked-read failed for %s", chat_id)
        return []

_MIME_EXT = {
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-m4a": ".m4a",
    "video/mp4": ".mp4",
    # Images archived via _archive_media get a real extension so
    # doc_read resolves the actual mime, not octet-stream.
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    # Inbound file messages (tickets, contracts, exports) — the archive
    # extension follows the declared mime so doc_read and humans see the
    # real format, not .bin.
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/csv": ".csv",
    "text/plain": ".txt",
}

# Inbound message types whose bytes come back from the api-data content
# endpoint besides image/audio (which have their own branches) — LINE
# 'file' documents (PDF tickets, sheets) and 'video' clips. sticker,
# location, imagemap and template carry no fetchable blob.
_MEDIA_INBOUND = ("file", "video")

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



def _ops_event(code: str, severity: str = "warn", **fields) -> None:
    """Structured ops event -> ops-events.jsonl for the WP ops
    timeline/watchdog. Cheap fields only (no subprocess, no I/O probes)
    — enrichment is the monitor's job; a failure here must never break
    message flow."""
    try:
        OPS_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "kind": "ops", "code": code, "severity": severity,
               "unit": "ada-line-relay", **fields}
        with OPS_EVENTS_FILE.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


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


_FOREIGN = "__foreign__"


def _group_persona() -> dict[str, str]:
    """LINE_GROUP_PERSONA="C4fb..:chaba,..." -> {room/group id: persona}.
    A mapped room is hard-pinned to one lane: every message routes to
    that persona; mentions of other personas are answered with a
    not-on-this-lane reply, never a foreign backend turn."""
    out: dict[str, str] = {}
    for part in os.environ.get("LINE_GROUP_PERSONA", "").split(","):
        if ":" in part:
            room, name = part.split(":", 1)
            room, name = room.strip(), name.strip().lower()
            if room and name:
                out[room] = name
    return out


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
    room_persona = _group_persona().get(room) if is_group else None
    if room_persona:
        mentioned = _mention_persona(text)
        if mentioned and mentioned != room_persona:
            return _FOREIGN, mentioned
        return room_persona, _strip_lead_mention(text, mentioned)
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

# Enroll-on-clips (card line-voice-enroll-recognize): after an unknown
# speaker's clip, the next VOICE_ENROLL_CLIPS clips from the same caller
# become voiceprints — the "send me 2-3 short clips" flow.
VOICE_ENROLL_CLIPS = int(os.environ.get("LINE_VOICE_ENROLL_CLIPS", "3"))
VOICE_ENROLL_TTL_S = float(os.environ.get("LINE_VOICE_ENROLL_TTL_S", "900"))


def _speaker_id() -> Any:
    """Shared ECAPA voiceprint store — lazy like _transcribe so the
    relay still starts when speechbrain/model files are missing."""
    from backend.speaker_id import SpeakerIdentifier
    return SpeakerIdentifier.get()


def _match_voice_file(path) -> dict:
    """speaker_id.match_file over a saved clip; {} on any failure —
    voice recognition must never break the archive/turn path."""
    try:
        return _speaker_id().match_file(str(path)) or {}
    except Exception as exc:
        logger.info("voice match %s failed: %s", path, exc)
        return {}


def _enroll_voice_file(path, name, person=None) -> dict:
    """speaker_id.enroll_from_file; {'error': ...} on failure."""
    try:
        return _speaker_id().enroll_from_file(
            str(path), name, person=person) or {}
    except Exception as exc:
        logger.info("voice enroll %s as %s failed: %s", path, name, exc)
        return {"error": str(exc),
                "matched_profile": getattr(exc, "matched", None)}


def _corpus_slug(caller: str | None, label: str, user_id: str) -> str:
    """The voice-corpus subdirectory for a sender — same rule
    _save_voice_clip uses, shared with the enroll-pending map."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-",
                  caller or label or user_id).strip("-").lower() \
        or "unknown"


def _caller_person(caller: str | None) -> str | None:
    """LINE_USER_CALLERS key -> person.* entity guess ('user-kk' ->
    'person.kk'). The profile enroll path normalizes the same way."""
    if not caller:
        return None
    slug = re.sub(r"^user-", "", caller).strip()
    return f"person.{slug}" if slug else None


def _voice_context(match: dict, pending: dict | None = None,
                   enrolled: str | None = None) -> str:
    """'[voice: speaker=KK score=0.62 person=person.kk]' — the turn
    context line so the persona names the speaker and can route
    person_entity -> scoped bank. 'speaker=unknown' plus an armed
    pending enrollment invites the 2-3-clip enroll offer."""
    if enrolled:
        return (f"[voice: speaker={enrolled} just-enrolled "
                f"prints>={VOICE_ENROLL_CLIPS}]")
    name = match.get("match")
    if name:
        ctx = (f"[voice: speaker={name} "
               f"score={match.get('score', 0):.2f}")
        if match.get("ha_person"):
            ctx += f" person={match['ha_person']}"
        return ctx + "]"
    best = match.get("best")
    ctx = f"[voice: speaker=unknown score={match.get('score', 0):.2f}"
    if best:
        ctx += f" best={best}@{match.get('best_score', 0):.2f}"
    if pending:
        ctx += f" enroll-armed:{pending['got']}/{VOICE_ENROLL_CLIPS}"
    return ctx + "]"


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


def _images_from(results: list[dict]) -> list[tuple[str, str, str]]:
    """(url, caption, kind) triples found in tool_result payloads — a
    camera snapshot's cast_url turns into an image message in the chat;
    a render's video_url becomes a LINE videoMessage."""
    out: list[tuple[str, str, str]] = []
    for res in results:
        if not isinstance(res, dict):
            continue
        vid = res.get("video_url")
        if isinstance(vid, str) and vid.startswith(("http", "/v1/")):
            cap = str(res.get("channel") or res.get("camera")
                      or res.get("title") or "")
            out.append((vid, cap, "video"))
            continue
        url = res.get("cast_url") or res.get("image_url") or res.get("url")
        # drive_get mints media_url for binary files — an image candidate
        # only when the file's own mime says so (a PDF's media_url is no
        # photo). It comes back host-relative (/v1/drive/media/...).
        if not url and str(res.get("mimeType") or "").startswith("image/"):
            url = res.get("media_url")
        if not (isinstance(url, str) and (url.startswith("http")
                                        or url.startswith("/v1/"))):
            continue
        cap = str(res.get("channel") or res.get("camera")
                  or res.get("title") or "")
        out.append((url, cap, "image"))
    return out


def _mine_image_urls(text: str, limit: int = 3) -> list[str]:
    """Image-bearing links inside a reply or /send text — the model often
    narrates a photo link instead of attaching it (Drive share link, a
    .jpg URL, an archived /relay-img/ or minted /v1/drive/media/ path).
    Anything that doesn't look like image bytes stays plain text."""
    out: list[str] = []
    for m in _TEXT_URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?)}")
        if u in out:
            continue
        path = u.split("?", 1)[0].split("#", 1)[0]
        if (_IMG_URL_TAIL.search(path) or _DRIVE_LINK_RE.search(u)
                or "/v1/drive/media/" in u or "/relay-img/" in u):
            out.append(u)
            if len(out) >= limit:
                break
    return out


class ChatSession:
    """One persona WS session per (chat, sender, persona) — in a group each
    member gets their own caller key and their own session per persona."""

    def __init__(self, user_id: str, relay: "LineRelay",
                 ws_url: str | None = None,
                 caller: str | None = None,
                 chat_id: str = "") -> None:
        self.user_id = user_id
        self.ws_url = ws_url or ADA_WS_URL
        self.caller = caller  # LINE_USER_CALLERS name; None -> default key
        # Originating chat (room/groupId or DM userId) — handed to the
        # backend at connect so a tool send inside a turn replies in the
        # chat the turn came from (card line-tool-reply-routing).
        self.chat_id = chat_id
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
        if self.chat_id:
            import urllib.parse
            url += "&chat_id=" + urllib.parse.quote(self.chat_id, safe="")
        # ada-text-mode-sessions: LINE_TEXT_MODE routes text chats to the
        # REST text provider — no Live session, metered per call.
        if os.environ.get("LINE_TEXT_MODE", "").strip().lower() in (
                "1", "true", "yes"):
            url += "&mode=text"
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

    async def send_turn(self, text: str) -> tuple[str, list[tuple[str, str, str]]]:
        """Send one user turn; returns (transcript, image urls+captions)."""
        return await self._send_and_collect({"type": "text", "text": text})

    async def send_image_turn(
            self, img: bytes, mime: str, caption: str
    ) -> tuple[str, list[tuple[str, str, str]]]:
        """Forward a user photo into the session as a real image turn —
        Ada sees the picture (her describe path), replies with text."""
        return await self._send_and_collect({
            "type": "image",
            "data": base64.b64encode(img).decode(),
            "mime": mime,
            "text": caption,
        })

    async def _send_and_collect(
            self, payload: dict
            ) -> tuple[str, list[tuple[str, str, str]]]:
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
        # In-flight image turns waiting out the caption window:
        # (chat_id, user_id) -> {ev, caption, reply_token, persona}
        self._img_pending: dict[tuple[str, str], dict] = {}
        # Pending enroll-on-clips: corpus slug -> {name, person, got,
        # armed_at}. Armed when an unknown speaker's clip arrives on a
        # caller-mapped lane; each later clip adds one print until
        # VOICE_ENROLL_CLIPS land or the TTL lapses.
        self._enroll_pending: dict[str, dict] = {}
        # Quota-depleted epoch: a 429 'monthly limit' sets this to
        # now+LINE_QUOTA_RETRY_S; bulk sends park silently under it and a
        # successful push clears it (recovery probe = urgent traffic).
        self._quota_blocked = 0.0
        # Per-chat debounce tasks flushing parked class=bulk payloads.
        self._bulk_flush: dict[str, asyncio.Task] = {}

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
                       kind: str, chat_id: str, label: str,
                       persona: str | None = None,
                       caller: str | None = None,
                       file_name: str = "",
                       size: int | None = None) -> str:
        """Persist inbound media bytes under
        <archive>/<chat>/<messageId>.<ext> + manifest line, and return a
        durable public /relay-img/arch/ URL -- received media outlives
        LINE's content retention and stays resendable. Mirrors the
        voice-corpus scheme. The manifest line carries sha256 + persona
        for the media-inbox generator's dedup/lane attribution
        (docs/kb/media-inbox-standard.md)."""
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
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": size if size is not None else len(data),
                    "file_name": file_name or None,
                    "persona": persona or None,
                    "caller": caller or None,
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
                       reply_token: str | None = None,
                       cls: str = "urgent") -> str:
        """Deliver msgs to a chat. Returns 'delivered' | 'parked' |
        'failed'. cls='bulk' never burns a quota call while the monthly
        block is known-set — it parks for the next free replyToken."""
        logger.info("deliver to %s: [%s]",
                    user_id, ",".join(m.get("type", "?") for m in msgs[:5]))
        parked: list[dict] = []
        if reply_token:
            # OPS-0651: outbound msgs parked on push-quota ride this free
            # reply call — one token, one call, max 5 bubbles.
            parked = _parked_take(user_id)
            send, overflow = _merge_msgs(parked + msgs)
            try:
                r = await self.line("reply", {
                    "replyToken": reply_token, "messages": send})
                if r.status_code == 200:
                    if overflow:
                        _parked_add(user_id, overflow)
                        logger.info("deliver to %s: %d msgs re-parked "
                                    "(5-bubble reply cap)",
                                    user_id, len(overflow))
                    if parked:
                        logger.info("deliver to %s: flushed %d parked msgs",
                                    user_id, len(parked))
                        _ops_event("parked_flushed", "info", chat_id=user_id,
                                   n=len(parked))
                    return "delivered"
                logger.info("replyToken failed (%s) — falling back to push",
                            r.status_code)
                _ops_event("reply_failed", "warn", chat_id=user_id,
                           http=r.status_code, detail="fell back to push")
            except Exception as exc:
                logger.info("reply call failed (%s) — falling back to push",
                            exc)
            # Nothing was sent — the parked batch goes back untouched; only
            # the current payload proceeds to the push path.
            _parked_add(user_id, parked, front=True)
        if cls == "bulk" and self._quota_blocked > time.time():
            # Spent month: don't burn a doomed API call — the next inbound
            # replyToken (or quota reset) flushes the queue for free.
            _parked_add(user_id, msgs)
            logger.info("bulk push to %s parked — quota depleted "
                        "(%d msgs)", user_id, len(msgs))
            return "parked"
        r = await self.line("push", {"to": user_id, "messages": msgs[:5]})
        req_id = (getattr(r, "headers", None) or {}).get(
            "x-line-request-id", "")
        if r.status_code == 200:
            if self._quota_blocked:
                self._quota_blocked = 0.0
                logger.info("push to %s landed — monthly quota block "
                            "cleared (req %s)", user_id, req_id)
                _ops_event("quota_recovered", "info", chat_id=user_id,
                           req_id=req_id)
            return "delivered"
        if r.status_code == 429:
            _parked_add(user_id, msgs)
            if _is_monthly_limit(r):
                if not self._quota_blocked:
                    asyncio.create_task(self._quota_alert(user_id))
                self._quota_blocked = time.time() + LINE_QUOTA_RETRY_S
            logger.warning("push to %s quota-blocked — parked %d msgs "
                           "for the next inbound (%s, req %s)",
                           user_id, len(msgs), r.text[:120], req_id)
            _ops_event("push_parked", "warn", chat_id=user_id,
                       http=r.status_code, detail=r.text[:200],
                       req_id=req_id, cls=cls)
            return "parked"
        logger.warning("push to %s failed: %s %s (req %s)",
                       user_id, r.status_code, r.text[:200], req_id)
        _ops_event("push_failed", "error", chat_id=user_id,
                   http=r.status_code, detail=r.text[:200], req_id=req_id)
        return "failed"

    async def _quota_probe(self) -> dict:
        """GET /v2/bot/message/quota + /quota/consumption — free reads,
        no quota cost. {limit, used, remaining} (limit None = unlimited
        plan)."""
        out: dict[str, Any] = {}
        if self.http is None:
            return {"error": "http not up"}
        try:
            h = {"Authorization": f"Bearer {CHANNEL_TOKEN}"}
            r = await self.http.get(f"{LINE_API}/v2/bot/message/quota",
                                    headers=h)
            if r.status_code == 200:
                q = r.json() or {}
                out["limit"] = q.get("value") \
                    if q.get("type") == "limited" else None
            else:
                out["quota_http"] = r.status_code
            r = await self.http.get(
                f"{LINE_API}/v2/bot/message/quota/consumption",
                headers=h)
            if r.status_code == 200:
                out["used"] = (r.json() or {}).get("totalUsage")
            else:
                out["consumption_http"] = r.status_code
        except Exception as exc:
            out["error"] = str(exc)[:120]
        if "limit" in out and "used" in out:
            try:
                out["remaining"] = (out["limit"] or 0) - (out["used"] or 0)
            except TypeError:
                pass
        return out

    async def _quota_alert(self, chat_id: str) -> None:
        """The 'quota depleted' alarm — deliberately NOT a LINE push (that
        would burn the very resource it reports). The ops event lands on
        the WP ops timeline / board comms instead."""
        q = await self._quota_probe()
        logger.error(
            "LINE monthly push quota depleted (used=%s/%s) — bulk "
            "pushes park until reset or a successful send",
            q.get("used"), q.get("limit"))
        _ops_event("quota_depleted", "error", chat_id=chat_id, **q)

    def _bulk_arm(self, chat_id: str) -> None:
        """Schedule the debounced bundled flush of this chat's parked
        bulk msgs. One task per chat — a second send inside the window
        just joins the bundle already queued on disk."""
        t = self._bulk_flush.get(chat_id)
        if t is not None and not t.done():
            return
        self._bulk_flush[chat_id] = asyncio.create_task(
            self._bulk_flush_later(chat_id))

    async def _bulk_flush_later(self, chat_id: str) -> None:
        try:
            await asyncio.sleep(LINE_BULK_COALESCE_S)
            if self._quota_blocked > time.time():
                return  # stays parked — inbound replyTokens flush free
            msgs = _parked_take(chat_id)
            if not msgs:
                return
            # Bundle: adjacent texts merge into one bubble, so N parked
            # caller msgs can leave as a single quota message.
            send, overflow = _merge_msgs(msgs)
            await self._deliver(chat_id, send, None, cls="bulk")
            if overflow:
                _parked_add(chat_id, overflow)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("bulk flush %s failed", chat_id)

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

    async def _resolve_doc_media(self, url: str) -> str:
        """Turn non-byte references into fetchable image URLs: Drive
        share links (drive.google.com/file/d/.., open?id=, uc?id=, docs
        /d/) go through doc-archive's media-token mint (the relay env
        carries DOC_ARCHIVE_API_KEY); host-relative /v1/drive/media/
        paths belong to DOC_ARCHIVE_URL, not LINE_URL_BASE."""
        if url.startswith("/v1/drive/media/"):
            return DOC_ARCHIVE_URL + url
        m = _DRIVE_LINK_RE.search(url)
        if not m:
            return url
        if not DOC_ARCHIVE_KEY:
            raise RuntimeError("drive share link but DOC_ARCHIVE_API_KEY "
                               "is unset — cannot mint a media URL")
        fid = next(g for g in m.groups() if g)
        r = await self.http.post(
            f"{DOC_ARCHIVE_URL}/v1/drive/media-token",
            json={"file_id": fid},
            headers={"X-API-Key": DOC_ARCHIVE_KEY}, timeout=15)
        r.raise_for_status()
        minted = str(r.json().get("url") or "")
        if not minted:
            raise RuntimeError("media-token mint returned no url")
        return DOC_ARCHIVE_URL + minted

    async def _mined_msgs(self, text: str, seen: set[str]) -> list[dict]:
        """Image links left inside the reply/send text still go out as
        image bubbles — mined candidates fail silent (the text link stays
        either way). LINE_TEXT_IMG_MINE=0 disables."""
        if os.environ.get("LINE_TEXT_IMG_MINE", "1") == "0":
            return []
        out: list[dict] = []
        for url in _mine_image_urls(text):
            if url in seen:
                continue
            seen.add(url)
            out.extend(await self._image_msgs(url, "", silent=True))
        return out

    @staticmethod
    def _video_poster(mp4: bytes) -> bytes | None:
        """First video frame -> small JPEG for LINE's required
        previewImageUrl. PyAV is already a dep (audio transcode)."""
        try:
            import io
            import av
            from PIL import Image
            container = av.open(io.BytesIO(mp4))
            frame = next(container.decode(video=0), None)
            container.close()
            if frame is None:
                return None
            im = frame.to_image()
            im.thumbnail((240, 240))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=70)
            return buf.getvalue()
        except Exception as exc:
            logger.info("video poster extract failed: %s", exc)
            return None

    async def _video_msgs(self, url: str, caption: str,
                          silent: bool = False) -> list[dict]:
        """Fetch the clip, host mp4 + poster publicly, return a LINE
        videoMessage. Falls back to a text link on any failure — the
        clip URL is still usable."""
        assert self.http is not None
        try:
            url = await self._resolve_doc_media(url)
            if url.startswith("/"):
                m = re.search(r"https?://\S+", url)
                if m:
                    url = m.group(0)
                else:
                    base = os.environ.get(
                        "LINE_URL_BASE", "http://100.102.134.91:11023")
                    url = base.rstrip("/") + url
            r = await self.http.get(url, timeout=240,
                                    follow_redirects=True)
            if r.status_code != 200 or len(r.content) < 1000:
                raise RuntimeError(f"fetch {r.status_code} {len(r.content)}B")
            if len(r.content) > 190 * 1024 * 1024:
                raise RuntimeError("clip exceeds LINE's video limit")
            mime = (r.headers.get("content-type")
                    or "video/mp4").split(";")[0]
            if not mime.startswith("video/") and                     b"ftyp" not in r.content[:32]:
                raise RuntimeError(
                    f"fetched {mime}, not a video ({len(r.content)}B)")
            if not mime.startswith("video/"):
                mime = "video/mp4"
            poster = await asyncio.to_thread(
                self._video_poster, r.content)
            if not poster:
                raise RuntimeError("no poster frame extracted")
            import secrets
            nonce = secrets.token_urlsafe(12)
            self._imgs[nonce] = (r.content, time.time(), mime)
            pnonce = nonce + "-p"
            self._imgs[pnonce] = (poster, time.time(), "image/jpeg")
            self._img_persist(nonce, r.content, mime)
            self._img_persist(pnonce, poster, "image/jpeg")
            msgs: list[dict] = [{"type": "video",
                                 "originalContentUrl":
                                     f"{LINE_PUBLIC_IMG_BASE}/{nonce}",
                                 "previewImageUrl":
                                     f"{LINE_PUBLIC_IMG_BASE}/{pnonce}"}]
            if caption:
                msgs.append({"type": "text", "text": caption[:4900]})
            return msgs
        except Exception as exc:
            logger.warning("video %s failed: %s", url, exc)
            if silent:
                return []
            return [{"type": "text",
                     "text": f"(video: {caption or url})"}]

    async def _media_msgs(self, url: str, caption: str, kind: str
                          ) -> list[dict]:
        if kind == "video":
            return await self._video_msgs(url, caption)
        return await self._image_msgs(url, caption)

    async def _image_msgs(self, url: str, caption: str,
                          silent: bool = False) -> list[dict]:
        """Fetch the frame, host it publicly, return LINE image messages.
        The served content-type must match the real bytes — LINE renders
        a blank image when a JPEG thumb is labelled image/png.
        silent=True is for text-mined candidates: on failure return []
        (the link is still sitting in the text message)."""
        assert self.http is not None
        try:
            url = await self._resolve_doc_media(url)
            # CMS docs embed site-relative paths (/api/documents/x.jpg) —
            # resolve them against LINE_URL_BASE so lanes can send them
            # verbatim. Absolute http(s) URLs pass through untouched.
            if url.startswith("/"):
                # Malformed path-embedded URLs first — lanes sometimes
                # emit '/image_url=https://cdn/x.jpg'; recover the inner
                # URL.
                m = re.search(r"https?://\S+", url)
                if m:
                    url = m.group(0)
                else:
                    base = os.environ.get(
                        "LINE_URL_BASE", "http://100.102.134.91:11023")
                    url = base.rstrip("/") + url
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
            if silent:
                return []
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
        last = self._refused.get((chat_id, user_id))
        if last is not None and time.monotonic() - last < _REFUSAL_GAP_S:
            return
        self._refused[(chat_id, user_id)] = time.monotonic()
        await self.send_text(
            chat_id,
            f"Not authorized in this room. LINE userId: {user_id} — the "
            "owner can whitelist it in LINE_USER_CALLERS.",
            reply_token=reply_token)

    def _session(self, skey: Any, user_id: str, persona_url: str,
                 caller: str | None, chat_id: str = "") -> ChatSession:
        sess = self.sessions.get(skey)
        if sess is None or sess.ws_url != persona_url:
            sess = ChatSession(user_id, self, ws_url=persona_url,
                               caller=caller, chat_id=chat_id)
            self.sessions[skey] = sess
        elif chat_id:
            sess.chat_id = chat_id
        return sess

    async def _deliver_turn(self, skey: Any, chat_id: str, user_id: str,
                            persona: str, persona_url: str,
                            caller: str | None, label: str,
                            turn_text: str, is_group: bool,
                            reply_token: str | None) -> None:
        """One text turn to a persona backend; reply tagged
        [persona->sender] in rooms."""
        self._recent_chat[persona] = (chat_id, time.monotonic())
        sess = self._session(skey, user_id, persona_url, caller,
                             chat_id)
        disp = _display_name(persona, turn_text)
        try:
            reply, images = await sess.send_turn(turn_text)
        except Exception as exc:
            logger.warning("user %s turn failed: %s", user_id, exc)
            _ops_event("turn_failed", "error", chat_id=user_id,
                       detail=str(exc)[:200])
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
        for url, cap, kind in images[:3]:
            msgs.extend(await self._media_msgs(url, cap, kind))
        msgs.extend(await self._mined_msgs(
            reply or "", {u for u, _, _ in images}))
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
        slug = _corpus_slug(caller, label, user_id)
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
        loop = asyncio.get_running_loop()
        # Speaker ID over the saved file — the boss-recognition leg
        # (card line-voice-enroll-recognize). Best-effort: {} leaves the
        # clip behaving like it always did.
        match = (await loop.run_in_executor(None, _match_voice_file, clip)
                 if os.environ.get("LINE_VOICE_MATCH", "1") != "0"
                 else {})
        slug = _corpus_slug(caller, label, user_id)
        pend = self._enroll_pending.get(slug)
        if pend and time.monotonic() - pend["armed_at"] \
                > VOICE_ENROLL_TTL_S:
            logger.info("enroll pending for %s expired", slug)
            self._enroll_pending.pop(slug, None)
            pend = None
        enrolled: str | None = None
        enroll_note = ""
        if match.get("match"):
            # A recognized voice closes any open enrollment — the flow
            # learned enough (match == pending name) or a different
            # enrolled speaker was the one talking.
            self._enroll_pending.pop(slug, None)
            pend = None
        elif os.environ.get("LINE_VOICE_ENROLL", "1") == "0":
            pend = None
        elif pend is not None:
            res = await loop.run_in_executor(
                None, _enroll_voice_file, clip, pend["name"],
                pend.get("person"))
            if res.get("error"):
                if res.get("matched_profile"):
                    # Foreign enrolled voice on this caller's clip —
                    # refuse + disarm; never poison another profile.
                    self._enroll_pending.pop(slug, None)
                    pend = None
                    enroll_note = (
                        f" — that clip matched {res['matched_profile']}, "
                        "enrollment stopped")
                # other errors: keep the pending window open
            else:
                pend["got"] = int(res.get("samples") or pend["got"] + 1)
                if pend["got"] >= VOICE_ENROLL_CLIPS:
                    enrolled = str(res.get("name") or pend["name"])
                    self._enroll_pending.pop(slug, None)
                    logger.info("enroll complete: %s (%d prints)",
                                enrolled, pend["got"])
                    pend = None
        elif caller:
            # Unknown voice on a caller-mapped lane — arm the "send me
            # 2-3 short clips" flow; the caller key binds the person.
            pend = {"name": re.sub(r"^user-", "", caller),
                    "person": _caller_person(caller),
                    "got": 0, "armed_at": time.monotonic()}
            self._enroll_pending[slug] = pend
            logger.info("enroll armed for %s (%s)", slug, pend["person"])
        voice_ctx = _voice_context(match, pend, enrolled)
        transcript = await loop.run_in_executor(None, _transcribe, clip)
        if transcript:
            await self.send_text(chat_id, f'\U0001f399 "{transcript}"',
                                 reply_token=reply_token)
            if enrolled:
                await self.send_text(
                    chat_id,
                    f"enrolled {enrolled}, {VOICE_ENROLL_CLIPS} prints")
        elif enrolled:
            await self.send_text(
                chat_id, f"enrolled {enrolled}, {VOICE_ENROLL_CLIPS} "
                "prints",
                reply_token=reply_token)
        elif match.get("match"):
            who = match.get("display_name") or match["match"]
            await self.send_text(
                chat_id, f"Saved {duration}s voice clip — {who} "
                f"({match.get('score', 0):.0%}).",
                reply_token=reply_token)
        elif pend is not None and pend["got"]:
            await self.send_text(
                chat_id, f"Saved {duration}s voice clip — learning "
                f"this voice ({pend['got']}/{VOICE_ENROLL_CLIPS} "
                f"prints{enroll_note}).",
                reply_token=reply_token)
        elif caller:
            await self.send_text(
                chat_id, f"Saved {duration}s voice clip — I don't "
                "recognize this voice yet; send me 2-3 short clips "
                f"and I'll learn it.{enroll_note}",
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
        if persona == _FOREIGN:
            fdisp = _display_name(turn, transcript)
            hdisp = _display_name(
                _group_persona().get(chat_id) or "", transcript)
            await self.send_text(
                chat_id, f"[{fdisp}->{label}] {fdisp} isn't on this "
                         f"lane — {hdisp} is home here.")
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
            f"[LINE group from {label}] {turn} {voice_ctx}"
            if is_group else f"{turn} {voice_ctx}",
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
            _ops_event("group_joined", "info", chat_id=group_id)
            await self.send_text(
                chat_id,
                f"Ada relay here. groupId={group_id} — the owner can "
                "enable this room via LINE_ALLOWED_GROUP_IDS.",
                reply_token=reply_token)
            return
        if is_group and group_id not in _allowed_groups():
            logger.info("ignoring event in unallowed %s %s",
                        src_type, group_id)
            _ops_event("unallowed_group", "warn", chat_id=group_id,
                       detail="event ignored — group not in "
                       "LINE_ALLOWED_GROUP_IDS")
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
            _ops_event("unauthorized_user", "warn", user_id=user_id)
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
        if mtype not in ("text", "image") + _MEDIA_INBOUND:
            if not is_group:
                await self.send_text(
                    chat_id,
                    f"(got {mtype}, text, voice, photos or files only)",
                    reply_token=reply_token)
            return
        if mtype == "text" and not text:
            return
        # Route: groups need a mention (or LINE_DEFAULT_PERSONA), DMs
        # default to ada. Group images/files have no caption — default
        # persona. An unmapped member gets no pin map: their mention
        # must not set the room pin (it would reroute every member's
        # unaddressed follow-ups), and a stranger's chatter must not
        # ride someone else's pin into a refusal.
        persona, turn = _route(
            text if mtype == "text" else "", is_group, chat_id,
            self._room_pin if caller is not None else None)
        if persona is None:
            logger.info("ignored unaddressed %s msg from %s (no persona mention)",
                        src_type, user_id)
            return  # unaddressed room chatter — ignore per spec
        if persona == _FOREIGN:
            fdisp = _display_name(turn, text)
            hdisp = _display_name(
                _group_persona().get(chat_id) or "", text)
            logger.info("foreign persona %s addressed in pinned room %s",
                        turn, chat_id)
            await self.send_text(
                chat_id,
                f"[{fdisp}->{caller or user_id}] {fdisp} isn't on this "
                f"lane — {hdisp} is home here.",
                reply_token=reply_token)
            return
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
        sess = self._session(skey, user_id, persona_url, caller,
                             chat_id)
        await self._mark_read(ev)
        if mtype == "image":
            try:
                img, mime = await self._fetch_content(
                    str(msg.get("id") or ""))
                media_url = self._archive_media(
                    msg, img, mime, "image", chat_id, label,
                    persona=persona, caller=caller, size=len(img))
                self._journal(chat_id, {"dir": "in", "from": label,
                                        "kind": "image",
                                        "message_id": msg.get("id"),
                                        "url": media_url})
                pend = {"ev": asyncio.Event(), "caption": None,
                        "reply_token": None, "persona": persona}
                pkey = (chat_id, user_id)
                old_pend = self._img_pending.get(pkey)
                if old_pend is not None:
                    old_pend["ev"].set()  # superseded — release its window
                self._img_pending[pkey] = pend
                try:
                    await asyncio.wait_for(
                        pend["ev"].wait(), LINE_IMAGE_CAPTION_WAIT_S)
                except asyncio.TimeoutError:
                    pass
                if self._img_pending.get(pkey) is pend:
                    self._img_pending.pop(pkey, None)
                if pend["reply_token"]:
                    reply_token = pend["reply_token"]
                where = (f"[via LINE group from {label}]"
                         if is_group else f"[via LINE from user {user_id}]")
                cap = pend["caption"]
                if cap:
                    turn_cap = (f"{where} Photo caption: {cap} — the photo "
                                f"is attached (media_url: {media_url}); "
                                "answer what they asked.")
                else:
                    turn_cap = (f"{where} The user sent this photo "
                                f"(media_url: {media_url}) — describe it "
                                "and respond naturally.")
                reply, images = await sess.send_image_turn(
                    img, mime, turn_cap)
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
            for url, cap, kind in images[:3]:
                msgs.extend(await self._media_msgs(url, cap, kind))
            msgs.extend(await self._mined_msgs(
                reply or "", {u for u, _, _ in images}))
            try:
                await self._deliver(chat_id, msgs[:5],
                                    reply_token=reply_token)
            except Exception as exc:
                logger.warning("user %s img-reply failed: %s",
                               user_id, exc)
            return
        if mtype in _MEDIA_INBOUND:
            # Inbound document/media (tickets, contracts, clips): archive
            # the bytes under the durable /relay-img/arch/ URL, journal
            # kind=<mtype>, then hand the persona a synthetic text turn
            # naming the file so it can react (doc_read reads the
            # archived file back inline).
            fname = str(msg.get("fileName") or "").strip()
            noun = "document" if mtype == "file" else mtype
            try:
                data, mime = await self._fetch_content(
                    str(msg.get("id") or ""))
                if mime == "application/octet-stream" and fname:
                    import mimetypes
                    mime = mimetypes.guess_type(fname)[0] or mime
                media_url = self._archive_media(
                    msg, data, mime, mtype, chat_id, label,
                    persona=persona, caller=caller, file_name=fname,
                    size=msg.get("fileSize"))
                self._journal(chat_id, {"dir": "in", "from": label,
                                        "kind": mtype,
                                        "message_id": msg.get("id"),
                                        "url": media_url,
                                        "name": fname, "mime": mime,
                                        "size": msg.get("fileSize")})
                where = (f"[via LINE group from {label}]"
                         if is_group else f"[via LINE from user {user_id}]")
                reply, images = await sess.send_turn(
                    f"{where} [{noun} received: {fname or 'unnamed'} "
                    f"{mime} {media_url}] — doc_read can extract its "
                    "contents if the user wants it processed.")
            except Exception as exc:
                logger.warning("user %s %s turn failed: %s",
                               user_id, mtype, exc)
                reply, images = (f"Couldn't pass that {noun} to {disp} — "
                                 "try again."), []
            self._journal(chat_id, {"dir": "out", "from": disp,
                                    "kind": "text",
                                    "text": reply or ""})
            tag = f"[{disp}->{label}] " if is_group else ""
            msgs = [{"type": "text",
                     "text": (tag + (reply or "(no reply)"))
                     [:REPLY_CHUNK]}]
            for url, cap, kind in images[:3]:
                msgs.extend(await self._media_msgs(url, cap, kind))
            msgs.extend(await self._mined_msgs(
                reply or "", {u for u, _, _ in images}))
            try:
                await self._deliver(chat_id, msgs[:5],
                                    reply_token=reply_token)
            except Exception as exc:
                logger.warning("user %s %s-reply failed: %s",
                               user_id, mtype, exc)
            return
        if mtype == "text":
            pend = self._img_pending.get((chat_id, user_id))
            if pend is not None and pend.get("persona") == persona:
                # Caption for an image turn still in its window — merge
                # instead of answering the text on its own.
                pend["caption"] = turn or text
                pend["reply_token"] = reply_token
                pend["ev"].set()
                self._journal(chat_id, {
                    "dir": "in", "from": label, "kind": "text",
                    "message_id": msg.get("id"), "text": text,
                    "merged_into": "image_turn"})
                return
        turn_text = (f"[LINE group from {label}] {turn}"
                     if is_group else turn)
        try:
            reply, images = await sess.send_turn(turn_text)
        except Exception as exc:
            logger.warning("user %s turn failed: %s", user_id, exc)
            _ops_event("turn_failed", "error", chat_id=user_id,
                       detail=str(exc)[:200])
            reply, images = (f"{persona} didn't answer that turn — the "
                             "session dropped. Try again."), []
            self.sessions.pop(skey, None)
        self._journal(chat_id, {"dir": "in", "from": label,
                                "persona": disp, "kind": "text",
                                "message_id": msg.get("id"),
                                "text": turn})
        self._journal(chat_id, {"dir": "out", "from": disp,
                                "kind": "text", "text": reply or ""})
        tag = f"[{disp}->{label}] " if is_group else ""
        msgs = [{"type": "text",
                 "text": (tag + (reply or "(no reply)"))[:REPLY_CHUNK]}]
        for url, cap, kind in images[:3]:
            msgs.extend(await self._media_msgs(url, cap, kind))
        msgs.extend(await self._mined_msgs(
            reply or "", {u for u, _, _ in images}))
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
            (127.0.0.1 listen). {to?, text?, image_url?, caption?,
            class?='urgent'|'bulk', priority? (alias)} — the image is
            fetched + publicly hosted via _image_msgs so LINE's CDN can
            pull it. class='bulk' payloads (digests, rollups, watch
            deltas) park and flush bundled — they don't burn quota
            head-on, and none while the monthly block is set."""
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
            if body.get("video_url"):
                msgs += await self._video_msgs(
                    str(body["video_url"]), str(body.get("caption") or ""))
            msgs += await self._mined_msgs(
                str(body.get("text") or ""),
                {str(body.get("image_url") or ""),
                 str(body.get("video_url") or "")})
            if body.get("audio_b64"):
                msgs += self._audio_msgs(
                    str(body["audio_b64"]),
                    int(body.get("duration_ms") or 0))
            if not msgs:
                return JSONResponse({"ok": False,
                                     "error": "nothing to send"},
                                    status_code=400)
            if inst:
                # Keep the in-context fallback warm under the key this
                # endpoint reads (the payload's ADA_INSTANCE_ID).
                self._recent_chat[inst] = (uid, time.monotonic())
            # Push class (linepush-quota-audit): bulk payloads park
            # durably, flush bundled after LINE_BULK_COALESCE_S, or ride
            # the next inbound replyToken free. Everything else stays
            # urgent and attempts LINE now.
            cls = _push_class(body)
            logger.info("send: inst=%s to=%s class=%s msgs=%d",
                        inst or "-", uid, cls, len(msgs))
            if cls == "bulk":
                _parked_add(uid, msgs[:5])
                if self._quota_blocked > time.time():
                    return JSONResponse({"ok": True, "to": uid,
                                         "parked": True,
                                         "reason": "quota-depleted"})
                self._bulk_arm(uid)
                return JSONResponse({"ok": True, "to": uid,
                                     "parked": True,
                                     "flush_in_s": LINE_BULK_COALESCE_S})
            try:
                st = await self._deliver(uid, msgs[:5], None)
                out = {"ok": True, "to": uid}
                if st == "parked":
                    out["parked"] = True
                return JSONResponse(out)
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

        async def quota(request):
            """Loopback quota/parked view — LINE's free quota endpoints
            plus the local park ledger, so ops can see headroom without
            spending a message."""
            parked: dict[str, int] = {}
            try:
                if PARKED_DIR.is_dir():
                    for f in PARKED_DIR.glob("*.jsonl"):
                        parked[f.stem] = sum(
                            1 for l in f.read_text().splitlines()
                            if l.strip())
            except Exception:
                pass
            return JSONResponse({
                "ok": True, "quota": await self._quota_probe(),
                "quota_blocked": self._quota_blocked > time.time(),
                "quota_blocked_until": self._quota_blocked or None,
                "parked": parked})

        app = Starlette(routes=[
            Route(LINE_PATH, webhook, methods=["POST"]),
            Route("/webhook/line-health", health, methods=["GET"]),
            Route("/send", send, methods=["POST"]),
            Route("/quota", quota, methods=["GET"]),
            Route("/relay-img/{nonce}", img, methods=["GET"]),
            Route("/relay-img/arch/{rest:path}", arch_img,
                  methods=["GET"]),
        ])
        host, _, port = LINE_LISTEN.rpartition(":")
        server = uvicorn.Server(uvicorn.Config(
            app, host=host or "127.0.0.1", port=int(port or 8912),
            log_level="warning"))
        logger.info("line relay up — webhook %s%s", host, LINE_PATH)
        _ops_event("relay_started", "info", webhook=f"{host}{LINE_PATH}")
        await server.serve()

    async def run(self) -> None:
        self.http = httpx.AsyncClient(timeout=40)
        asyncio.create_task(self.sweeper())
        # Free quota probe at boot — headroom lands in the journal, and
        # an already-spent month skips the doomed-push discovery pass.
        q = await self._quota_probe()
        logger.info("LINE push quota: used=%s/%s remaining=%s",
                    q.get("used"), q.get("limit"), q.get("remaining"))
        try:
            if q.get("limit") is not None and \
                    (q.get("used") or 0) >= (q.get("limit") or 0):
                self._quota_blocked = time.time() + LINE_QUOTA_RETRY_S
                _ops_event("quota_depleted", "error", boot=True, **q)
        except Exception:
            pass
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
