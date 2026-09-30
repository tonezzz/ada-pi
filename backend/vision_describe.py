"""Cheap vision-description sidecar for camera/display frames.

When Gemini Live quota is saturated (429 storms), an inline image turn is
the most likely thing to stall the session — the frame is also ~1-2k
tokens of context per shot. This sends the captured frame to a smaller
vision model (OpenRouter-compatible chat/completions) and returns plain
text Ada can relay immediately.

Env:
  ADA_VISION_MODE      off (default) | describe | both
    describe — replace the inline image turn with the text description
               (fails safe to inline image when the sidecar is down)
    both     — send the description text AND the image frame
  ADA_VISION_BASE_URL  default https://openrouter.ai/api/v1
  ADA_VISION_API_KEY   required for any non-off mode
  ADA_VISION_MODEL     default meta-llama/llama-3.2-11b-vision-instruct:free
"""

from __future__ import annotations

import base64
import io
import logging
import os

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen/qwen3.8-27b:free"


def _shrink(img: bytes, mime: str) -> tuple[bytes, str]:
    """Downscale to <=800px JPEG q0.65 — 1.2MB PNG frames blow free-tier
    payload limits and add seconds of upload time."""
    try:
        im = Image.open(io.BytesIO(img))
        im.thumbnail((800, 800))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=65)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return img, mime


def mode() -> str:
    return os.environ.get("ADA_VISION_MODE", "off").strip().lower()


def configured() -> bool:
    return mode() in ("describe", "both") and bool(
        os.environ.get("ADA_VISION_API_KEY"))


async def describe_frame(img: bytes, mime: str, label: str) -> str | None:
    """One short factual description of the frame, or None on any failure."""
    base = os.environ.get(
        "ADA_VISION_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    key = os.environ.get("ADA_VISION_API_KEY", "")
    model = os.environ.get("ADA_VISION_MODEL", DEFAULT_MODEL)
    img, mime = _shrink(img, mime)
    b64 = base64.b64encode(img).decode()
    body = {
        "model": model,
        "max_tokens": 80,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": (
                f"This is a still frame from '{label}'. Describe it "
                "factually in 1-2 short sentences: people, vehicles, "
                "weather, anything notable or wrong. If it is dark, "
                "frozen, or shows an error screen, say so.")},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}],
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(
                float(os.environ.get("ADA_VISION_TIMEOUT_S", "90")))) as c:
            r = await c.post(f"{base}/chat/completions", json=body, headers={
                "authorization": f"Bearer {key}",
                "content-type": "application/json",
            })
        r.raise_for_status()
        text = (r.json().get("choices") or [{}])[0] \
            .get("message", {}).get("content", "")
        return str(text).strip() or None
    except Exception as exc:
        logger.warning("vision describe failed (%s): %s", model, exc)
        return None
