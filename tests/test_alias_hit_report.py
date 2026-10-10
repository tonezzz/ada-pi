"""alias_hit_report unit tests — synthetic call-logs/reports dirs, no
journal, no network. Covers the card contract: per-emitted-name counts
split declared|alias|unknown, quiet-day removal eligibility, the
reports fallback for sessions without call-logs, dead-surface union
with the journal path, and the CMS publish meta contract."""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts.ada import alias_hit_report as ahr

TODAY = date(2026, 10, 10)
DECLARED = {"ada_memory_search", "ada_remember", "cms_read", "ada_ops"}
ALIASES = {"guest_recall": "ada_memory_search",
           "vocab_note": "ada_remember",
           "cms_list_pages": "cms_read",
           "ada_deep_research": "ada_ops"}
FAM = {a: "memory" for a in ALIASES}


def _call_log(d: Path, day: str, session: str, events: list[dict]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    lines = [{"ts": f"{day}T10:00:00+07:00", "session_id": session, **ev}
             for ev in events]
    (d / f"{day}-{session}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in lines) + "\n", encoding="utf-8")


def _report(d: Path, day: str, session: str, events: list[dict]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}-{session}.json").write_text(json.dumps(
        {"session_events": [{"ts": f"{day}T10:00:00Z", "kind": k, **f}
                            for k, f in events]}, ensure_ascii=False),
        encoding="utf-8")


def _rollup(calls, journal=None, eligible_days=14, days=14):
    return ahr.rollup(DECLARED, ALIASES, FAM, calls, journal,
                      today=TODAY, days=days, eligible_days=eligible_days,
                      sources=["test"])


class ScanTest(unittest.TestCase):
    def test_function_call_names_counted_results_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            cl = Path(td) / "call-logs"
            _call_log(cl, "2026-10-09", "s1", [
                {"event": "function_call", "name": "guest_recall"},
                {"event": "function_result", "name": "ada_memory_search"},
                {"event": "function_call", "name": "ada_memory_search"},
                {"event": "function_call", "name": "cms_list_pages"}])
            got = list(ahr.iter_call_log_calls(cl, "2026-10-01", "2026-10-10"))
        self.assertEqual([n for _, _, n in got],
                         ["guest_recall", "ada_memory_search",
                          "cms_list_pages"])

    def test_window_excludes_old_files(self):
        with tempfile.TemporaryDirectory() as td:
            cl = Path(td) / "call-logs"
            _call_log(cl, "2026-09-01", "old", [
                {"event": "function_call", "name": "guest_recall"}])
            _call_log(cl, "2026-10-09", "s1", [
                {"event": "function_call", "name": "ada_memory_search"}])
            got = list(ahr.iter_call_log_calls(cl, "2026-10-01", "2026-10-10"))
        self.assertEqual([n for _, _, n in got], ["ada_memory_search"])

    def test_reports_fallback_only_for_unlogged_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            cl = Path(td) / "call-logs"
            rp = Path(td) / "reports"
            _call_log(cl, "2026-10-09", "has-log", [
                {"event": "function_call", "name": "ada_memory_search"}])
            # session WITH a call-log — its report events must not
            # double-count
            _report(rp, "2026-10-09", "has-log", [
                ("tool_call", {"tool": "cms_read", "emitted": "cms_read"})])
            # session with NO call-log — journald-era data still lands
            _report(rp, "2026-10-08", "no-log", [
                ("tool_call", {"tool": "ada_ops",
                               "emitted": "ada_deep_research"}),
                ("tool_call", {"tool": "cms_read"}),  # pre-emitted= row
                ("connect", {"owner": "tony"})])
            skip = ahr.logged_sessions(cl)
            got = list(ahr.iter_report_calls(
                rp, "2026-10-01", "2026-10-10", skip))
        self.assertEqual(
            sorted(got),
            [("2026-10-08", "no-log", "ada_deep_research"),
             ("2026-10-08", "no-log", "cms_read")])


class RollupTest(unittest.TestCase):
    def test_kind_split_and_counts(self):
        r = _rollup([
            ("2026-10-09", "s1", "guest_recall"),
            ("2026-10-09", "s1", "ada_memory_search"),
            ("2026-10-08", "s2", "typo_tool")])
        self.assertEqual(r["names"]["guest_recall"]["kind"], "alias")
        self.assertEqual(r["names"]["ada_memory_search"]["kind"], "declared")
        self.assertEqual(r["names"]["typo_tool"]["kind"], "unknown")
        self.assertEqual(r["aliases"]["guest_recall"]["transcript_hits"], 1)
        self.assertEqual(r["totals"]["alias_hit_names"], 1)

    def test_eligibility_needs_quiet_window(self):
        # only 4 days of data — nothing is eligible even at 0 hits
        r = _rollup([("2026-10-07", "s1", "ada_memory_search")])
        self.assertFalse(
            r["aliases"]["vocab_note"]["removal_eligible"])
        self.assertFalse(r["coverage"]["window_complete"])
        self.assertEqual(r["aliases"]["vocab_note"]["quiet_days"], 3)

    def test_eligibility_after_full_window(self):
        r = _rollup([("2026-09-26", "s1", "ada_memory_search"),
                     ("2026-10-09", "s2", "cms_list_pages")])
        self.assertTrue(r["coverage"]["window_complete"])
        self.assertTrue(r["aliases"]["vocab_note"]["removal_eligible"])
        self.assertFalse(r["aliases"]["cms_list_pages"]["removal_eligible"])
        self.assertEqual(r["aliases"]["cms_list_pages"]["quiet_days"], 1)

    def test_journal_hit_blocks_eligibility_and_feeds_dead(self):
        # ws/HTTP path hit on ada_deep_research — transcript-clean but
        # not quiet
        journal = {"alias_days": {"ada_deep_research": "2026-10-09"},
                   "alias_hits": {"ada_deep_research": 2},
                   "calls": {"cms_read", "ada_ops"}}
        r = _rollup([("2026-09-26", "s1", "ada_memory_search")],
                    journal=journal)
        self.assertFalse(r["aliases"]["ada_deep_research"]["removal_eligible"])
        self.assertEqual(r["aliases"]["ada_deep_research"]["journal_hits"], 2)
        # journal tool lines mark canonical names as used
        self.assertNotIn("cms_read", r["dead_surface"])
        self.assertIn("ada_remember", r["dead_surface"])

    def test_alias_hit_counts_canonical_as_used(self):
        r = _rollup([("2026-09-26", "s1", "guest_recall")],
                    eligible_days=14)
        self.assertNotIn("ada_memory_search", r["dead_surface"])
        self.assertIn("cms_read", r["dead_surface"])


class RenderPublishTest(unittest.TestCase):
    def _r(self):
        return _rollup([("2026-09-26", "s1", "guest_recall"),
                        ("2026-10-09", "s1", "ada_memory_search")])

    def test_render_sections(self):
        md = ahr.render(self._r(), datetime(2026, 10, 10, 12,
                                            tzinfo=timezone.utc))
        self.assertIn("Compat layer", md)
        self.assertIn("`guest_recall`", md)
        self.assertIn("Dead surface", md)
        self.assertIn("Removal path", md)

    def test_publish_meta_contract_and_payload(self):
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent["payload"] = json.loads(req.data)
            return mock.Mock(read=lambda: b"{}")

        with mock.patch.object(ahr.urllib.request, "urlopen",
                               fake_urlopen):
            ok = ahr.publish("http://mddb/v1", "# md", self._r(),
                             datetime(2026, 10, 10, 12,
                                      tzinfo=timezone.utc))
        self.assertTrue(ok)
        doc = sent["payload"]
        self.assertEqual(doc["key"], "report/alias-hits")
        self.assertEqual(doc["collection"], "ada-cms-pages")
        check = ahr.validate_report_meta(doc["meta"])
        self.assertTrue(check["ok"], check)
        self.assertEqual(doc["meta"]["kind"], ["report"])
        self.assertEqual(doc["meta"]["domain"], ["tools"])


if __name__ == "__main__":
    unittest.main()
