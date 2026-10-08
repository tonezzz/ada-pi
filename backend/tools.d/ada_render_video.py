"""ada_render_video — drop-in tool (card veo-video-render).

Render a short video clip from an assembled prompt — or animate a
reference image — through Veo's REST predictLongRunning endpoint on
generativelanguage (default veo-3.1-fast-generate-preview). The call
returns an operation name immediately; run() polls it with a bounded
wait (~120s default, env-tunable) until done, downloads the mp4, saves
it under ADA_RENDER_DIR, and publishes through the same input-bridge
/frame asset hop ada_render_image uses — the returned video_url is the
playable clip, fetchable by relays and castable via cast_to_screen
action='play'.

A render that outlives the wait budget returns ok:false + pending +
the operation name: re-calling with operation= resumes the poll
without billing a second render (Veo keeps generating server-side).

ref_url (image-to-video first frame) accepts the same three shapes as
ada_render_image:
  - an intake key 'doc/...' from a [document uploaded] session note —
    resolved in-process via the shared document_check engine;
  - a /api/documents/<key>/{image,preview} path (relative or absolute);
  - any other fetchable http(s) image URL.

Env:
    GEMINI_API_KEY / GOOGLE_API_KEY — required
    ADA_RENDER_VIDEO_MODEL  — default veo-3.1-fast-generate-preview
    ADA_GEMINI_API_BASE     — default https://generativelanguage.googleapis.com
    ADA_RENDER_DIR          — artifact dir, default ~/.local/share/ada/renders
    ADA_RENDER_VIDEO_WAIT_S — poll budget, default 120
    ADA_RENDER_VIDEO_POLL_S — poll interval, default 6
    ADA_SELF_URL            — self API base for relative ref paths
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

logger = logging.getLogger("tools.ada_render_video")

DECLARATION = {
    "name": "ada_render_video",
    "description": (
        "Render a short video clip from a prompt (optionally animating "
        "the ref_url image) and return a playable clip URL. Renders "
        "take ~1-2 minutes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The assembled scene description — "
                               "subject, motion, style, audio cues; "
                               "not a bare keyword. Required unless "
                               "operation= is given.",
            },
            "ref_url": {
                "type": "string",
                "description": "Optional source image to animate: a "
                               "'doc/...' intake key, a /api/documents/... "
                               "path, or any fetchable image URL.",
            },
            "secs": {
                "type": "integer",
                "description": "Clip length: 4, 6, or 8 (default 8).",
            },
            "aspect": {
                "type": "string",
                "description": "landscape (16:9, default) | portrait "
                               "(9:16) | an explicit ratio.",
            },
            "operation": {
                "type": "string",
                "description": "Resume waiting on an in-flight render: "
                               "the 'operation' name returned by an "
                               "earlier call that answered pending. "
                               "No new render is billed.",
            },
        },
    },
}

_DEFAULT_MODEL = "veo-3.1-fast-generate-preview"
_DEFAULT_API_BASE = "https://generativelanguage.googleapis.com"
_DEFAULT_DIR = "~/.local/share/ada/renders"
_PROMPT_MAX = 2000
_REF_MAX_BYTES = 15 * 1024 * 1024
_VIDEO_MAX_BYTES = 64 * 1024 * 1024
_WAIT_S = 120.0
_POLL_S = 6.0

# Veo 3.x aspect ratios + spoken aliases — no square (honest error).
_ASPECT_RATIOS = {"16:9", "9:16"}
_ASPECT_ALIASES = {
    "landscape": "16:9", "wide": "16:9", "portrait": "9:16",
    "tall": "9:16", "vertical": "9:16",
}
_SECS_CHOICES = {4, 6, 8}

_DOC_KEY_RE = re.compile(r"^doc/[a-z0-9][a-z0-9._-]*$")
_DOC_PATH_RE = re.compile(
    r"/api/documents/([^?#]+?)/(image|preview|pdf)(?:[?#].*)?$")
_OP_RE = re.compile(r"^models/[\w.-]+/operations/[\w.-]+$")


def _api_key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get(
        "GOOGLE_API_KEY") or ""


def _api_base() -> str:
    return os.environ.get(
        "ADA_GEMINI_API_BASE", _DEFAULT_API_BASE).rstrip("/")


def _model() -> str:
    return os.environ.get("ADA_RENDER_VIDEO_MODEL") or _DEFAULT_MODEL


def _self_base() -> str:
    return (os.environ.get("ADA_SELF_URL")
            or f"http://127.0.0.1:{os.environ.get('ADA_PORT') or '8000'}"
            ).rstrip("/")


def _wait_s() -> float:
    try:
        return float(os.environ.get("ADA_RENDER_VIDEO_WAIT_S") or _WAIT_S)
    except ValueError:
        return _WAIT_S


def _poll_s() -> float:
    try:
        return float(os.environ.get("ADA_RENDER_VIDEO_POLL_S") or _POLL_S)
    except ValueError:
        return _POLL_S


def _aspect(raw: Any) -> tuple[str | None, str | None]:
    """-> (ratio, error). Blank defaults to landscape."""
    val = str(raw or "").strip().lower()
    if not val:
        return "16:9", None
    if val in _ASPECT_RATIOS:
        return val, None
    if val in _ASPECT_ALIASES:
        return _ASPECT_ALIASES[val], None
    m = re.fullmatch(r"(\d{1,2})\s*[:x×]\s*(\d{1,2})", val)
    if m and f"{m.group(1)}:{m.group(2)}" in _ASPECT_RATIOS:
        return f"{m.group(1)}:{m.group(2)}", None
    return None, (
        f"unknown aspect {raw!r} — Veo renders landscape (16:9) or "
        "portrait (9:16) only, no square")


def _secs(raw: Any) -> tuple[int | None, str | None]:
    """-> (seconds, error). Blank defaults to 8."""
    if raw in (None, ""):
        return 8, None
    try:
        n = int(str(raw).strip().rstrip("s"))
    except ValueError:
        n = -1
    if n in _SECS_CHOICES:
        return n, None
    return None, (
        f"unknown secs {raw!r} — Veo renders 4, 6, or 8 second clips")


def _ref_from_engine(key: str) -> tuple[bytes, str] | None:
    """Resolve a held intake key in-process — the document_check engine
    is a process-wide singleton shared with the /api/documents/*
    endpoints, so a 'doc/...' key needs no HTTP hop. Prefers the color
    archive rendition over the grayscale A4 print preview."""
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
        # absolute URL whose doc path isn't held locally — fall through
        # to a plain fetch (e.g. another ada instance's API).
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


def _api_error(payload: Any, status: int) -> str:
    msg = (payload.get("error") or {}).get("message") \
        if isinstance(payload, dict) else None
    return (f"the video model returned HTTP {status}"
            + (f": {str(msg)[:160]}" if msg else ""))


async def _submit(client: httpx.AsyncClient, api_key: str, prompt: str,
                  ref: tuple[bytes, str] | None, ratio: str, secs: int
                  ) -> tuple[str | None, str | None]:
    """POST predictLongRunning -> (operation name, None) or (None, err)."""
    instance: dict[str, Any] = {"prompt": prompt}
    if ref:
        data, mime = ref
        instance["image"] = {"inlineData": {
            "mimeType": mime, "data": base64.b64encode(data).decode()}}
    body = {
        "instances": [instance],
        "parameters": {"aspectRatio": ratio, "durationSeconds": secs},
    }
    url = f"{_api_base()}/v1beta/models/{_model()}:predictLongRunning"
    try:
        resp = await client.post(
            url, json=body, headers={"x-goog-api-key": api_key},
            timeout=45)
    except (httpx.HTTPError, TimeoutError) as exc:
        return None, (f"I couldn't reach the video model "
                      f"({exc.__class__.__name__})")
    try:
        payload = resp.json()
    except Exception:
        payload = {}
    if resp.status_code >= 400:
        return None, _api_error(payload, resp.status_code)
    name = payload.get("name") if isinstance(payload, dict) else None
    if not name:
        return None, "the video model accepted the render but returned " \
                     "no operation name — can't track it"
    return str(name), None


def _parse_operation(payload: dict[str, Any]
                     ) -> tuple[str | None, str | None, list[str]]:
    """Done-operation -> (video download uri, error, filtered reasons)."""
    op_err = payload.get("error")
    if isinstance(op_err, dict) and op_err.get("message"):
        return None, (f"the video render failed: "
                      f"{str(op_err['message'])[:160]}"), []
    resp = payload.get("response") or {}
    gvr = resp.get("generateVideoResponse") or {}
    filtered = [str(r) for r in gvr.get("raiMediaFilteredReasons") or []]
    for sample in gvr.get("generatedSamples") or []:
        uri = ((sample.get("video") or {}).get("uri"))
        if uri:
            return str(uri), None, filtered
    return None, ("the video render finished but returned no clip"
                  + (f" ({'; '.join(filtered)})" if filtered else "")
                  + " — rephrase the prompt and try once more"), filtered


async def _poll_done(client: httpx.AsyncClient, api_key: str, op_name: str
                     ) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Bounded poll -> (done payload, None, False) | (None, err, pending)."""
    url = f"{_api_base()}/v1beta/{op_name}"
    deadline = asyncio.get_running_loop().time() + _wait_s()
    while True:
        try:
            resp = await client.get(
                url, headers={"x-goog-api-key": api_key}, timeout=20)
        except (httpx.HTTPError, TimeoutError) as exc:
            return None, (f"lost contact with the render while polling "
                          f"({exc.__class__.__name__}) — call again with "
                          f"operation={op_name!r} to keep waiting"), True
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if resp.status_code >= 400:
            return None, _api_error(payload, resp.status_code), False
        if isinstance(payload, dict) and payload.get("done"):
            return payload, None, False
        if asyncio.get_running_loop().time() >= deadline:
            return None, (
                f"the video is still rendering after ~{int(_wait_s())}s "
                "— Veo can take a few minutes. Call ada_render_video "
                f"again with operation={op_name!r} to keep waiting; the "
                "render continues server-side and no new clip is billed"), True
        await asyncio.sleep(_poll_s())


async def _download(client: httpx.AsyncClient, api_key: str, uri: str
                    ) -> tuple[bytes | None, str | None]:
    """-> (mp4 bytes, None) or (None, error)."""
    try:
        resp = await client.get(
            uri, headers={"x-goog-api-key": api_key},
            follow_redirects=True, timeout=90)
    except (httpx.HTTPError, TimeoutError) as exc:
        return None, (f"the clip rendered but downloading it failed "
                      f"({exc.__class__.__name__})")
    if resp.status_code >= 400:
        return None, (f"the clip rendered but its download URL returned "
                      f"HTTP {resp.status_code}")
    if len(resp.content) > _VIDEO_MAX_BYTES:
        return None, "the rendered clip is too large to publish (64MB max)"
    if len(resp.content) < 500:
        return None, "the clip download returned an empty file"
    return resp.content, None


def _save(data: bytes) -> Path:
    d = Path(os.path.expanduser(
        os.environ.get("ADA_RENDER_DIR") or _DEFAULT_DIR))
    d.mkdir(parents=True, exist_ok=True)
    path = d / (f"render-vid-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                f"-{secrets.token_hex(3)}.mp4")
    path.write_bytes(data)
    return path


async def _finish(runner: Any, client: httpx.AsyncClient, api_key: str,
                  op_name: str, prompt: str, ratio: str, secs: int
                  ) -> dict[str, Any]:
    """Poll a submitted/resumed operation to a published clip URL."""
    payload, err, pending = await _poll_done(client, api_key, op_name)
    if err:
        out: dict[str, Any] = {"ok": False, "error": err}
        if pending:
            out["operation"] = op_name
            out["pending"] = True
        return out
    assert payload is not None
    uri, err, filtered = _parse_operation(payload)
    if err:
        return {"ok": False, "error": err}
    assert uri is not None
    data, err = await _download(client, api_key, uri)
    if err:
        return {"ok": False, "error": err, "operation": op_name}
    assert data is not None
    try:
        path = _save(data)
    except OSError as exc:
        logger.warning("render save failed: %s", exc)
        return {"ok": False,
                "error": f"the clip rendered but saving it failed: {exc}"}
    slug = re.sub(r"[^a-z0-9]+", "-", (prompt or "clip").lower())[:40] \
        .strip("-") or "clip"
    from backend import traffic_camera
    url_out = await asyncio.to_thread(
        traffic_camera.publish_relay, data, slug, "video/mp4", "render")
    if not url_out:
        return {"ok": False,
                "file": str(path),
                "error": "the video rendered and was saved, but publishing "
                         "a playable URL failed (input-bridge /frame) — "
                         "the artifact is kept at 'file'; a retry renders "
                         "a fresh clip"}
    out = {
        "ok": True,
        "video_url": url_out,
        "title": (prompt or "video clip")[:60],
        "file": str(path),
        "secs": secs,
        "aspect": ratio,
        "model": _model(),
        "bytes": len(data),
        "note": "video_url is the playable mp4 clip — send it to a "
                "display with cast_to_screen(action='play', url=video_url) "
                "or share the link; 'file' is the local artifact.",
    }
    if filtered:
        out["filtered"] = filtered
        out["note"] += (" Note: " + "; ".join(filtered)
                        + " — say so if the clip seems muted/cut.")
    return out


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    operation = str(args.get("operation") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    if operation:
        if not _OP_RE.match(operation):
            return {"ok": False,
                    "error": "operation must be the 'models/.../operations/"
                             "...' name a pending call returned"}
    elif not prompt:
        return {"ok": False,
                "error": "a prompt describing the video is required"}
    prompt = prompt[:_PROMPT_MAX]
    ratio, err = _aspect(args.get("aspect"))
    if err:
        return {"ok": False, "error": err}
    assert ratio is not None
    secs, err = _secs(args.get("secs"))
    if err:
        return {"ok": False, "error": err}
    assert secs is not None
    api_key = _api_key()
    if not api_key:
        return {"ok": False,
                "error": "video rendering isn't configured — "
                         "GEMINI_API_KEY is missing"}

    ref_url = str(args.get("ref_url") or "").strip()
    if ref_url and not (
            _DOC_KEY_RE.match(ref_url) or _DOC_PATH_RE.search(ref_url)
            or ref_url.startswith(("/", "http://", "https://"))):
        return {"ok": False,
                "error": "ref_url must be a 'doc/...' intake key, a "
                         "/api/documents/... path, or an http(s) image URL"}

    async with httpx.AsyncClient(timeout=90.0) as client:
        if operation:
            return await _finish(runner, client, api_key, operation,
                                 prompt, ratio, secs)
        ref = None
        if ref_url:
            ref, err = await _resolve_ref(client, ref_url)
            if err:
                return {"ok": False, "error": err}
        op_name, err = await _submit(
            client, api_key, prompt, ref, ratio, secs)
        if err:
            return {"ok": False, "error": err}
        assert op_name is not None
        logger.info("veo render submitted: %s", op_name)
        return await _finish(runner, client, api_key, op_name,
                             prompt, ratio, secs)
