"""Pre-write guard for memory banks — hard size caps with a loud,
self-correctable refusal (card ada-memory-write-caps).

Every memory write path (ada_remember, guest_remember,
guest_remember_private, vocab_note) must pass check_write() before the
store call. A refusal is structured so the model can fix the call in the
same turn instead of giving up:

    {"ok": False, "error": "memory_cap", "field": "text",
     "size": N, "cap": M,
     "hint": "shorten by K chars or split into N entries"}

Contract rules:
  - at or under the cap passes; strictly over refuses (never truncates —
    a silent cut once lost the interesting half of a memory)
  - append/merge paths (vocab_note) must pass the FINAL merged body, not
    the delta
  - guest_remember/guest_remember_private inherit the same caps
  - bank doc-count over the warn threshold logs a memory-bank-doc-warn
    event — banks legitimately grow; the sweep jobs handle decay, so this
    is never a hard stop

Extension note: the injection-scan stage (card ada-memory-injection-scan,
merged to chaba/master) slots into check_write() AHEAD of the cap checks
— same refusal shape with error="memory_scan". Keep the class table
data-driven at the top of that section.

Run `python3 backend/memory_write_guard.py --selftest` — prints
SELFTEST-OK and exits 0 when the boundary behavior holds.
"""
from __future__ import annotations

import logging
import math
import os
import sys
import time
from typing import Any

try:
    from backend import event_log
except ImportError:  # standalone run: python3 backend/memory_write_guard.py
    try:
        import event_log  # type: ignore[no-redef]
    except ImportError:
        event_log = None  # type: ignore[assignment]

logger = logging.getLogger("tools")


def _event(kind: str, subject: str, text: str) -> None:
    """Best-effort ada events.md audit line — never carries payload
    content, never blocks the check that emitted it."""
    if event_log is None:
        return
    try:
        event_log.log_event(kind, "guard", subject, text)
    except Exception:  # noqa: BLE001
        pass

# --- cap configuration ---------------------------------------------------
# Per-entry body cap. Hermes caps a whole store at ~2,200 chars; ours is
# per entry — a single memory doc should stay excerpt-sized (recall
# injects hits into live turns; ADA_HIT_MAX_CHARS excerpts them at 2000).
ENTRY_BODY_CAP = int(os.environ.get("ADA_MEMORY_ENTRY_CAP", "2000"))

# Key/title-ish fields (key, subject, attribute, supersedes target).
# Keys are slugged from these — a long subject would mint an unreadable
# doc key.
FIELD_CAP = int(os.environ.get("ADA_MEMORY_FIELD_CAP", "160"))

# Bank doc-count warn threshold. NOT a cap — crossing it only logs an
# event (banks grow legitimately; sweep jobs decay them).
BANK_DOC_WARN = int(os.environ.get("ADA_MEMORY_BANK_DOC_WARN", "400"))

# Per-bank overrides — e.g. BANK_CAPS["devin"] = {"entry": 50000} for an
# archive bank. Keys: "entry", "field", "doc_warn".
BANK_CAPS: dict[str, dict[str, int]] = {}

# The doc-count check costs one listing call — throttle it per bank so a
# chatty session doesn't list on every write.
DOC_WARN_INTERVAL_S = float(
    os.environ.get("ADA_MEMORY_DOC_WARN_INTERVAL_S", "600"))

_last_doc_check: dict[str, float] = {}
_last_doc_warn: dict[str, float] = {}


def _cap(bank: str | None, key: str, default: int) -> int:
    override = (BANK_CAPS.get(str(bank or "")) or {}).get(key)
    return int(override) if override is not None else default


def entry_cap(bank: str | None = None) -> int:
    return _cap(bank, "entry", ENTRY_BODY_CAP)


def field_cap(bank: str | None = None) -> int:
    return _cap(bank, "field", FIELD_CAP)


def doc_warn_threshold(bank: str | None = None) -> int:
    return _cap(bank, "doc_warn", BANK_DOC_WARN)


def _refusal(field: str, size: int, cap: int, hint: str,
             bank: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "error": "memory_cap",
        "field": field,
        "size": size,
        "cap": cap,
        "hint": hint,
    }
    # Audit without the payload — sizes and fields, never content.
    logger.warning(
        "memory_cap refused bank=%s field=%s size=%d cap=%d",
        bank or "-", field, size, cap)
    _event("memory-cap-refused", f"{bank or '-'}:{field}",
           f"size={size} cap={cap}")
    return out


def check_field(name: str, value: Any, bank: str | None = None
                ) -> dict[str, Any] | None:
    """Cap one key-ish field (key, subject, attribute, …). None = pass."""
    if value is None:
        return None
    size = len(str(value))
    cap = field_cap(bank)
    if size <= cap:
        return None
    return _refusal(
        name, size, cap,
        f"shorten '{name}' by {size - cap} chars (cap {cap})",
        bank=bank)


def check_entry(text: Any, bank: str | None = None,
                field: str = "text") -> dict[str, Any] | None:
    """Cap the FINAL stored body. Merge/append paths pass the merged
    result, not the delta. None = pass."""
    size = len(str(text or ""))
    cap = entry_cap(bank)
    if size <= cap:
        return None
    parts = max(2, math.ceil(size / cap))
    return _refusal(
        field, size, cap,
        f"shorten by {size - cap} chars or split into {parts} entries",
        bank=bank)


def check_write(text: Any = None, bank: str | None = None,
                fields: dict[str, Any] | None = None
                ) -> dict[str, Any] | None:
    """Full pre-write check: named fields first (a bad key fails cheap),
    then the body. Returns the first refusal or None.

    The injection-scan stage (ada-memory-injection-scan) merges in ahead
    of these checks — keep this the single entry point."""
    for name, value in (fields or {}).items():
        refusal = check_field(name, value, bank=bank)
        if refusal is not None:
            return refusal
    return check_entry(text, bank=bank)


def note_doc_count(bank: str, count: int, source: str = "mddb"
                   ) -> dict[str, Any] | None:
    """Warn-only bank growth check — returns a warn payload and logs a
    memory-bank-doc-warn event when count crosses the threshold, at most
    once per DOC_WARN_INTERVAL_S per bank. Never refuses."""
    thr = doc_warn_threshold(bank)
    if thr <= 0 or count < thr:
        return None
    now = time.monotonic()
    if now - _last_doc_warn.get(bank, 0.0) < DOC_WARN_INTERVAL_S:
        return {"bank": bank, "doc_count": count, "warn_threshold": thr,
                "warned": False}
    _last_doc_warn[bank] = now
    logger.warning(
        "memory bank %r crossed doc-count warn threshold: %d >= %d",
        bank, count, thr)
    _event("memory-bank-doc-warn", str(bank),
           f"{count} docs >= warn {thr} ({source})")
    return {"bank": bank, "doc_count": count, "warn_threshold": thr,
            "warned": True}


async def warn_if_bank_large(mddb: Any, collection: str,
                             bank: str) -> dict[str, Any] | None:
    """Throttled doc-count probe after a successful bank write. Costs one
    listing per bank per DOC_WARN_INTERVAL_S — otherwise returns None
    without touching the store. Advisory: a listing failure reads as
    count 0 and simply doesn't warn."""
    thr = doc_warn_threshold(bank)
    if thr <= 0:
        return None
    now = time.monotonic()
    if now - _last_doc_check.get(str(collection), 0.0) < DOC_WARN_INTERVAL_S:
        return None
    _last_doc_check[str(collection)] = now
    try:
        docs = await mddb.search_documents(
            collection=collection, limit=thr + 1)
        count = len(docs or [])
    except Exception:  # noqa: BLE001 — advisory only, never break a write
        return None
    return note_doc_count(bank, count)


def _selftest() -> int:
    """Boundary + merged-append coverage. No store needed — the checks are
    string-level. Prints SELFTEST-OK on success."""
    import tempfile
    from pathlib import Path

    # Events land in a scratch file, not the real timeline.
    tmp = Path(tempfile.mkdtemp()) / "events.md"
    os.environ["ADA_EVENTS_FILE"] = str(tmp)
    cap = entry_cap()

    checks: list[tuple[str, bool]] = []

    # under / at / over the entry cap
    checks.append(("under passes",
                   check_entry("x" * (cap - 1)) is None))
    checks.append(("at-cap passes",
                   check_entry("x" * cap) is None))
    over = check_entry("x" * (cap + 1))
    checks.append(("over refuses", bool(over) and over["ok"] is False
                   and over["error"] == "memory_cap"))
    checks.append(("over reports size/cap",
                   over["size"] == cap + 1 and over["cap"] == cap))
    checks.append(("hint carries the math",
                   "shorten by 1 " in over["hint"]
                   and "split into 2 entries" in over["hint"]))
    big = check_entry("x" * (cap * 3 + 5))
    checks.append(("split count rounds up",
                   f"split into 4 entries" in big["hint"]))

    # key/title field cap
    fcap = field_cap()
    checks.append(("field at-cap passes",
                   check_field("key", "k" * fcap) is None))
    field_over = check_field("key", "k" * (fcap + 1))
    checks.append(("field over refuses",
                   bool(field_over) and field_over["error"] == "memory_cap"
                   and field_over["field"] == "key"))
    checks.append(("field None passes",
                   check_field("subject", None) is None))

    # merged-append (vocab_note path): the cap applies to the FINAL body,
    # not the appended delta — a small line onto a near-full log refuses.
    existing = "# Vocabulary\n" + "x" * cap
    line = "- term → correction — 2026-10-10\n"
    merged = existing.rstrip("\n") + "\n" + line
    assert len(merged) > cap > len(line)
    checks.append(("merged over refuses",
                   bool(check_entry(merged))
                   and check_entry(merged)["size"] == len(merged)))
    checks.append(("merged delta alone would pass",
                   check_entry(line) is None))
    small_merged = "# Vocabulary\n" + line
    checks.append(("merged under passes",
                   check_entry(small_merged) is None))

    # check_write order: bad field beats a bad body
    both = check_write(text="x" * (cap + 1), fields={"key": "k" * 999})
    checks.append(("field refusal wins",
                   both["field"] == "key"))

    # per-bank override
    BANK_CAPS["archive-test"] = {"entry": cap * 10}
    checks.append(("per-bank override",
                   check_entry("x" * (cap * 2), bank="archive-test") is None
                   and entry_cap("archive-test") == cap * 10))
    del BANK_CAPS["archive-test"]

    # bank doc-count warn: under stays quiet, over warns once and throttles
    thr = doc_warn_threshold()
    checks.append(("doc count under quiet",
                   note_doc_count("t-under", thr - 1) is None))
    _last_doc_warn.pop("t-over", None)
    w1 = note_doc_count("t-over", thr + 5)
    w2 = note_doc_count("t-over", thr + 6)
    checks.append(("doc warn fires once",
                   bool(w1 and w1["warned"]) and not w2["warned"]))
    checks.append(("warn event logged",
                   tmp.exists() and "memory-bank-doc-warn" in tmp.read_text()
                   and "memory-cap-refused" in tmp.read_text()))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  {'ok' if ok else 'FAIL'} {name}")
    if failed:
        print(f"SELFTEST-FAIL: {failed}")
        return 1
    print("SELFTEST-OK")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
