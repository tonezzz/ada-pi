"""Report meta contract — the fields every CMS report page must carry.

Declared as meta_contract in ssot.apps.ada-cms-reports.yml: every report
page in ada-cms-pages (kind page|report) carries

  summary    one-line brief Ada can answer from without re-reading the
             page — the reports-index row. ~240 chars max.
  domain     grouping tag ('flood', 'health', 'bench', 'tools', 'news').
  fresh_for  staleness hint '30m'/'1h'/'6h'/'1d' — reports-index flags
             the page STALE once updated exceeds it.
  confidence trust level: high|medium|low|unverified — low/unverified
             must not be stated as settled.
  timeline   append-only audit entries ('<ts> what changed').
  updated    ISO-8601 timestamp of the last write.

Enforcement is two-layer (ada-report-quality, 2026-10-05):
  - reject: cms_publish_page's gate refuses calls missing the
    model-supplied fields (PUBLISH_ARG_FIELDS) before the confirmation
    handshake is even registered, so the model fixes the call instead of
    burning a user confirm on a doomed write;
  - warn:   the merged-meta check in cms_publish_page/cms_note_update
    and the per-page 'meta_contract' block in cms_verify_page surface
    residual gaps (legacy pages, internal republish paths) without
    breaking merge-only edits.

MDDB stores meta values as lists; validators normalize scalars via
first(). Stdlib-only — scripts/*-report.py import this module directly.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# Fields every report page must carry. timeline/updated are stamped by
# the write path itself; the first four are what the model must pass.
REQUIRED_FIELDS = ("summary", "domain", "fresh_for",
                   "confidence", "timeline", "updated")
PUBLISH_ARG_FIELDS = ("summary", "domain", "fresh_for", "confidence")

CONFIDENCE_VALUES = {"high", "medium", "low", "unverified"}
SUMMARY_MAX_CHARS = 240

_FRESH_FOR_RE = re.compile(r"^(\d+(?:\.\d+)?)([mhd])$")
_FRESH_FOR_MULT = {"m": 60, "h": 3600, "d": 86400}


def first(meta: dict[str, Any], name: str) -> str:
    """First string value of a meta field — MDDB stores lists, but
    scalars from hand-built metas are tolerated."""
    v = (meta or {}).get(name)
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v) if isinstance(v, (str, int, float)) else ""


def meta_list(meta: dict[str, Any], name: str) -> list[str]:
    v = (meta or {}).get(name)
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)] if isinstance(v, str) and v else []


def fresh_for_seconds(hint: str) -> int | None:
    """'30m' '1h' '6h' '1d' '7d' → seconds; None when unparseable.
    Same unit set as tool_runner._cms_reports_index's staleness check."""
    m = _FRESH_FOR_RE.match((hint or "").strip().lower())
    if not m:
        return None
    return int(float(m.group(1)) * _FRESH_FOR_MULT[m.group(2)])


def _iso_parseable(ts: str) -> bool:
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def validate_report_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Check a doc's meta against the report meta contract.

    Returns {"ok": bool, "missing": [field...], "warnings": [str...]} —
    'missing' is a hard failure (field absent or blank); 'warnings' are
    present-but-suspect values that should not block a write.
    """
    meta = meta or {}
    missing: list[str] = []
    warnings: list[str] = []

    for name in REQUIRED_FIELDS:
        if name == "timeline":
            if not meta_list(meta, "timeline"):
                missing.append("timeline")
            continue
        if not first(meta, name):
            missing.append(name)

    summary = first(meta, "summary")
    if summary and len(summary) > SUMMARY_MAX_CHARS:
        warnings.append(
            f"summary is {len(summary)} chars — contract is ~{SUMMARY_MAX_CHARS} max")

    ff = first(meta, "fresh_for")
    if ff and fresh_for_seconds(ff) is None:
        warnings.append(
            f"fresh_for {ff!r} doesn't parse (expected e.g. '1h', '6h', '1d') — "
            "reports-index cannot flag staleness")

    conf = first(meta, "confidence")
    if conf and conf.lower() not in CONFIDENCE_VALUES:
        warnings.append(
            f"confidence {conf!r} outside {sorted(CONFIDENCE_VALUES)}")

    upd = first(meta, "updated")
    if upd and not _iso_parseable(upd):
        warnings.append(f"updated {upd!r} is not an ISO-8601 timestamp")

    return {"ok": not missing, "missing": missing, "warnings": warnings}


def missing_publish_fields(args: dict[str, Any]) -> list[str]:
    """The model-supplied contract fields absent from a cms_publish_page
    call. fresh_for present-but-unparseable counts as missing — the
    caller gets one shot at a correct value."""
    missing = [
        f for f in PUBLISH_ARG_FIELDS
        if not str(args.get(f) or "").strip()
    ]
    ff = str(args.get("fresh_for") or "").strip()
    if ff and fresh_for_seconds(ff) is None:
        missing.append("fresh_for (unparseable)")
    return missing
