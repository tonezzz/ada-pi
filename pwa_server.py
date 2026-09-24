from __future__ import annotations

import asyncio
import base64
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
from backend import chaba_memory
from backend.speaker_id import SpeakerIdentifier, SpeakerSession

# System note injected when a speaker's voice matches no enrolled profile.
UNRECOGNIZED_SPEAKER_NOTE = (
    "(system) The current speaker's voice does not match any enrolled "
    "voice profile (voice identification ran on their speech). If the "
    "moment is right, you may offer to enroll their voice with "
    "ada_enroll_speaker — ask their name first. Do not announce this "
    "unprompted mid-task."
)

# Speaker identification is opt-in via ADA_SPEAKER_ID=true.  When disabled
# (or when speechbrain/torch are not installed), the voice path is unchanged.
SPEAKER_ID_ENABLED = os.environ.get("ADA_SPEAKER_ID", "true").lower() == "true"

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


@app.post("/api/auth/redeem-token")
async def mint_redeem(request: Request) -> dict:
    """Mint a one-time redeem URL bound to the caller's key.

    Opening {base}/redeem/{token} hands the device the real API key plus a
    session cookie — designed for QR-code onboarding of phones/tablets.
    """
    name, payload = await _auth_payload(request)
    return _redeem_response(name, payload)


@app.get("/api/auth/keys")
async def list_keys(request: Request) -> dict:
    """List issued (file-backed) device key names."""
    name, _ = await _auth_payload(request)
    return {"caller": name, "issued": auth.issued_key_names(), "bindings": auth.issued_key_bindings()}


@app.post("/api/auth/keys")
async def create_key(request: Request) -> dict:
    """Issue a new named device key and return a one-time redeem URL for it.

    The raw key is never returned to the admin — it is only delivered inside
    the single-use redeem link. Revoke with DELETE /api/auth/keys/{name}.
    """
    _, payload = await _auth_payload(request)
    key_name = str(payload.get("name") or "").strip()
    key = auth.create_key(key_name)
    if key is None:
        raise HTTPException(status_code=409, detail="invalid or taken name")
    logger.info("issued device key name=%s", key_name)
    return {"name": key_name, **_redeem_response(key_name, payload)}


@app.delete("/api/auth/keys/{name}")
async def revoke_key(name: str, request: Request) -> dict:
    """Revoke an issued device key (kills its sessions too)."""
    await _auth_payload(request)
    if not auth.revoke_key(name):
        raise HTTPException(status_code=404, detail="no such issued key")
    logger.info("revoked device key name=%s", name)
    return {"ok": True}


@app.post("/api/auth/keys/{name}/redeem")
async def reissue_key_redeem(name: str, request: Request) -> dict:
    """Mint a fresh one-time redeem URL for an existing issued key.

    Re-pairing keeps the same key — the new link just hands it to a
    device again (e.g. after the device cleared its browser storage).
    """
    _, payload = await _auth_payload(request)
    if name not in auth.issued_key_names():
        raise HTTPException(status_code=404, detail="no such issued key")
    auth.unbind_device(name)
    logger.info("re-pair redeem minted for name=%s (device binding reset)", name)
    return _redeem_response(name, payload)


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
            person_entity=tool_runner._memory_identity(),
        )
        if text:
            await provider.send_text_turn(text)
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
    tool_runner.session_caller_name = auth.websocket_caller(ws) or None
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
    # One ConversationMemory per websocket session, shared across provider
    # reconnects so the transcript survives a Gemini session swap.
    conversation = ConversationMemory(session_id)
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
        tool_runner=tool_runner, session_id=session_id, conversation=conversation
    )]
    closed = asyncio.Event()

    # Speaker identification: non-blocking, runs in parallel with the
    # audio forward path.  When a new speaker is identified, inject the
    # identity as a system text turn and notify the browser.
    speaker_session: SpeakerSession | None = None
    # Test hook: ?no_speaker_id=1 disables voice identification for this
    # session — memory identity then falls back to the issued-key name, so
    # an admin can faithfully test a restricted-key (e.g. testo) experience.
    no_speaker_id = (ws.query_params.get("no_speaker_id") or "").lower() in ("1", "true")
    if SPEAKER_ID_ENABLED and not no_speaker_id:
        try:
            identifier = SpeakerIdentifier.get()
            async def _on_speaker(name: str, confidence: float) -> None:
                # Level 1: set the provider's HA person so get_home_state
                # queries the speaker's person entity instead of the
                # instance default.
                ha_person = identifier.get_ha_person(name)
                display_name = identifier.get_display_name(name) or name
                provider = provider_ref[0]
                provider.current_speaker = name
                provider.current_speaker_ha_person = ha_person
                # Sync to tool_runner so memory ops route to the speaker's
                # person-scoped bank (e.g. personal-kk instead of personal).
                if provider.tool_runner is not None:
                    provider.tool_runner.current_speaker_ha_person = ha_person
                with suppress(Exception):
                    await ws.send_text(json.dumps({
                        "type": "speaker",
                        "name": name,
                        "display_name": display_name,
                        "ha_person": ha_person,
                        "confidence": round(confidence, 3),
                    }))
                # Level 2: inject identity as system context for Gemini.
                # Use display_name (e.g. "Tony") rather than the raw key.
                with suppress(Exception):
                    await provider.send_text_turn(
                        f"(system) The current speaker is {display_name} "
                        f"(confidence {confidence:.0%}). Use this to personalize "
                        f"your response if appropriate, but do not announce it "
                        f"unless the user asks who you are talking to."
                    )
            async def _on_unrecognized(best_score: float) -> None:
                # Voice heard repeatedly but matches no enrolled profile —
                # tell Gemini so it can offer ada_enroll_speaker.
                with suppress(Exception):
                    await ws.send_text(json.dumps({
                        "type": "speaker_unrecognized",
                        "best_score": round(best_score, 3),
                    }))
                with suppress(Exception):
                    await provider_ref[0].send_text_turn(UNRECOGNIZED_SPEAKER_NOTE)
            speaker_session = SpeakerSession(
                identifier, _on_speaker, on_unrecognized=_on_unrecognized
            )
            # Link to tool_runner so ada_enroll_speaker can capture
            # enrollment audio from the session buffer.
            tool_runner.speaker_session = speaker_session
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
    # Send 'ready' as soon as Gemini is live; reconnect-context lookup and
    # memory priming can stall on MDDB, so they run in the background.
    try:
        await ws.send_text(json.dumps({"type": "ready"}))
    except Exception:
        return
    asyncio.create_task(_prime_session_task(provider_ref[0], ws))

    async def browser_to_provider() -> None:
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                pcm16 = message.get("bytes")
                if pcm16:
                    with suppress(Exception):
                        await provider_ref[0].send_audio(pcm16)
                    if speaker_session is not None:
                        with suppress(Exception):
                            await speaker_session.feed(pcm16)
                    continue
                text = message.get("text")
                if text:
                    with suppress(ValueError, TypeError):
                        control = json.loads(text)
                        if control.get("type") in ("local_speech_started", "local_speech_stopped"):
                            logger.info("session=%s %s", session_id, control.get("type"))
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
                        elif control.get("type") == "text":
                            chat_text = str(control.get("text") or "").strip()
                            if chat_text:
                                logger.info("session=%s chat text turn (%d chars)", session_id, len(chat_text))
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
                    if not suppressing:
                        await ws.send_bytes(event.data["pcm16"])
                elif event.type in (
                    "speech_started",
                    "speech_stopped",
                    "response_started",
                    "response_completed",
                    "response_interrupted",
                    "user_transcript",
                    "assistant_transcript_delta",
                    "expression",
                    "tool_call",
                    "tool_result",
                ):
                    if event.type == "speech_started" or event.type == "response_interrupted":
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
                        )
                        await new_provider.connect(resumption_handle=handle)
                        provider_ref[0] = new_provider
                        if not handle:
                            # Fresh Gemini context — re-prime so the new
                            # session starts aware of recent/general memory.
                            await _prime_session(new_provider)
                        await ws.send_text(json.dumps({"type": "ready"}))
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

    tasks = {
        asyncio.create_task(browser_to_provider()),
        asyncio.create_task(provider_to_browser()),
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
        if speaker_session is not None:
            with suppress(Exception):
                await speaker_session.close()
            tool_runner.speaker_session = None
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


_document_engine: DocumentCheckEngine | None = None


def _get_document_engine() -> DocumentCheckEngine:
    """Lazy — document intake keeps rendered artifacts in RAM only;
    printing/archive are separate confirmed tools (P2/P3)."""
    global _document_engine
    if _document_engine is None:
        _document_engine = DocumentCheckEngine()
    return _document_engine


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
    return {
        "tools": [
            name for name in dir(tool_runner)
            if not name.startswith("_") and name != "execute" and callable(getattr(tool_runner, name, None))
        ]
    }


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
        return {"tool": name, "status": "ok", "output": output}
    except PermissionError as exc:
        logger.warning("tool %s denied client=%s: %s", name, client_ip, exc)
        return {"tool": name, "status": "denied", "error": str(exc)}
    except Exception as exc:
        logger.warning("tool %s failed: %s", name, exc)
        return {"tool": name, "status": "error", "error": str(exc)}


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
async def cms_list_pages(request: Request, limit: int = 50) -> dict:
    """List miniapp pages (slug/title/format/updated). Read-only, key-gated."""
    _require_api_key(request)
    return {"pages": await tool_runner.cms_list_pages(limit=limit)}


@app.get("/api/cms/pages/{slug}")
async def cms_get_page(request: Request, slug: str) -> dict:
    """Fetch one miniapp page's content by slug. Read-only, key-gated."""
    _require_api_key(request)
    try:
        page = await tool_runner.cms_get_page(slug)
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


static_dir = ROOT / "frontend"
pwa_dir = ROOT / "pwa"
app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount("/", StaticFiles(directory=pwa_dir, html=True), name="pwa")
