"""Process-local token usage ledger shared by the Live provider and the
generate_content verifiers, so the ada_usage_summary tool can report spend."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

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


def _bucket() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "input_by_modality": {},
        "output_by_modality": {},
        "sessions": set(),
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
    return round(cost / 1_000_000, 4)


def _report(bucket: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": bucket["input_tokens"],
        "output_tokens": bucket["output_tokens"],
        "total_tokens": bucket["input_tokens"] + bucket["output_tokens"],
        "input_by_modality": dict(bucket["input_by_modality"]),
        "output_by_modality": dict(bucket["output_by_modality"]),
        "session_count": len(bucket["sessions"]),
        "est_cost_usd": _est_cost(bucket),
    }


class UsageLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sources: dict[str, dict[str, Any]] = {}
        self._since = datetime.now(timezone.utc).isoformat()

    def record(self, source: str, session_id: str | None = None,
               input_tokens: int = 0, output_tokens: int = 0,
               input_by_modality: dict[str, int] | None = None,
               output_by_modality: dict[str, int] | None = None) -> None:
        with self._lock:
            bucket = self._sources.setdefault(str(source), _bucket())
            bucket["input_tokens"] += int(input_tokens)
            bucket["output_tokens"] += int(output_tokens)
            if session_id:
                bucket["sessions"].add(str(session_id))
            for mod, n in (input_by_modality or {}).items():
                mod = str(mod)
                bucket["input_by_modality"][mod] = bucket["input_by_modality"].get(mod, 0) + int(n)
            for mod, n in (output_by_modality or {}).items():
                mod = str(mod)
                bucket["output_by_modality"][mod] = bucket["output_by_modality"].get(mod, 0) + int(n)

    def snapshot(self, source: str | None = None) -> dict[str, Any]:
        with self._lock:
            if source and source != "all":
                report = _report(self._sources.get(source) or _bucket())
                report["source"] = source
                return report
            total = _bucket()
            by_source = {}
            for name, bucket in self._sources.items():
                total["input_tokens"] += bucket["input_tokens"]
                total["output_tokens"] += bucket["output_tokens"]
                total["sessions"] |= bucket["sessions"]
                for mod, n in bucket["input_by_modality"].items():
                    total["input_by_modality"][mod] = total["input_by_modality"].get(mod, 0) + n
                for mod, n in bucket["output_by_modality"].items():
                    total["output_by_modality"][mod] = total["output_by_modality"].get(mod, 0) + n
                by_source[name] = _report(bucket)
            report = _report(total)
            report["by_source"] = by_source
            report["since"] = self._since
            return report

    def reset(self) -> None:
        with self._lock:
            self._sources.clear()
            self._since = datetime.now(timezone.utc).isoformat()


usage_ledger = UsageLedger()
