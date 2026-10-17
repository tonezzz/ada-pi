"""Write-fidelity guard tests (card ada-cms-write-fidelity, MEM-0498).

Two layers landed together:

- provider-side `_write_fidelity_problems` — advisory ops event when a
  publish call's persisted content arg CONTRADICTS fact tokens (weekday /
  clock time / date) in the narration or the user seed. The kk-cms-only
  bench caught 'Tuesdays' stored while 'Mondays' was narrated (2026-10-12).
- tool-side cms_publish_page read-back — after add_document succeeds the
  stored doc is re-read; stored_verified/stored_excerpt echo into the
  tool result so the model can self-correct and the transcript carries
  what actually persisted.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from backend.realtime_provider import (
    _fidelity_tokens,
    _write_fidelity_problems,
)


def _pub(content: str, slug: str = "garden", status: str = "published",
         ok: bool = True):
    result = {"ok": ok, "status": status, "slug": slug}
    return [("cms_publish_page",
             {"slug": slug, "title": "Garden", "content": content},
             result)]


class FidelityTokensTest(unittest.TestCase):

    def test_en_weekdays_plural(self):
        self.assertEqual(
            _fidelity_tokens("watering on Mondays and Thursdays")["day"],
            {"mon", "thu"})

    def test_thai_weekday(self):
        self.assertEqual(
            _fidelity_tokens("รดน้ำทุกวันจันทร์กับวันพฤหัสบดี")["day"],
            {"mon", "thu"})

    def test_ampm_times_normalize_24h(self):
        self.assertEqual(
            _fidelity_tokens("at 6am and 6:30pm")["time"],
            {"06:00", "18:30"})
        self.assertEqual(_fidelity_tokens("at 18:00")["time"], {"18:00"})

    def test_thai_times(self):
        self.assertEqual(_fidelity_tokens("6 โมงเช้า")["time"], {"06:00"})
        self.assertEqual(_fidelity_tokens("6 โมงเย็น")["time"], {"18:00"})
        self.assertEqual(_fidelity_tokens("18 น.")["time"], {"18:00"})

    def test_iso_and_slash_dates(self):
        self.assertEqual(
            _fidelity_tokens("on 2026-10-17 and 17/10/2026")["date"],
            {"2026-10-17", "17/10/2026"})


class WriteFidelityProblemsTest(unittest.TestCase):

    def test_stored_tuesday_vs_narrated_monday_flags(self):
        flags = _write_fidelity_problems(
            "I've saved the page — watering is on Mondays at 6am.",
            "",
            _pub("Watering is on Tuesdays at 6am."))
        self.assertTrue(any("day" in f and "tue" in f for f in flags))

    def test_stored_vs_user_seed_flags(self):
        flags = _write_fidelity_problems(
            "Saved the page.",  # vague narration carries no day facts
            "the garden watering schedule is Mondays and Thursdays at 6am",
            _pub("Watering is on Tuesdays at 6am, prime the pump."))
        self.assertTrue(any("user" in f for f in flags))

    def test_agreeing_facts_silent(self):
        flags = _write_fidelity_problems(
            "Saved it — Mondays and Thursdays at 6am, pump primed first.",
            "watering is Mondays and Thursdays at 6am, prime the pump",
            _pub("Watering Mondays and Thursdays at 6am; prime the pump."))
        self.assertEqual(flags, [])

    def test_vague_narration_and_seed_silent(self):
        self.assertEqual(
            _write_fidelity_problems("Done, saved the page.", "",
                                     _pub("Watering on Tuesdays.")),
            [])

    def test_partial_overlap_no_flag(self):
        # 'Monday' narrated, stored Monday+Thursday — not disjoint.
        flags = _write_fidelity_problems(
            "saved — watering Mondays",
            "",
            _pub("Watering Mondays and Thursdays."))
        self.assertEqual(flags, [])

    def test_unpublished_results_never_flag(self):
        for res in ({"ok": True, "status": "queued"},
                    {"ok": False, "error": "denied"}):
            flags = _write_fidelity_problems(
                "saved Mondays", "",
                [("cms_publish_page", {"content": "Tuesdays"}, res)])
            self.assertEqual(flags, [], res)

    def test_unwatched_tools_never_flag(self):
        flags = _write_fidelity_problems(
            "saved Mondays", "",
            [("cms_edit", {"action": "note", "content": "Tuesdays"},
              {"ok": True, "status": "published"})])
        self.assertEqual(flags, [])

    def test_time_contradiction_flags(self):
        flags = _write_fidelity_problems(
            "watering at 6am", "", _pub("Watering at 7am."))
        self.assertTrue(any("time" in f for f in flags))

    def test_cross_language_contradiction_flags(self):
        # Thai narration 'วันจันทร์' vs stored 'Tuesday' — same fact space.
        flags = _write_fidelity_problems(
            "บันทึกแล้วค่ะ รดน้ำวันจันทร์", "",
            _pub("Watering on Tuesdays."))
        self.assertTrue(any("day" in f for f in flags))


class PublishReadbackTest(unittest.IsolatedAsyncioTestCase):
    """cms_publish_page post-write read-back: the stored doc is re-read
    and stored_verified/stored_excerpt reflect what actually landed."""

    async def _runner(self, stored_doc):
        from backend.tool_runner import ToolRunner
        ha = AsyncMock()
        ha.base_url = "http://test:8123"
        runner = ToolRunner(ha, instance_id="test")
        runner.mddb = AsyncMock()
        # get_document fires twice: existing-meta merge, then read-back.
        runner.mddb.get_document.side_effect = [None, stored_doc]
        runner.mddb.add_document.return_value = {"ok": True}
        # create_task really schedules this — an AsyncMock keeps it silent.
        runner._cms_reports_index = AsyncMock()
        return runner

    async def test_verbatim_store_verified(self):
        content = "Watering Mondays and Thursdays at 6am."
        runner = await self._runner({"contentMd": content})
        out = await runner.cms_publish_page(
            slug="garden", title="Garden", content=content)
        self.assertEqual(out["status"], "published")
        self.assertEqual(out["stored_verified"], True)
        self.assertIn("stored_sha256", out)
        self.assertEqual(out["stored_excerpt"], content[:240])

    async def test_mangled_store_flags_warning(self):
        runner = await self._runner({"contentMd": "Watering Tuesdays."})
        out = await runner.cms_publish_page(
            slug="garden", title="Garden", content="Watering Mondays.")
        self.assertEqual(out["stored_verified"], False)
        self.assertIn("WARNING", out["note"])

    async def test_readback_miss_marks_unknown(self):
        runner = await self._runner(None)
        out = await runner.cms_publish_page(
            slug="garden", title="Garden", content="x")
        self.assertEqual(out["stored_verified"], "unknown")


if __name__ == "__main__":
    unittest.main()
