"""Tests for the transcript pass in scripts/tool-usage-report.py
(card ada-dead-surface-metric).

scan_reports() reads session-report JSONs (reports/<day>-<session>.json
-> session_events kind=tool_call/tool_denied) and buckets canonical
per-tool call counts into trailing 7d/30d windows. These fixtures pin
the verify contract on the card: "report matches a manual count of one
week's tool_call events".
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "tool-usage-report.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


usage = _load("tool_usage_report", SCRIPT)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
# cutoffs at NOW: 7d -> 2026-10-03, 30d -> 2026-09-10
DECLARED = {"ada_memory_search", "get_home_state", "kanban",
            "lost_mode", "press_button"}
ALIASES = {"guest_recall": "ada_memory_search",
           "ada_ha_get_state": "get_home_state"}


def _report(events):
    return {"summary": "fixture", "session_events": events}


def _ev(kind, tool=None, ts=None, **kw):
    ev = {"kind": kind, **kw}
    if tool is not None:
        ev["tool"] = tool
    if ts is not None:
        ev["ts"] = ts
    return ev


def _write(dirpath: Path, name: str, doc: dict) -> None:
    (dirpath / name).write_text(json.dumps(doc), encoding="utf-8")


def _fixture_dir(root: Path) -> Path:
    d = root / "reports"
    d.mkdir()
    # Same-day session: direct calls + an alias call + a denial.
    _write(d, "2026-10-09-s1.json", _report([
        _ev("connect", owner="tony"),
        _ev("tool_call", "ada_memory_search", dur_ms=120, ok=True),
        _ev("tool_call", "ada_memory_search", dur_ms=98, ok=True),
        _ev("tool_call", "guest_recall", dur_ms=140, ok=True),   # alias
        _ev("tool_denied", "kanban", speaker="guest"),
        _ev("turn_latency", ttft_ms=800),
    ]))
    # Inside 30d but outside 7d: alias call + a phonetic spelling.
    _write(d, "2026-09-20-s2.json", _report([
        _ev("tool_call", "ada_ha_get_state", dur_ms=200, ok=True),
        _ev("tool_call", "ada_memorysearch", dur_ms=80, ok=True),  # normalized
    ]))
    # Older than 30d — contributes nothing.
    _write(d, "2026-08-15-s3.json", _report([
        _ev("tool_call", "press_button", dur_ms=50, ok=True),
    ]))
    # As-called name that resolves to nothing — undeclared bucket.
    _write(d, "2026-10-08-s4.json", _report([
        _ev("tool_call", "totally_bogus_tool", dur_ms=5, ok=False),
    ]))
    # Event with no ts falls back to the file's date prefix.
    _write(d, "2026-10-07-s5.json", _report([
        _ev("tool_call", "kanban", dur_ms=300, ok=True),
    ]))
    # Unrelated JSON in the same dir is skipped gracefully.
    _write(d, "not-a-report.json", {"hello": "world"})
    return d


class CanonicalNameTests(unittest.TestCase):
    def test_alias_maps_to_canonical(self):
        self.assertEqual(
            usage.canonical_name("guest_recall", ALIASES, DECLARED),
            "ada_memory_search")

    def test_declared_passes_through(self):
        self.assertEqual(
            usage.canonical_name("kanban", ALIASES, DECLARED), "kanban")

    def test_underscore_drift_normalizes(self):
        # execute() repairs phonetic spellings by stripping underscores —
        # the census must do the same or drift lands in 'undeclared'.
        self.assertEqual(
            usage.canonical_name("ada_memorysearch", ALIASES, DECLARED),
            "ada_memory_search")

    def test_unknown_stays_unknown(self):
        self.assertEqual(
            usage.canonical_name("totally_bogus_tool", ALIASES, DECLARED),
            "totally_bogus_tool")


class ScanReportsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reports_dir = _fixture_dir(Path(self._tmp.name))
        self.stats = usage.scan_reports(
            self.reports_dir, DECLARED, ALIASES, NOW)

    def test_window_counts_match_manual_count(self):
        c7, c30 = self.stats["calls"][7], self.stats["calls"][30]
        # Manual 7d count from the fixtures: ada_memory_search x2 direct
        # + guest_recall alias + ada_memorysearch normalized + kanban x1.
        self.assertEqual(c7["ada_memory_search"], 3)
        self.assertEqual(c7["kanban"], 1)
        self.assertEqual(c30["ada_memory_search"], 4)
        self.assertEqual(c30["get_home_state"], 1)
        # The 30d-old press_button call lands in neither window.
        self.assertNotIn("press_button", c7)
        self.assertNotIn("press_button", c30)

    def test_denied_does_not_count_as_call(self):
        self.assertEqual(self.stats["denied"]["kanban"], 1)
        # kanban had a real call on 10-07 too — denied adds, not replaces.
        self.assertEqual(self.stats["calls"][30]["kanban"], 1)

    def test_zero_call_list(self):
        # kanban was called (10-07) — the 30d-zero set is press_button
        # (call decayed out) plus never-called lost_mode.
        self.assertEqual(self.stats["zero_call"],
                         ["lost_mode", "press_button"])

    def test_last_seen_and_alias_attribution(self):
        self.assertEqual(self.stats["last_seen"]["ada_memory_search"],
                         "2026-10-09")
        self.assertEqual(
            self.stats["as_called"]["ada_memory_search"]["guest_recall"], 1)

    def test_undeclared_bucket(self):
        self.assertEqual(self.stats["unknown"]["totally_bogus_tool"], 1)

    def test_file_count_in_window(self):
        # s3 (Aug) parses but has no in-window events; not-a-report is skipped.
        self.assertEqual(self.stats["files"], 4)

    def test_missing_dir_is_empty(self):
        stats = usage.scan_reports(
            Path(self._tmp.name) / "nope", DECLARED, ALIASES, NOW)
        self.assertEqual(stats["files"], 0)
        self.assertEqual(stats["zero_call"], sorted(DECLARED))


class RenderTests(unittest.TestCase):
    def test_transcript_sections_render(self):
        with tempfile.TemporaryDirectory() as td:
            reports_dir = _fixture_dir(Path(td))
            rstats = usage.scan_reports(
                reports_dir, DECLARED, ALIASES, NOW)
        stats = {"calls": {}, "denied": {}, "alias_hits": {},
                 "normalized": {}, "unknown": {}, "zero_use": [],
                 "since_days": 7}
        md = usage.render(stats, DECLARED, ["ada-ha-tony"], NOW, rstats)
        self.assertIn("Canonical calls — transcript pass", md)
        self.assertIn("Zero-call tools — 30d", md)
        self.assertIn("`lost_mode`", md)
        self.assertIn("guest_recall×1", md)
        self.assertIn("totally_bogus_tool", md)

    def test_render_without_reports(self):
        stats = {"calls": {}, "denied": {}, "alias_hits": {},
                 "normalized": {}, "unknown": {}, "zero_use": [],
                 "since_days": 7}
        md = usage.render(stats, DECLARED, ["ada-ha-tony"], NOW)
        self.assertNotIn("transcript pass", md)


if __name__ == "__main__":
    unittest.main()
