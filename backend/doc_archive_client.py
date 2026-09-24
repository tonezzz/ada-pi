"""Client for the doc-archive service (stacks/idc01/doc-archive) + MDDB
`documents` index — the Ada side of the unified document memory.

Search hits MDDB directly (same collection Devin reads via the mddb MCP
server). get/archive/print go through the service's REST API so the
dedup + Drive-write logic stays in one place.

Env:
  DOC_ARCHIVE_URL    default http://100.74.146.0:11025
  DOC_ARCHIVE_API_KEY required for get/archive/print (X-API-Key)
  MDDB_BASE          default http://100.74.146.0:11023
  DOC_PRINT_SSH      default tony-dell  (host with the CUPS queue)
  DOC_PRINT_QUEUE    default HP_DeskJet_2700
"""
import asyncio
import base64
import io
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

DOC_ARCHIVE_URL = os.environ.get(
    "DOC_ARCHIVE_URL", "http://100.74.146.0:11025").rstrip("/")
DOC_ARCHIVE_API_KEY = os.environ.get("DOC_ARCHIVE_API_KEY", "")
MDDB_BASE = os.environ.get("MDDB_BASE", "http://100.74.146.0:11023").rstrip("/")
MDDB_COLLECTION = os.environ.get("MDDB_DOC_COLLECTION", "documents")
DOC_PRINT_SSH = os.environ.get("DOC_PRINT_SSH", "tony-dell")
DOC_PRINT_QUEUE = os.environ.get("DOC_PRINT_QUEUE", "HP_DeskJet_2700")


# ---------------------------------------------------------------- REST helpers

def _post(url: str, payload: dict, timeout: float = 20.0,
          headers: dict | None = None) -> Any:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _get(url: str, timeout: float = 30.0,
         headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _api_headers() -> dict:
    return {"X-API-Key": DOC_ARCHIVE_API_KEY}


# ---------------------------------------------------------------- search (MDDB)

async def doc_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Semantic-ish search over the documents collection. Returns compact
    [{slug, doc_type, drive_path, summary, page_names}] for the model."""
    def _run() -> list[dict[str, Any]]:
        docs = _post(f"{MDDB_BASE}/v1/search", {
            "collection": MDDB_COLLECTION, "query": query,
            "limit": max(1, min(int(limit), 20)), "offset": 0})
        out = []
        for d in docs or []:
            meta = d.get("meta") or {}

            def first(name: str) -> str:
                v = meta.get(name) or []
                return str(v[0]) if v else ""
            content = str(d.get("contentMd") or "")
            summary = " ".join(
                ln for ln in content.splitlines()
                if ln.startswith("- identifiers") or ln.startswith("- type")
            ) or content.splitlines()[0] if content else ""
            out.append({
                "slug": d.get("key"),
                "doc_type": first("doc_type"),
                "drive_path": first("drive_path"),
                "archived_at": first("archived_at"),
                "page_names": meta.get("page_names") or [],
                "summary": summary[:400],
            })
        return out
    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------- service REST

async def doc_get(slug: str) -> dict[str, Any]:
    """Manifest + index doc for one archived set (404 -> RuntimeError)."""
    def _run() -> dict[str, Any]:
        try:
            raw = _get(f"{DOC_ARCHIVE_URL}/v1/archive/{slug}",
                       headers=_api_headers())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError(f"no archive named '{slug}'") from exc
            raise
        return json.loads(raw)
    return await asyncio.to_thread(_run)


async def doc_page_bytes(slug: str, page: int) -> bytes:
    def _run() -> bytes:
        return _get(f"{DOC_ARCHIVE_URL}/v1/archive/{slug}/page/{page}",
                    timeout=60.0, headers=_api_headers())
    return await asyncio.to_thread(_run)


async def doc_archive(slug: str, doc_type: str,
                      pages: list[tuple[str, bytes]]) -> dict[str, Any]:
    """POST /v1/archive — pages as [(filename, bytes)]. Dedup is the
    service's job; 409 slug collision raises RuntimeError."""
    payload = {
        "slug": slug,
        "doc_type": doc_type or "document",
        "files": [{"name": n, "data_b64": base64.b64encode(b).decode()}
                  for n, b in pages],
    }
    def _run() -> dict[str, Any]:
        try:
            return _post(f"{DOC_ARCHIVE_URL}/v1/archive", payload,
                         timeout=120.0, headers=_api_headers())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:300]
            if exc.code == 409:
                raise RuntimeError(
                    f"archive '{slug}' already exists (pick a new slug)") from exc
            raise RuntimeError(f"archive service error {exc.code}: {body}") from exc
    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------- print

def _pagespec(manifest_pages: int, spec: str | None) -> list[int]:
    """'all'|None -> every page; '1-3'; '2,4' -> 1-based indices."""
    if not spec or spec == "all":
        return list(range(1, manifest_pages + 1))
    out: list[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return [p for p in out if 1 <= p <= manifest_pages]


async def doc_print_pdf(slug: str, pages: str | None = None,
                        true_size_mm: Optional[tuple[float, float]] = None,
                        binarize: bool | None = None) -> dict[str, Any]:
    """Fetch archived pages -> enhance_for_print -> A4 PDF -> lp via ssh.
    Returns {pages, queue, job_note} — the print is fire-and-forget; CUPS
    errors surface only in the ssh stderr."""
    from PIL import Image
    from backend.document_check import (
        trim_border, enhance_for_print, render_a4, DPI)

    info = await doc_get(slug)
    names = ((info.get("mddb") or {}).get("meta") or {}).get("page_names") or []
    n_pages = len(names) or len(info.get("manifest", {}).get("pages") or [])
    if not n_pages:
        raise RuntimeError(f"archive '{slug}' has no pages")
    wanted = _pagespec(n_pages, pages)
    if not wanted:
        raise RuntimeError(f"no pages match '{pages}'")

    sheets = []
    for n in wanted:
        blob = await doc_page_bytes(slug, n)
        im = Image.open(io.BytesIO(blob))
        enh = enhance_for_print(trim_border(im), binarize=binarize)
        canvas, _ = render_a4(enh, true_size_mm)
        sheets.append(canvas)

    buf = io.BytesIO()
    sheets[0].save(buf, "PDF", resolution=DPI, save_all=True,
                   append_images=sheets[1:])
    pdf = buf.getvalue()

    lp_args = ["lp", "-d", DOC_PRINT_QUEUE, "-o", "media=a4"]
    if not true_size_mm:
        lp_args += ["-o", "fit-to-page"]
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        DOC_PRINT_SSH, " ".join(lp_args),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(pdf), timeout=90)
    if proc.returncode != 0:
        raise RuntimeError(
            f"print submit failed (rc={proc.returncode}): "
            f"{err.decode(errors='replace')[:300]}")
    return {"pages": wanted, "queue": DOC_PRINT_QUEUE,
            "lp": out.decode(errors="replace").strip()[:200]}


def configured() -> bool:
    return bool(DOC_ARCHIVE_API_KEY)
