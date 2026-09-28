"""Tests for the daily/weekly summary rollup tier (backend/summary_rollups.py):

- stored daily/weekly docs are returned without an LLM call
- daily digests roll up that day's session-summary docs and persist
- weekly comparison covers a rolling window and flags thin data
- tools dispatch through ToolRunner
"""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ADA_INSTANCE_ID", "test")

from backend import conversation_memory as cm
from backend import summary_rollups as sr

COLLECTION = "ada-ha-recall-summary-test"


class FakeMddb:
    """In-memory MDDB stand-in honoring filter_meta on search."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.adds: list[dict] = []

    async def add_document(self, collection, key, lang, content_md, meta=None,
                           timeout=None):
        doc = {"key": key, "contentMd": content_md, "meta": meta or {}}
        self.docs[key] = doc
        self.adds.append({"collection": collection, "key": key,
                          "content_md": content_md, "meta": meta})
        return doc

    async def get_document(self, collection, key, lang="en"):
        return self.docs.get(key)

    async def search_documents(self, collection, query="*", filter_meta=None,
                               limit=10):
        out = []
        for doc in self.docs.values():
            meta = doc.get("meta") or {}
            if filter_meta and not all(
                    meta.get(k) == v for k, v in filter_meta.items()):
                continue
            out.append(doc)
        return out[:limit]


def _genai_client(*texts):
    """Fake genai client; each generate_content pops one .text response."""
    responses = [MagicMock(text=t) for t in texts]
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=responses)
    return client


async def _seed_session(fake: FakeMddb, day: str, session: str, text: str):
    await fake.add_document(
        COLLECTION, f"session-{day}-{session}", "en", text,
        meta={"kind": ["session-summary"], "session_id": [session],
              "date": [day]})


async def _seed_daily(fake: FakeMddb, day: str, text: str, sessions: int = 2):
    await fake.add_document(
        COLLECTION, f"daily-{day}", "en", text,
        meta={"kind": ["daily-summary"], "date": [day],
              "sessions": [str(sessions)]})


class DailySummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_stored_doc_without_generating(self):
        fake = FakeMddb()
        await _seed_daily(fake, "2026-09-26", "Stored digest")
        with patch("google.genai.Client") as client_cls:
            out = await sr.daily_summary(fake, day="2026-09-26")
        self.assertEqual(out["status"], "ok")
        self.assertFalse(out["generated"])
        self.assertEqual(out["summary"], "Stored digest")
        client_cls.assert_not_called()

    async def test_no_sessions(self):
        out = await sr.daily_summary(FakeMddb(), day="2026-09-26")
        self.assertEqual(out["status"], "no_sessions")
        self.assertEqual(out["sessions"], 0)

    async def test_generates_and_persists_rollup(self):
        fake = FakeMddb()
        await _seed_session(fake, "2026-09-26", "s1", "Talked about the gate.")
        await _seed_session(fake, "2026-09-26", "s2", "Planned a dentist visit.")
        with patch.object(cm, "_GEMINI_API_KEY", "x"), \
                patch("google.genai.Client",
                      return_value=_genai_client("Day digest")):
            out = await sr.daily_summary(fake, day="2026-09-26")
        self.assertTrue(out["generated"])
        self.assertEqual(out["sessions"], 2)
        stored = fake.docs["daily-2026-09-26"]
        self.assertEqual(stored["contentMd"], "Day digest")
        self.assertEqual(stored["meta"]["kind"], ["daily-summary"])
        self.assertEqual(stored["meta"]["date"], ["2026-09-26"])

    async def test_mddb_none_is_unavailable(self):
        out = await sr.daily_summary(None, day="today")
        self.assertEqual(out["status"], "unavailable")

    async def test_bad_day_raises(self):
        with self.assertRaises(Exception):
            await sr.daily_summary(FakeMddb(), day="last thursdayish")


class WeeklyComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def test_insufficient_data(self):
        fake = FakeMddb()
        await _seed_daily(fake, "2026-09-26", "Only day")
        out = await sr.weekly_comparison(
            fake, end="2026-09-27", days=7)
        self.assertEqual(out["status"], "insufficient_data")
        self.assertEqual(len(out["days"]), 7)

    async def test_generates_comparison_over_window(self):
        fake = FakeMddb()
        for i, day in enumerate(
                ("2026-09-25", "2026-09-26", "2026-09-27")):
            await _seed_daily(fake, day, f"Digest {i}")
        with patch.object(cm, "_GEMINI_API_KEY", "x"), \
                patch("google.genai.Client",
                      return_value=_genai_client("Week comparison")):
            out = await sr.weekly_comparison(
                fake, end="2026-09-27", days=7)
        self.assertTrue(out["generated"])
        self.assertEqual(out["comparison"], "Week comparison")
        self.assertEqual(out["period"], "2026-09-21..2026-09-27")
        stored = fake.docs["weekly-2026-09-27"]
        self.assertEqual(stored["meta"]["kind"], ["weekly-summary"])
        self.assertEqual(stored["meta"]["days_with_data"], ["3"])

    async def test_generates_missing_daily_from_sessions(self):
        """A window day without a daily doc is rolled up on the fly."""
        fake = FakeMddb()
        await _seed_daily(fake, "2026-09-26", "Stored digest")
        await _seed_session(fake, "2026-09-27", "s1", "Morning chat")
        await _seed_session(fake, "2026-09-27", "s2", "Evening chat")
        client = _genai_client("Generated daily", "Generated weekly")
        with patch.object(cm, "_GEMINI_API_KEY", "x"), \
                patch("google.genai.Client", return_value=client):
            out = await sr.weekly_comparison(
                fake, end="2026-09-27", days=2)
        self.assertTrue(out["generated"])
        self.assertEqual(fake.docs["daily-2026-09-27"]["contentMd"],
                         "Generated daily")
        self.assertEqual(out["comparison"], "Generated weekly")

    async def test_cached_comparison_returned(self):
        fake = FakeMddb()
        await fake.add_document(
            COLLECTION, "weekly-2026-09-27", "en", "Old comparison",
            meta={"kind": ["weekly-summary"]})
        with patch("google.genai.Client") as client_cls:
            out = await sr.weekly_comparison(fake, end="2026-09-27")
        self.assertFalse(out["generated"])
        self.assertEqual(out["comparison"], "Old comparison")
        client_cls.assert_not_called()

    async def test_days_clamped(self):
        out = await sr.weekly_comparison(FakeMddb(), end="2026-09-27", days=60)
        self.assertEqual(len(out["days"]), sr._WEEKLY_MAX_DAYS)


class RefreshDailyTests(unittest.IsolatedAsyncioTestCase):
    async def test_regenerates_today(self):
        fake = FakeMddb()
        day = datetime.now(timezone.utc).date().isoformat()
        await _seed_daily(fake, day, "Stale digest")
        await _seed_session(fake, day, "s1", "New chat")
        with patch.object(cm, "_GEMINI_API_KEY", "x"), \
                patch("google.genai.Client",
                      return_value=_genai_client("Fresh digest")):
            await sr.refresh_daily(fake, day=day)
        self.assertEqual(fake.docs[f"daily-{day}"]["contentMd"],
                         "Fresh digest")

    async def test_disabled_by_flag(self):
        fake = FakeMddb()
        with patch.object(sr, "_DAILY_ROLLUP", False):
            await sr.refresh_daily(fake, day="2026-09-27")
        self.assertEqual(fake.adds, [])

    async def test_never_raises_on_failure(self):
        class BadMddb:
            async def get_document(self, *a, **k):
                raise RuntimeError("boom")

            async def search_documents(self, *a, **k):
                raise RuntimeError("boom")

        cm._health["daily_rollup_error"] = None
        await sr.refresh_daily(BadMddb(), day="2026-09-27")
        self.assertTrue(cm._health["daily_rollup_error"])


class ToolDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_routes_daily_and_weekly(self):
        from backend.tool_runner import ToolRunner
        runner = ToolRunner(AsyncMock(), instance_id="test")
        with patch.object(
                sr, "daily_summary",
                new=AsyncMock(return_value={"status": "ok"})) as daily, \
                patch.object(
                    sr, "weekly_comparison",
                    new=AsyncMock(return_value={"status": "ok"})) as weekly:
            out1 = await runner.execute(
                "ada_daily_summary", {"day": "yesterday"})
            out2 = await runner.execute(
                "ada_weekly_comparison",
                {"end": "2026-09-27", "days": 5})
        self.assertEqual(out1["status"], "ok")
        self.assertEqual(out2["status"], "ok")
        daily.assert_awaited_once()
        weekly.assert_awaited_once()
        self.assertEqual(weekly.await_args.kwargs["days"], 5)


if __name__ == "__main__":
    unittest.main()
