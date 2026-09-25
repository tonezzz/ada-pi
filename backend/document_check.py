"""Document intake pipeline — classify, measure, optimize for print/archive.

Stages: decode -> measure -> classify -> plan -> render -> hold.

Same shape as decision_check: an engine holds a lazy Gemini client and
exposes one async entry point. Imaging is pure PIL/numpy, RAM-only —
rendered artifacts live in a small in-process result store and are
served back by /api/documents/{key}/{pdf,preview}; nothing touches disk
(the archive service owns persistence — see chaba
docs/kb/document-archive-service.md).

Doc-type profiles decide output shape: most documents render onto a
printable A4 grayscale page; ID cards keep their physical size (ID-1 =
85.6 x 54 mm) centered on A4 so `lp -o print-scaling=none` reproduces
the real card size.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from google import genai
from google.genai import types

import numpy as np
from PIL import Image, ImageFilter, ImageOps

logger = logging.getLogger("documents.check")

DOC_MODEL = os.environ.get("DOC_MODEL", "gemini-2.5-flash")
DOC_TIMEOUT_S = float(os.environ.get("DOC_TIMEOUT_S", "60"))
RESULT_CAP = 20  # in-process result store: newest N intakes

A4_PX = (2480, 3508)  # A4 portrait at 300 DPI
DPI = 300
MM_PER_INCH = 25.4

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {
            "type": "string",
            "enum": ["id_card", "passport", "deed", "contract", "receipt",
                     "letter", "form", "photo", "other"],
        },
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
        "true_size_mm": {
            "type": "array",
            "items": {"type": "number"},
            "description": "[width, height] in mm if the document has a "
                           "known physical size (e.g. [85.6, 54] ID-1 card); "
                           "empty if unknown",
        },
        "orientation": {"type": "string", "enum": ["portrait", "landscape"]},
        "doc_number": {"type": "string"},
    },
    "required": ["doc_type", "confidence", "summary"],
    "additionalProperties": False,
}


@dataclass
class DocumentResult:
    ok: bool = False
    key: str | None = None
    doc_type: str = "other"
    confidence: float = 0.0
    summary: str = ""
    measured: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    durations_ms: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "ok": self.ok,
            "key": self.key,
            "doc_type": self.doc_type,
            "confidence": self.confidence,
            "summary": self.summary,
            "measured": self.measured,
            "plan": self.plan,
            "warnings": self.warnings,
            "durations_ms": self.durations_ms,
            "error": self.error,
        }
        if self.key:
            d["pdf_url"] = f"/api/documents/{self.key}/pdf"
            d["preview_url"] = f"/api/documents/{self.key}/preview"
        return d


# ---------------------------------------------------------------- imaging

def _to_gray_array(im: Image.Image) -> np.ndarray:
    return np.asarray(im.convert("L"), dtype=np.float32)


def _flat_field(gray: np.ndarray, radius: int = 25) -> np.ndarray:
    """Divide out the paper's shading: estimate background with a heavy
    blur, then normalize so the background lands near white."""
    bg_im = Image.fromarray(np.clip(gray, 0, 255).astype(np.uint8), mode="L")
    bg = np.asarray(bg_im.filter(ImageFilter.GaussianBlur(radius=radius)),
                    dtype=np.float32)
    bg = np.clip(bg, 20.0, 255.0)
    flat = gray / bg * 255.0
    return np.clip(flat, 0, 255)


def _levels(gray: np.ndarray, lo_pct: float = 1.0, hi_pct: float = 97.0) -> np.ndarray:
    lo, hi = np.percentile(gray, lo_pct), np.percentile(gray, hi_pct)
    if hi - lo < 10:
        hi = lo + 10
    return np.clip((gray - lo) / (hi - lo) * 255.0, 0, 255)


def _binarize_local(gray: np.ndarray, radius: int = 18,
                    factor: float = 0.88) -> np.ndarray:
    """Local-mean threshold: ink where the pixel sits below its local
    background estimate. Turns faint grayscale text into clean black-on-white
    that survives inkjet dithering."""
    src = Image.fromarray(np.clip(gray, 0, 255).astype(np.uint8), mode="L")
    mean = np.asarray(src.filter(ImageFilter.GaussianBlur(radius=radius)),
                      dtype=np.float32)
    bw = np.where(gray < mean * factor, 0, 255).astype(np.uint8)
    return np.asarray(Image.fromarray(bw, mode="L")
                      .filter(ImageFilter.MedianFilter(3)), dtype=np.uint8)


def enhance_for_print(im: Image.Image,
                      binarize: bool | None = None) -> Image.Image:
    """B&W print path: flatten shading -> stretch levels -> unsharp.

    Low-res scans (min side < 1600px) get a 4x Lanczos pre-upscale and, when
    `binarize` is true, a soft blend with a local-threshold pass (55% bilevel
    + 45% gray): ink prints crisp black while the paper keeps its organic
    texture. `binarize=None` auto-enables it for low-res scans; callers should
    pass False for documents with photos/halftones (id_card, passport,
    photo)."""
    w, h = im.size
    low_res = min(w, h) < 1600
    if binarize is None:
        binarize = low_res
    if low_res:
        im = im.resize((w * 4, h * 4), Image.LANCZOS)
    g = _flat_field(_to_gray_array(im))
    g = _levels(g, lo_pct=2.0, hi_pct=98.0)
    out = Image.fromarray(g.astype(np.uint8), mode="L")
    out = out.filter(ImageFilter.UnsharpMask(radius=3.0, percent=140,
                                             threshold=3))
    if binarize:
        bw = _binarize_local(np.asarray(out, dtype=np.float32),
                             radius=30, factor=0.90)
        out = Image.fromarray(
            (0.55 * bw.astype(np.float32)
             + 0.45 * np.asarray(out, dtype=np.float32)).astype(np.uint8),
            mode="L")
    return out


def enhance_for_archive(im: Image.Image) -> Image.Image:
    """Color archival path: per-channel white balance -> black point ->
    light unsharp. Keeps hue so red stamps/blue signatures survive."""
    a = np.asarray(im.convert("RGB"), dtype=np.float32)
    for ch in range(3):
        wp = np.percentile(a[:, :, ch], 98)
        a[:, :, ch] = np.clip(a[:, :, ch] * (255.0 / max(wp, 1.0)), 0, 255)
    lum = a.mean(axis=2)
    bp = np.percentile(lum, 1)
    a = np.clip((a - bp) / (255.0 - bp) * 255.0, 0, 255)
    out = Image.fromarray(a.astype(np.uint8))
    return out.filter(ImageFilter.UnsharpMask(radius=1.5, percent=70, threshold=4))


def mm_to_px(mm: float) -> int:
    return int(round(mm / MM_PER_INCH * DPI))


def render_a4(im: Image.Image, true_size_mm: tuple[float, float] | None = None,
              margin_mm: float = 12.0) -> tuple[Image.Image, dict[str, Any]]:
    """Place `im` (already enhanced, L mode) on a white A4 300dpi canvas.

    true_size_mm -> the document keeps its physical size (ID cards).
    Otherwise the document is scaled to fit inside the margins.
    Returns (canvas, placement report)."""
    canvas = Image.new("L", A4_PX, 255)
    margin = mm_to_px(margin_mm)
    max_w, max_h = A4_PX[0] - 2 * margin, A4_PX[1] - 2 * margin

    if true_size_mm:
        w_px, h_px = mm_to_px(true_size_mm[0]), mm_to_px(true_size_mm[1])
        # never exceed the printable area
        scale = min(1.0, max_w / w_px, max_h / h_px)
        w_px, h_px = int(w_px * scale), int(h_px * scale)
        mode = "true_size"
    else:
        scale = min(max_w / im.width, max_h / im.height)
        w_px, h_px = int(im.width * scale), int(im.height * scale)
        mode = "fit_a4"
    if w_px < 10 or h_px < 10:
        w_px, h_px, mode = min(im.width, max_w), min(im.height, max_h), "fit_a4"
    page = im.resize((w_px, h_px), Image.LANCZOS)
    x, y = (A4_PX[0] - w_px) // 2, (A4_PX[1] - h_px) // 2
    canvas.paste(page, (x, y))
    placement = {
        "mode": mode,
        "placed_px": [w_px, h_px],
        "placed_mm": [round(w_px / DPI * MM_PER_INCH, 1),
                      round(h_px / DPI * MM_PER_INCH, 1)],
        "dpi": DPI,
    }
    return canvas, placement


def to_pdf_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PDF", resolution=DPI)
    return buf.getvalue()


def to_jpeg_bytes(im: Image.Image, max_w: int = 1400, quality: int = 85) -> bytes:
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def measure(im: Image.Image) -> dict[str, Any]:
    """Cheap physical metrics + quality warnings."""
    lum = _to_gray_array(im)
    dpi = im.info.get("dpi")
    est_dpi = dpi[0] if isinstance(dpi, tuple) else None
    m = {
        "width": im.width,
        "height": im.height,
        "src_dpi": round(est_dpi, 0) if est_dpi else None,
        "mean_lum": round(float(lum.mean()), 1),
        "contrast": round(float(lum.std()), 1),
    }
    warnings = []
    if im.width < 900 or im.height < 900:
        warnings.append("low resolution — small text may not survive printing")
    elif est_dpi and est_dpi < 150:
        warnings.append(f"source tagged {est_dpi:.0f} dpi — likely a photo/screen capture")
    if m["contrast"] < 30:
        warnings.append("low contrast — faded scan or flat photo")
    if m["mean_lum"] < 90:
        warnings.append("very dark image")
    m["warnings"] = warnings
    return m


def split_spread(im: Image.Image) -> list[Image.Image] | None:
    """Detect a two-page spread (photo of an open document pair) by a
    dark vertical gutter near the middle. Returns [left, right] or None."""
    g = _to_gray_array(im)
    h, w = g.shape
    if w / h < 1.15:  # only landscape-ish images can be spreads
        return None
    band = g[:, int(w * 0.42):int(w * 0.58)]
    col_dark = (band < 120).mean(axis=0)
    if col_dark.max() < 0.5:  # no continuous dark gutter -> not a spread
        return None
    mid = int(w * 0.42) + int(np.argmax(col_dark))
    return [im.crop((0, 0, mid, h)), im.crop((mid, 0, w, h))]


def trim_border(im: Image.Image, pad: int = 6) -> Image.Image:
    a = _to_gray_array(im)
    mask = a < 235
    rows, cols = mask.sum(axis=1), mask.sum(axis=0)
    r = np.where(rows > 8)[0]
    c = np.where(cols > 8)[0]
    if len(r) == 0 or len(c) == 0:
        return im
    h, w = a.shape
    return im.crop((max(0, c.min() - pad), max(0, r.min() - pad),
                    min(w, c.max() + pad), min(h, r.max() + pad)))


# ---------------------------------------------------------------- engine

@dataclass
class _Held:
    pdf: bytes
    preview: bytes
    meta: dict[str, Any]


class DocumentCheckEngine:
    """intake() -> classify + measure + render; results held in RAM for
    preview/download and (later) the print/archive tools."""

    def __init__(self, client: Any = None) -> None:
        self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.model = DOC_MODEL
        self._client = client
        self._held: OrderedDict[str, _Held] = OrderedDict()

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("GEMINI_API_KEY is not set")
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    # -- held results ------------------------------------------------
    def held(self, key: str) -> _Held | None:
        return self._held.get(key)

    def _hold(self, key: str, pdf: bytes, preview: bytes, meta: dict) -> None:
        self._held[key] = _Held(pdf, preview, meta)
        self._held.move_to_end(key)
        while len(self._held) > RESULT_CAP:
            self._held.popitem(last=False)

    # -- pipeline ----------------------------------------------------
    async def intake(
        self,
        image: bytes,
        image_mime: str = "image/jpeg",
        filename: str = "",
        mode: str = "print",  # print | archive | both
    ) -> DocumentResult:
        started = time.monotonic()
        durations: dict[str, int] = {}
        result = DocumentResult(durations_ms=durations)
        try:
            im = Image.open(io.BytesIO(image))
            im = ImageOps.exif_transpose(im)  # phone shots carry EXIF orientation
            im.load()
        except Exception as exc:
            result.error = f"cannot decode image: {exc}"
            return result
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")

        t = time.monotonic()
        result.measured = measure(im)
        pages = split_spread(im)
        if pages:
            result.measured["spread"] = True
            # P1 handles one page per intake: render each half and hold the
            # first; a follow-up tool can iterate pages later.
            im = pages[0]
            result.measured["note"] = "two-page spread detected — processed left page"
        durations["measure_ms"] = int((time.monotonic() - t) * 1000)

        t = time.monotonic()
        try:
            cls = await self._classify(image, image_mime)
        except Exception as exc:
            logger.warning("document classify failed: %s", exc)
            cls = {"doc_type": "other", "confidence": 0.0,
                   "summary": f"classification unavailable: {exc}"}
        durations["classify_ms"] = int((time.monotonic() - t) * 1000)
        result.doc_type = str(cls.get("doc_type") or "other")
        result.confidence = max(0.0, min(1.0, float(cls.get("confidence") or 0)))
        result.summary = str(cls.get("summary") or "")
        if cls.get("doc_number"):
            result.measured["doc_number"] = cls["doc_number"]
        if result.confidence < 0.4:
            result.warnings.append("low classification confidence — treating as generic document")

        t = time.monotonic()
        true_size = None
        if result.doc_type == "id_card":
            true_size = (85.6, 54.0)
        elif isinstance(cls.get("true_size_mm"), list) and len(cls["true_size_mm"]) == 2:
            try:
                ts = [float(x) for x in cls["true_size_mm"]]
                if 20 < ts[0] < 300 and 20 < ts[1] < 450:
                    true_size = (ts[0], ts[1])
            except (TypeError, ValueError):
                pass

        trimmed = trim_border(im)
        print_im = enhance_for_print(
            trimmed,
            binarize=result.doc_type not in ("id_card", "passport", "photo"))
        canvas, placement = render_a4(print_im, true_size)
        pdf = to_pdf_bytes(canvas)
        preview = to_jpeg_bytes(canvas)
        archive_jpg = to_jpeg_bytes(enhance_for_archive(trimmed), max_w=2000, quality=90)
        durations["render_ms"] = int((time.monotonic() - t) * 1000)

        now = datetime.now(timezone.utc)
        stem = re.sub(r"[^a-z0-9]+", "-", (filename or "doc").lower())[:24] or "doc"
        key = f"doc/{now.strftime('%Y%m%d-%H%M%S')}-{stem}-{hashlib.sha1(image).hexdigest()[:6]}"
        result.key = key
        result.plan = {
            "output": placement["mode"],
            "placed_mm": placement["placed_mm"],
            "canvas": "a4-300dpi",
            "color": "grayscale" if mode != "archive" else "color",
            "archive_bytes": len(archive_jpg),
        }
        result.warnings = result.measured.get("warnings", []) + result.warnings
        self._hold(key, pdf, preview, {
            "doc_type": result.doc_type, "filename": filename,
            "archive_jpg": archive_jpg,
            "created": now.isoformat(timespec="seconds"),
        })
        result.ok = True
        durations["total_ms"] = int((time.monotonic() - started) * 1000)
        return result

    async def _classify(self, image: bytes, image_mime: str) -> dict[str, Any]:
        prompt = (
            "Classify this document image and answer with JSON. doc_type is one "
            "of: id_card, passport, deed, contract, receipt, letter, form, "
            "photo, other. true_size_mm: real physical size in [w,h] mm ONLY "
            "when the document has a standard size (Thai national ID card = "
            "[85.6,54]; passport page = [125,88]); omit otherwise. summary: one "
            "sentence, what it is + who it belongs to if readable. doc_number: "
            "the main ID/serial/deed number if clearly readable."
        )
        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=[
                types.Part.from_text(text=prompt),
                types.Part.from_bytes(data=image, mime_type=image_mime),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=CLASSIFY_SCHEMA,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
        raw = response.text.strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        return json.loads(m.group(0) if m else raw)


def decode_image(payload: dict[str, Any]) -> tuple[bytes | None, str]:
    """Accept image as base64 in JSON ('image_b64' + 'image_mime').
    Tolerates a data-URL prefix, unlike decision_check.decode_image."""
    raw = payload.get("image_b64")
    if not raw:
        return None, "image/jpeg"
    raw = str(raw)
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    data = base64.b64decode(raw, validate=True)
    if len(data) > 15 * 1024 * 1024:
        raise ValueError("image too large (15MB max)")
    mime = str(payload.get("image_mime") or "image/jpeg")
    if not mime.startswith("image/"):
        raise ValueError("image_mime must be image/*")
    return data, mime


_engine_singleton: "DocumentCheckEngine | None" = None


def engine() -> "DocumentCheckEngine":
    """Process-wide intake engine — held results live in RAM only, so the
    PWA endpoint, ToolRunner (ada_doc_archive intake_key resolution) and
    ConversationMemory (unclosed-upload proposals) must all see the same
    instance. Replaces the pwa_server-local singleton."""
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = DocumentCheckEngine()
    return _engine_singleton
