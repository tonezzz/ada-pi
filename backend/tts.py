"""tts.py — shared Gemini-TTS synth helper (24kHz mono PCM).

Extracted pattern from tool_runner/screens.py::_vcast_tts so other lanes
(LINE voice messages, etc.) can speak without duplicating the REST call.
Plain urllib REST — the google-genai sync client dies inside to_thread
with "client has been closed" (same reason _vcast_tts hand-rolls it).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import urllib.error
import urllib.request
import wave

from backend import gemini_pool

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # s16le mono


def tts_model() -> str:
    return os.environ.get(
        "ADA_TTS_MODEL",
        os.environ.get("VCAST_TTS_MODEL",
                       "gemini-2.5-flash-preview-tts"))


async def synth_pcm(text: str, mddb=None, tool: str = "tts",
                    session_id: str = "") -> bytes | None:
    """Synthesize `text` with Gemini TTS in the instance's configured
    voice — returns raw 24kHz s16le mono PCM, or None on failure."""
    from backend import voice_config
    model = tts_model()
    api_key = gemini_pool.next_key(model)
    if not api_key:
        return None
    text = str(text or "").strip()
    if not text:
        return None

    def _synth() -> bytes | None:
        payload = json.dumps({
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": voice_config.current_voice()}}},
            }}).encode()
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/{model}:generateContent?key={api_key}",
            data=payload,
            headers={"Content-Type": "application/json"})
        resp = json.load(urllib.request.urlopen(req, timeout=30))
        for part in (resp.get("candidates", [{}])[0]
                     .get("content", {}).get("parts") or []):
            data = (part.get("inlineData") or {}).get("data")
            if data:
                return base64.b64decode(data)
        return None

    try:
        return await asyncio.to_thread(_synth)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            gemini_pool.mark_exhausted(api_key, model, e, tool=tool)
            gemini_pool.emit_ops_event(
                mddb, tool, e, session_id=str(session_id or "runner"))
        logger.exception("%s tts failed", tool)
        return None
    except Exception:
        logger.exception("%s tts failed", tool)
        return None


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def pcm_duration_ms(pcm: bytes) -> int:
    return len(pcm) // BYTES_PER_MS
