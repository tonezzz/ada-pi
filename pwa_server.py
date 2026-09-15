from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.realtime_provider import create_provider
from backend.home_assistant import HomeAssistantClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("pwa_server")

app = FastAPI(title="Ada iPad PWA backend")
ha_client = HomeAssistantClient()


@app.websocket("/ws")
async def voice_socket(ws: WebSocket) -> None:
    await ws.accept()
    session_id = uuid.uuid4().hex[:10]
    logger.info("session=%s client connected", session_id)
    provider = create_provider(home_assistant_client=ha_client)
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
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("on"), bool):
            raise ValueError("on must be true or false")
        return await ha_client.set_power(entity_id, payload["on"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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


static_dir = ROOT / "frontend"
pwa_dir = ROOT / "pwa"
app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount("/", StaticFiles(directory=pwa_dir, html=True), name="pwa")
