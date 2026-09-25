"""Append-only structured event timeline — ada-side counterpart of
chaba_memory._log_event. Same '## <ts> — <kind>: <actor> (<subject>)'
markdown format so a mirrored file renders through the same rolling-log
section in memory.yml.

Default path ~/.local/share/ada/events.md (ADA_EVENTS_FILE override) —
the chaba pipeline mirrors it to ~/.local/share/ada-review/events.md.
Names/kinds only — never secrets.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

_MAX_ENTRIES = 120


def _path() -> Path:
    return Path(os.environ.get(
        "ADA_EVENTS_FILE",
        os.path.expanduser("~/.local/share/ada/events.md"),
    ))


def log_event(kind: str, actor: str, subject: str, text: str = "") -> None:
    """Append '## <ts> — <kind>: <actor> (<subject>)' + detail line."""
    path = _path()
    stamp = time.strftime("%Y-%m-%d %H:%M")
    entry = f"## {stamp} — {kind}: {actor} ({subject})\n{text[:200]}".strip()
    try:
        old = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        old = ""
    entries = [e.strip() for e in re.split(r"\n(?=## )", old) if e.strip()]
    entries.append(entry)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n\n".join(entries[-_MAX_ENTRIES:]) + "\n",
                        encoding="utf-8")
    except OSError:
        pass
