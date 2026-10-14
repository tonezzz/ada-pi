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
ref_urls is the same shape as a list — the image model composes several
references in one render ('combine these photos').

Iterate-on-last (card render-flow-quality-pass): the session's last
successful render is remembered (its render-dir file, not a RAM copy).
A call with no ref args auto-feeds it as the edit base, so 'make it
warmer' keeps composition instead of rerolling — fresh=true forces a
clean generate. Entries expire after ADA_RENDER_LAST_TTL_S.

Vision self-check: after each render one cheap flash-lite pass compares
the output to the ask; an obvious miss (wrong subject, garbled text)
auto-retries once with the problem folded back into the prompt. The
check is advisory — any check failure just skips it, a render is never
held back by the reviewer.

Transient retry: the image call itself gets one retry on gateway/
transient failures (5xx, timeouts, connection errors). Quota-shaped
errors are excluded — gemini_pool already rotated keys; a drained pool
is spent, not transient.

Env:
    GEMINI_API_KEY / GOOGLE_API_KEY — required (pool bookkeeping; the
        actual request key is ADA_GEMINI_REST_API_KEY when the gateway
        env is set)
    ADA_RENDER_IMAGE_MODEL — default gemini-3.1-flash-image
    ADA_RENDER_CHECK       — '0' disables the vision self-check
    ADA_RENDER_CHECK_MODEL — default gemini-3.5-flash-lite
    ADA_RENDER_LAST_TTL_S  — iterate-on-last memory, default 14400 (4h)
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
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from collections import OrderedDict
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
                               "path, or any fetchable image URL. Omit "
                               "it to iterate on the session's last "
                               "render — it's auto-attached as the edit "
                               "base; pass fresh=true for an unrelated "
                               "new image.",
            },
            "ref_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of reference images (same "
                               "shapes as ref_url, max 4) to compose in "
                               "one render — 'combine these photos'.",
            },
            "fresh": {
                "type": "boolean",
                "description": "true = generate unrelated to this "
                               "session's previous render (skips the "
                               "auto-attached last render).",
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

_REF_MAX = 4
_CHECK_MODEL = "gemini-3.5-flash-lite"
_LAST_CAP = 16
_LAST_TTL_S = 14400.0

# Iterate-on-last store: session -> the last render's saved file (the
# artifact already lives on disk in ADA_RENDER_DIR — hold the path, not
# a RAM copy of multi-MB PNGs).
_LAST_RENDER: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

# Aspect intent: page-ish asks and explicit pixel dims point the canvas
# away from the square default — real pages are tall.
_PAGE_WORDS_RE = re.compile(
    r"landing\s*page|web\s*page|web\s*site|home\s*page|homepage|"
    r"full[\s-]*page|landing\s*site|page\s*layout", re.I)
_DIMS_RE = re.compile(
    r"\b(\d{3,5})\s*(?:px|pixels)?\s*[x×]\s*(\d{3,5})\s*(?:px|pixels)?\b",
    re.I)
_ASPECT_VALUES = {"16:9": 16 / 9, "4:3": 4 / 3, "1:1": 1.0,
                  "3:4": 3 / 4, "9:16": 9 / 16}

# Transient (worth one retry) vs quota (spent — gemini_pool already
# rotated keys) vs client errors (retrying can't help).
_TRANSIENT_RE = re.compile(
    r"timed? ?out|timeout|unavailable|overloaded|temporar|connection|"
    r"conn refused|reset|broken pipe|eof|\b50[0234]\b|\b408\b|"
    r"try again|upstream", re.I)


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


def _session_key(runner: Any) -> str:
    """Per-session bucket for iterate-on-last. runner.context.session_id
    is the tools.d seam; anything else (REST/test callers, a missing
    context) shares the '_' bucket."""
    ctx = getattr(runner, "context", None)
    sid = getattr(ctx, "session_id", None)
    return sid if isinstance(sid, str) and sid else "_"


def _last_ttl() -> float:
    try:
        return float(os.environ.get(
            "ADA_RENDER_LAST_TTL_S", "") or _LAST_TTL_S)
    except ValueError:
        return _LAST_TTL_S


def _last_render(session: str) -> dict[str, Any] | None:
    ent = _LAST_RENDER.get(session)
    if not ent:
        return None
    if time.time() - float(ent.get("at") or 0) > _last_ttl():
        _LAST_RENDER.pop(session, None)
        return None
    if not Path(str(ent.get("path") or "")).is_file():
        _LAST_RENDER.pop(session, None)
        return None
    _LAST_RENDER.move_to_end(session)
    return ent


def _remember_render(session: str, path: Path, mime: str, prompt: str,
                     url: str) -> None:
    _LAST_RENDER[session] = {
        "path": str(path), "mime": mime, "prompt": prompt[:200],
        "image_url": url, "at": time.time()}
    _LAST_RENDER.move_to_end(session)
    while len(_LAST_RENDER) > _LAST_CAP:
        _LAST_RENDER.popitem(last=False)


def _truthy(raw: Any) -> bool:
    if raw is True:
        return True
    return isinstance(raw, str) and raw.strip().lower() in (
        "1", "true", "yes", "on")


def _is_transient(exc: BaseException) -> bool:
    """Gateway/transport-shaped failure worth one retry. Quota-shaped
    errors are excluded — the pool already walked its keys, and a
    drained pool is spent, not transient. Hard 4xx can't heal either."""
    from backend import gemini_pool
    try:
        if gemini_pool.is_quota_error(exc):
            return False
    except Exception:
        pass
    for attr in ("code", "status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            if v in (408, 500, 502, 503, 504):
                return True
            if 400 <= v < 500:
                return False
    if isinstance(exc, (httpx.HTTPError, TimeoutError,
                        asyncio.TimeoutError, ConnectionError)):
        return True
    return bool(_TRANSIENT_RE.search(str(exc)))


def _check_enabled() -> bool:
    return os.environ.get("ADA_RENDER_CHECK", "1").strip().lower() \
        not in ("0", "off", "false", "no")


def _shrink_for_check(data: bytes) -> tuple[bytes, str]:
    """<=800px JPEG for the review pass — a 4K PNG is needless tokens."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.thumbnail((800, 800))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=70)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return data, "image/png"


def _check_config() -> Any:
    schema = {"type": "object",
              "properties": {
                  "match": {"type": "boolean"},
                  "problem": {"type": "string"}},
              "required": ["match", "problem"],
              "additionalProperties": False}
    try:
        from google.genai import types
        return types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=schema)
    except Exception:
        return {"response_mime_type": "application/json",
                "response_json_schema": schema}


async def _self_check(data: bytes, prompt: str
                      ) -> dict[str, Any] | None:
    """One cheap vision verdict on the render — {match, problem} or None
    when the check itself fails (it is advisory, never a blocker)."""
    try:
        img, mime = _shrink_for_check(data)
        contents = [{"role": "user", "parts": [
            {"text": (
                "An image generator was given this brief: "
                f"{prompt[:1500]!r}\n"
                "Look at the image it produced. match=true when it "
                "visibly satisfies the brief; match=false only on an "
                "obvious miss — wrong subject entirely, blank or "
                "corrupt output, or text the brief asked for that is "
                "garbled/unreadable. Style nits are a pass. On a miss, "
                "problem= the one concrete thing to fix.")},
            {"inline_data": {
                "mime_type": mime,
                "data": base64.b64encode(img).decode()}}]}]
        from backend import gemini_pool
        resp = await gemini_pool.generate(
            os.environ.get("ADA_RENDER_CHECK_MODEL") or _CHECK_MODEL,
            contents, config=_check_config(), tool="render_image_check")
        text = " ".join(
            str(getattr(p, "text") or "")
            for c in getattr(resp, "candidates", None) or []
            for p in (getattr(getattr(c, "content", None),
                              "parts", None) or [])).strip()
        if not text:
            text = str(getattr(resp, "text", "") or "").strip()
        verdict = json.loads(text)
        return {"match": bool(verdict.get("match")),
                "problem": str(verdict.get("problem") or "")[:300]}
    except Exception as exc:
        logger.info("render self-check skipped: %s", exc)
        return None


def _page_hint() -> str:
    return ("page-ish ask: image gen draws a PICTURE of a page capped at "
            "fixed aspect ratios — real pages are far taller. For a long "
            "page, render each section separately (portrait) and stack "
            "them; if the member wants a working page (HTML), that's a "
            "code/CMS task — say so instead of over-promising.")


def _nearest_aspect(w: int, h: int) -> str:
    if h <= 0:
        return "1:1"
    want = w / h
    return min(_ASPECT_VALUES, key=lambda k: abs(_ASPECT_VALUES[k] - want))


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


async def _render(prompt: str, refs: list[tuple[bytes, str]],
                  ratio: str, size: str
                  ) -> tuple[bytes | None, str | None, str | None]:
    """-> (image bytes, model text, None) or (None, None, error)."""
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for data, mime in refs:
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


def _ref_specs(args: dict[str, Any]) -> tuple[list[str], str | None]:
    """-> (ref specs, error). Merges ref_url + ref_urls; validates each
    shape before any fetch."""
    raw_multi = args.get("ref_urls")
    if raw_multi is None:
        multi: list[Any] = []
    elif isinstance(raw_multi, str):
        multi = [raw_multi]
    elif isinstance(raw_multi, (list, tuple)):
        multi = list(raw_multi)
    else:
        return [], "ref_urls must be a list of image URLs/keys"
    specs = [str(args.get("ref_url") or "").strip()]
    specs += [str(v or "").strip() for v in multi]
    specs = [s for s in specs if s]
    if len(specs) > _REF_MAX:
        return [], f"at most {_REF_MAX} reference images per render"
    bad = [s for s in specs if not (
        _DOC_KEY_RE.match(s) or _DOC_PATH_RE.search(s)
        or s.startswith(("/", "http://", "https://")))]
    if bad:
        return [], ("each ref_url/ref_urls entry must be a 'doc/...' "
                    "intake key, a /api/documents/... path, or an "
                    f"http(s) image URL — bad: {bad[0]!r}")
    return specs, None


async def _resolve_refs(specs: list[str]
                        ) -> tuple[list[tuple[bytes, str]], str | None]:
    refs: list[tuple[bytes, str]] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for spec in specs:
            ref, err = await _resolve_ref(client, spec)
            if err:
                return [], err
            assert ref is not None
            refs.append(ref)
    return refs, None


async def _render_retry(prompt: str, refs: list[tuple[bytes, str]],
                        ratio: str, size: str
                        ) -> tuple[bytes | None, str | None, str | None]:
    """_render + one automatic retry on transient (non-quota) failures —
    a gateway blip shouldn't reach the member as an error."""
    try:
        return await _render(prompt, refs, ratio, size)
    except Exception as exc:
        if not _is_transient(exc):
            raise
        logger.info("render transient failure, retrying once: %s", exc)
        await asyncio.sleep(1.5)
        return await _render(prompt, refs, ratio, size)


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    closed = _gate_closed()
    if closed:
        return {"ok": False, "error": closed}
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False,
                "error": "a prompt describing the image is required"}
    prompt = prompt[:_PROMPT_MAX]
    aspect_given = bool(str(args.get("aspect") or "").strip())
    ratio, err = _aspect(args.get("aspect"))
    if err:
        return {"ok": False, "error": err}
    assert ratio is not None
    size, err = _size(args.get("size"))
    if err:
        return {"ok": False, "error": err}
    assert size is not None
    # Aspect intent: 'a 1920x1080 banner' maps to its nearest canvas; a
    # page-ish ask with no explicit aspect goes portrait — real pages
    # are tall. An explicit aspect always wins.
    page_intent = bool(_PAGE_WORDS_RE.search(prompt))
    dims = _DIMS_RE.search(prompt)
    if not aspect_given:
        if dims:
            ratio = _nearest_aspect(int(dims.group(1)),
                                    int(dims.group(2)))
        elif page_intent:
            ratio = "9:16"
    specs, err = _ref_specs(args)
    if err:
        return {"ok": False, "error": err}
    try:
        from backend import gemini_pool
        if not (gemini_pool.configured_keys()
                or os.environ.get("ADA_GEMINI_REST_BASE_URL")):
            return {"ok": False,
                    "error": "image rendering isn't configured — "
                             "GEMINI_API_KEY is missing"}
    except Exception:
        pass

    skey = _session_key(runner)
    iterated = None
    refs, err = await _resolve_refs(specs)
    if err:
        return {"ok": False, "error": err}
    if not refs and not _truthy(args.get("fresh")):
        last = _last_render(skey)
        if last:
            try:
                data_last = Path(str(last["path"])).read_bytes()
            except OSError:
                data_last = b""
            if 0 < len(data_last) <= _REF_MAX_BYTES:
                refs = [(data_last, str(last["mime"]))]
                iterated = last

    try:
        data, model_text, err = await _render_retry(
            prompt, refs, ratio, size)
    except Exception as exc:
        return {"ok": False,
                "error": f"the image model call failed: {exc}"}
    if err:
        return {"ok": False, "error": err,
                "model_text": model_text or ""}
    assert data is not None

    # Vision self-check: one cheap verdict vs the ask; an obvious miss
    # re-renders once with the problem folded back into the prompt.
    check = None
    retried = False
    if _check_enabled():
        check = await _self_check(data, prompt)
        if check and not check["match"]:
            fix_prompt = (
                prompt + "\n\nThe previous attempt was rejected on "
                f"review: {check['problem']}. Fix exactly that and keep "
                "the rest of the brief.")
            try:
                d2, t2, e2 = await _render_retry(
                    fix_prompt, refs, ratio, size)
            except Exception as exc:
                logger.info("self-check re-render failed: %s", exc)
                d2, t2, e2 = None, None, str(exc)
            if d2:
                data, model_text, retried = d2, t2, True
                check = await _self_check(data, prompt) or check

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
    _remember_render(skey, path, mime, prompt, url_out)
    note = ("image_url is the shareable render — relays post it as a "
            "photo automatically; 'file' is the local artifact.")
    if size in ("2K", "4K"):
        note += (" LINE/TG re-encode the photo bubble — include the "
                 "image_url link in your reply so the member can grab "
                 "the original-quality file.")
    out: dict[str, Any] = {
        "ok": True,
        "image_url": url_out,
        "title": prompt[:60],
        "file": str(path),
        "aspect": ratio,
        "size": size,
        "model": _model(),
        "bytes": len(data),
        "note": note,
    }
    if iterated:
        out["iterated"] = True
        out["iterated_from"] = iterated.get("prompt") or ""
    if refs:
        out["ref_count"] = len(refs)
    if check is not None:
        out["self_check"] = {"match": check["match"],
                             "problem": check["problem"],
                             "retried": retried}
        if not check["match"]:
            out["self_check"]["note"] = (
                "still flagged after one retry — tell the member it may "
                "be off and offer another pass")
    if page_intent:
        out["page_hint"] = _page_hint()
    if size in ("2K", "4K"):
        out["full_res_url"] = url_out
    if model_text:
        out["model_text"] = model_text[:400]
    return out
