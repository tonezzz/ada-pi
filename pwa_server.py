from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.realtime_provider import create_provider
from backend.home_assistant import HomeAssistantClient
from backend.tool_runner import ToolRunner
from backend import auth

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
    cookie_path = str(payload.get("path") or "/")
    if not cookie_path.startswith("/"):
        cookie_path = "/"
    secure = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        max_age=auth.SESSION_TTL_S, httponly=True, samesite="lax",
        secure=secure, path=cookie_path,
    )
    return {"ok": True, "name": name, "expires_in": auth.SESSION_TTL_S}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict:
    cookie_path = request.query_params.get("path") or "/"
    response.delete_cookie(auth.SESSION_COOKIE, path=cookie_path)
    return {"ok": True}


@app.post("/api/auth/redeem-token")
async def mint_redeem(request: Request) -> dict:
    """Mint a one-time redeem URL bound to the caller's key.

    Opening {base}/redeem/{token} hands the device the real API key plus a
    session cookie — designed for QR-code onboarding of phones/tablets.
    """
    if not auth.configured():
        raise HTTPException(status_code=400, detail="auth not configured")
    name = auth.caller_name(request)
    if name is None:
        raise HTTPException(status_code=401, detail="invalid or missing api key")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    base = str(payload.get("path") or "/") if isinstance(payload, dict) else "/"
    if not base.startswith("/") or "//" in base:
        base = "/"
    base = base.rstrip("/") or "/"
    token = auth.mint_redeem_token(name, base)
    url = f"{base}/redeem/{token}" if base != "/" else f"/redeem/{token}"
    logger.info("redeem token minted by name=%s path=%s", name, base)
    result = {"redeem_url": url, "expires_in": auth.REDEEM_TTL_S}
    if isinstance(payload, dict) and payload.get("qr"):
        origin = str(payload.get("origin") or "").rstrip("/")
        if origin.startswith("http"):
            result["qr_svg"] = _qr_svg(origin + url)
    return result


def _qr_svg(data: str) -> str | None:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    return img.to_string(encoding="unicode")


@app.get("/redeem/{token}")
async def redeem(token: str, request: Request):
    """One-time redeem: set the session cookie and hand the key to the PWA."""
    entry = auth.redeem_token(token)
    if entry is None:
        raise HTTPException(status_code=403, detail="redeem link is expired or already used")
    name, key, base = entry
    session = auth.issue_session_for_name(name)
    target = f"{base}/?api_key={key}" if base != "/" else f"/?api_key={key}"
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
    try:
        await tool_runner.memory.refresh()
        logger.info("cache warm complete")
    except Exception as exc:
        logger.warning("cache warm failed, will retry on first request: %s", exc)
    if ha_client.configured:
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
    await tool_runner.events.stop()


@app.websocket("/ws")
async def voice_socket(ws: WebSocket) -> None:
    if not auth.websocket_authorized(ws):
        client = ws.client.host if ws.client else "unknown"
        logger.warning("ws connect denied (auth) client=%s", client)
        await ws.close(code=4401)
        return
    await ws.accept()
    session_id = uuid.uuid4().hex[:10]
    logger.info("session=%s client connected", session_id)
    provider = create_provider(tool_runner=tool_runner, session_id=session_id)
    closed = asyncio.Event()

    try:
        await provider.connect()
    except Exception as exc:
        logger.exception("session=%s provider connect failed", session_id)
        with suppress(Exception):
            await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
            await ws.close(code=1011)
        return

    try:
        await ws.send_text(json.dumps({"type": "ready"}))
    except Exception:
        return

    async def browser_to_provider() -> None:
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                pcm16 = message.get("bytes")
                if pcm16:
                    await provider.send_audio(pcm16)
                    continue
                text = message.get("text")
                if text:
                    with suppress(ValueError, TypeError):
                        control = json.loads(text)
                        if control.get("type") in ("local_speech_started", "local_speech_stopped"):
                            logger.info("session=%s %s", session_id, control.get("type"))
        except WebSocketDisconnect:
            pass
        finally:
            closed.set()

    async def provider_to_browser() -> None:
        try:
            async for event in provider.events():
                if event.type == "audio":
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
                ):
                    if event.type == "speech_started" or event.type == "response_interrupted":
                        await ws.send_text(json.dumps({"type": "clear_audio"}))
                    await ws.send_text(json.dumps({"type": event.type, **event.data}))
                elif event.type == "go_away":
                    break
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
            await provider.close()
        with suppress(Exception):
            await ws.close()
        logger.info("session=%s closed", session_id)


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


static_dir = ROOT / "frontend"
pwa_dir = ROOT / "pwa"
app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount("/", StaticFiles(directory=pwa_dir, html=True), name="pwa")
