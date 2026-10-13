"""doc_read — read an archived LINE document/media file and answer an
extraction prompt against it via the lane's Gemini pool.

line_relay archives inbound file/image/video bytes under
LINE_MEDIA_ARCHIVE_DIR/<chat>/<messageId>.<ext>, journals them, and
hands the session a "[document received: <name> <mime> <url>]" turn —
<url> is the durable public /relay-img/arch/<chat>/<file> hop. This
tool is the read side: it resolves that URL (or a relay-img nonce, an
archive-relative "<chat>/<file>" name, or an absolute path inside one
of the media roots), loads the bytes, and passes them to Gemini as an
inline part with the caller's extraction prompt — booking refs, flight
legs, dates, totals. Gemini ingests PDF/CSV/text natively; images and
short mp4 clips work too.

Reads are confined to the two relay media roots — never the wider
filesystem — so the tool can run on member lanes.

Dedup per docs/kb/media-inbox-standard.md:
  T2 content   before extracting, the file's mid is looked up in the
               relay manifest; same sha256 under an earlier mid means
               this is a resend — extraction redirects to the first
               mid's media-<mid>-extract leaf.
  T3 extract   media-<mid>-extract leaf IS the cache: exists -> return
               stored fields (no Gemini spend); missing -> extract, write
               the leaf into the lane's CMS collection, then return.

Env:
    LINE_MEDIA_ARCHIVE_DIR — must match the relay (default
        ~/.local/share/ada/line-media)
    LINE_IMG_CACHE_DIR — relay's transient img cache (default
        ~/.local/state/ada-line-relay/img-cache)
    ADA_DOC_READ_MODEL — default gemini-3.5-flash-lite
    ADA_DOC_READ_MAX_BYTES — inline payload cap (default 15 MiB)
"""

from __future__ import annotations

import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend import gemini_pool
from backend.tool_runner.common import CMS_COLLECTION

ARCHIVE_DIR = Path(os.environ.get(
    "LINE_MEDIA_ARCHIVE_DIR",
    os.path.expanduser("~/.local/share/ada/line-media")))
IMG_CACHE_DIR = Path(os.environ.get(
    "LINE_IMG_CACHE_DIR",
    str(Path.home() / ".local/state/ada-line-relay/img-cache")))

MODEL = os.environ.get("ADA_DOC_READ_MODEL", "gemini-3.5-flash-lite")
MAX_BYTES = int(os.environ.get("ADA_DOC_READ_MAX_BYTES",
                               str(15 * 1024 * 1024)))

DECLARATION = {
    "name": "doc_read",
    "description": (
        "Read a document the user sent on LINE (the /relay-img/arch/ URL "
        "from a 'document received' note, or an archive path) and extract "
        "what prompt asks for — refs, flight legs, dates, totals."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path_or_url": {
                "type": "string",
                "description": "The /relay-img/arch/<chat>/<file> URL "
                               "from a 'document received' note, an "
                               "archive-relative <chat>/<file> name, or "
                               "an absolute path under the media roots.",
            },
            "prompt": {
                "type": "string",
                "description": "What to pull from the document (default: "
                               "summarize + key fields). E.g. 'booking "
                               "refs and flight legs, as a list'.",
            },
        },
        "required": ["path_or_url"],
        "additionalProperties": False,
    },
}

_DEFAULT_PROMPT = (
    "Summarize this document in 2-3 sentences, then list the key fields "
    "(names, dates, reference/booking numbers, amounts) one per line.")


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _resolve(spec: str) -> tuple[Path | None, str | None]:
    """Map a path_or_url arg to a file inside ARCHIVE_DIR/IMG_CACHE_DIR.

    Returns (path, None) or (None, error). Anything that resolves outside
    the media roots is refused — this is the whole sandbox.
    """
    s = spec.strip()
    if not s:
        return None, "path_or_url is empty"
    url_path = None
    if s.startswith(("http://", "https://")):
        from urllib.parse import urlparse
        url_path = urlparse(s).path
    elif s.startswith("/relay-img/"):
        url_path = s
    if url_path is not None:
        if url_path.startswith("/relay-img/arch/"):
            cand = ARCHIVE_DIR / url_path[len("/relay-img/arch/"):]
            root = ARCHIVE_DIR
        elif url_path.startswith("/relay-img/"):
            cand = IMG_CACHE_DIR / url_path[len("/relay-img/"):]
            root = IMG_CACHE_DIR
        else:
            return None, ("only /relay-img/ URLs on the relay host "
                          "resolve — fetch public pages with web tools "
                          "instead")
    else:
        cand = Path(s)
        if cand.is_absolute():
            root = None
            for r in (ARCHIVE_DIR, IMG_CACHE_DIR):
                if _under(cand, r):
                    root = r
                    break
            if root is None:
                return None, ("path is outside the LINE media archive — "
                              "doc_read only reads files the relay "
                              "archived (use the /relay-img/arch/ URL "
                              "from the 'document received' note)")
        elif "/" in s:
            cand, root = ARCHIVE_DIR / s, ARCHIVE_DIR
        else:
            # Bare name: img-cache nonce first, then <chat>/<name> glob.
            cand, root = IMG_CACHE_DIR / s, IMG_CACHE_DIR
            if not cand.is_file():
                hits = sorted(ARCHIVE_DIR.glob(f"*/{s}")) \
                    if ARCHIVE_DIR.is_dir() else []
                hits = [h for h in hits if _under(h, ARCHIVE_DIR)]
                if hits:
                    cand = hits[-1]
    if root is not None and not _under(cand, root):
        return None, "path escapes the media archive — refused"
    if not cand.is_file():
        return None, (f"no archived file at {s!r} — check the URL from "
                      "the 'document received' note or conv_journal")
    return cand, None


def _mid_of(path: Path) -> str | None:
    """message-id for an archived media file — the stem of
    <archive>/<chat>/<mid>.<ext>. Files outside ARCHIVE_DIR (img-cache
    nonces) have no mid and skip the dedup/cache lane."""
    try:
        path.resolve().relative_to(ARCHIVE_DIR.resolve())
    except (OSError, ValueError):
        return None
    return path.stem or None


def _manifest_lines() -> list[dict]:
    mf = ARCHIVE_DIR / "manifest.jsonl"
    if not mf.is_file():
        return []
    out = []
    try:
        for ln in mf.read_text(errors="replace").splitlines():
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return out


def _canonical_mid(mid: str) -> tuple[str, dict | None]:
    """T2 redirect target: earliest mid sharing this item's sha256.

    -> (canonical_mid, manifest_line_of_canonical). No sha256 on record
    means the mid is its own canonical copy."""
    own = None
    lines = _manifest_lines()
    for e in lines:
        if str(e.get("message_id") or "") == mid:
            own = e
            break
    if not own or not own.get("sha256"):
        return mid, own
    sha = own["sha256"]
    canon = min((e for e in lines if e.get("sha256") == sha),
                key=lambda e: str(e.get("ts") or ""), default=own)
    return str(canon.get("message_id") or mid), canon


async def _extract_leaf(mddb: Any, mid: str) -> dict | None:
    """Stored extraction for a mid — the media-<mid>-extract leaf is the
    T3 cache. Returns the mddb doc or None."""
    if mddb is None or not mid:
        return None
    key = f"media-{mid}-extract"
    try:
        docs = await mddb.search_documents(
            collection=CMS_COLLECTION,
            filter_meta={"slug": [key]}, limit=3)
    except Exception:
        return None
    for d in docs or []:
        if d.get("key") == key:
            return d
    return None


def _first(meta: dict | None, field: str) -> str:
    v = (meta or {}).get(field)
    return str(v[0]) if isinstance(v, list) and v else ""


async def _write_extract_leaf(mddb: Any, mid: str, canon_mid: str,
                              mline: dict | None, path: Path,
                              mime: str, text: str) -> None:
    """Persist the extraction as media-<mid>-extract — doc_read owns
    these leaves; media-inbox-update never re-renders them."""
    if mddb is None:
        return
    try:
        rel = str(path.resolve().relative_to(ARCHIVE_DIR.resolve()))
    except (OSError, ValueError):
        rel = str(path)
    now = datetime.now(timezone.utc)
    name = (mline or {}).get("file_name") or path.name
    body = (f"# Extract — {name}\n\n{text}\n\n---\n"
            f"_meta: parent=media-inbox · report_role=leaf · "
            f"sources=doc_read + {rel}_\n")
    await mddb.add_document(
        collection=CMS_COLLECTION, key=f"media-{canon_mid}-extract",
        lang="en", content_md=body,
        meta={
            "slug": [f"media-{canon_mid}-extract"],
            "title": [f"Extract — {name}"],
            "kind": ["report"], "report_role": ["leaf"],
            "domain": ["media"], "status": ["active"],
            "parent": ["media-inbox"], "format": ["markdown"],
            "generated_by": ["doc_read"],
            "sources": [f"doc_read + {rel} ({mime})"],
            "media_id": [canon_mid],
            "duplicate_of": [canon_mid] if canon_mid != mid else [],
            "updated": [now.isoformat(timespec="seconds")],
            "last_verified": [now.date().isoformat()],
            "fresh_for": ["30d"],
        },
        tool="doc_read")


def _mime_of(path: Path) -> str:
    """img-cache nonces keep a <nonce>.mime sidecar; archive files key off
    the extension _MIME_EXT picked at ingest."""
    sidecar = path.with_name(path.name + ".mime")
    try:
        if sidecar.is_file():
            m = sidecar.read_text().strip()
            if m:
                return m
    except OSError:
        pass
    # .bin is the pre-extension-fix archive suffix — /etc/mime.types maps
    # it to octet-stream, so sniff magic bytes before trusting the guess.
    head = b""
    mime = mimetypes.guess_type(path.name)[0]
    if mime and path.suffix != ".bin":
        return mime
    try:
        head = path.open("rb").read(12)
    except OSError:
        head = b""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    return "application/octet-stream"


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    spec = str(args.get("path_or_url") or args.get("path")
            or args.get("url") or "").strip()
    if not spec:
        return {"ok": False,
                "error": "path_or_url is required — use the "
                         "/relay-img/arch/ URL from the 'document "
                         "received' note"}
    prompt = (str(args.get("prompt") or "").strip()
              or _DEFAULT_PROMPT)[:2000]
    path, err = _resolve(spec)
    if err:
        return {"ok": False, "error": err}
    assert path is not None

    # T2/T3: resolve the canonical mid, then the extract leaf is the
    # cache — a hit answers without reading bytes or calling Gemini.
    ctx = getattr(runner, "context", None)
    mddb = getattr(ctx, "mddb", None)
    mid = _mid_of(path)
    canon_mid, mline = (mid, None)
    if mid:
        canon_mid, mline = _canonical_mid(mid)
    leaf_doc = None
    if canon_mid:
        leaf_doc = await _extract_leaf(mddb, canon_mid)
        if leaf_doc is None and mid and mid != canon_mid:
            leaf_doc = await _extract_leaf(mddb, mid)  # pre-T2 leaf
    if leaf_doc is not None:
        key = f"media-{canon_mid}-extract"
        out = {
            "ok": True,
            "text": (leaf_doc.get("contentMd") or "").strip(),
            "name": path.name, "source": str(path),
            "cached": True, "leaf": leaf_doc.get("key") or key,
        }
        if canon_mid and canon_mid != mid:
            out["duplicate_of"] = canon_mid
        return out

    try:
        data = path.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"couldn't read the file: {exc}"}
    if not data:
        return {"ok": False, "error": "the archived file is empty"}
    if len(data) > MAX_BYTES:
        return {"ok": False,
                "error": f"file is {len(data) // 1024 // 1024} MiB — over "
                         f"the {MAX_BYTES // 1024 // 1024} MiB inline "
                         "limit; split it or pull pages with conv_journal"}
    mime = _mime_of(path)
    try:
        from google.genai import types
        resp = await gemini_pool.generate(
            model=MODEL,
            contents=[
                types.Part.from_bytes(data=data, mime_type=mime),
                prompt,
            ],
            tool="doc_read",
            mddb=getattr(ctx, "mddb", None),
            session_id=str(getattr(ctx, "session_id", "") or "runner"),
            timeout=60.0)
    except gemini_pool.QuotaExhaustedError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False,
                "error": f"document read failed ({type(exc).__name__}): "
                         f"{exc} — say the reader is unavailable, not "
                         "that the file is missing"}
    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        return {"ok": False,
                "error": "the reader returned nothing for this file — "
                         "it may be an unsupported format or empty scan"}
    # T3: persist as media-<canon>-extract — the leaf IS the cache; the
    # next doc_read on this mid (or a same-sha256 resend) reads it back.
    if canon_mid:
        try:
            await _write_extract_leaf(mddb, mid, canon_mid, mline,
                                      path, mime, text)
        except Exception as exc:
            import logging
            logging.getLogger("tools").info(
                "doc_read extract-leaf write failed: %s", exc)
    out = {
        "ok": True,
        "text": text,
        "name": path.name,
        "mime": mime,
        "bytes": len(data),
        "source": str(path),
        "model": MODEL,
        "cached": False,
    }
    if canon_mid:
        out["leaf"] = f"media-{canon_mid}-extract"
    if canon_mid and canon_mid != mid:
        out["duplicate_of"] = canon_mid
    return out
