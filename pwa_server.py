from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.conversation_memory import (
    ConversationMemory,
    last_session_end,
    recent_summary,
    record_session_end,
)
from backend.realtime_provider import create_provider
from backend import memory_ops
from backend.home_assistant import HomeAssistantClient
from backend.tool_runner import ToolRunner
from backend.conversation_memory import conversation_health
from backend.decision_check import DecisionCheckEngine, decode_image
from backend.document_check import DocumentCheckEngine
from backend.document_check import decode_image as doc_decode_image
from backend.memory_banks import get_registry
from backend import auth
from backend import board_client
from backend import chaba_memory
from backend.speaker_id import SpeakerIdentifier, SpeakerSession

# Voiceprint drift tracker — rolling per-speaker identification scores.
# A sagging mean (illness, mic change, stale prints) trips one ops event
# per speaker per day so re-enrollment is suggested before misses start.
_DRIFT_WINDOW = 12        # scores per speaker
_DRIFT_MIN_SAMPLES = 6    # need this many before judging
_DRIFT_WARN = 0.58        # healthy matches run 0.7+; 0.45 is the bar
_DRIFT_COOLDOWN_S = 86400
_drift_scores: dict[str, collections.deque] = {}
_drift_warned: dict[str, float] = {}


def _drift_track(name: str, confidence: float, provider) -> None:
    """Feed one identification score; emit voiceprint_drift when the
    rolling mean sags below _DRIFT_WARN (once per speaker per day)."""
    dq = _drift_scores.setdefault(name, collections.deque(maxlen=_DRIFT_WINDOW))
    dq.append(confidence)
    if len(dq) < _DRIFT_MIN_SAMPLES:
        return
    mean = sum(dq) / len(dq)
    if mean >= _DRIFT_WARN:
        return
    last = _drift_warned.get(name, 0.0)
    if time.time() - last < _DRIFT_COOLDOWN_S:
        return
    _drift_warned[name] = time.time()
    provider._emit_ops_event(
        "voiceprint_drift",
        f"speaker '{name}' identify scores sagging: mean {mean:.2f} over "
        f"{len(dq)} chunks (bar {_DRIFT_WARN}) — prints may be stale; "
        "suggest re-enrollment.")

# System note injected when a speaker's voice matches no enrolled profile.
UNRECOGNIZED_SPEAKER_NOTE = (
    "(system) The current speaker's voice does not match any enrolled "
    "voice profile (voice identification ran on their speech). If the "
    "moment is right, you may offer to enroll their voice with "
    "ada_enroll_speaker — ask their name first. Do not announce this "
    "unprompted mid-task."
)

# Speech heard but no audible reply — covers dead VAD turns (no input
# transcription ever arrives), responses killed mid-flight, and silent
# tool-call storms where the model works without ever speaking (seen
# live 2026-10-01: 6 user turns transcribed, zero replies, 1M input
# tokens). The nudge asks for ONE short line — not an apology essay.
SPEECH_STALL_S = float(os.environ.get("ADA_SPEECH_STALL_S", "10"))
SPEECH_STALL_NOTE = (
    "(system) The user just spoke but you produced no audible reply — "
    "the turn ended silently or was cut off. Say ONE short sentence "
    "acknowledging it in the user's recent language — e.g. 'ขอโทษค่ะ "
    "ฟังไม่ชัด ขออีกทีนะคะ' or 'Sorry, I didn't catch that — say it "
    "again?'. Then stop."
)

# Speaker identification is opt-in via ADA_SPEAKER_ID=true.  When disabled
# (or when speechbrain/torch are not installed), the voice path is unchanged.
SPEAKER_ID_ENABLED = os.environ.get("ADA_SPEAKER_ID", "true").lower() == "true"

# Raw user audio archive: every pcm16 frame the client sends is appended
# to ~/.local/share/ada/audio/<date>-<session>.pcm (16kHz mono s16le) so
# sessions can be replayed for speaker-id debugging, scenario audio
# feeds, and "what did the STT actually hear" inspection. Same privacy
# posture as transcripts — local only, never synced. ADA_AUDIO_ARCHIVE=0
# disables; retention prunes files older than ADA_AUDIO_DAYS (default 3)
# and trims the dir to ADA_AUDIO_MAX_MB (default 500) oldest-first.
AUDIO_ARCHIVE_ENABLED = os.environ.get("ADA_AUDIO_ARCHIVE", "1") != "0"
AUDIO_ARCHIVE_DAYS = int(os.environ.get("ADA_AUDIO_DAYS", "3"))
AUDIO_ARCHIVE_MAX_MB = int(os.environ.get("ADA_AUDIO_MAX_MB", "500"))
AUDIO_DIR = Path.home() / ".local/share/ada/audio"


def _prune_audio_archive() -> None:
    try:
        files = sorted(AUDIO_DIR.glob("*.pcm"), key=lambda p: p.stat().st_mtime)
        cutoff = time.time() - AUDIO_ARCHIVE_DAYS * 86400
        for f in files:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        files = [f for f in files if f.exists()]
        total = sum(f.stat().st_size for f in files)
        cap = AUDIO_ARCHIVE_MAX_MB * 1024 * 1024
        for f in files:
            if total <= cap:
                break
            size = f.stat().st_size
            f.unlink(missing_ok=True)
            total -= size
    except Exception as exc:
        logger.info("audio archive prune failed: %s", exc)

# CHABA_MEMORY=1 runs this service as the Chaba guest assistant: file-backed
# public memory under ~/.local/share/chaba/, no MDDB/NotebookLM. See
# backend/chaba_memory.py and docs/ssot/chaba/ssot.chaba.memory.yml.
CHABA_MODE = chaba_memory.enabled()
chaba = chaba_memory.get_store() if CHABA_MODE else None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("pwa_server")

app = FastAPI(title="Ada iPad PWA backend")
ha_client = HomeAssistantClient()
tool_runner = ToolRunner(ha_client)

def _require_api_key(request: Request) -> None:
    """Require a configured API key or valid session cookie when auth is on."""
    if not auth.configured():
        return
    if auth.caller_name(request) is None:
        raise HTTPException(status_code=401, detail="invalid or missing api key")


def _require_dispatch(request: Request) -> str:
    """Auth + the 'dispatch' key capability; returns the caller name.

    401 when unauthenticated; 403 when the caller's file-issued key lacks
    'dispatch' in its apps metadata (auth.key_can). Env/operator keys
    have no apps metadata and pass."""
    if not auth.configured():
        return "anon"
    name = auth.caller_name(request)
    if name is None:
        raise HTTPException(status_code=401, detail="invalid or missing api key")
    if not auth.key_can(name, "dispatch"):
        raise HTTPException(status_code=403,
                            detail="key lacks the 'dispatch' capability")
    return name


@app.get("/api/health")
async def api_health() -> dict:
    """Liveness + subsystem failure surface. Unauthenticated on purpose —
    reports only ok/error strings, no data. Non-null errors mean something
    failed recently (fail-quick reporting, not silent decay)."""
    mem = conversation_health()
    if CHABA_MODE:
        cstat = chaba.health()
        return {
            "ok": True,
            "mode": "chaba-guest",
            "conversation_memory": mem,
            "chaba": cstat,
            "degraded": bool(mem["errors"] or not cstat["context_present"]),
        }
    banks = get_registry().health()
    return {
        "ok": True,
        "conversation_memory": mem,
        "memory_banks": {
            "configured": banks["configured"],
            "bank_count": banks["bank_count"],
            "errors": banks["errors"],
        },
        "degraded": bool(mem["errors"] or banks["errors"]),
    }


@app.get("/api/auth/status")
async def auth_status(request: Request) -> dict:
    name = auth.caller_name(request)
    return {
        "auth_configured": auth.configured(),
        "authenticated": name is not None,
        "name": name,
    }


@app.post("/api/auth/session")
async def auth_session(request: Request, response: Response) -> dict:
    """Trade an API key for a short-lived HttpOnly session cookie."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    issued = auth.issue_session(str(payload.get("api_key", "")))
    if issued is None:
        raise HTTPException(status_code=401, detail="invalid api key")
    name, token = issued
    device_id = str(payload.get("device_id") or "")
    if auth.enforce_device(name, device_id) is None:
        raise HTTPException(status_code=401, detail="key is bound to another device — re-pair required")
    bound = auth.bound_device(name)
    cookie_path = str(payload.get("path") or "/")
    if not cookie_path.startswith("/"):
        cookie_path = "/"
    secure = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        max_age=auth.SESSION_TTL_S, httponly=True, samesite="lax",
        secure=secure, path=cookie_path,
    )
    return {"ok": True, "name": name, "device_bound": bound is not None, "expires_in": auth.SESSION_TTL_S}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict:
    cookie_path = request.query_params.get("path") or "/"
    response.delete_cookie(auth.SESSION_COOKIE, path=cookie_path)
    return {"ok": True}


def _redeem_response(name: str, payload: dict) -> dict:
    """Mint a redeem URL for a caller name and shape the JSON response."""
    base = str(payload.get("path") or "/")
    if not base.startswith("/") or "//" in base:
        base = "/"
    base = base.rstrip("/") or "/"
    redirect = str(payload.get("redirect") or "").rstrip("/")
    if not redirect.startswith("/") or "//" in redirect:
        redirect = ""
    token = auth.mint_redeem_token(name, base, redirect or None)
    url = f"{base}/redeem/{token}" if base != "/" else f"/redeem/{token}"
    logger.info("redeem token minted for name=%s path=%s", name, base)
    result = {"redeem_url": url, "expires_in": auth.REDEEM_TTL_S}
    if payload.get("qr"):
        origin = str(payload.get("origin") or "").rstrip("/")
        if origin.startswith("http"):
            result["qr_svg"] = _qr_svg(origin + url)
    return result


async def _auth_payload(request: Request) -> tuple[str, dict]:
    """Require auth; return (caller_name, json_body_or_empty)."""
    if not auth.configured():
        raise HTTPException(status_code=400, detail="auth not configured")
    name = auth.caller_name(request)
    if name is None:
        raise HTTPException(status_code=401, detail="invalid or missing api key")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return name, payload if isinstance(payload, dict) else {}


# -- User invites: per-user key + one-time redeem URL/QR -------------------
# Flow:
#   1. POST /api/auth/invites  {name, ha_person?, path?, redirect?, qr?, origin?}
#      -> creates the user's key AND mints a burn-once redeem link in one call;
#         the raw key is never returned — only the single-use link/QR.
#   2. The user scans the QR / opens {base}/redeem/{token}: the token burns,
#      an HttpOnly session cookie is set, and the PWA is handed the key once —
#      the browser session is now tied to that user.
#   3. Re-pair: POST /api/auth/invites/{name} mints a fresh link for the same
#      key (device binding reset). Revoke: DELETE /api/auth/keys/{name}.


_HA_PERSON_RE = re.compile(r"^person\.[a-z0-9_]+$")


def _payload_ha_person(payload: dict) -> str | None:
    hp = str(payload.get("ha_person") or "").strip().lower()
    if hp and not _HA_PERSON_RE.match(hp):
        raise HTTPException(status_code=422, detail="ha_person must look like 'person.<id>'")
    return hp or None


async def _ha_person_exists(ha_person: str | None) -> bool | None:
    """Best-effort existence check for an ha_person binding: True/False,
    None when HA is unreachable. Warn-only — a binding typo should surface
    in the invite response instead of silently routing nowhere."""
    if not ha_person:
        return None
    try:
        rp = await tool_runner.context.ha_client.resolve_person(ha_person)
        return bool(rp and rp.get("entity_id") == ha_person)
    except Exception:
        return None


@app.post("/api/auth/invites")
async def create_invite(request: Request) -> dict:
    """Issue a per-user key and return its one-time redeem URL (+ QR SVG).

    Body: {name, ha_person?, path?, redirect?, qr?, origin?}. ha_person binds
    the key to an HA person entity so sessions inherit person.<id> identity
    (memory banks, persona, actuation ACL) even before voice enrollment.
    """
    _, payload = await _auth_payload(request)
    key_name = str(payload.get("name") or "").strip()
    ha_person = _payload_ha_person(payload)
    exists = await _ha_person_exists(ha_person)
    if exists is False:
        logger.warning("invite %s: ha_person %s does not exist in Home Assistant",
                       key_name, ha_person)
    if auth.create_key(key_name, ha_person=ha_person) is None:
        raise HTTPException(status_code=409, detail="invalid or taken name")
    logger.info("issued user key name=%s ha_person=%s", key_name, ha_person)
    return {
        "name": key_name,
        "ha_person": ha_person,
        "ha_person_exists": exists,
        **_redeem_response(key_name, payload),
    }


@app.get("/api/auth/invites")
async def list_invites(request: Request) -> dict:
    """List issued user keys: name, issued date, device binding, ha_person."""
    name, _ = await _auth_payload(request)
    return {"caller": name, "users": auth.issued_key_details()}


@app.post("/api/auth/invites/{name}")
async def reissue_invite(name: str, request: Request) -> dict:
    """Mint a fresh one-time redeem link for an existing user key.

    Re-pairing keeps the same key — the new link hands it to a device again
    (e.g. after the device cleared its browser storage). The device binding
    is reset so the next session TOFU-binds to the new device.
    """
    _, payload = await _auth_payload(request)
    if name not in auth.issued_key_names():
        raise HTTPException(status_code=404, detail="no such issued key")
    auth.unbind_device(name)
    logger.info("re-pair redeem minted for name=%s (device binding reset)", name)
    return {"name": name, **_redeem_response(name, payload)}


@app.put("/api/auth/invites/{name}")
async def update_invite(name: str, request: Request) -> dict:
    """Update a user key's metadata: bind/clear its HA person entity."""
    _, payload = await _auth_payload(request)
    if name not in auth.issued_key_names():
        raise HTTPException(status_code=404, detail="no such issued key")
    ha_person = _payload_ha_person(payload)
    exists = await _ha_person_exists(ha_person)
    if exists is False:
        logger.warning("invite %s: ha_person %s does not exist in Home Assistant",
                       name, ha_person)
    if not auth.set_key_ha_person(name, ha_person):
        raise HTTPException(status_code=404, detail="no such issued key")
    return {"ok": True, "name": name, "ha_person": ha_person,
            "ha_person_exists": exists}


# -- Legacy aliases (kept for existing admin tooling) -----------------------

@app.post("/api/auth/redeem-token")
async def mint_redeem(request: Request) -> dict:
    """Mint a one-time redeem URL bound to the caller's own key."""
    name, payload = await _auth_payload(request)
    return _redeem_response(name, payload)


@app.get("/api/auth/keys")
async def list_keys(request: Request) -> dict:
    name, _ = await _auth_payload(request)
    return {"caller": name, "issued": auth.issued_key_names(),
            "bindings": auth.issued_key_bindings(), "apps": auth.issued_key_apps()}


@app.post("/api/auth/keys")
async def create_key(request: Request) -> dict:
    _, payload = await _auth_payload(request)
    key_name = str(payload.get("name") or "").strip()
    ha_person = _payload_ha_person(payload)
    key = auth.create_key(key_name, apps=payload.get("apps"),
                          ha_person=ha_person)
    if key is None:
        raise HTTPException(status_code=409, detail="invalid or taken name")
    logger.info("issued device key name=%s", key_name)
    return {"name": key_name, "ha_person": ha_person,
            **_redeem_response(key_name, payload)}


@app.delete("/api/auth/keys/{name}")
async def revoke_key(name: str, request: Request) -> dict:
    """Revoke an issued user key (kills its sessions too)."""
    await _auth_payload(request)
    if not auth.revoke_key(name):
        raise HTTPException(status_code=404, detail="no such issued key")
    logger.info("revoked device key name=%s", name)
    return {"ok": True}


@app.post("/api/auth/keys/{name}/redeem")
async def reissue_key_redeem(name: str, request: Request) -> dict:
    """Alias of POST /api/auth/invites/{name} — re-mint a re-pair link."""
    return await reissue_invite(name, request)


def _qr_svg(data: str) -> str | None:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    return img.to_string(encoding="unicode")


@app.post("/api/auth/redeem")
async def api_redeem(request: Request, response: Response) -> dict:
    """In-app redeem for PWA re-pairing: same key name only, no silent swap.

    The token burns only when its bound name matches the caller's identity —
    a valid presented key wins over the declared expect_name. Mismatch,
    expiry, or a foreign device binding leaves the token untouched.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    token = str(payload.get("token") or "")
    device_id = str(payload.get("device_id") or "")
    caller = auth.caller_name(request)
    expected = caller or str(payload.get("expect_name") or "")
    entry = auth.peek_redeem_token(token)
    if entry is None:
        raise HTTPException(status_code=403, detail="redeem link is expired or already used")
    name, base, _redirect = entry
    if not expected or name != expected:
        raise HTTPException(status_code=403, detail="redeem link was issued for a different device name")
    if auth.enforce_device(name, device_id) is None:
        raise HTTPException(status_code=403, detail="key is bound to another device — re-pair required")
    key = auth.key_for_name(name)
    if key is None:
        raise HTTPException(status_code=403, detail="key no longer exists")
    auth.burn_redeem_token(token)
    session = auth.issue_session_for_name(name)
    if session:
        secure = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
        response.set_cookie(
            auth.SESSION_COOKIE, session,
            max_age=auth.SESSION_TTL_S, httponly=True, samesite="lax",
            secure=secure, path=base,
        )
    logger.info("api redeem: name=%s", name)
    return {"ok": True, "name": name, "api_key": key}


@app.get("/redeem/{token}")
async def redeem(token: str, request: Request):
    """One-time redeem: set the session cookie and hand the key to the PWA."""
    entry = auth.redeem_token(token)
    if entry is None:
        raise HTTPException(status_code=403, detail="redeem link is expired or already used")
    name, key, base, redirect = entry
    session = auth.issue_session_for_name(name)
    dest = redirect or base
    target = f"{dest}/?api_key={key}" if dest != "/" else f"/?api_key={key}"
    response = RedirectResponse(target, status_code=302)
    if session:
        secure = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
        response.set_cookie(
            auth.SESSION_COOKIE, session,
            max_age=auth.SESSION_TTL_S, httponly=True, samesite="lax",
            secure=secure, path=base,
        )
    logger.info("redeem token used: name=%s path=%s", name, base)
    return response


@app.on_event("startup")
async def warm_cache() -> None:
    logger.info("warming HA and confidence cache")
    if CHABA_MODE:
        logger.info("chaba guest mode — skipping HA memory cache warm")
    else:
        try:
            await tool_runner.memory.refresh()
            logger.info("cache warm complete")
        except Exception as exc:
            logger.warning("cache warm failed, will retry on first request: %s", exc)
    if ha_client.configured and tool_runner.events is not None:
        await tool_runner.events.start()
        logger.info("ha event recorder running=%s", tool_runner.events.running)
    if not auth.configured():
        logger.warning(
            "ADA_API_KEY is not set — /api/tools/call, power endpoints, and /ws are UNAUTHENTICATED. "
            "Dangerous devices are still gated by confirmed=true and rate limits."
        )
    if os.environ.get("ADA_READ_ONLY") == "true":
        logger.warning("ADA_READ_ONLY=true — all control tools are disabled")


@app.on_event("shutdown")
async def stop_event_recorder() -> None:
    if tool_runner.events is not None:
        await tool_runner.events.stop()


# Reconnect continuity: the previous websocket session's end timestamp and
# transcript tail. In-memory is the fast path; _mark_session_end also mirrors
# it to MDDB so a service restart still knows how long the user was away.
_last_session_end: dict[str, Any] | None = None

# Live /ws sessions keyed by session_id, for the transcript endpoint.
_live_sessions: dict[str, dict[str, Any]] = {}


async def _reconnect_context(ws: WebSocket) -> tuple[float | None, str]:
    """(away_seconds, transcript tail) since the previous session ended.

    ?simulate_away_s=<seconds> forces a gap — used by tests/scenarios-live
    to exercise reconnect tiers without waiting real days."""
    sim = ws.query_params.get("simulate_away_s")
    if sim is not None:
        try:
            return max(0.0, float(sim)), ""
        except (TypeError, ValueError):
            pass
    if _last_session_end is not None:
        return (
            time.time() - float(_last_session_end["ts"]),
            str(_last_session_end.get("tail") or ""),
        )
    if tool_runner.mddb is None:
        return None, ""
    ts, tail = await last_session_end(tool_runner.mddb)
    if ts is None:
        return None, ""
    return time.time() - ts, tail


async def _mark_session_end(conversation: ConversationMemory, session_id: str = "") -> None:
    global _last_session_end
    tail = conversation.recent_context(max_turns=6, max_chars=800)
    _last_session_end = {"ts": time.time(), "tail": tail}
    if tool_runner.mddb is not None:
        await record_session_end(tool_runner.mddb, tail)
    if CHABA_MODE and chaba is not None:
        with suppress(Exception):
            chaba.append_session_log(session_id, tail)


async def _prime_session_task(provider: Any, ws: WebSocket) -> None:
    """Background session prime: resolve reconnect context (may hit MDDB)
    then inject the session-start text. Runs after 'ready' is sent so a
    stalled MDDB can't hold up the client handshake."""
    try:
        reconnect = await asyncio.wait_for(_reconnect_context(ws), timeout=6.0)
    except Exception:
        reconnect = None
    await _prime_session(provider, reconnect=reconnect)


async def _prime_session(
    provider: Any, reconnect: tuple[float | None, str] | None = None
) -> None:
    """Inject session-start context (recent-sessions summary + top personal/
    general memories) so Ada starts aware of general info instead of blank.
    On websocket connect, reconnect=(away_seconds, tail) adds a directive so
    the greeting matches how long the user was gone."""
    if CHABA_MODE:
        try:
            ctx = chaba.system_context(provider.session_id)
            if ctx:
                await provider.send_text_turn("(system) Guest context follows.\n\n" + ctx)
        except Exception as exc:
            logger.warning("chaba session prime failed: %s", exc)
        return
    try:
        away_s, tail = reconnect if reconnect else (None, "")
        text = await memory_ops.session_prime_text(
            tool_runner.mddb,
            tool_runner.banks,
            summary=recent_summary(),
            away_seconds=away_s,
            last_tail=tail,
            person_entity=tool_runner.policy_identity(),
        )
        if text:
            t0 = time.monotonic()
            await provider.send_text_turn(text)
            logger.info("session=%s prime injected (%d chars, %.1fs)",
                        provider.session_id, len(text), time.monotonic() - t0)
    except Exception as exc:
        logger.warning("session prime failed: %s", exc)


@app.websocket("/ws")
async def voice_socket(ws: WebSocket) -> None:
    if not auth.websocket_authorized(ws):
        client = ws.client.host if ws.client else "unknown"
        logger.warning("ws connect denied (auth) client=%s", client)
        await ws.close(code=4401)
        return
    await ws.accept()
    session_id = uuid.uuid4().hex[:10]
    caller_name = auth.websocket_caller(ws) or None
    # If the session key was issued bound to an HA person (invite ha_person),
    # sessions inherit that identity until a voiceprint overrides it.
    caller_person = auth.ha_person_for_key(caller_name) if caller_name else None
    if caller_person is None and caller_name == "admin":
        # The env-issued admin key has no keys-file entry to bind a
        # ha_person to — map it via env so the owner's identified voice is
        # the session OWNER on admin sessions, not a secondary speaker
        # (2026-10-01: person.tony was denied ada_remember/cast_to_screen
        # on admin-keyed iPad sessions because owner stayed 'admin').
        caller_person = os.environ.get("ADA_ADMIN_PERSON") or None
    if caller_person is None and caller_name and caller_name != "admin":
        # Bare key names that resolve to an HA person ("tony" -> person.tony)
        # inherit it — keeps unbound legacy/env keys owner-consistent so a
        # matching voiceprint is recognized AS the owner, not a guest.
        try:
            rp = await tool_runner.context.ha_client.resolve_person(caller_name)
            caller_person = rp["entity_id"] if rp else None
        except Exception:
            caller_person = None
    tool_runner.session_caller_name = caller_name
    tool_runner.session_caller_ha_person = caller_person
    # Session-security policy P1: the owner is pinned here, at connect, and
    # never changes for the session's lifetime. All permission checks run
    # against this identity; a recognized guest voice may personalize the
    # conversation but can never widen authorization.
    tool_runner.session_owner_identity = caller_person or caller_name
    # tool_runner is shared across sessions — speaker identity must NOT
    # bleed over from the previous session (a Tony-identified session would
    # otherwise grant the next caller person.tony's full bank/policy scope).
    # Speaker ID re-sets this once it identifies the voice in THIS session.
    tool_runner.current_speaker_ha_person = None
    logger.info(
        "session=%s client connected (caller=%s)",
        session_id, tool_runner.session_caller_name or "anonymous",
    )
    # Chaba guest mode: a ?name= query param binds the visitor's declared
    # name to this session so memory writes land under guests/<name>.yml.
    # A matching users/<name>.yml upgrades to kind "user" so promoted users
    # keep their private namespace across reconnects.
    if CHABA_MODE:
        guest_name = (ws.query_params.get("name") or "").strip()
        if guest_name:
            kind = "user" if chaba.user_file(guest_name).exists() else "guest"
            chaba.set_identity(session_id, kind, guest_name)
    # Relay channels (?channel=telegram|line) run text-only Gemini Live
    # sessions — no TTS, no avatar tools.
    channel = (ws.query_params.get("channel") or "").strip().lower() or None
    # One ConversationMemory per websocket session, shared across provider
    # reconnects so the transcript survives a Gemini session swap.
    conversation = ConversationMemory(session_id)
    conversation.log_event(
        "connect",
        caller=tool_runner.session_caller_name,
        ha_person=tool_runner.session_caller_ha_person,
        owner=tool_runner.session_owner_identity,
        client=ws.client.host if ws.client else None,
        channel=channel,
    )
    # Fallback identity for memory routing/extraction until speaker ID
    # identifies the voice (then _on_speaker updates this).
    conversation.speaker_identity = tool_runner.session_caller_name
    # Test hook: ?no_persist=1 skips transcript persist, extraction,
    # summaries and the session-end marker so scenario runs never pollute
    # real memory (banks are unaffected — explicit ada_remember still writes).
    no_persist = (ws.query_params.get("no_persist") or "").lower() in ("1", "true")
    conversation.no_persist = no_persist
    _live_sessions[session_id] = {
        "conversation": conversation,
        "connected_at": time.time(),
        "client": ws.client.host if ws.client else None,
        "ws": ws,
    }
    provider_ref = [create_provider(
        tool_runner=tool_runner, session_id=session_id, conversation=conversation,
        caller_name=tool_runner.session_caller_name,
        caller_person=tool_runner.session_caller_ha_person,
        channel=channel,
    )]
    # Keep the ref (not the instance) so provider swaps on Gemini reconnect
    # stay visible to /api/notify.
    _live_sessions[session_id]["provider"] = provider_ref
    closed = asyncio.Event()
    # Speech-stall tracking: pending_at set when the user finished a real
    # speech burst (client VAD) or an input transcription landed, cleared
    # when any assistant output reaches the client. stall_watchdog nudges
    # Ada with a one-line acknowledgement if speech never got a reply.
    speech_state: dict[str, float | None] = {
        "burst_at": None, "pending_at": None, "nudged_until": 0.0
    }

    # Speaker identification: non-blocking, runs in parallel with the
    # audio forward path.  When a new speaker is identified, inject the
    # identity as a system text turn and notify the browser.
    speaker_session: SpeakerSession | None = None
    speaker_identifier: SpeakerIdentifier | None = None
    # Test hook: ?no_speaker_id=1 disables voice identification for this
    # session — memory identity then falls back to the issued-key name, so
    # an admin can faithfully test a restricted-key (e.g. testo) experience.
    no_speaker_id = (ws.query_params.get("no_speaker_id") or "").lower() in ("1", "true")
    if SPEAKER_ID_ENABLED and not no_speaker_id:
        try:
            identifier = SpeakerIdentifier.get()
            speaker_identifier = identifier
            async def _on_speaker(name: str, confidence: float) -> None:
                # Media/device voice (TV, video, podcast) — do NOT switch
                # the session identity; steer the model to ignore the
                # content so Ada stops answering ambient audio mid-topic.
                if identifier.is_media(name):
                    provider = provider_ref[0]
                    with suppress(Exception):
                        await ws.send_text(json.dumps({
                            "type": "speaker", "name": name,
                            "display_name": identifier.get_display_name(name) or name,
                            "confidence": round(confidence, 3),
                            "media": True,
                        }))
                    with suppress(Exception):
                        await provider.send_text_turn(
                            f"(system) Ambient audio detected — the voice matches "
                            f"'{name}', a media/device profile (TV, video, "
                            "podcast), not a person in the room. Do not answer "
                            "its content; continue the prior topic. If the user "
                            "asks what that sound is, say it's likely audio "
                            "playing nearby.")
                    return
                # Level 1: set the provider's HA person so get_home_state
                # queries the speaker's person entity instead of the
                # instance default.
                ha_person = identifier.get_ha_person(name)
                display_name = identifier.get_display_name(name) or name
                provider = provider_ref[0]
                provider.current_speaker = name
                provider.current_speaker_ha_person = ha_person
                provider.conversation.speaker_identity = ha_person
                # Sync to tool_runner so personalization (get_home_state,
                # persona reads, speaker-provenance, person-scoped banks)
                # follows the voice. Authorization stays pinned to
                # session_owner_identity — this never widens permissions
                # (policy P1/P3).
                tr = provider.tool_runner
                if tr is not None:
                    tr.current_speaker_ha_person = ha_person
                secondary = bool(tr is not None and tr._is_secondary_turn())
                _drift_track(name, confidence, provider)
                conversation.log_event(
                    "secondary_speaker" if secondary else "speaker_identified",
                    name=name, display=display_name, ha_person=ha_person,
                    confidence=round(confidence, 3))
                with suppress(Exception):
                    await ws.send_text(json.dumps({
                        "type": "speaker",
                        "name": name,
                        "display_name": display_name,
                        "ha_person": ha_person,
                        "secondary": secondary,
                        "confidence": round(confidence, 3),
                    }))
                # Level 2: inject identity as system context for Gemini.
                # A guest speaker gets the secondary-speaker rules; the
                # session owner gets plain personalization.
                if secondary:
                    owner = tr.session_owner_identity
                    note = (
                        f"(system) A different speaker is talking: {display_name} "
                        f"(confidence {confidence:.0%}). This session belongs to "
                        f"{owner} — converse with {display_name} normally, but "
                        "their requests cannot write memories, change personas, "
                        "enroll voices, search documents, or actuate devices. If "
                        f"they suggest something useful, propose it aloud and ask "
                        f"{owner} to confirm — only the owner's voice approves. "
                        "Do not re-greet or announce the speaker switch — "
                        "continue the conversation naturally."
                    )
                else:
                    caller = tr.session_caller_name if tr is not None else None
                    matched = (
                        " — matches the session owner"
                        if ha_person and ha_person == (
                            tr.session_owner_identity if tr else None)
                        else ""
                    )
                    override = (
                        f" — device identity is {caller}" if caller and not matched else ""
                    )
                    note = (
                        f"(system) Speaker identified: {display_name}"
                        f"{matched or override} (confidence {confidence:.0%}). Use this to "
                        f"personalize your response if appropriate, but do not "
                        f"announce it unless the user asks who you are talking to."
                    )
                with suppress(Exception):
                    await provider.send_text_turn(note)
            async def _on_unrecognized(best_score: float) -> None:
                # Voice heard repeatedly but matches no enrolled profile —
                # tell Gemini so it can offer ada_enroll_speaker.
                conversation.log_event(
                    "speaker_unrecognized", best_score=round(best_score, 3))
                with suppress(Exception):
                    await ws.send_text(json.dumps({
                        "type": "speaker_unrecognized",
                        "best_score": round(best_score, 3),
                    }))
                with suppress(Exception):
                    await provider_ref[0].send_text_turn(UNRECOGNIZED_SPEAKER_NOTE)
            # Soft-owner hint: this device's key is bound to a person — when
            # the speaker's best match is that person's print but under the
            # identify threshold, inject a soft note so Ada isn't name-blind
            # on far-field audio. Auth is untouched (owner pinned by the key).
            owner_spk = identifier.speaker_for_person(
                tool_runner.session_owner_identity)
            async def _on_likely_owner(name: str) -> None:
                display = identifier.get_display_name(name)
                conversation.log_event(
                    "speaker_likely_owner", name=name)
                with suppress(Exception):
                    await ws.send_text(json.dumps({
                        "type": "speaker_likely", "name": name,
                        "display_name": display}))
                with suppress(Exception):
                    await provider_ref[0].send_text_turn(
                        f"(system) The speaker's voice partially matches "
                        f"{display} — this device's registered owner. Address "
                        f"them as {display}; if they correct you, they're "
                        "likely a different person.")
            # Shadow backend (ADA_SPEAKER_SHADOW_MODEL): every identify
            # probe is re-scored in parallel — agreements go to the log,
            # disagreements become ops events for the digest.
            def _on_shadow(ev):
                if not ev.get("agree"):
                    provider_ref[0]._emit_ops_event(
                        "speaker_shadow",
                        f"{ev['model']}: shadow={ev['shadow_name']}@"
                        f"{ev['shadow_score']:.2f} vs primary="
                        f"{ev['primary_name']}@{ev['primary_score']:.2f}")
            identifier.on_shadow = _on_shadow
            speaker_session = SpeakerSession(
                identifier, _on_speaker, on_unrecognized=_on_unrecognized,
                owner_speaker=owner_spk, on_likely_owner=_on_likely_owner,
            )
            # Link to tool_runner so ada_enroll_speaker can capture
            # enrollment audio from the session buffer.
            tool_runner.speaker_session = speaker_session
            provider_ref[0].speaker_session = speaker_session
            logger.info("session=%s speaker identification enabled", session_id)
        except Exception as exc:
            logger.warning("session=%s speaker ID disabled: %s", session_id, exc)

    try:
        await provider_ref[0].connect()
    except Exception as exc:
        logger.exception("session=%s provider connect failed", session_id)
        with suppress(Exception):
            await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
            await ws.close(code=1011)
        return
    # Test hook: ?simulate_unknown_speaker=1 injects the unrecognized-speaker
    # note without real audio (text-driven scenario tests).
    if (ws.query_params.get("simulate_unknown_speaker") or "").lower() in ("1", "true"):
        with suppress(Exception):
            await ws.send_text(json.dumps({
                "type": "speaker_unrecognized", "best_score": 0.0, "simulated": True
            }))
        with suppress(Exception):
            await provider_ref[0].send_text_turn(UNRECOGNIZED_SPEAKER_NOTE)
    # Test hook: ?simulate_speaker=KK runs the real _on_speaker path with a
    # fake identification — exercises ha_person lookup, secondary-turn
    # rules, and the owner-vs-guest persona boundary without audio.
    if speaker_session is not None:
        sim_name = (ws.query_params.get("simulate_speaker") or "").strip()
        if sim_name:
            with suppress(Exception):
                await _on_speaker(sim_name, 0.99)
    # Send 'ready' as soon as Gemini is live; reconnect-context lookup and
    # memory priming can stall on MDDB, so they run in the background.
    # session_id included so /api/notify can target this exact session.
    try:
        await ws.send_text(json.dumps({"type": "ready", "session": session_id}))
    except Exception:
        return
    asyncio.create_task(_prime_session_task(provider_ref[0], ws))

    audio_fh = None
    if AUDIO_ARCHIVE_ENABLED:
        try:
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            _prune_audio_archive()
            audio_fh = open(AUDIO_DIR / f"{time.strftime('%Y-%m-%d')}-{session_id}.pcm", "ab")
        except Exception as exc:
            logger.info("session=%s audio archive disabled: %s", session_id, exc)

    async def browser_to_provider() -> None:
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                pcm16 = message.get("bytes")
                if pcm16:
                    if audio_fh is not None:
                        with suppress(Exception):
                            audio_fh.write(pcm16)
                    with suppress(Exception):
                        await provider_ref[0].send_audio(pcm16)
                    if speaker_session is not None:
                        # Don't identify while Ada is talking: her TTS bleeds
                        # into the mic on any speaker the browser's AEC can't
                        # reference and lands on foreign profiles — the
                        # 2026-10-03 "greeted Tony then KK" session identified
                        # HER OWN greeting audio as KK (0.68, auto-learned!).
                        prov_active = getattr(
                            provider_ref[0], "_response_active", False)
                        if prov_active:
                            speech_state["tts_active_at"] = time.monotonic()
                        elif time.monotonic() - speech_state.get(
                                "tts_active_at", 0) > 0.6:
                            with suppress(Exception):
                                await speaker_session.feed(pcm16)
                    continue
                text = message.get("text")
                if text:
                    with suppress(ValueError, TypeError):
                        control = json.loads(text)
                        if control.get("type") in ("local_speech_started", "local_speech_stopped"):
                            logger.info("session=%s %s", session_id, control.get("type"))
                            if control.get("type") == "local_speech_started":
                                speech_state["burst_at"] = time.monotonic()
                            else:
                                burst = speech_state.pop("burst_at", None)
                                # Sub-500ms bursts are coughs/clicks — only
                                # real speech attempts arm the stall nudge.
                                if burst and time.monotonic() - burst >= 0.5:
                                    speech_state["pending_at"] = time.monotonic()
                        elif control.get("type") == "client_noul":
                            # Browser-side student verdict (edge tier).
                            # Advisory: stashed on the provider so the
                            # jev corpus row can carry client_p alongside
                            # the server-side regex/jev scores.
                            try:
                                p = float(control.get("p"))
                                ms = int(control.get("ms") or 0)
                                note = getattr(provider_ref[0],
                                               "note_client_noul", None)
                                if note:
                                    note(p, ms)
                            except (TypeError, ValueError):
                                pass
                        elif control.get("type") == "register" and CHABA_MODE:
                            # Guest name registration: binds name to this
                            # session for memory writes and queues a pending
                            # record for admin promotion.
                            gname = str(control.get("name") or "").strip()
                            try:
                                res = chaba.register_pending(gname, session_id=session_id)
                                await ws.send_text(json.dumps({
                                    "type": "registered",
                                    "name": res["name"], "status": res["status"],
                                }))
                            except Exception as exc:
                                await ws.send_text(json.dumps({
                                    "type": "error", "message": f"register failed: {exc}",
                                }))
                        elif control.get("type") == "image":
                            # Chat relays (LINE/TG) forward a user photo:
                            # {"type":"image","data":<b64>,"mime",
                            #  "text":<caption/from-line>}
                            import base64 as _b64
                            try:
                                img = _b64.b64decode(
                                    str(control.get("data") or ""))
                                if not img or len(img) > 6 * 1024 * 1024:
                                    raise ValueError("bad image size")
                                mime = str(control.get("mime")
                                           or "image/jpeg")[:40]
                                cap = str(control.get("text") or "")[:1000]
                                logger.info(
                                    "session=%s chat image turn (%dB %s)",
                                    session_id, len(img), mime)
                                await provider_ref[0].send_image_turn(
                                    img, mime, cap)
                                conversation.add_user(
                                    f"[image received {len(img)}B] "
                                    + cap[:200])
                            except Exception as exc:
                                logger.warning(
                                    "session=%s image turn failed: %s",
                                    session_id, exc)
                                with suppress(Exception):
                                    await ws.send_text(json.dumps({
                                        "type": "error",
                                        "message": "image send failed"}))
                        elif control.get("type") == "text":
                            chat_text = str(control.get("text") or "").strip()
                            if chat_text:
                                logger.info("session=%s chat text turn (%d chars)", session_id, len(chat_text))
                                # Doc-upload notes are context, not urgency —
                                # wait out any in-flight speech so the note
                                # doesn't abort a response or merge into the
                                # session-open greeting.
                                if chat_text.startswith("[document uploaded"):
                                    wait_idle = getattr(provider_ref[0], "_wait_for_idle", None)
                                    if wait_idle:
                                        await wait_idle(timeout=8.0)
                                try:
                                    await provider_ref[0].send_text_turn(chat_text[:4000])
                                    # Text turns produce no input transcription,
                                    # so record the user side explicitly.
                                    conversation.add_user(chat_text[:4000])
                                except Exception as exc:
                                    logger.warning("session=%s text turn failed: %s", session_id, exc)
                                    with suppress(Exception):
                                        await ws.send_text(json.dumps({"type": "error", "message": "text send failed"}))
        except WebSocketDisconnect:
            pass
        finally:
            closed.set()
            # Pending CMS write confirmations are conversational — they
            # belong to this session's dialogue. Leaving them on the shared
            # tool_runner leaks the pending ask into the next session
            # ("should I delete that page?" as a greeting).
            if tool_runner is not None:
                getattr(tool_runner, "_cms_pending", {}).clear()

    async def provider_to_browser() -> None:
        # In-flight assistant text the browser already heard. When the Gemini
        # stream dies mid-response we reconnect with the resumption handle and
        # the server REPLAYS the whole response — suppress the heard prefix so
        # the user doesn't hear Ada stop mid-sentence then respeak it all.
        live_turn_text = ""
        suppress_target = ""   # heard prefix to swallow after a resume
        replayed = ""          # replayed text seen so far post-resume
        suppressing = False

        async def pump() -> None:
            nonlocal live_turn_text, suppress_target, replayed, suppressing
            async for event in provider_ref[0].events():
                if event.type == "audio":
                    speech_state["pending_at"] = None  # audible reply arrived
                    if not suppressing:
                        await ws.send_bytes(event.data["pcm16"])
                elif event.type == "barge_noise":
                    # Barge-in classified as noise — it never entered the
                    # transcript; tell Ada to resume the interrupted reply.
                    with suppress(Exception):
                        await ws.send_text(json.dumps({
                            "type": "barge_noise",
                            "text": str(event.data.get("text") or "")[:120],
                        }))
                    with suppress(Exception):
                        await provider_ref[0].send_text_turn(
                            "(system) That interruption was background noise, "
                            "not addressed to you — ignore it and resume what "
                            "you were saying.")
                elif event.type in (
                    "response_started",
                    "response_completed",
                    "response_interrupted",
                    "user_transcript",
                    "assistant_transcript_delta",
                    "expression",
                    "tool_call",
                    "tool_result",
                    # per-turn token counts (card ada-context-budget) —
                    # scenario-live asserts input_tokens_below on these.
                    "usage",
                ):
                    if event.type == "user_transcript":
                        # Transcribed speech is a user turn even without
                        # client VAD frames — arm the same stall nudge.
                        speech_state["pending_at"] = time.monotonic()
                    elif event.type in (
                            "assistant_transcript_delta",
                            "response_completed", "response_interrupted"):
                        speech_state["pending_at"] = None
                    if event.type == "response_interrupted":
                        await ws.send_text(json.dumps({"type": "clear_audio"}))
                    if event.type == "response_started":
                        live_turn_text = ""
                        if suppressing:
                            continue   # replay's start marker — swallow it
                    elif event.type == "assistant_transcript_delta":
                        delta = str(event.data.get("text") or "")
                        if suppressing:
                            replayed += delta
                            if suppress_target.startswith(replayed):
                                continue   # still inside the heard prefix
                            suppressing = False
                            if replayed.startswith(suppress_target):
                                delta = replayed[len(suppress_target):]
                                replayed = ""
                                if not delta:
                                    continue   # exact catch-up, nothing new
                            else:
                                # Diverged — model regenerated; forward it all.
                                delta = replayed
                                replayed = ""
                        live_turn_text += delta
                    elif event.type in ("response_completed", "response_interrupted"):
                        live_turn_text = ""
                        suppressing = False
                    await ws.send_text(json.dumps({"type": event.type, **event.data}))
                elif event.type == "go_away":
                    return

        try:
            while not closed.is_set():
                try:
                    await pump()
                except Exception as exc:
                    if closed.is_set():
                        break
                    logger.warning("session=%s provider stream ended: %s", session_id, exc)
                if closed.is_set():
                    break
                # Gemini Live session ended (go_away or drop) — resume with the
                # last resumption handle instead of killing the browser socket.
                old = provider_ref[0]
                handle = old.resumption_handle
                suppress_target = live_turn_text if handle else ""
                replayed = ""
                suppressing = bool(suppress_target)
                with suppress(Exception):
                    await old.close()
                with suppress(Exception):
                    await ws.send_text(json.dumps({"type": "live_reconnecting"}))
                    await ws.send_text(json.dumps({"type": "clear_audio"}))
                delay = 1.0
                while not closed.is_set():
                    try:
                        new_provider = create_provider(
                            tool_runner=tool_runner, session_id=session_id,
                            conversation=conversation,
                            caller_name=tool_runner.session_caller_name,
                            caller_person=tool_runner.session_caller_ha_person,
                            channel=channel,
                        )
                        await new_provider.connect(resumption_handle=handle)
                        provider_ref[0] = new_provider
                        conversation.log_event(
                            "live_reconnect", resumed=bool(handle))
                        if not handle:
                            # Fresh Gemini context — re-prime so the new
                            # session starts aware of recent/general memory.
                            await _prime_session(new_provider)
                        await ws.send_text(json.dumps({"type": "ready", "session": session_id}))
                        logger.info("session=%s provider reconnected (resumed=%s)", session_id, bool(handle))
                        break
                    except Exception as exc:
                        logger.warning("session=%s provider reconnect failed: %s", session_id, exc)
                        await asyncio.sleep(delay)
                        delay = min(15.0, delay * 2)
        except Exception:
            logger.exception("session=%s provider_to_browser", session_id)
        finally:
            closed.set()

    async def stall_watchdog() -> None:
        """Two tiers: (1) user speech heard but Ada produced no audible
        reply within SPEECH_STALL_S — nudge her to voice a one-line
        acknowledgement; (2) provider stream completely silent — close it
        so the reconnect loop in provider_to_browser fires."""
        while not closed.is_set():
            await asyncio.sleep(2)
            p = provider_ref[0]
            if p is not None and p.is_stalled():
                logger.warning(
                    "session=%s provider stalled >%.0fs after user turn — "
                    "forcing reconnect", session_id, p._stall_timeout)
                with suppress(Exception):
                    await ws.send_text(json.dumps({"type": "live_stalled"}))
                with suppress(Exception):
                    await p.close()
                continue
            pending_at = speech_state["pending_at"]
            now = time.monotonic()
            if (pending_at is None
                    or now - pending_at < SPEECH_STALL_S
                    or now < speech_state["nudged_until"]):
                continue
            # Media/device voice (TV, podcast) is ambient audio — nudging
            # on it would make Ada interrupt the room every few seconds.
            last_name = (speaker_session.current_speaker
                         if speaker_session is not None else None)
            if (last_name and speaker_identifier is not None
                    and speaker_identifier.is_media(last_name)):
                speech_state["pending_at"] = None
                continue
            speech_state["pending_at"] = None
            speech_state["nudged_until"] = now + 25
            conversation.log_event("speech_stall_nudge")
            logger.info(
                "session=%s speech heard, silent >%.0fs — voice-back nudge",
                session_id, SPEECH_STALL_S)
            with suppress(Exception):
                await ws.send_text(json.dumps({"type": "speech_stall"}))
            with suppress(Exception):
                await provider_ref[0].send_text_turn(SPEECH_STALL_NOTE)

    tasks = {
        asyncio.create_task(browser_to_provider()),
        asyncio.create_task(provider_to_browser()),
        asyncio.create_task(stall_watchdog()),
    }
    try:
        await closed.wait()
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        with suppress(Exception):
            await provider_ref[0].close()
        if audio_fh is not None:
            with suppress(Exception):
                audio_fh.close()
        if speaker_session is not None:
            with suppress(Exception):
                await speaker_session.close()
            tool_runner.speaker_session = None
        # Drop the identified-speaker binding with the session — it must
        # not carry into the next session on this shared runner.
        tool_runner.current_speaker_ha_person = None
        tool_runner.session_caller_ha_person = None
        tool_runner.session_owner_identity = None
        with suppress(Exception):
            await ws.close()
        if not no_persist:
            with suppress(Exception):
                await _mark_session_end(conversation, session_id)
        _live_sessions.pop(session_id, None)
        if CHABA_MODE:
            chaba.sessions.pop(session_id, None)
        logger.info("session=%s closed", session_id)


@app.get("/api/chaba/pending")
async def chaba_pending(request: Request) -> dict:
    """List guest registrations awaiting admin promotion (chaba mode only)."""
    _require_api_key(request)
    if not CHABA_MODE:
        return {"pending": []}
    return {"pending": chaba.pending_list()}


@app.post("/api/chaba/promote/{name}")
async def chaba_promote(name: str, request: Request) -> dict:
    """Admin promotes a pending guest to a named user: moves their public
    memory file, creates their private namespace, binds ha_person, and pushes
    identity_changed to any live session so the client switches without a
    reconnect."""
    _require_api_key(request)
    if not CHABA_MODE:
        raise HTTPException(status_code=409, detail="chaba mode not enabled")
    payload: dict[str, Any] = {}
    try:
        payload = await request.json()
    except Exception:
        pass
    result = chaba.promote(name, ha_person=payload.get("ha_person"))
    # Best-effort HA person creation — needs an admin HA token; if it fails
    # the admin can create the person manually and re-run metadata binding.
    if ha_client.configured:
        try:
            person = await ha_client.ws_command({
                "type": "person/create",
                "name": result["name"],
            })
            result["ha_person_created"] = True
            result["ha_person"] = f"person.{person.get('id', result['slug'])}"
        except Exception as exc:
            result["ha_person_created"] = False
            result["ha_person_error"] = str(exc)
            logger.warning("person/create for %s failed: %s", result["name"], exc)
    # Bind the guest's enrolled voiceprint (guest-<slug>) to their person.
    try:
        identifier = SpeakerIdentifier.get()
        if identifier.set_metadata(
            f"guest-{result['slug']}", ha_person=result["ha_person"]
        ):
            result["voiceprint_bound"] = True
    except Exception as exc:
        logger.info("voiceprint bind skipped for %s: %s", result["name"], exc)
    for sid in result["sessions"]:
        live = _live_sessions.get(sid)
        if live and live.get("ws"):
            with suppress(Exception):
                await live["ws"].send_text(json.dumps({
                    "type": "identity_changed",
                    "kind": "user",
                    "name": result["name"],
                    "ha_person": result["ha_person"],
                }))
    return result


@app.post("/api/chaba/revoke/{name}")
async def chaba_revoke(name: str, request: Request) -> dict:
    """Admin revokes a guest/user: their files are archived under revoked/
    and live sessions for that name are dropped back to anonymous guest."""
    _require_api_key(request)
    if not CHABA_MODE:
        raise HTTPException(status_code=409, detail="chaba mode not enabled")
    import shutil
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    archived = []
    for p in (chaba.guest_file(name), chaba.user_file(name),
              chaba.private_file(name), chaba.pending_file(name)):
        if p.exists():
            dest = chaba._path("revoked", p.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
            archived.append(p.name)
    dropped = []
    for sid, ident in list(chaba.sessions.items()):
        if re.sub(r"[^a-z0-9]+", "-", ident.get("name", "").lower()).strip("-") == slug:
            chaba.set_identity(sid, "guest", "")
            dropped.append(sid)
            live = _live_sessions.get(sid)
            if live and live.get("ws"):
                with suppress(Exception):
                    await live["ws"].send_text(json.dumps({
                        "type": "identity_changed", "kind": "guest", "name": "",
                    }))
    return {"ok": True, "archived": archived, "sessions_dropped": dropped}


# -- Chaba guest HA auto-login bridge --------------------------------------
# One-time bridge tokens minted by admins; the /local/chaba-gate.html page on
# the HA origin fetches /api/chaba/ha-bridge/<token> (burn-once, CORS-scoped)
# and writes the returned hassTokens into localStorage — landing the guest in
# the HA web UI already logged in as the shared `guest` account.

_bridge_tokens: dict[str, float] = {}
_BRIDGE_TTL_S = 900  # 15 min to scan the QR


def _ha_guest_credentials() -> tuple[str, str, str | None] | None:
    origin = os.environ.get("CHABA_HA_ORIGIN", "").rstrip("/")
    user = os.environ.get("CHABA_GUEST_USER", "guest")
    password = os.environ.get("CHABA_GUEST_PASSWORD") or None
    if not origin:
        return None
    return origin, user, password


_guest_user_id_cache: str | None = None


async def _ha_guest_user_id(username: str) -> str:
    """Find the guest user's id via the admin auth list (cached)."""
    global _guest_user_id_cache
    if _guest_user_id_cache:
        return _guest_user_id_cache
    users = await ha_client.ws_command({"type": "config/auth/list"})
    for u in users:
        if u.get("username") == username or u.get("name", "").lower() == username:
            _guest_user_id_cache = u["id"]
            return _guest_user_id_cache
    raise RuntimeError(f"HA user '{username}' not found")


async def _ha_guest_login(ha_origin: str, user: str, password: str | None) -> dict[str, Any]:
    """Mint tokens for the shared guest user.

    Preferred path: trusted_networks login flow — works only when this
    service's connection to HA is from a trusted network (127.0.0.1 added
    to trusted_networks). No shared password needed.

    Fallback: homeassistant provider with CHABA_GUEST_PASSWORD.

    Calls go to HOME_ASSISTANT_URL (loopback); ha_origin is only the
    client_id/hassUrl the guest browser will use, so LAN and tailnet
    guests get correctly-scoped tokens."""
    import httpx
    # Password mode talks to loopback HA directly. Trusted-networks mode must
    # go through ha_origin (the Caddy LAN listener): the connection sources
    # from 192.168.2.67 — which IS in trusted_networks — while HA ignores
    # loopback there by design.
    ha_api = os.environ.get("HOME_ASSISTANT_URL", ha_origin).rstrip("/") if password else ha_origin
    client_id = f"{ha_origin}/"
    handler = ["homeassistant", None] if password else ["trusted_networks", None]
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(f"{ha_api}/auth/login_flow", json={
            "client_id": client_id,
            "handler": handler,
            "redirect_uri": f"{ha_origin}/",
        })
        r.raise_for_status()
        flow_id = r.json()["flow_id"]
        if password:
            data = {"client_id": client_id, "username": user, "password": password}
        else:
            data = {"client_id": client_id, "user": await _ha_guest_user_id(user)}
        r = await c.post(f"{ha_api}/auth/login_flow/{flow_id}", json=data)
        r.raise_for_status()
        step = r.json()
        if step.get("type") != "create_entry":
            raise RuntimeError(f"login flow did not complete: {step.get('type')}")
        r = await c.post(f"{ha_api}/auth/token", data={
            "grant_type": "authorization_code",
            "code": step["result"],
            "client_id": client_id,
        })
        r.raise_for_status()
        return r.json()


@app.post("/api/chaba/ha-bridge")
async def chaba_ha_bridge_mint(request: Request) -> dict:
    """Admin mints a one-time HA auto-login bridge token (chaba mode only)."""
    _require_api_key(request)
    if not CHABA_MODE:
        raise HTTPException(status_code=409, detail="chaba mode not enabled")
    creds = _ha_guest_credentials()
    if creds is None:
        raise HTTPException(status_code=503, detail="CHABA_HA_ORIGIN not set")
    origin, _, _ = creds
    token = uuid.uuid4().hex
    _bridge_tokens[token] = time.time() + _BRIDGE_TTL_S
    gate_url = f"{origin}/local/chaba-gate.html?t={token}"
    return {"token": token, "gate_url": gate_url, "expires_in": _BRIDGE_TTL_S}


@app.get("/api/chaba/ha-bridge/{token}")
async def chaba_ha_bridge_redeem(token: str) -> Response:
    """Burn-once endpoint returning hassTokens JSON for localStorage."""
    cors = os.environ.get("CHABA_HA_ORIGIN", "").rstrip("/") or "*"
    headers = {"Access-Control-Allow-Origin": cors}
    if not CHABA_MODE:
        return Response(status_code=404, headers=headers)
    expires = _bridge_tokens.pop(token, None)  # burn on read
    if expires is None or expires < time.time():
        return Response(json.dumps({"error": "invalid or expired token"}),
                        status_code=410, media_type="application/json", headers=headers)
    creds = _ha_guest_credentials()
    if creds is None:
        return Response(json.dumps({"error": "guest login not configured"}),
                        status_code=503, media_type="application/json", headers=headers)
    origin, user, password = creds
    try:
        tok = await _ha_guest_login(origin, user, password)
    except Exception as exc:
        logger.warning("guest HA login failed: %s", exc)
        return Response(json.dumps({"error": "login failed"}),
                        status_code=502, media_type="application/json", headers=headers)
    body = {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "token_type": "Bearer",
        "expires_in": tok.get("expires_in", 1800),
        "hassUrl": origin,
        "clientId": f"{origin}/",
        "expires": int(time.time() * 1000) + int(tok.get("expires_in", 1800)) * 1000,
    }
    return Response(json.dumps(body), media_type="application/json", headers=headers)


@app.get("/api/home-assistant/entities")
async def get_entities() -> dict:
    if not ha_client.configured:
        return {"status": "unavailable", "error": "HOME_ASSISTANT_TOKEN not set", "entities": []}
    try:
        return {"status": "connected", "entities": await ha_client.entities()}
    except Exception as exc:
        logger.warning("home assistant entities failed: %s", exc)
        return {"status": "unavailable", "error": str(exc), "entities": []}


@app.post("/api/home-assistant/entities/{entity_id}/power")
async def set_power(entity_id: str, request: Request) -> dict:
    _require_api_key(request)
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("on"), bool):
            raise ValueError("on must be true or false")
        await tool_runner._check_control_allowed(
            "control_entity",
            {"entity_id": entity_id, "on": payload["on"]},
            payload.get("confirmed"),
            payload.get("confirm_token"),
        )
        return await ha_client.set_power(entity_id, payload["on"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("home assistant set_power failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/home-assistant/sensors")
async def get_sensors(search: str = "", limit: int = 50) -> dict:
    if not ha_client.configured:
        return {"status": "unavailable", "error": "HOME_ASSISTANT_TOKEN not set", "sensors": []}
    try:
        return {"status": "connected", "sensors": await ha_client.sensors(search=search or None, limit=limit)}
    except Exception as exc:
        logger.warning("home assistant sensors failed: %s", exc)
        return {"status": "unavailable", "error": str(exc), "sensors": []}


# -- Speaker identification REST API ------------------------------------

@app.get("/api/speakers")
async def list_speakers(request: Request) -> dict:
    """List enrolled speaker profiles with HA person mappings."""
    _require_api_key(request)
    if not SPEAKER_ID_ENABLED:
        return {"enabled": False, "speakers": []}
    try:
        identifier = SpeakerIdentifier.get()
        return {"enabled": True, "speakers": identifier.enrolled_info()}
    except Exception as exc:
        logger.warning("list speakers failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/speakers/enroll")
async def enroll_speaker(request: Request) -> dict:
    """Enroll a speaker from base64-encoded PCM16 audio.

    Body: {"name": "tony", "audio": "<base64 PCM16 16kHz mono>",
           "ha_person": "person.tony", "display_name": "Tony"}
    Minimum 3 seconds of audio recommended.
    """
    _require_api_key(request)
    if not SPEAKER_ID_ENABLED:
        raise HTTPException(status_code=503, detail="speaker ID is disabled")
    try:
        payload = await request.json()
        name = str(payload.get("name", "")).strip()
        audio_b64 = str(payload.get("audio", ""))
        ha_person = str(payload.get("ha_person", "")).strip() or None
        display_name = str(payload.get("display_name", "")).strip() or None
        if not name:
            raise ValueError("name is required")
        if not audio_b64:
            raise ValueError("audio (base64 PCM16) is required")
        pcm16 = base64.b64decode(audio_b64)
        # Minimum 1.5 s of audio (48 000 bytes at 16 kHz 16-bit).
        if len(pcm16) < 48000:
            raise ValueError(
                f"audio too short: {len(pcm16) / 32000:.1f}s, need at least 1.5s"
            )
        identifier = SpeakerIdentifier.get()
        result = identifier.enroll(name, pcm16, ha_person=ha_person,
                                  display_name=display_name)
        logger.info("speaker enrolled via REST: %s", result)
        return {"status": "ok", **result}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("enroll speaker failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/api/speakers/{name}")
async def delete_speaker(name: str, request: Request) -> dict:
    """Remove an enrolled speaker voiceprint (used by test cleanup)."""
    _require_api_key(request)
    if not SPEAKER_ID_ENABLED:
        raise HTTPException(status_code=503, detail="speaker ID is disabled")
    try:
        identifier = SpeakerIdentifier.get()
        if not identifier.remove(name):
            raise HTTPException(status_code=404, detail=f"speaker '{name}' not found")
        return {"status": "ok", "removed": name}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("delete speaker failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.put("/api/speakers/{name}/metadata")
async def update_speaker_metadata(name: str, request: Request) -> dict:
    """Update HA person mapping and/or display name for an enrolled speaker.

    Body: {"ha_person": "person.tony", "display_name": "Tony"}
    Either field is optional; pass null/empty to clear.
    """
    _require_api_key(request)
    if not SPEAKER_ID_ENABLED:
        raise HTTPException(status_code=503, detail="speaker ID is disabled")
    try:
        payload = await request.json()
        ha_person = str(payload.get("ha_person", "")).strip() or None
        display_name = str(payload.get("display_name", "")).strip() or None
        identifier = SpeakerIdentifier.get()
        if not identifier.set_metadata(name, ha_person=ha_person,
                                       display_name=display_name):
            raise HTTPException(status_code=404, detail=f"speaker '{name}' not found")
        return {"status": "ok", "name": name, "ha_person": ha_person,
                "display_name": display_name}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("update speaker metadata failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/api/speakers/{name}")
async def remove_speaker(name: str, request: Request) -> dict:
    """Remove an enrolled speaker profile."""
    _require_api_key(request)
    if not SPEAKER_ID_ENABLED:
        raise HTTPException(status_code=503, detail="speaker ID is disabled")
    try:
        identifier = SpeakerIdentifier.get()
        removed = identifier.remove(name)
        if not removed:
            raise HTTPException(status_code=404, detail=f"speaker '{name}' not found")
        return {"status": "ok", "removed": name}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("remove speaker failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/home-assistant/history")
async def get_history(entity_id: str, hours: int = 24) -> dict:
    if not ha_client.configured:
        return {"status": "unavailable", "error": "HOME_ASSISTANT_TOKEN not set", "history": []}
    try:
        history = await ha_client.history(entity_id, hours=hours)
        return {"status": "connected", "history": history}
    except Exception as exc:
        logger.warning("home assistant history failed: %s", exc)
        return {"status": "unavailable", "error": str(exc), "history": []}


@app.get("/api/home-assistant/logbook")
async def get_logbook(entity_id: str = "", hours: int = 24) -> dict:
    if not ha_client.configured:
        return {"status": "unavailable", "error": "HOME_ASSISTANT_TOKEN not set", "entries": []}
    try:
        entries = await ha_client.logbook(entity_id=entity_id or None, hours=hours)
        return {"status": "connected", "hours": hours, "count": len(entries), "entries": entries[:100]}
    except Exception as exc:
        logger.warning("home assistant logbook failed: %s", exc)
        return {"status": "unavailable", "error": str(exc), "entries": []}


@app.get("/api/home-assistant/events")
async def get_recent_events(hours: int = 24, query: str = "", limit: int = 50) -> dict:
    if not ha_client.configured:
        return {"status": "unavailable", "error": "HOME_ASSISTANT_TOKEN not set", "events": []}
    try:
        result = await ha_client.recent_events(hours=hours, query=query or None, limit=limit)
        return {"status": "connected", **result}
    except Exception as exc:
        logger.warning("home assistant recent events failed: %s", exc)
        return {"status": "unavailable", "error": str(exc), "events": []}


@app.get("/api/home-assistant/entity-events")
async def get_entity_events(entity_id: str, hours: int = 24) -> dict:
    if not ha_client.configured:
        return {"status": "unavailable", "error": "HOME_ASSISTANT_TOKEN not set", "entities": []}
    try:
        result = await ha_client.state_transitions(entity_id, hours=hours)
        return {"status": "connected", **result}
    except Exception as exc:
        logger.warning("home assistant entity events failed: %s", exc)
        return {"status": "unavailable", "error": str(exc), "entities": []}


@app.get("/api/home-assistant/event-recorder")
async def get_event_recorder(hours: float = 24, query: str = "", limit: int = 50) -> dict:
    """Recorder status plus its in-memory transition buffer."""
    return {
        "status": "ok",
        "recorder": tool_runner.events.status(),
        "events": tool_runner.events.recent(hours=hours, query=query or None, limit=limit),
    }


@app.get("/api/home-assistant/dashboard-tab")
async def get_dashboard_tab(tab: str, url_path: str = "tony-test") -> dict:
    if not ha_client.configured:
        return {"status": "unavailable", "error": "HOME_ASSISTANT_TOKEN not set", "tab": tab}
    try:
        result = await ha_client.dashboard_tab(tab=tab, url_path=url_path)
        return {"status": "ok" if "error" not in result else "not_found", **result}
    except Exception as exc:
        logger.warning("home assistant dashboard tab failed: %s", exc)
        return {"status": "unavailable", "error": str(exc), "tab": tab}


@app.get("/api/home-assistant/power-summary")
async def get_power_summary(hours: int = 24) -> dict:
    if not ha_client.configured:
        return {"status": "unavailable", "error": "HOME_ASSISTANT_TOKEN not set", "summary": {}}
    try:
        return {"status": "connected", "summary": await ha_client.power_summary(hours=hours)}
    except Exception as exc:
        logger.warning("home assistant power summary failed: %s", exc)
        return {"status": "unavailable", "error": str(exc), "summary": {}}


@app.get("/api/memory/banks")
async def memory_banks(request: Request) -> dict:
    """List memory banks assigned to this instance with resolved
    collection/notebook routing, plus registry load errors."""
    _require_api_key(request)
    registry = get_registry()
    return {
        "instance": registry.instance,
        "registry": registry.path,
        "errors": registry.errors,
        "banks": {
            name: {
                "title": b.title,
                "description": b.description,
                "scope": b.scope,
                "mddb_collection": b.mddb_collection,
                "notebooklm_group": b.notebooklm_group,
                "notebooklm_id": b.notebook(registry.notebook_ids),
                "kinds": b.kinds,
                "writable": b.writable,
                "write_policy": b.write_policy,
                "allowed_tools": b.allowed_tools,
                "status": b.status,
            }
            for name, b in registry.banks().items()
        },
    }


_decision_engine: DecisionCheckEngine | None = None


def _get_decision_engine() -> DecisionCheckEngine:
    """Lazy: shares the ToolRunner's MDDB client and bank registry so the
    check lands in the same purchase bank voice recall searches."""
    global _decision_engine
    if _decision_engine is None:
        _decision_engine = DecisionCheckEngine(
            tool_runner.mddb, tool_runner.banks, tool_runner.memory.instance,
            ha_client=ha_client,
        )
    return _decision_engine


@app.post("/api/decision/check")
async def decision_check(request: Request) -> dict:
    """Verify a purchase/listing before buying. Body: {text?, url?,
    image_b64?, image_mime?, mode: quick|deep}. Persisted to the purchase
    memory bank and the chaba-admin Events feed."""
    _require_api_key(request)
    caller = auth.caller_name(request) or "api"
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="body must be an object")
    try:
        image, image_mime = decode_image(payload)
    except (ValueError, Exception) as exc:
        raise HTTPException(status_code=422, detail=f"invalid image: {exc}") from exc
    mode = str(payload.get("mode") or "quick")
    try:
        result = await _get_decision_engine().check(
            text=str(payload.get("text") or ""),
            image=image,
            image_mime=image_mime,
            url=str(payload.get("url") or ""),
            mode=mode,
            caller=caller,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("decision check failed caller=%s: %s", caller, exc)
        raise HTTPException(status_code=502, detail=f"check failed: {exc}") from exc
    return result.to_dict()


def _get_document_engine() -> DocumentCheckEngine:
    """Process-wide intake engine (backend.document_check.engine()) —
    held results are RAM-only, so the REST endpoints, ToolRunner and
    session-end reporting share one instance."""
    from backend import document_check
    return document_check.engine()


@app.post("/api/documents/intake")
async def documents_intake(request: Request) -> dict:
    """Upload a document image for classify+measure+render. Body:
    {image_b64 (data-URL ok), image_mime?, filename?, mode: print|archive}.
    Returns the assessment + pdf_url/preview_url keys."""
    _require_api_key(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="body must be an object")
    try:
        image, image_mime = doc_decode_image(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid image: {exc}") from exc
    if not image:
        raise HTTPException(status_code=422, detail="image_b64 required")
    try:
        result = await _get_document_engine().intake(
            image=image,
            image_mime=image_mime,
            filename=str(payload.get("filename") or ""),
            mode=str(payload.get("mode") or "print"),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("document intake failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"intake failed: {exc}") from exc
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error or "intake failed")
    return result.to_dict()


@app.get("/api/documents/{key:path}/pdf")
async def documents_pdf(request: Request, key: str) -> Response:
    """Print-ready A4 PDF for a held intake result (RAM-only, last 20)."""
    _require_api_key(request)
    held = _get_document_engine().held(key)
    if held is None:
        raise HTTPException(status_code=404, detail="unknown or expired document key")
    return Response(content=held.pdf, media_type="application/pdf")


@app.get("/api/documents/{key:path}/preview")
async def documents_preview(request: Request, key: str) -> Response:
    """JPEG preview of the rendered page for a held intake result."""
    _require_api_key(request)
    held = _get_document_engine().held(key)
    if held is None:
        raise HTTPException(status_code=404, detail="unknown or expired document key")
    return Response(content=held.preview, media_type="image/jpeg")


@app.get("/api/decision/history")
async def decision_history(request: Request, limit: int = 20) -> dict:
    """Newest-first list of past purchase checks from the bank."""
    _require_api_key(request)
    try:
        engine = _get_decision_engine()
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc), "checks": []}
    return {"status": "ok", "checks": await engine.history(limit)}


@app.get("/api/tools")
async def list_tools(request: Request) -> dict:
    """List the tool names available via /api/tools/call."""
    _require_api_key(request)
    from backend import tools_loader
    names = [
        name for name in dir(tool_runner)
        if not name.startswith("_") and name != "execute" and callable(getattr(tool_runner, name, None))
    ]
    # tools.d drop-ins dispatch through execute() too — list them so
    # scenario needs_tools gating sees the real surface.
    names += sorted(tools_loader.registry().tools)
    return {"tools": names}


@app.post("/api/tools/call")
async def call_tool(request: Request) -> dict:
    """Execute a tool by name. Body: {tool: str, args: dict}."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    name = payload.get("tool") or payload.get("name")
    if not name or not isinstance(name, str):
        raise HTTPException(status_code=422, detail="tool/name is required")
    args = payload.get("args", {})
    if not isinstance(args, dict):
        raise HTTPException(status_code=422, detail="args must be an object")
    _require_api_key(request)
    client_ip = request.client.host if request.client else "unknown"
    caller = auth.caller_name(request) or "anonymous"
    tool_runner.session_caller_name = auth.caller_name(request)
    logger.info("tool call: %s args=%r client=%s caller=%s", name, args, client_ip, caller)
    try:
        output = await tool_runner.execute(name, args)
        # Uniform result contract: the tool's own outcome is a
        # {ok: bool, ...} dict; gate denials still raise below.
        status = "ok"
        if isinstance(output, dict) and output.get("ok") is False:
            status = (
                "denied" if (output.get("error_type") == "PermissionError"
                             or output.get("needs_confirm")) else "error")
        return {"tool": name, "status": status, "output": output}
    except PermissionError as exc:
        logger.warning("tool %s denied client=%s: %s", name, client_ip, exc)
        return {"tool": name, "status": "denied", "error": str(exc)}
    except Exception as exc:
        logger.warning("tool %s failed: %s", name, exc)
        return {"tool": name, "status": "error", "error": str(exc)}


@app.post("/api/notify")
async def api_notify(request: Request) -> dict:
    """Inject a system text turn into the newest live voice session so Ada
    announces an event without being asked (e.g. a long-running job like
    yt-live finishing its transcode and starting the TV)."""
    _require_api_key(request)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    text = str(body.get("text") or "").strip()[:500]
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    urgent = str(body.get("urgent") or "").lower() in ("1", "true", "yes")
    if not _live_sessions:
        return {"delivered": False, "error": "no live session"}
    target_sid = str(body.get("session") or "").strip()
    if target_sid:
        # Explicit target (scenarios, per-screen notifies) — the newest-
        # session heuristic would otherwise steal it mid-test.
        sess = _live_sessions.get(target_sid)
        if sess is None:
            return {"delivered": False, "error": f"session {target_sid} not live"}
        sid = target_sid
    else:
        sid, sess = max(_live_sessions.items(),
                        key=lambda kv: kv[1]["connected_at"])
    pref = sess.get("provider")
    if not pref or not pref[0]:
        return {"delivered": False, "error": "session has no provider"}
    if not urgent:
        # Non-urgent: a flat machine voice (speechSynthesis) announces the
        # arrival immediately — Ada never stops her current task. A silent
        # context note is injected so she can circle back later.
        ws = sess.get("ws")
        if ws is not None:
            with suppress(Exception):
                await ws.send_text(json.dumps(
                    {"type": "notify_voice", "text": text[:160]}))
    # The ws registers the session before Gemini finishes connecting;
    # give it a short window before giving up.
    last_exc: Exception | None = None
    for _ in range(15):
        try:
            result = await pref[0].notify_or_defer(text, urgent=urgent)
            logger.info("session=%s notify %s urgent=%s (%d chars)",
                        sid, result, urgent, len(text))
            return {"delivered": result in ("interrupted", "context"),
                    "queued": result == "queued", "session": sid}
        except Exception as exc:
            last_exc = exc
            if "not connected" not in str(exc):
                break
            await asyncio.sleep(1)
    logger.warning("session=%s notify failed: %s", sid, last_exc)
    return {"delivered": False, "error": str(last_exc)}


@app.get("/api/chat/transcript")
async def chat_transcript(request: Request, max_turns: int = 200) -> dict:
    """Live chat transcripts plus the persisted last-session tail.

    Text chats only exist in process memory until the session ends and is
    summarized into MDDB — this endpoint is the way to read them live.
    """
    _require_api_key(request)
    sessions = []
    for sid, sess in sorted(
        _live_sessions.items(),
        key=lambda kv: kv[1]["connected_at"],
        reverse=True,
    ):
        turns = sess["conversation"].turns()
        sessions.append({
            "session_id": sid,
            "connected_at": datetime.fromtimestamp(
                sess["connected_at"], timezone.utc
            ).isoformat(),
            "client": sess.get("client"),
            "turn_count": len(turns),
            "turns": turns[-max_turns:] if max_turns > 0 else turns,
        })
    last_end: dict[str, Any] | None = None
    if _last_session_end is not None:
        last_end = {
            "ended_at": datetime.fromtimestamp(
                _last_session_end["ts"], timezone.utc
            ).isoformat(),
            "tail": _last_session_end.get("tail") or "",
        }
    else:
        ts, tail = await last_session_end(tool_runner.mddb)
        if ts is not None:
            last_end = {
                "ended_at": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                "tail": tail,
            }
    return {"live_sessions": sessions, "last_session_end": last_end}


@app.get("/api/cms/pages")
async def cms_list_pages(request: Request, limit: int = 500) -> dict:
    """List miniapp pages (slug/title/format/updated). Read-only, key-gated."""
    _require_api_key(request)
    return {"pages": await tool_runner.cms_list_pages(limit=limit)}


@app.get("/api/cms/pages/{slug}")
async def cms_get_page(request: Request, slug: str, lang: str = "en") -> dict:
    """Fetch one miniapp page's content by slug (+ ?lang=th for the Thai
    variant; falls back to en). Read-only, key-gated."""
    _require_api_key(request)
    try:
        page = await tool_runner.cms_get_page(slug, lang)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")
    return page


@app.get("/api/cms/pages/{slug}/verify")
async def cms_verify_page(request: Request, slug: str) -> dict:
    """Parse-check a page against its declared format. Read-only, key-gated."""
    _require_api_key(request)
    try:
        report = await tool_runner.cms_verify_page(slug)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if report.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="page not found")
    return report


# Command-kind generators — allowlisted name -> argv executed by the
# regenerate endpoint when a page's ada-cms-automation registry doc
# declares {"generator": {"kind": "command", "name": ...}}. Names map to
# fixed argv only — the registry can never inject arbitrary shell (see
# ssot.apps.cms-reports regenerate_contract).
CMS_COMMAND_GENERATORS = {
    "host-services-cms": [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "tony-dell",
        "python3",
        "/home/tony/CascadeProjects/chaba/scripts/ada/host-services-cms.py",
        "--all",
    ],
}


async def _cms_run_command_generator(cfg: dict) -> dict | None:
    """Run a registry-declared command generator synchronously.

    Returns None when the doc doesn't declare a command generator —
    callers then fall back to the queued run_now path."""
    gen = cfg.get("generator")
    if not isinstance(gen, dict) or gen.get("kind") != "command":
        return None
    name = str(gen.get("name") or "")
    argv = CMS_COMMAND_GENERATORS.get(name)
    if not argv:
        raise HTTPException(
            status_code=422,
            detail=f"unknown command generator {name!r} — not in allowlist")
    timeout = int((gen.get("cost") or {}).get("timeout_s", 300))
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"status": "timeout", "generator": name,
                "timeout_s": timeout}
    tail = (out or b"").decode(errors="replace")[-2000:]
    return {"status": "done" if proc.returncode == 0 else "error",
            "generator": name, "exit_code": proc.returncode,
            "duration_s": round(time.monotonic() - started, 1),
            "output": tail}


@app.post("/api/cms/pages/{slug}/regenerate")
async def cms_regenerate_page(request: Request, slug: str) -> dict:
    """Regenerate a generated page.

    Pages whose registry doc declares a command generator run it
    synchronously — the button waits, then reloads fresh content. All
    other pages queue run_now; the scheduled worker picks them up on its
    next pass. The button click plus the API key is the user's explicit
    action, so this calls the runner method directly rather than going
    through the voice confirm gate."""
    _require_api_key(request)
    try:
        state = await tool_runner.cms_automation("get", slug=slug)
    except Exception:
        state = {}
    cfg = state.get("config") if isinstance(state, dict) else None
    result = await _cms_run_command_generator(cfg or {})
    if result is not None:
        return result
    try:
        result = await tool_runner.cms_automation("run", slug=slug)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@app.post("/api/cms/pages/{slug}/edit")
async def cms_edit_page(request: Request, slug: str) -> dict:
    """Structured page/section edit from the CMS edit drawer — the API key
    + explicit button click is the user's action, so this calls the runner
    directly like /regenerate does (no voice confirm gate)."""
    _require_api_key(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    try:
        result = await tool_runner._cms_edit_sections(
            slug,
            op=str(body.get("op") or ""),
            text=str(body.get("text") or ""),
            section=str(body.get("section") or ""),
            instruction=str(body.get("instruction") or ""),
            lang=str(body.get("lang") or "en"),
            target_slug=str(body.get("target_slug") or ""),
            title=str(body.get("title") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="page not found")
    return result


_CMS_SLUG_RE = re.compile(r"^[a-z0-9-]{1,80}$")


@app.post("/api/cms/spawn")
async def cms_spawn_session(request: Request) -> dict:
    """Queue a Devin session on a CMS page (report→session loop §1b).

    Reads the page for its title, then files an armed kanban card through
    board-api POST /card (report:<slug>, action dispatch, queue:true,
    on_exists:queue) — kanban-dispatch claims it like any queued card.
    A second click on the same page re-queues or reports already-active
    instead of duplicating.

    Capability-gated on the 'dispatch' key capability: the shared
    cms-viewer key is a READ key embedded in iframes (apps=['view']) and
    must not gain dispatch power — it gets 403. Keys re-issued with
    apps=[view, dispatch] via the pair flow, and unscoped env/operator
    keys, may spawn.
    """
    caller = _require_dispatch(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    slug = str(body.get("slug") or "").strip()
    if not _CMS_SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=422,
                            detail="slug must match [a-z0-9-] (1-80 chars)")
    try:
        page = await tool_runner.cms_get_page(slug)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")
    title = str(page.get("title") or "").strip() or slug
    status, data = await board_client.post("/card", {
        "title": title,
        "report": slug,
        "action": {"type": "dispatch", "repo": "chaba"},
        "queue": True,
        "on_exists": "queue",
        "tags": ["report-session-loop", "cms"],
        "brief": (f"Spawned from CMS page '{slug}' — session keeps the "
                  f"report updated; watch card comms."),
        "from": "ada",
        "text": (f"▶ Devin session spawned from CMS page '{slug}' "
                 f"(key: {caller})"),
    })
    if status == 0:
        raise HTTPException(status_code=502,
                            detail=data.get("error") or "board unreachable")
    if status >= 400:
        raise HTTPException(
            status_code=status,
            detail=str(data.get("error") or f"board-api HTTP {status}"))
    message = str(data.get("message") or "")
    m = re.search(r"card ([a-z0-9-]+)", message)
    return {
        "ok": True,
        "status": ("already_active"
                   if re.search(r"already (running|queued|active)",
                                message, re.I)
                   else "queued"),
        "card_id": m.group(1) if m else None,
        "message": message,
        "board_url": board_client.board_page_url(),
    }


static_dir = ROOT / "frontend"
pwa_dir = ROOT / "pwa"
# Client-side model artifacts (browser AI lanes — int8 ONNX students,
# later CAM++/YOLO). Models stay out-of-band like every other model
# in the stack; the dir is rsynced to the host, not committed.
pwa_models_dir = Path(os.environ.get(
    "ADA_PWA_MODELS",
    os.path.expanduser("~/.local/share/ada-pi/pwa-models")))
if pwa_models_dir.is_dir():
    app.mount("/models", StaticFiles(directory=pwa_models_dir),
              name="pwa-models")
# Dynamic manifest — must precede the "/" StaticFiles mount. iOS uses the
# apple-mobile-web-app-title meta (synced in index.html); Android uses this
# manifest name. Per-instance branding: ADA_PWA_NAME wins, else
# ADA-HA(<Instance>) e.g. ADA-HA(Tony) / ADA-HA(Michael).
@app.get("/manifest.json")
async def pwa_manifest() -> dict:
    inst = os.environ.get("ADA_INSTANCE_ID", "default")
    name = os.environ.get("ADA_PWA_NAME") or f"ADA-HA({inst.capitalize()})"
    return {
        "name": name,
        "short_name": name,
        "start_url": ".",
        "scope": ".",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#000000",
        "orientation": "landscape",
        "icons": [],
    }


app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount("/", StaticFiles(directory=pwa_dir, html=True), name="pwa")
