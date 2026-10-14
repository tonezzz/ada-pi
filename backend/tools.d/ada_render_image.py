"""ada_render_image — drop-in tool (cards nano-banana-image, ada-render-image-v2).

Generate an image from an assembled prompt — or edit/compose the ref_url
image — through the Gemini image model (default gemini-3.1-flash-image,
"Nano Banana 2": one model does text-to-image AND image+instruction
editing). The call goes through gemini_pool.generate(): on gateway lanes
(ADA_GEMINI_REST_BASE_URL set) the raw name maps to the 'image' model
class — LiteLLM routing + per-lane virtual-key metering; direct-Gemini
dev keeps the raw model name.

The PNG lands under ADA_RENDER_DIR and publishes through the
input-bridge /frame asset hop ada_render_video uses — the returned
image_url is fetchable by the relays (LINE imageMessage / TG sendPhoto
auto-attach it via _images_from).

ref_url (edit/compose a source photo — 'make the car red', 'put me on
a beach') accepts the same three shapes as ada_render_video:
  - an intake key 'doc/...' from a [document uploaded] session note —
    resolved in-process via the shared document_check engine;
  - a /api/documents/<key>/{image,preview} path (relative or absolute);
  - any other fetchable http(s) image URL — including the relay's own
    /relay-img/arch/<chat>/<message>.jpg archives of photos users sent.

Env:
    GEMINI_API_KEY / GOOGLE_API_KEY — required (pool bookkeeping; the
        actual request key is ADA_GEMINI_REST_API_KEY when the gateway
        env is set)
    ADA_RENDER_IMAGE_MODEL — default gemini-3.1-flash-image
    ADA_GATES_DIR          — owner kill-switch dir (default
        ~/.local/share/ada/gates); touch ada_render_image.closed or
        ada_render_image.<lane>.closed to refuse renders instantly
    ADA_RENDER_DIR         — artifact dir, default ~/.local/share/ada/renders
    ADA_SELF_URL           — self API base for relative ref paths
                             (default http://127.0.0.1:$ADA_PORT or :8000)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import secrets
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("tools.ada_render_image")

DECLARATION = {
    "name": "ada_render_image",
    "description": (
        "Generate an image from a prompt, or edit the ref_url photo "
        "with instructions, and return a shareable image_url the chat "
        "relays can post as a photo."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The assembled image description — "
                               "subject, style, mood, detail; with "
                               "ref_url it is the edit instruction. "
                               "Required.",
            },
            "ref_url": {
                "type": "string",
                "description": "Optional source image to edit: a "
                               "'doc/...' intake key, a /api/documents/... "
                               "path, or any fetchable image URL.",
            },
            "aspect": {
                "type": "string",
                "description": "square (1:1, default) | landscape "
                               "(16:9) | portrait (9:16) | an explicit "
                               "ratio.",
            },
            "size": {
                "type": "string",
                "description": "1K (default, ~1024px, cheapest) | 2K "
                               "| 4K (~4096px, sharpest, costs most). "
                               "Only go above 1K when detail matters.",
            },
        },
        "required": ["prompt"],
    },
}

_DEFAULT_MODEL = "gemini-3.1-flash-image"
_DEFAULT_DIR = "~/.local/share/ada/renders"
_PROMPT_MAX = 2000
_REF_MAX_BYTES = 15 * 1024 * 1024
_IMG_MAX_BYTES = 20 * 1024 * 1024

_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4"}
_ASPECT_ALIASES = {
    "square": "1:1", "landscape": "16:9", "wide": "16:9",
    "portrait": "9:16", "tall": "9:16", "vertical": "9:16",
}

_SIZES = {"1K", "2K", "4K"}
_SIZE_ALIASES = {"small": "1K", "default": "1K", "medium": "2K",
                 "hd": "2K", "large": "4K", "big": "4K", "ultra": "4K"}

_DOC_KEY_RE = re.compile(r"^doc/[a-z0-9][a-z0-9._-]*$")
_DOC_PATH_RE = re.compile(
    r"/api/documents/([^?#]+?)/(image|preview|pdf)(?:[?#].*)?$")


def _model() -> str:
    return os.environ.get("ADA_RENDER_IMAGE_MODEL") or _DEFAULT_MODEL


def _self_base() -> str:
    return (os.environ.get("ADA_SELF_URL")
            or f"http://127.0.0.1:{os.environ.get('ADA_PORT') or '8000'}"
            ).rstrip("/")


def _aspect(raw: Any) -> tuple[str | None, str | None]:
    """-> (ratio, error). Blank defaults to square."""
    val = str(raw or "").strip().lower()
    if not val:
        return "1:1", None
    if val in _ASPECT_RATIOS:
        return val, None
    if val in _ASPECT_ALIASES:
        return _ASPECT_ALIASES[val], None
    m = re.fullmatch(r"(\d{1,2})\s*[:x×]\s*(\d{1,2})", val)
    if m and f"{m.group(1)}:{m.group(2)}" in _ASPECT_RATIOS:
        return f"{m.group(1)}:{m.group(2)}", None
    return None, (
        f"unknown aspect {raw!r} — image renders square (1:1), "
        "landscape (16:9), portrait (9:16), 4:3 or 3:4")


def _size(raw: Any) -> tuple[str | None, str | None]:
    """-> (size, error). Blank defaults to 1K."""
    val = str(raw or "").strip()
    if not val:
        return "1K", None
    if val.upper() in _SIZES:
        return val.upper(), None
    if val.lower() in _SIZE_ALIASES:
        return _SIZE_ALIASES[val.lower()], None
    return None, (
        f"unknown size {raw!r} — image renders at 1K (default), "
        "2K or 4K")


def _gate_closed() -> str | None:
    """Owner kill-switch — a file in ADA_GATES_DIR closes the tool
    instantly, no restart: <tool>.closed gates every lane,
    <tool>.<ADA_INSTANCE_ID>.closed gates one lane."""
    d = Path(os.path.expanduser(
        os.environ.get("ADA_GATES_DIR") or "~/.local/share/ada/gates"))
    lane = os.environ.get("ADA_INSTANCE_ID") or ""
    names = ["ada_render_image.closed"]
    if lane:
        names.append(f"ada_render_image.{lane}.closed")
    if any((d / n).exists() for n in names):
        return ("image rendering is switched off by the owner right "
                "now — tell the user it's temporarily closed")
    return None


def _ref_from_engine(key: str) -> tuple[bytes, str] | None:
    """Resolve a held intake key in-process — same singleton the video
    tool uses."""
    try:
        from backend import document_check
        held = document_check.engine().held(key)
    except Exception as exc:
        logger.info("ref resolve %r: engine lookup failed: %s", key, exc)
        return None
    if held is None:
        return None
    jpg = (held.meta or {}).get("archive_jpg")
    if isinstance(jpg, bytes) and jpg:
        return jpg, "image/jpeg"
    if isinstance(held.preview, bytes) and held.preview:
        return held.preview, "image/jpeg"
    return None


async def _resolve_ref(client: httpx.AsyncClient, ref: str
                       ) -> tuple[tuple[bytes, str] | None, str | None]:
    """-> ((bytes, mime), None) or (None, error)."""
    ref = ref.strip()
    doc = _DOC_PATH_RE.search(ref) if "/" in ref else None
    if doc:
        key = urllib.parse.unquote(doc.group(1))
        hit = _ref_from_engine(key)
        if hit:
            return hit, None
        if not ref.startswith(("http://", "https://")):
            return None, (
                f"no held document for key {key!r} — intake results live "
                "in RAM only, ask the user to upload it again")
    elif _DOC_KEY_RE.match(ref):
        hit = _ref_from_engine(ref)
        if hit:
            return hit, None
        return None, (
            f"no held document for key {ref!r} — intake results live in "
            "RAM only, ask the user to upload it again")
    elif ref.startswith("/"):
        ref = _self_base() + ref
    if not ref.startswith(("http://", "https://")):
        return None, (
            "ref_url must be a 'doc/...' intake key, a /api/documents/... "
            "path, or an http(s) image URL")
    headers = {}
    host = urllib.parse.urlparse(ref).hostname or ""
    key = os.environ.get("ADA_API_KEY", "")
    if key and host in ("127.0.0.1", "localhost", "::1"):
        headers["x-api-key"] = key
    try:
        resp = await client.get(
            ref, headers=headers, follow_redirects=True, timeout=30)
    except (httpx.HTTPError, TimeoutError) as exc:
        return None, (f"I couldn't fetch the reference image "
                      f"({exc.__class__.__name__})")
    if resp.status_code >= 400:
        return None, f"the reference image URL returned HTTP {resp.status_code}"
    mime = (resp.headers.get("content-type") or "").split(";")[0].strip()
    if not mime.startswith("image/"):
        return None, f"the reference URL isn't an image (got {mime or 'unknown'})"
    if len(resp.content) > _REF_MAX_BYTES:
        return None, "the reference image is too large (15MB max)"
    return (resp.content, mime), None


def _gen_config(ratio: str, size: str) -> Any:
    """responseModalities + aspect/size — types first, dict fallback for
    old SDK builds."""
    try:
        from google.genai import types
        return types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio=ratio, image_size=size))
    except Exception:
        return {"response_modalities": ["TEXT", "IMAGE"],
                "image_config": {"aspect_ratio": ratio,
                                 "image_size": size}}


async def _render(prompt: str, ref: tuple[bytes, str] | None,
                  ratio: str, size: str
                  ) -> tuple[bytes | None, str | None, str | None]:
    """-> (image bytes, model text, None) or (None, None, error)."""
    parts: list[dict[str, Any]] = [{"text": prompt}]
    if ref:
        data, mime = ref
        parts.append({"inline_data": {
            "mime_type": mime, "data": base64.b64encode(data).decode()}})
    contents = [{"role": "user", "parts": parts}]
    from backend import gemini_pool
    resp = await gemini_pool.generate(
        _model(), contents, config=_gen_config(ratio, size),
        tool="render_image")
    text_bits: list[str] = []
    for cand in getattr(resp, "candidates", None) or []:
        for part in (getattr(cand, "content", None) and
                     getattr(cand.content, "parts", None)) or []:
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                return inline.data, " ".join(text_bits).strip() or None, None
            t = getattr(part, "text", None)
            if t:
                text_bits.append(str(t))
    reason = None
    try:
        reason = getattr(resp, "prompt_feedback", None)
    except Exception:
        pass
    return None, (" ".join(text_bits).strip() or None), (
        "the image model answered with no image — rephrase the prompt "
        "and try once more" + (f" (feedback: {reason})" if reason else ""))


def _save(data: bytes, mime: str) -> Path:
    ext = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".png")
    d = Path(os.path.expanduser(
        os.environ.get("ADA_RENDER_DIR") or _DEFAULT_DIR))
    d.mkdir(parents=True, exist_ok=True)
    path = d / (f"render-img-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                f"-{secrets.token_hex(3)}{ext}")
    path.write_bytes(data)
    return path


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    closed = _gate_closed()
    if closed:
        return {"ok": False, "error": closed}
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False,
                "error": "a prompt describing the image is required"}
    prompt = prompt[:_PROMPT_MAX]
    ratio, err = _aspect(args.get("aspect"))
    if err:
        return {"ok": False, "error": err}
    assert ratio is not None
    size, err = _size(args.get("size"))
    if err:
        return {"ok": False, "error": err}
    assert size is not None
    ref_url = str(args.get("ref_url") or "").strip()
    if ref_url and not (
            _DOC_KEY_RE.match(ref_url) or _DOC_PATH_RE.search(ref_url)
            or ref_url.startswith(("/", "http://", "https://"))):
        return {"ok": False,
                "error": "ref_url must be a 'doc/...' intake key, a "
                         "/api/documents/... path, or an http(s) image URL"}
    try:
        from backend import gemini_pool
        if not (gemini_pool.configured_keys()
                or os.environ.get("ADA_GEMINI_REST_BASE_URL")):
            return {"ok": False,
                    "error": "image rendering isn't configured — "
                             "GEMINI_API_KEY is missing"}
    except Exception:
        pass

    ref = None
    async with httpx.AsyncClient(timeout=60.0) as client:
        if ref_url:
            ref, err = await _resolve_ref(client, ref_url)
            if err:
                return {"ok": False, "error": err}
    try:
        data, model_text, err = await _render(prompt, ref, ratio, size)
    except Exception as exc:
        return {"ok": False,
                "error": f"the image model call failed: {exc}"}
    if err:
        return {"ok": False, "error": err,
                "model_text": model_text or ""}
    assert data is not None
    if len(data) > _IMG_MAX_BYTES:
        return {"ok": False, "error": "the rendered image is too large"}
    mime = "image/png"
    try:
        path = _save(data, mime)
    except OSError as exc:
        logger.warning("render save failed: %s", exc)
        return {"ok": False,
                "error": f"the image rendered but saving it failed: {exc}"}
    slug = re.sub(r"[^a-z0-9]+", "-", (prompt or "image").lower())[:40] \
        .strip("-") or "image"
    from backend import traffic_camera
    url_out = await asyncio.to_thread(
        traffic_camera.publish_relay, data, slug, mime, "render")
    if not url_out:
        return {"ok": False,
                "file": str(path),
                "error": "the image rendered and was saved, but publishing "
                         "a shareable URL failed (input-bridge /frame) — "
                         "the artifact is kept at 'file'; a retry renders "
                         "a fresh image"}
    out: dict[str, Any] = {
        "ok": True,
        "image_url": url_out,
        "title": prompt[:60],
        "file": str(path),
        "aspect": ratio,
        "size": size,
        "model": _model(),
        "bytes": len(data),
        "note": "image_url is the shareable render — relays post it as a "
                "photo automatically; 'file' is the local artifact.",
    }
    if model_text:
        out["model_text"] = model_text[:400]
    return out
