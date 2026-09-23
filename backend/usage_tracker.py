"""Token usage ledger for Gemini calls.

Every usage_metadata event (Live turns, generate_content verifiers) is
appended to ADA_USAGE_LOG (default data/usage.jsonl) so counters survive
restarts; the in-memory view is rebuilt from that file on first use.
Set ADA_USAGE_LOG=off to disable persistence."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("usage")

# Rough USD per 1M tokens for the est_cost_usd field (Flash-Live audio +
# Flash-Lite rates, Sept 2026). Verify against ai.google.dev pricing.
RATES_PER_MILLION = {
    "audio": {"in": 3.00, "out": 12.00},
    "text": {"in": 0.30, "out": 0.60},
    "image": {"in": 0.30, "out": 0.0},
    "video": {"in": 0.30, "out": 0.0},
    "document": {"in": 0.30, "out": 0.0},
    "unknown": {"in": 3.00, "out": 12.00},
}

# Cached input tokens are billed at a fraction of the input rate.
CACHED_RATE_FRACTION = 0.25


def _bucket() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "tool_use_tokens": 0,
        "turns": 0,
        "input_by_modality": {},
        "output_by_modality": {},
        "sessions": set(),
        "first_event_at": None,
        "last_event_at": None,
    }


def _rate(modality: str, direction: str) -> float:
    return RATES_PER_MILLION.get(modality, RATES_PER_MILLION["unknown"])[direction]


def _est_cost(bucket: dict[str, Any]) -> float:
    cost = 0.0
    for mod, n in bucket["input_by_modality"].items():
        cost += n * _rate(mod, "in")
    for mod, n in bucket["output_by_modality"].items():
        cost += n * _rate(mod, "out")
    unattributed_in = bucket["input_tokens"] - sum(bucket["input_by_modality"].values())
    unattributed_out = bucket["output_tokens"] - sum(bucket["output_by_modality"].values())
    cost += max(0, unattributed_in) * _rate("unknown", "in")
    cost += max(0, unattributed_out) * _rate("unknown", "out")
    cost += bucket["cached_tokens"] * _rate("unknown", "in") * CACHED_RATE_FRACTION
    return round(cost / 1_000_000, 4)


def _report(bucket: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": bucket["input_tokens"],
        "output_tokens": bucket["output_tokens"],
        "total_tokens": bucket["input_tokens"] + bucket["output_tokens"],
        "cached_tokens": bucket["cached_tokens"],
        "tool_use_tokens": bucket["tool_use_tokens"],
        "turns": bucket["turns"],
        "input_by_modality": dict(bucket["input_by_modality"]),
        "output_by_modality": dict(bucket["output_by_modality"]),
        "session_count": len(bucket["sessions"]),
        "first_event_at": bucket["first_event_at"],
        "last_event_at": bucket["last_event_at"],
        "est_cost_usd": _est_cost(bucket),
    }


def _merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    dst["input_tokens"] += src["input_tokens"]
    dst["output_tokens"] += src["output_tokens"]
    dst["cached_tokens"] += src["cached_tokens"]
    dst["tool_use_tokens"] += src["tool_use_tokens"]
    dst["turns"] += src["turns"]
    dst["sessions"] |= src["sessions"]
    for mod, n in src["input_by_modality"].items():
        dst["input_by_modality"][mod] = dst["input_by_modality"].get(mod, 0) + n
    for mod, n in src["output_by_modality"].items():
        dst["output_by_modality"][mod] = dst["output_by_modality"].get(mod, 0) + n
    for field, pick in (("first_event_at", min), ("last_event_at", max)):
        a, b = dst[field], src[field]
        if a is None or (b is not None and pick(a, b) == b):
            dst[field] = b


class UsageLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sources: dict[str, dict[str, Any]] = {}
        self._by_day: dict[str, dict[str, Any]] = {}
        self._since = datetime.now(timezone.utc).isoformat()
        self._loaded = False
        raw = os.environ.get("ADA_USAGE_LOG", "data/usage.jsonl")
        self._path: str | None = (
            None if str(raw).strip().lower() in ("", "off", "none") else str(raw)
        )

    def configure(self, path: str | None) -> None:
        with self._lock:
            self._path = path
            self._loaded = path is None

    @staticmethod
    def _apply(bucket: dict[str, Any], rec: dict[str, Any]) -> None:
        bucket["input_tokens"] += int(rec.get("in", 0) or 0)
        bucket["output_tokens"] += int(rec.get("out", 0) or 0)
        bucket["cached_tokens"] += int(rec.get("cached", 0) or 0)
        bucket["tool_use_tokens"] += int(rec.get("tool_use", 0) or 0)
        bucket["turns"] += 1
        if rec.get("session_id"):
            bucket["sessions"].add(str(rec["session_id"]))
        for mod, n in (rec.get("in_mod") or {}).items():
            mod = str(mod)
            bucket["input_by_modality"][mod] = bucket["input_by_modality"].get(mod, 0) + int(n)
        for mod, n in (rec.get("out_mod") or {}).items():
            mod = str(mod)
            bucket["output_by_modality"][mod] = bucket["output_by_modality"].get(mod, 0) + int(n)
        ts = rec.get("ts")
        if ts:
            if bucket["first_event_at"] is None or ts < bucket["first_event_at"]:
                bucket["first_event_at"] = ts
            if bucket["last_event_at"] is None or ts > bucket["last_event_at"]:
                bucket["last_event_at"] = ts

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path or not Path(self._path).exists():
            return
        try:
            for line in Path(self._path).read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._apply(self._sources.setdefault(str(rec.get("source") or "unknown"), _bucket()), rec)
                day = str(rec.get("ts") or "")[:10]
                if day:
                    self._apply(self._by_day.setdefault(day, _bucket()), rec)
        except OSError as exc:
            logger.warning("usage log load failed (%s): %s", self._path, exc)

    def record(self, source: str, session_id: str | None = None,
               input_tokens: int = 0, output_tokens: int = 0,
               input_by_modality: dict[str, int] | None = None,
               output_by_modality: dict[str, int] | None = None,
               cached_tokens: int = 0, tool_use_tokens: int = 0) -> None:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": str(source),
            "session_id": session_id,
            "in": int(input_tokens),
            "out": int(output_tokens),
            "in_mod": {str(k): int(v) for k, v in (input_by_modality or {}).items()},
            "out_mod": {str(k): int(v) for k, v in (output_by_modality or {}).items()},
            "cached": int(cached_tokens),
            "tool_use": int(tool_use_tokens),
        }
        with self._lock:
            self._ensure_loaded()
            self._apply(self._sources.setdefault(rec["source"], _bucket()), rec)
            self._apply(self._by_day.setdefault(rec["ts"][:10], _bucket()), rec)
            if self._path:
                try:
                    Path(self._path).parent.mkdir(parents=True, exist_ok=True)
                    with open(self._path, "a") as f:
                        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
                except OSError as exc:
                    logger.warning("usage log write failed (%s): %s", self._path, exc)

    def snapshot(self, source: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            if source and source != "all":
                report = _report(self._sources.get(source) or _bucket())
                report["source"] = source
                return report
            total = _bucket()
            by_source = {}
            for name, bucket in self._sources.items():
                _merge(total, bucket)
                by_source[name] = _report(bucket)
            report = _report(total)
            report["by_source"] = by_source
            report["by_day"] = {
                day: _report(b) for day, b in sorted(self._by_day.items())
            }
            report["since"] = self._since
            return report

    def reset(self) -> None:
        with self._lock:
            self._sources.clear()
            self._by_day.clear()
            self._since = datetime.now(timezone.utc).isoformat()
            if self._path:
                try:
                    Path(self._path).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("usage log reset failed (%s): %s", self._path, exc)


usage_ledger = UsageLedger()
