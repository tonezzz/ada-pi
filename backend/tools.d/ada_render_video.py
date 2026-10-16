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

Spend metering (card ada-render-video-bypasses-litellm): a submitted
render appends a flat-cost row (secs * per-second rate) to the local
usage ledger (usage_ledger -> ADA_USAGE_LOG, source='render_video'),
so ada_usage_summary's est_cost_usd covers Veo even though the call
never crosses the LiteLLM spend ledger. Pointing ADA_GEMINI_API_BASE
at a LiteLLM proxy's /gemini passthrough (upstream supports
predictLongRunning + operation polling + file download) routes the
whole flow through the gateway — the clip download URI is rewritten
onto the api base so the virtual key works end-to-end.

ref_url (image-to-video first frame) accepts the same three shapes as
ada_render_image:
  - an intake key 'doc/...' from a [document uploaded] session note —
    resolved in-process via the shared document_check engine;
  - a /api/documents/<key>/{image,preview} path (relative or absolute);
  - any other fetchable http(s) image URL.

Env:
    GEMINI_API_KEY / GOOGLE_API_KEY — required (unless _API_KEY below)
    ADA_RENDER_VIDEO_API_KEY — explicit key override (e.g. the LiteLLM
        virtual key when riding the /gemini passthrough)
    ADA_RENDER_VIDEO_MODEL  — default veo-3.1-fast-generate-preview
    ADA_RENDER_VIDEO_OPEN   — hard gate: must be "1" or every call
        refuses (owner-only tool); set only on approved lanes
    ADA_GATES_DIR           — instant kill-switch dir (default
        ~/.local/share/ada/gates); ada_render_video.closed or
        ada_render_video.<lane>.closed refuses renders
    ADA_GEMINI_API_BASE     — default https://generativelanguage.googleapis.com;
        set to <litellm-proxy>/gemini to ride the gateway passthrough
    ADA_RENDER_VIDEO_RATE_USD — per-second USD rate for models missing
        from the rate table (default 0.10)
    ADA_RENDER_DIR          — artifact dir, default ~/.local/share/ada/renders
    ADA_RENDER_VIDEO_WAIT_S — poll budget, default 120
    ADA_RENDER_VIDEO_POLL_S — poll interval, default 6
    ADA_SELF_URL            — self API base for relative ref paths
                              (default http://127.0.0.1:$ADA_PORT or :8000)

Every call that reaches Veo also logs a render_job post to the WP
boards (card ada-render-job-wp-log) via backend/wp_client.py — kind=
video meta for visibility + cost tracking. Fire-and-forget: it never
fails a render, and hosts without wp-boards.env skip it silently.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import secrets
import socket
import time
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
            "res": {
                "type": "string",
                "description": "720p (default) | 1080p (sharpest — "
                               "landscape/16:9 only).",
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


# Gemini API bills Veo per generated second (audio included), tiered
# by resolution where the rate card splits it — ai.google.dev pricing,
# Oct 2026; re-verify on change. Keys are model-id prefixes
# (-generate-preview / -001 share a rate); order matters: 'fast' and
# 'lite' must precede the bare 'veo-3.1' prefix.
_RATE_PER_SEC: dict[str, dict[str, float]] = {
    "veo-3.1-fast": {"720p": 0.10, "1080p": 0.12, "default": 0.10},
    "veo-3.1-lite": {"720p": 0.05, "1080p": 0.08, "default": 0.05},
    "veo-3.1":      {"720p": 0.40, "1080p": 0.40, "default": 0.40},
}
_FALLBACK_RATE = 0.10  # the deployed default model's 720p rate


def _rate_per_sec(model: str, res: str) -> float:
    """Estimated USD per generated second for the spend ledger."""
    for prefix, tiers in _RATE_PER_SEC.items():
        if model.startswith(prefix):
            return float(tiers.get(res) or tiers["default"])
    try:
        return float(os.environ.get("ADA_RENDER_VIDEO_RATE_USD")
                     or _FALLBACK_RATE)
    except ValueError:
        return _FALLBACK_RATE


def _api_key() -> str:
    return (os.environ.get("ADA_RENDER_VIDEO_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY") or "")


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


def _gate_closed() -> str | None:
    """Owner kill-switch — a file in ADA_GATES_DIR closes the tool
    instantly, no restart: <tool>.closed gates every lane,
    <tool>.<ADA_INSTANCE_ID>.closed gates one lane."""
    d = Path(os.path.expanduser(
        os.environ.get("ADA_GATES_DIR") or "~/.local/share/ada/gates"))
    lane = os.environ.get("ADA_INSTANCE_ID") or ""
    names = ["ada_render_video.closed"]
    if lane:
        names.append(f"ada_render_video.{lane}.closed")
    if any((d / n).exists() for n in names):
        return ("video rendering is switched off by the owner right "
                "now — tell the user it's temporarily closed")
    return None


_RES_ALIASES = {"720": "720p", "hd": "720p", "1080": "1080p",
                 "fullhd": "1080p", "fhd": "1080p"}


def _res(raw: Any) -> tuple[str | None, str | None]:
    """-> (resolution, error). Blank defaults to 720p."""
    val = str(raw or "").strip().lower()
    if not val:
        return "720p", None
    if val in ("720p", "1080p"):
        return val, None
    if val in _RES_ALIASES:
        return _RES_ALIASES[val], None
    return None, (f"unknown resolution {raw!r} — video renders at "
                  "720p (default) or 1080p (landscape only)")


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
                  ref: tuple[bytes, str] | None, ratio: str, secs: int,
                  res: str = "720p"
                  ) -> tuple[str | None, str | None]:
    """POST predictLongRunning -> (operation name, None) or (None, err)."""
    instance: dict[str, Any] = {"prompt": prompt}
    if ref:
        data, mime = ref
        instance["image"] = {
            "bytesBase64Encoded": base64.b64encode(data).decode(),
            "mimeType": mime}
    body = {
        "instances": [instance],
        "parameters": {"aspectRatio": ratio, "durationSeconds": secs,
                       "resolution": res},
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
    """-> (mp4 bytes, None) or (None, error).

    Veo returns an absolute generativelanguage download URI — fine on
    the direct path, but through a LiteLLM /gemini passthrough the clip
    must also ride the proxy (a virtual key 401s against Google), so
    the Google host is rewritten onto the api base the same way the
    LiteLLM docs prescribe."""
    google_root = f"{_DEFAULT_API_BASE}/v1beta/"
    base = _api_base()
    if base != _DEFAULT_API_BASE and uri.startswith(google_root):
        uri = f"{base}/v1beta/" + uri[len(google_root):]
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


def _arch_publish(data: bytes, name: str) -> str | None:
    """Durable public URL via the LINE relay's media archive — the
    arch_img route serves files under LINE_MEDIA_ARCHIVE_DIR at
    {public}/arch/<dir>/<name> with the right mime, no size cap. The
    input-bridge /frame hop dies on multi-MB clips (base64 POST inside
    a 10s urllib call), so video publishes here first."""
    arch = Path(os.path.expanduser(
        os.environ.get("LINE_MEDIA_ARCHIVE_DIR")
        or "~/.local/share/ada/line-media"))
    try:
        d = arch / "renders"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
    except OSError as exc:
        logger.warning("arch publish failed: %s", exc)
        return None
    base = (os.environ.get("ADA_RELAY_PUBLIC_BASE")
            or os.environ.get("LINE_PUBLIC_IMG_BASE")
            or "https://157.85.102.125.sslip.io/relay-img").rstrip("/")
    return f"{base}/arch/renders/{name}"


def _save(data: bytes) -> Path:
    d = Path(os.path.expanduser(
        os.environ.get("ADA_RENDER_DIR") or _DEFAULT_DIR))
    d.mkdir(parents=True, exist_ok=True)
    path = d / (f"render-vid-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                f"-{secrets.token_hex(3)}.mp4")
    path.write_bytes(data)
    return path


# --- render_job board logging (card ada-render-job-wp-log) --------------
# Every call that reaches Veo (submit or resume) lands one render_job
# post on the WP boards so renders are visible and cost-trackable. The
# write is fire-and-forget: it must never fail a render or hold _finish's
# return, and hosts without wp-boards.env skip it silently.

_WP_TASKS: set[asyncio.Task] = set()

# Rough USD per generated second at published list prices — an estimate
# for cost tracking only, NOT a billing figure. veo-3.1-fast runs
# ~$0.10/s at 720p and ~$0.12/s at 1080p; full veo-3.x ~$0.40/s.
def _est_cost_usd(secs: int, res: str) -> float:
    return round(_rate_per_sec(_model(), res) * secs, 4)


def _requested_by(runner: Any) -> str:
    """lane/user best-effort: ADA_INSTANCE_ID plus the caller's memory
    identity when the session resolved one."""
    lane = os.environ.get("ADA_INSTANCE_ID") or ""
    user = ""
    try:
        ident = runner._memory_identity()
        if isinstance(ident, str):
            user = ident
    except Exception:
        pass
    return "/".join(p for p in (lane, user) if p) or "unknown"


async def _post_render_job(env: dict[str, str], title: str,
                           body: str, meta: dict[str, Any]) -> None:
    try:
        from backend import wp_client as wp
        async with httpx.AsyncClient(
                timeout=15, follow_redirects=True) as client:
            res = await wp.create_post(client, env, title, body,
                                       post_type="render_job", meta=meta)
        if res.get("ok"):
            logger.info("render_job posted: %s", res.get("link"))
        else:
            logger.warning("render_job post failed: %s", res.get("error"))
    except Exception as exc:
        logger.warning("render_job post failed: %s", exc)


def _log_render_job(job: dict[str, Any], out: dict[str, Any]) -> None:
    """Schedule the render_job post for one finished call. Never raises,
    never blocks — a WP outage or slow board cannot touch the render."""
    try:
        from backend import wp_client as wp
        env = wp.env()
        if not wp.configured(env):
            return  # no wp-boards.env on this host — skip silently
        status = ("ok" if out.get("ok")
                  else "pending" if out.get("pending") else "error")
        err = str(out.get("error") or "")
        est = _est_cost_usd(job["secs"], job["res"])
        meta: dict[str, Any] = {
            "kind": "video",
            "prompt": job["prompt"],
            "model": _model(),
            "secs": job["secs"],
            "aspect": job["ratio"],
            "res": job["res"],
            "ref_url": job["ref_url"],
            "operation": job["operation"],   # Veo op — resume dedup key
            "duration_s": round(time.monotonic() - job["t0"], 1),
            "host": socket.gethostname(),
            "backend": "veo",
            "status": status,
            "output_url": out.get("video_url") or "",
            "source": "ada_render_video",
            "requested_by": _requested_by(job["runner"]),
            "est_cost_usd": est,
            # reserved for extend chains — this tool renders one op per
            # job and a resume shares the submit's operation (dedup lands
            # it on the same post), so every job is a root here.
            "parent_job": "",
            "chain_index": 0,
        }
        if status == "error":
            meta["error"] = err[:400]
        short = job["prompt"][:60].strip() or "video clip"
        title = f"Render — {short} {datetime.now().strftime('%Y-%m-%d')}"
        url_out = str(out.get("video_url") or "")
        body = (f"<p>{job['prompt'] or '(resumed render)'}</p>\n"
                f"<p>{_model()} · {job['secs']}s · {job['ratio']} · "
                f"{job['res']} · est ${est:.2f}</p>\n"
                + (f'<p><a href="{url_out}">video</a></p>\n'
                   if url_out else "")
                + (f"<p>error: {err[:200]}</p>\n"
                   if status == "error" else ""))
        task = asyncio.create_task(
            _post_render_job(env, title, body, meta))
        _WP_TASKS.add(task)
        task.add_done_callback(_WP_TASKS.discard)
    except Exception as exc:
        logger.info("render_job log skipped: %s", exc)
def _session_id(runner: Any) -> str | None:
    """Calling session via the runner.context facade — None for REST/
    test callers and MagicMock runners."""
    sid = getattr(getattr(runner, "context", None), "session_id", None)
    return sid if isinstance(sid, str) and sid else None


def _meter_submit(runner: Any, op_name: str, secs: int, ratio: str,
                  res: str) -> float:
    """Append the estimated spend to the local usage ledger at submit
    time — the render is billed once the API accepts it, and the direct
    call never crosses the LiteLLM spend ledger. Returns the estimate
    for the result payload. Never raises: metering must not break a
    paid render."""
    est = round(secs * _rate_per_sec(_model(), res), 4)
    try:
        from backend.usage_tracker import usage_ledger
        usage_ledger.record(
            source="render_video",
            session_id=_session_id(runner),
            est_cost_usd=est,
            detail={
                "kind": "veo_render",
                "model": _model(), "secs": secs,
                "aspect": ratio, "res": res,
                "operation": op_name,
                "api_base": _api_base(),
            })
    except Exception as exc:
        logger.warning("render spend metering failed: %s", exc)
    return est


async def _finish(runner: Any, client: httpx.AsyncClient, api_key: str,
                  op_name: str, prompt: str, ratio: str, secs: int,
                  est_cost: float | None = None
                  ) -> dict[str, Any]:
    """Poll a submitted/resumed operation to a published clip URL."""
    payload, err, pending = await _poll_done(client, api_key, op_name)
    if err:
        out: dict[str, Any] = {"ok": False, "error": err}
        if pending:
            out["operation"] = op_name
            out["pending"] = True
            if est_cost is not None:
                out["est_cost_usd"] = est_cost
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
    url_out = _arch_publish(data, path.name)
    if not url_out:
        from backend import traffic_camera
        url_out = await asyncio.to_thread(
            traffic_camera.publish_relay, data, slug, "video/mp4",
            "render")
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
    if est_cost is not None:
        out["est_cost_usd"] = est_cost
    if filtered:
        out["filtered"] = filtered
        out["note"] += (" Note: " + "; ".join(filtered)
                        + " — say so if the clip seems muted/cut.")
    return out


async def _attempt(runner: Any, client: httpx.AsyncClient, api_key: str,
                   job: dict[str, Any]) -> dict[str, Any]:
    """Submit-or-resume flow; fills job['operation'] once Veo accepts."""
    if job["operation"]:
        return await _finish(runner, client, api_key, job["operation"],
                             job["prompt"], job["ratio"], job["secs"])
    ref = None
    if job["ref_url"]:
        ref, err = await _resolve_ref(client, job["ref_url"])
        if err:
            return {"ok": False, "error": err}
    op_name, err = await _submit(client, api_key, job["prompt"], ref,
                                 job["ratio"], job["secs"], job["res"])
    if err:
        return {"ok": False, "error": err}
    assert op_name is not None
    job["operation"] = op_name
    est = _meter_submit(runner, op_name, job["secs"], job["ratio"],
                        job["res"])
    logger.info("veo render submitted: %s (~$%.4f est)", op_name, est)
    return await _finish(runner, client, api_key, op_name,
                         job["prompt"], job["ratio"], job["secs"],
                         est_cost=est)


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    closed = _gate_closed()
    if closed:
        return {"ok": False, "error": closed}
    if os.environ.get("ADA_RENDER_VIDEO_OPEN") != "1":
        return {"ok": False, "error": (
            "video rendering isn't open on this lane — it's an "
            "owner-only tool right now; tell the user plainly")}
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
    res, err = _res(args.get("res"))
    if err:
        return {"ok": False, "error": err}
    assert res is not None
    if res == "1080p" and ratio != "16:9":
        return {"ok": False, "error": (
            "1080p only renders landscape (16:9) — portrait tops "
            "out at 720p")}
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

    job = {"t0": time.monotonic(), "prompt": prompt, "ratio": ratio,
           "secs": secs, "res": res, "ref_url": ref_url,
           "operation": operation, "runner": runner}
    async with httpx.AsyncClient(timeout=90.0) as client:
        out = await _attempt(runner, client, api_key, job)
    _log_render_job(job, out)
    return out
