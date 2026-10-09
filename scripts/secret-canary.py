#!/usr/bin/env python3
"""secret-canary — grep-canary for key material in durable text surfaces.

Standard: docs/kb/ada-secrets-hygiene.md. Scans comms/transcript/inbox
roots for credential patterns; findings file an incident card on the
kanban board (rotate-then-scrub) and exit nonzero.

Findings report file + line + pattern id + sha8 fingerprint of the match —
the matched text itself is NEVER printed, logged, or posted.

    secret-canary.py [ROOT ...]      # extra roots override the defaults
    SECRET_CANARY_ROOTS=a:b:c        # colon-separated roots
    ADA_BOARD_API_URL=...            # board-api base (card filing)

Defaults: ~/.local/share/ada/transcripts, ~/.local/share/ada/events.md,
~/.local/share/ada-review, ~/focus-inbox, plus this repo's dispatch
outcomes. Missing roots are skipped. Stdlib-only; no secrets leave stdout.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# pattern id -> compiled regex. Keep these anchored on real credential
# shapes — broad patterns make the canary cry wolf on every hash.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("openai-sk", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("openai-proj", re.compile(r"\bsk-proj-[A-Za-z0-9_-]{16,}\b")),
    ("google-api", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("slack", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("cfut", re.compile(r"\bcfut_[0-9A-Za-z_-]{10,}\b")),
    ("github-pat", re.compile(r"\bghp_[0-9A-Za-z]{20,}\b")),
    ("github-fine", re.compile(r"\bgithub_pat_[0-9A-Za-z_]{20,}\b")),
    ("gitlab-pat", re.compile(r"\bglpat-[0-9A-Za-z_-]{16,}\b")),
    ("ada-key", re.compile(r"\bada-[A-Za-z0-9_-]{16,}\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

_MAX_FILE_BYTES = 8 * 1024 * 1024
_TEXT_EXT = {".txt", ".md", ".yml", ".yaml", ".json", ".jsonl", ".log",
             ".csv", ".py", ".js", ".html", ".xml", ".env", ""}

REPO = Path(__file__).resolve().parents[1]


def default_roots() -> list[Path]:
    home = Path.home()
    return [
        home / ".local/share/ada/transcripts",
        home / ".local/share/ada/events.md",
        home / ".local/share/ada-review",
        home / "focus-inbox",
    ] + sorted(REPO.glob("dispatch-outcome*.md"))


def _iter_files(root: Path):
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p


def _scan_file(path: Path, out: list[dict]) -> None:
    if path.suffix.lower() not in _TEXT_EXT:
        return
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return
    except OSError:
        return
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for lineno, line in enumerate(fh, 1):
            if lineno > 200000:
                break
            for pid, rx in PATTERNS:
                for m in rx.finditer(line):
                    # fingerprint only — the match itself never leaves here
                    fp = hashlib.sha256(m.group(0).encode()).hexdigest()[:8]
                    out.append({"file": str(path), "line": lineno,
                                "pattern": pid, "sha8": fp})


def _file_incident(findings: list[dict]) -> dict:
    """POST an incident card to board-api (127.0.0.1:8787 locally, else
    ADA_BOARD_API_URL). Findings payload carries fingerprints only."""
    base = (os.environ.get("ADA_BOARD_API_URL")
            or "http://127.0.0.1:8787").rstrip("/")
    by_pat: dict[str, int] = {}
    for f in findings:
        by_pat[f["pattern"]] = by_pat.get(f["pattern"], 0) + 1
    brief = ("secret-canary hit %d candidate secret(s) in durable text "
             "surfaces. Per docs/kb/ada-secrets-hygiene.md: rotate the "
             "affected credential(s) immediately, then scrub. "
             "Patterns: %s" % (len(findings),
                               ", ".join(f"{k}×{v}" for k, v
                                         in sorted(by_pat.items()))))
    body = {
        "title": f"secret-canary: {len(findings)} credential pattern(s) "
                 f"found in text surfaces",
        "tags": ["incident", "secrets-hygiene"],
        "from": "ada",
        "brief": brief,
        "text": brief + " — findings (fingerprinted): "
                + json.dumps(findings[:50]),
    }
    try:
        req = urllib.request.Request(
            base + "/card",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Tailscale-User-Login": "ada"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return {"posted": True, "status": r.status,
                    "body": json.loads(r.read() or b"{}")}
    except Exception as exc:
        return {"posted": False, "error": f"{exc.__class__.__name__}: {exc}"}


def main(argv: list[str]) -> int:
    roots = [Path(p).expanduser() for p in argv]
    if not roots:
        env = os.environ.get("SECRET_CANARY_ROOTS", "")
        roots = ([Path(p).expanduser() for p in env.split(":") if p]
                 if env else default_roots())
    findings: list[dict] = []
    scanned = 0
    for root in roots:
        for f in _iter_files(root):
            scanned += 1
            _scan_file(f, findings)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if not findings:
        print(f"{stamp} secret-canary: clean — {scanned} file(s) scanned")
        return 0
    result = _file_incident(findings)
    print(f"{stamp} secret-canary: {len(findings)} finding(s) in "
          f"{scanned} file(s) — incident card "
          f"{'posted' if result['posted'] else 'FAILED ' + result.get('error', '')}")
    for f in findings:
        # file + line + pattern + fingerprint — never the match text
        print(f"  {f['file']}:{f['line']}  {f['pattern']}  sha8={f['sha8']}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
