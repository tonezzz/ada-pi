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

Env:
    LINE_MEDIA_ARCHIVE_DIR — must match the relay (default
        ~/.local/share/ada/line-media)
    LINE_IMG_CACHE_DIR — relay's transient img cache (default
        ~/.local/state/ada-line-relay/img-cache)
    ADA_DOC_READ_MODEL — default gemini-3.5-flash-lite
    ADA_DOC_READ_MAX_BYTES — inline payload cap (default 15 MiB)
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from backend import gemini_pool

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
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


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
    ctx = getattr(runner, "context", None)
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
    return {
        "ok": True,
        "text": text,
        "name": path.name,
        "mime": mime,
        "bytes": len(data),
        "source": str(path),
        "model": MODEL,
    }
