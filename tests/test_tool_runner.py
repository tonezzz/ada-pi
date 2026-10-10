import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch, ANY

from backend.memory_banks import MemoryBankRegistry
from backend.tool_runner import AdaMemoryStore, ToolRunner


# Report meta contract fields every cms_publish_page call must carry
# (ssot.apps.ada-cms-reports.yml; enforced since ada-report-quality).
_PAGE_META = {"summary": "Pool status brief", "domain": "home",
              "fresh_for": "6h", "confidence": "high"}


def _page_call(runner, slug):
    """Return (args, kwargs) of the add_document call that wrote `slug`.

    Publishing/auto-ops also fire the reports-index regen in the background,
    so `call_args` (the LAST call) is unreliable — find the page write.
    """
    for c in runner.mddb.add_document.call_args_list:
        if len(c.args) > 1 and c.args[1] == slug:
            return c.args, c.kwargs
    raise AssertionError(f"no add_document call for slug {slug!r}")


def _hermetic_registry(instance="test") -> MemoryBankRegistry:
    """Empty local registry so gate tests don't depend on the ambient
    ~/.config/ada/memory-banks.json (whose control_policies vary by host)."""
    path = Path(tempfile.mkdtemp()) / "banks.json"
    path.write_text(json.dumps({"banks": {}}))
    return MemoryBankRegistry(path=str(path), instance=instance, notebook_ids={})


class ToolRunnerSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.mddb_client = AsyncMock()
        self.mddb_client.search_documents.return_value = []
        self.mddb_client.add_document.return_value = {}

    def _controllable(self, *entities):
        self.ha_client.entities.return_value = list(entities)
        return self.ha_client

    async def test_cover_defaults_to_dangerous_safety_and_light_to_caution(self):
        self._controllable(
            {"entity_id": "cover.gate", "state": "closed", "available": True, "name": "Gate"},
            {"entity_id": "light.office", "state": "on", "available": True, "name": "Office"},
        )
        store = AdaMemoryStore(self.ha_client, mddb_client=self.mddb_client, instance_id="test")
        await store.refresh()

        groups = store.confidence_groups()
        by_id = {dev["entity_id"]: dev for group in groups.values() for dev in group}
        self.assertEqual(by_id["cover.gate"]["safety"], "dangerous")
        self.assertEqual(by_id["light.office"]["safety"], "caution")

    async def test_set_confidence_and_safety_persists_both_to_mddb(self):
        self._controllable(
            {"entity_id": "cover.gate", "state": "closed", "available": True, "name": "Gate"},
        )
        store = AdaMemoryStore(self.ha_client, mddb_client=self.mddb_client, instance_id="test")
        await store.refresh()

        await store.set_confidence("cover.gate", "trusted_working", "safe")

        self.assertEqual(store._confidence.get("cover.gate"), "trusted_working")
        self.assertEqual(store._safety.get("cover.gate"), "safe")

        calls = [call.kwargs for call in self.mddb_client.add_document.call_args_list]
        confidence_call = any(c.get("collection") == store._confidence_collection for c in calls)
        safety_call = any(c.get("collection") == store._safety_collection for c in calls)
        self.assertTrue(confidence_call)
        self.assertTrue(safety_call)

    async def test_set_confidence_without_safety_preserves_existing_safety(self):
        self._controllable(
            {"entity_id": "cover.gate", "state": "closed", "available": True, "name": "Gate"},
        )
        store = AdaMemoryStore(self.ha_client, mddb_client=self.mddb_client, instance_id="test")
        await store.refresh()

        # Default safety for cover is dangerous
        self.assertEqual(store._safety.get("cover.gate"), "dangerous")

        await store.set_confidence("cover.gate", "trusted_working")

        self.assertEqual(store._confidence.get("cover.gate"), "trusted_working")
        self.assertEqual(store._safety.get("cover.gate"), "dangerous")


class ControlGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.ha_client.entities.return_value = [
            {"entity_id": "cover.gate", "state": "closed", "available": True, "name": "Gate"},
            {"entity_id": "light.office", "state": "on", "available": True, "name": "Office"},
            {"entity_id": "button.gate_my_position", "state": "unknown", "available": True, "name": "Gate my position"},
        ]
        self.ha_client.control_cover.return_value = {"ok": True}
        self.ha_client.press_button.return_value = {"pressed": True}
        self.ha_client.set_power.return_value = {"state": "off"}
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.runner.mddb = AsyncMock()
        # no live captures in unit tests — keep the capture_reminder
        # advisory off result dicts (and don't probe the real relay)
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}
        self.runner.mddb.search_documents.return_value = []
        self.runner.memory.mddb = self.runner.mddb

    async def test_dangerous_cover_rejected_without_confirmed(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute("control_cover", {"entity_id": "cover.gate", "action": "open"})
        self.ha_client.control_cover.assert_not_awaited()

    async def test_dangerous_cover_allowed_with_confirmed(self):
        result = await self.runner.execute(
            "control_cover", {"entity_id": "cover.gate", "action": "open", "confirmed": True}
        )
        self.assertTrue(result.get("ok"))  # upstream may add advisory fields (capture_reminder)
        self.ha_client.control_cover.assert_awaited_once()

    async def test_button_inherits_danger_from_cover_prefix(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute("press_button", {"entity_id": "button.gate_my_position"})
        self.ha_client.press_button.assert_not_awaited()

    async def test_caution_entity_allowed_without_confirmed(self):
        await self.runner.execute("control_entity", {"entity_id": "light.office", "on": False})
        self.ha_client.set_power.assert_awaited_once_with("light.office", False)

    async def test_per_entity_rate_limit(self):
        for _ in range(5):
            await self.runner.execute("control_entity", {"entity_id": "light.office", "on": False})
        with self.assertRaises(PermissionError):
            await self.runner.execute("control_entity", {"entity_id": "light.office", "on": False})

    async def test_read_only_blocks_control(self):
        os.environ["ADA_READ_ONLY"] = "true"
        try:
            with self.assertRaises(PermissionError):
                await self.runner.execute("control_entity", {"entity_id": "light.office", "on": False})
        finally:
            os.environ.pop("ADA_READ_ONLY", None)

    async def test_non_control_tool_unaffected(self):
        self.ha_client.snapshot.return_value = AsyncMock(
            person_state="home", plugs_on=(), plug_states={}, plug_names={},
        )
        result = await self.runner.execute("get_home_state", {})
        self.assertEqual(result["person"], "home")

    async def test_question_alias_maps_to_query(self):
        self.runner.memory.search_all = AsyncMock(return_value={})
        self.runner.events.recent = lambda *a, **kw: []
        result = await self.runner.execute("ada_ha_recall", {"question": "what did we discuss?"})
        self.runner.memory.search_all.assert_awaited_once_with("what did we discuss?", 10)
        self.assertIn("events", result)

    async def test_unexpected_args_are_ignored(self):
        self.runner.memory.search_all = AsyncMock(return_value={})
        self.runner.events.recent = lambda *a, **kw: []
        await self.runner.execute("ada_ha_recall", {"query": "power", "session_id": "abc"})
        self.runner.memory.search_all.assert_awaited_once_with("power", 10)


class MemoryMergeAliasTests(unittest.IsolatedAsyncioTestCase):
    """tools-merge-memory (8 -> 4): the six absorbed names stay callable
    via _ALIASES and route to their canonical parent — search, remember
    kinds, and recall scope."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.runner.mddb = AsyncMock()
        # no live captures in unit tests — keep the capture_reminder
        # advisory off result dicts (and don't probe the real relay)
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}
        self.runner.mddb.search_documents.return_value = []
        self.runner.mddb.vector_search.return_value = []
        self.runner.memory.mddb = self.runner.mddb
        self.runner.memory.search_all = AsyncMock(return_value={})
        self.runner.events.recent = lambda *a, **kw: []

    async def test_guest_recall_alias_routes_to_search_guest_scope(self):
        self.runner.chaba = SimpleNamespace()
        self.runner.chaba.recall = lambda q, session_id=None, limit=10: [
            {"key": "fav-drink", "name": "guest", "score": 1.0,
             "text": "likes iced tea", "at": "2026-10-04"}]
        result = await self.runner.execute("guest_recall", {"query": "tea"})
        self.assertEqual(result["scope"], "guest")
        self.assertEqual(result["hits"][0]["bank"], "guest")
        self.assertEqual(result["hits"][0]["key"], "fav-drink")

    async def test_vocab_note_alias_maps_term_correct_to_text(self):
        with patch.object(self.runner, "_vocab_append",
                          new=AsyncMock(return_value={"status": "noted"})) as v:
            result = await self.runner.execute(
                "vocab_note",
                {"term": "methodogy", "correct": "methodology",
                 "note": "heard in passing"})
        v.assert_awaited_once_with("methodogy → methodology", "heard in passing")
        self.assertEqual(result["status"], "noted")

    async def test_report_habit_observation_alias_is_provider_dispatched(self):
        result = await self.runner.execute(
            "report_habit_observation",
            {"challenge_id": "w1", "habit_key": "not_drinking_enough_water",
             "observed": True, "confidence": 0.9, "reason": "saw a drink"})
        self.assertIn("live session", result["error"])

    async def test_guest_remember_alias_routes_public(self):
        self.runner.chaba = SimpleNamespace()
        self.runner.chaba.remember = Mock(return_value={"ok": True})
        self.runner.chaba.remember_private = Mock(return_value={"ok": True})
        result = await self.runner.execute(
            "guest_remember", {"key": "fav-drink", "text": "likes iced tea"})
        self.runner.chaba.remember.assert_called_once_with(
            self.runner.session_id, "fav-drink", "likes iced tea")
        self.runner.chaba.remember_private.assert_not_called()
        self.assertEqual(result, {"ok": True})

    async def test_guest_remember_private_alias_routes_private(self):
        self.runner.chaba = SimpleNamespace()
        self.runner.chaba.remember = Mock(return_value={"ok": True})
        self.runner.chaba.remember_private = Mock(return_value={"ok": True})
        await self.runner.execute(
            "guest_remember_private", {"key": "wifi", "text": "pw on fridge"})
        self.runner.chaba.remember_private.assert_called_once_with(
            self.runner.session_id, "wifi", "pw on fridge")
        self.runner.chaba.remember.assert_not_called()

    async def test_ada_ha_recall_alias_routes_to_session_recall_history(self):
        result = await self.runner.execute("ada_ha_recall", {"query": "power"})
        self.runner.memory.search_all.assert_awaited_once_with("power", 10)
        self.assertIn("events", result)

    async def test_canonical_search_scope_rejects_unknown(self):
        out = await self.runner.execute(
            "ada_memory_search", {"query": "x", "scope": "bogus"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")


class CmsToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner.mddb = AsyncMock()
        self.runner.mddb.search_documents.return_value = []
        self.runner.mddb.add_document.return_value = {"status": "ok"}
        self.runner.mddb.get_document.return_value = None
        self.runner.mddb.delete_document.return_value = {"status": "deleted"}

    async def test_publish_denied_without_confirmed(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "cms_publish_page",
                {"slug": "pool-notes", "title": "Pool", "content": "# hi",
                 **_PAGE_META},
            )
        self.runner.mddb.add_document.assert_not_awaited()

    async def test_publish_confirmed_without_pending_denied(self):
        # Regression for the 2026-09-25 flip-flop: model retried
        # confirmed=true without ever asking the user — must be rejected
        # until a pending request has been registered.
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "cms_publish_page",
                {"slug": "pool-notes", "title": "Pool",
                 "content": "# hi", "confirmed": True, **_PAGE_META},
            )
        self.runner.mddb.add_document.assert_not_awaited()

    async def test_publish_rejects_missing_report_meta(self):
        # meta_contract (ssot.apps.ada-cms-reports.yml): the gate refuses
        # BEFORE the confirm handshake registers — the error names the
        # missing fields so the model can fix and resubmit.
        with self.assertRaises(ValueError) as ctx:
            await self.runner.execute(
                "cms_publish_page",
                {"slug": "pool-notes", "title": "Pool", "content": "# hi"})
        msg = str(ctx.exception)
        for f in ("summary", "domain", "fresh_for", "confidence"):
            self.assertIn(f, msg)
        self.runner.mddb.add_document.assert_not_awaited()
        # …and the rejected call never armed the pending-confirm slot.
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "cms_publish_page",
                {"slug": "pool-notes", "title": "Pool", "content": "# hi",
                 "confirmed": True, **_PAGE_META})

    async def test_publish_rejects_unparseable_fresh_for(self):
        with self.assertRaises(ValueError) as ctx:
            await self.runner.execute(
                "cms_publish_page",
                {"slug": "pool-notes", "title": "Pool", "content": "# hi",
                 "summary": "s", "domain": "home",
                 "fresh_for": "soon-ish", "confidence": "high"})
        self.assertIn("fresh_for", str(ctx.exception))
        self.runner.mddb.add_document.assert_not_awaited()

    async def test_publish_with_confirmed_writes_page_doc(self):
        # Two-step handshake: first call registers pending, the confirmed
        # resubmit publishes.
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "cms_publish_page",
                {"slug": "Pool Notes", "title": "Pool notes",
                 "content": "# Pool\npH 7.4", **_PAGE_META},
            )
        result = await self.runner.execute(
            "cms_publish_page",
            {
                "slug": "Pool Notes",
                "title": "Pool notes",
                "content": "# Pool\npH 7.4",
                "confirmed": True,
                **_PAGE_META,
            },
        )
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["slug"], "pool-notes")
        self.assertNotIn("meta_contract", result)  # clean write — no gaps
        args, kwargs = _page_call(self.runner, "pool-notes")
        self.assertEqual(args[:3], ("ada-cms-pages", "pool-notes", "en"))
        meta = kwargs["meta"]
        self.assertEqual(meta["kind"], ["page"])
        self.assertEqual(meta["slug"], ["pool-notes"])
        self.assertEqual(meta["format"], ["markdown"])
        # contract fields landed: model args + tool-stamped updated/timeline
        for f in ("summary", "domain", "fresh_for", "confidence"):
            self.assertEqual(meta[f], [_PAGE_META[f]], f)
        self.assertTrue(meta["updated"][0])
        self.assertTrue(meta["timeline"][0].endswith("published: Pool notes"))

    async def test_publish_rejects_bad_slug_and_format(self):
        # Missing report-meta fails in the gate before confirmation registers.
        for bad in ({"slug": "../evil", "title": "x", "content": "x"},
                    {"slug": "ok", "title": "x", "content": "x", "format": "exe"}):
            with self.assertRaises(ValueError):
                await self.runner.execute("cms_publish_page", bad)
        # With meta: the unconfirmed call registers pending and the gate
        # denies; confirmed calls pass the gate and fail validation inside
        # the method — a contract {ok: False} result, not an exception.
        for bad in ({"slug": "../evil", "title": "x", "content": "x",
                     **_PAGE_META},
                    {"slug": "ok", "title": "x", "content": "x",
                     "format": "exe", **_PAGE_META}):
            with self.assertRaises(PermissionError):
                await self.runner.execute("cms_publish_page", bad)
        for bad in (
            {"slug": "../evil", "title": "x", "content": "x",
             "confirmed": True, **_PAGE_META},
            {"slug": "ok", "title": "x", "content": "x", "format": "exe",
             "confirmed": True, **_PAGE_META},
        ):
            out = await self.runner.execute("cms_publish_page", bad)
            self.assertFalse(out["ok"])
            self.assertEqual(out["error_type"], "ValueError")

    async def test_list_and_get_are_not_gated(self):
        self.runner.mddb.search_documents.return_value = [
            {"key": "pool-notes", "meta": {"slug": ["pool-notes"], "title": ["Pool"], "format": ["markdown"], "updated": ["2026-09-22T10:00:00+00:00"]}},
        ]
        pages = await self.runner.execute("cms_list_pages", {})
        self.assertEqual(pages["output"][0]["slug"], "pool-notes")
        self.assertEqual(pages["output"][0]["title"], "Pool")

        self.runner.mddb.get_document.return_value = {
            "key": "pool-notes",
            "contentMd": "# Pool",
            "meta": {"slug": ["pool-notes"], "title": ["Pool"], "format": ["markdown"]},
        }
        page = await self.runner.execute("cms_get_page", {"slug": "pool-notes"})
        self.assertEqual(page["content"], "# Pool")

    async def test_list_includes_reports_sorted_deduped(self):
        # ada-video-query-routing (2026-10-08): kind=report docs were
        # invisible to 'list' (kind=page-only filter) and the raw-doc cap
        # with no server ordering buried everything past the cam flood.
        self.runner.mddb.search_documents.return_value = [
            {"key": "cam-a", "lang": "en",
             "meta": {"slug": ["cam-a"], "kind": ["page"],
                      "title": ["Cam A"],
                      "updated": ["2026-10-07T23:00:00+00:00"]}},
            {"key": "cam-a", "lang": "th",
             "meta": {"slug": ["cam-a"], "kind": ["page"],
                      "title": ["Cam A TH"],
                      "updated": ["2026-10-07T23:00:00+00:00"]}},
            {"key": "cached-videos-report",
             "meta": {"slug": ["cached-videos-report"], "kind": ["report"],
                      "title": ["Cached Videos"],
                      "updated": ["2026-10-07T23:05:00+00:00"]}},
            {"key": "dead-page",
             "meta": {"slug": ["dead-page"], "kind": ["page"],
                      "status": ["superseded"], "title": ["Dead"],
                      "updated": ["2026-10-07T23:10:00+00:00"]}},
        ]
        pages = await self.runner.execute("cms_list_pages", {})
        out = pages["output"]
        self.assertEqual([p["slug"] for p in out],
                         ["cached-videos-report", "cam-a"])
        self.assertEqual(out[0]["kind"], "report")
        self.assertEqual(out[1]["langs"], ["en", "th"])
        _, kwargs = self.runner.mddb.search_documents.await_args
        self.assertEqual(kwargs["filter_meta"],
                         {"kind": ["page", "report"]})
        self.assertGreaterEqual(kwargs["limit"], 400)

    async def test_reports_index_dedupes_and_leads_with_reports(self):
        # Live index on 2026-10-07 was all cam-* rows — en/th dup rows +
        # recency sort + row cap pushed every report out of the table.
        self.runner.mddb.search_documents.return_value = [
            {"key": "cam-a", "lang": "en",
             "meta": {"slug": ["cam-a"], "kind": ["page"],
                      "title": ["Cam A"],
                      "updated": ["2026-10-07T23:02:00+00:00"]}},
            {"key": "cam-a", "lang": "th",
             "meta": {"slug": ["cam-a"], "kind": ["page"],
                      "title": ["Cam A"],
                      "updated": ["2026-10-07T23:02:00+00:00"]}},
            {"key": "cached-videos-report",
             "meta": {"slug": ["cached-videos-report"], "kind": ["report"],
                      "title": ["Cached Videos"], "domain": ["media"],
                      "summary": ["playable cached clips"],
                      "updated": ["2026-10-07T11:00:00+00:00"]}},
            {"key": "gone-page",
             "meta": {"slug": ["gone-page"], "kind": ["page"],
                      "status": ["superseded"],
                      "updated": ["2026-10-07T23:03:00+00:00"]}},
        ]
        await self.runner._cms_reports_index()
        args, _ = self.runner.mddb.add_document.await_args
        md = args[3]
        self.assertEqual(md.count("| cached-videos-report |"), 1)
        self.assertEqual(md.count("| cam-a |"), 1)
        self.assertNotIn("gone-page", md)
        self.assertLess(md.index("| cached-videos-report |"),
                        md.index("| cam-a |"))

    async def test_delete_requires_confirmed(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute("cms_delete_page", {"slug": "pool-notes"})
        result = await self.runner.execute(
            "cms_delete_page", {"slug": "pool-notes", "confirmed": True}
        )
        self.assertEqual(result["status"], "deleted")
        self.runner.mddb.delete_document.assert_awaited_once_with(
            "ada-cms-pages", "pool-notes",
            durable=True, tool="cms_delete_page", session_id=None)

    async def test_verify_page_yaml_ok_and_bad(self):
        self.runner.mddb.get_document.return_value = {
            "key": "dash",
            "contentMd": "title: T\nsections:\n  - label: A\n    items:\n      - label: x\n",
            "meta": {"slug": ["dash"], "title": ["Dash"], "format": ["yaml"]},
        }
        report = await self.runner.execute("cms_verify_page", {"slug": "dash"})
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["section_count"], 1)
        self.assertEqual(report["summary"]["sections"][0]["items"], 1)

        self.runner.mddb.get_document.return_value = {
            "key": "dash",
            "contentMd": "title: [unclosed",
            "meta": {"slug": ["dash"], "title": ["Dash"], "format": ["yaml"]},
        }
        report = await self.runner.execute("cms_verify_page", {"slug": "dash"})
        self.assertFalse(report["ok"])
        self.assertIn("yaml", report["error"])

    async def test_verify_page_not_found_and_markdown(self):
        report = await self.runner.execute("cms_verify_page", {"slug": "missing"})
        self.assertEqual(report["status"], "not_found")

        self.runner.mddb.get_document.return_value = {
            "key": "doc",
            "contentMd": "# Title\n\n## Sub\ntext",
            "meta": {"slug": ["doc"], "title": ["Doc"], "format": ["markdown"]},
        }
        report = await self.runner.execute("cms_verify_page", {"slug": "doc"})
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["headings"], ["# Title", "## Sub"])

    async def test_verify_page_reports_meta_contract(self):
        # Legacy page missing the contract fields → meta_contract.ok False
        # with the missing field names; content parse stays ok.
        self.runner.mddb.get_document.return_value = {
            "key": "legacy", "contentMd": "# T",
            "meta": {"slug": ["legacy"], "title": ["L"],
                     "format": ["markdown"]},
        }
        report = await self.runner.execute("cms_verify_page", {"slug": "legacy"})
        self.assertTrue(report["ok"])
        self.assertFalse(report["meta_contract"]["ok"])
        for f in ("summary", "domain", "fresh_for", "confidence",
                  "timeline", "updated"):
            self.assertIn(f, report["meta_contract"]["missing"])

        # Conformant page → clean contract block.
        self.runner.mddb.get_document.return_value = {
            "key": "ok-page", "contentMd": "# T",
            "meta": {"slug": ["ok-page"], "title": ["T"],
                     "format": ["markdown"], "summary": ["brief"],
                     "domain": ["home"], "fresh_for": ["1d"],
                     "confidence": ["high"],
                     "updated": ["2026-10-05T10:00:00+00:00"],
                     "timeline": ["2026-10-05T10:00 published: T"]},
        }
        report = await self.runner.execute("cms_verify_page", {"slug": "ok-page"})
        self.assertTrue(report["meta_contract"]["ok"])
        self.assertEqual(report["meta_contract"]["warnings"], [])

    async def test_note_update_surfaces_meta_contract_gap(self):
        # note is merge-only/ungated — it must not fail on a legacy page,
        # but the gap is reported so Ada can republish properly.
        self.runner.mddb.get_document.return_value = {
            "key": "legacy", "lang": "en", "contentMd": "# L",
            "meta": {"slug": ["legacy"], "title": ["L"]},
        }
        out = await self.runner.execute(
            "cms_note_update", {"slug": "legacy", "note": "ping"})
        self.assertEqual(out["status"], "noted")
        self.assertFalse(out["meta_contract"]["ok"])
        self.assertIn("summary", out["meta_contract"]["missing"])

    async def test_read_only_blocks_publish(self):
        os.environ["ADA_READ_ONLY"] = "true"
        try:
            with self.assertRaises(PermissionError):
                await self.runner.execute(
                    "cms_publish_page",
                    {"slug": "x", "title": "x", "content": "x", "confirmed": True},
                )
        finally:
            os.environ.pop("ADA_READ_ONLY", None)

    async def test_publish_merges_existing_meta_and_stamps_schema(self):
        # Republishing a generated page must preserve provenance meta and
        # fill the memory-schema fields (the drift the normalizer kept fixing).
        self.runner.mddb.get_document.return_value = {
            "key": "flood-report", "contentMd": "# old",
            "meta": {"generated_by": ["flood-news-update.py --page flood-report"],
                     "report_role": ["leaf"], "parent": ["flood-report-nongdon"],
                     "status": ["draft"], "subject": ["flood-report"],
                     "custom": "kept"},
        }
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "cms_publish_page",
                {"slug": "flood-report", "title": "Flood",
                 "content": "# Flood\nnew", **_PAGE_META},
            )
        await self.runner.execute(
            "cms_publish_page",
            {"slug": "flood-report", "title": "Flood",
             "content": "# Flood\nnew", "confirmed": True, **_PAGE_META},
        )
        meta = _page_call(self.runner, "flood-report")[1]["meta"]
        self.assertEqual(meta["generated_by"],
                         ["flood-news-update.py --page flood-report"])
        self.assertEqual(meta["report_role"], ["leaf"])
        self.assertEqual(meta["status"], ["draft"])  # existing value wins
        self.assertEqual(meta["custom"], ["kept"])   # strings normalized
        self.assertEqual(meta["bank"], ["cms"])
        self.assertEqual(meta["scope"], ["tony"])
        self.assertEqual(meta["source"], ["api"])
        self.assertEqual(meta["written_by"], ["cms_publish_page"])
        self.assertIn("valid_from", meta)
        self.assertIn("last_verified", meta)


class CmsAutomationTests(unittest.IsolatedAsyncioTestCase):
    """cms_automation — Ada's switches/knobs over the ada-cms-automation
    registry the flood-news worker honors."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner.mddb = AsyncMock()
        self.runner.mddb.add_document.return_value = {"status": "ok"}
        self.runner.mddb.get_document.return_value = {
            "key": "flood-report", "lang": "en",
            "contentMd": '{"enabled": true, "interval_min": 240,'
                        ' "feeds": [["bangkok", "https://news.google.com/rss/x"]],'
                        ' "last_status": "ok", "last_count": 6}',
            "meta": {"kind": ["automation-config"], "slug": ["flood-report"]},
        }
        self.runner.mddb.search_documents.return_value = [
            {"key": "flood-report",
             "contentMd": '{"enabled": true, "last_status": "ok"}'},
            {"key": "flood-report-nongdon",
             "contentMd": '{"enabled": false, "interval_min": 120}'},
        ]

    async def test_list_and_get_are_not_gated(self):
        out = await self.runner.execute("cms_automation", {"action": "list"})
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["pages"][0]["slug"], "flood-report")
        self.assertTrue(out["pages"][0]["enabled"])
        self.assertFalse(out["pages"][1]["enabled"])

        out = await self.runner.execute("cms_automation",
                                        {"action": "get", "slug": "flood-report"})
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["config"]["interval_min"], 240)
        self.assertEqual(out["last_status"], "ok")
        self.assertEqual(out["last_count"], 6)

    async def test_get_missing_doc(self):
        self.runner.mddb.get_document.return_value = None
        out = await self.runner.execute("cms_automation",
                                        {"action": "get", "slug": "nope"})
        self.assertEqual(out["status"], "not_found")

    async def test_writes_require_confirmation(self):
        for args in ({"action": "disable", "slug": "flood-report"},
                     {"action": "run", "slug": "flood-report"},
                     {"action": "set", "slug": "flood-report", "interval_min": 60}):
            with self.assertRaises(PermissionError):
                await self.runner.execute("cms_automation", args)
        self.runner.mddb.add_document.assert_not_awaited()

    async def test_read_actions_pass_under_read_only(self):
        os.environ["ADA_READ_ONLY"] = "true"
        try:
            out = await self.runner.execute("cms_automation", {"action": "list"})
            self.assertEqual(out["count"], 2)
            with self.assertRaises(PermissionError):
                await self.runner.execute(
                    "cms_automation",
                    {"action": "disable", "slug": "flood-report",
                     "confirmed": True})
        finally:
            os.environ.pop("ADA_READ_ONLY", None)

    async def test_disable_merges_and_writes_back(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "cms_automation", {"action": "disable", "slug": "flood-report"})
        out = await self.runner.execute(
            "cms_automation",
            {"action": "disable", "slug": "flood-report", "confirmed": True})
        self.assertEqual(out["status"], "updated")
        self.assertEqual(out["changes"], {"enabled": False})
        args, kwargs = self.runner.mddb.add_document.call_args
        self.assertEqual(args[:3],
                         ("ada-cms-automation", "flood-report", "en"))
        body = json.loads(args[3])
        self.assertFalse(body["enabled"])
        # untouched knobs and worker state survive the merge
        self.assertEqual(body["interval_min"], 240)
        self.assertEqual(body["last_status"], "ok")
        self.assertEqual(kwargs["meta"]["kind"], ["automation-config"])

    async def test_run_sets_run_now(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "cms_automation", {"action": "run", "slug": "flood-report"})
        out = await self.runner.execute(
            "cms_automation",
            {"action": "run", "slug": "flood-report", "confirmed": True})
        self.assertTrue(out["queued"])
        body = json.loads(self.runner.mddb.add_document.call_args[0][3])
        self.assertTrue(body["run_now"])
        self.assertTrue(body["enabled"])  # existing config preserved

    async def test_set_validates_knobs(self):
        good = await self.runner.execute(
            "cms_automation",
            {"action": "set", "slug": "flood-report", "confirmed": True,
             "interval_min": 60, "max_items": 5, "since_hours": 48,
             "require": "น้ำ|flood", "langs": ["en", "th"],
             "feeds": [["bkk", "https://news.google.com/rss/x"]],
             "parent": "flood-report-nongdon"})
        body = json.loads(self.runner.mddb.add_document.call_args[0][3])
        self.assertEqual(body["interval_min"], 60)
        self.assertEqual(body["feeds"], [["bkk", "https://news.google.com/rss/x"]])
        self.assertEqual(body["parent"], "flood-report-nongdon")

        for bad in ({"action": "set", "slug": "flood-report", "confirmed": True,
                     "interval_min": -5},
                    {"action": "set", "slug": "flood-report", "confirmed": True,
                     "max_items": 0},
                    {"action": "set", "slug": "flood-report", "confirmed": True,
                     "require": "[unclosed"},
                    {"action": "set", "slug": "flood-report", "confirmed": True,
                     "feeds": [["x", "ftp://nope"]]},
                    {"action": "set", "slug": "flood-report", "confirmed": True,
                     "langs": ["de"]}):
            out = await self.runner.execute("cms_automation", bad)
            self.assertFalse(out["ok"])
            self.assertEqual(out["error_type"], "ValueError")

    async def test_bad_action_and_empty_set(self):
        for bad in (
            {"action": "bogus", "slug": "flood-report", "confirmed": True},
            {"action": "set", "slug": "flood-report", "confirmed": True},
        ):
            out = await self.runner.execute("cms_automation", bad)
            self.assertFalse(out["ok"])
            self.assertEqual(out["error_type"], "ValueError")


class CmsMergeAliasTests(unittest.IsolatedAsyncioTestCase):
    """tools-merge-cms (7 -> 3): the six absorbed names stay callable via
    _ALIASES and route to their canonical parent — cms_read for
    get/list/verify, cms_edit for note/delete/automate."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.runner.mddb = AsyncMock()
        # no live captures in unit tests — keep the capture_reminder
        # advisory off result dicts (and don't probe the real relay)
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}
        self.runner.mddb.search_documents.return_value = []
        self.runner.mddb.add_document.return_value = {"status": "ok"}
        self.runner.mddb.get_document.return_value = None
        self.runner.mddb.delete_document.return_value = {"status": "deleted"}
        # write paths schedule a reports-index regen — stub it out
        self.runner._cms_reports_index = AsyncMock()

    async def test_list_pages_alias_routes_to_read_list(self):
        pages = await self.runner.execute("cms_list_pages", {})
        self.assertEqual(pages["output"], [])
        self.runner.mddb.search_documents.assert_awaited_once()

    async def test_get_page_alias_maps_slug_to_key(self):
        self.runner.mddb.get_document.return_value = {
            "key": "pool-notes", "contentMd": "# Pool",
            "meta": {"slug": ["pool-notes"], "title": ["Pool"],
                     "format": ["markdown"]},
        }
        page = await self.runner.execute("cms_get_page", {"slug": "pool-notes"})
        self.assertEqual(page["content"], "# Pool")
        self.runner.mddb.get_document.assert_awaited_with(
            "ada-cms-pages", "pool-notes", "en")

    async def test_verify_page_alias_routes_to_read_verify(self):
        report = await self.runner.execute("cms_verify_page", {"slug": "missing"})
        self.assertEqual(report["status"], "not_found")

    async def test_note_update_alias_routes_ungated(self):
        # absorbed cms_note_update was never confirm-gated — action='note'
        # must execute without confirmed=true.
        self.runner.mddb.get_document.return_value = {
            "key": "flood-report", "lang": "en", "contentMd": "# Flood",
            "meta": {"slug": ["flood-report"], "title": ["Flood"]},
        }
        out = await self.runner.execute(
            "cms_note_update",
            {"slug": "flood-report", "note": "still rising"})
        self.assertEqual(out["status"], "noted")

    async def test_delete_page_alias_keeps_confirm_gate(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute("cms_delete_page", {"slug": "x"})
        out = await self.runner.execute(
            "cms_delete_page", {"slug": "x", "confirmed": True})
        self.assertEqual(out["status"], "deleted")

    async def test_automation_alias_moves_caller_action_to_op(self):
        # The caller's own action= collides with the implied
        # action='automate' — the shim must move it to op= so reads stay
        # free and writes stay confirmation-gated.
        out = await self.runner.execute("cms_automation", {"action": "list"})
        self.assertIn("pages", out)
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "cms_automation", {"action": "run", "slug": "x"})

    async def test_canonical_actions_reject_unknown(self):
        out = await self.runner.execute("cms_read", {"action": "bogus"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")
        # cms_edit is a write-tool seat — the confirm gate runs before
        # action validation, same as cms_automation did pre-merge.
        out = await self.runner.execute(
            "cms_edit", {"action": "bogus", "confirmed": True})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    async def test_canonical_read_and_edit_dispatch(self):
        pages = await self.runner.execute("cms_read", {"action": "list"})
        self.assertEqual(pages["output"], [])
        self.runner.mddb.get_document.return_value = {
            "key": "flood-report", "lang": "en", "contentMd": "# Flood",
            "meta": {"slug": ["flood-report"], "title": ["Flood"]},
        }
        out = await self.runner.execute(
            "cms_edit",
            {"action": "note", "slug": "flood-report", "note": "direct"})
        self.assertEqual(out["status"], "noted")
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "cms_edit", {"action": "delete", "slug": "x"})


class CmsDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    """liam-e2e fallout (card ada-cms-discovery-fixes, 2026-10-08):
    action='list' is newest-first over a wide fetch window, action=
    'search' finds pages by slug/title/summary, and the reports index
    dedupes lang variants + caps each domain."""

    def _cms_doc(self, slug, title, updated, lang="en", domain="",
                 summary="", status="active", kind="page"):
        return {
            "key": slug, "lang": lang, "contentMd": f"# {title}",
            "meta": {
                "kind": [kind], "slug": [slug], "title": [title],
                "status": [status], "updated": [updated],
                **({"domain": [domain]} if domain else {}),
                **({"summary": [summary]} if summary else {}),
            },
        }

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.runner.mddb = AsyncMock()
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}
        self.runner.mddb.add_document.return_value = {"status": "ok"}

    async def test_list_orders_newest_first(self):
        # MDDB returns an arbitrary subset — the tool must sort by
        # updated desc itself or a limit=N call shows random pages.
        self.runner.mddb.search_documents.return_value = [
            self._cms_doc("old-page", "Old", "2026-01-01T00:00:00+00:00"),
            self._cms_doc("new-page", "New", "2026-10-08T00:00:00+00:00"),
            self._cms_doc("mid-page", "Mid", "2026-06-01T00:00:00+00:00"),
        ]
        out = await self.runner.execute("cms_read", {"action": "list"})
        slugs = [p["slug"] for p in out["output"]]
        self.assertEqual(slugs, ["new-page", "mid-page", "old-page"])
        # The fetch window, not the caller's limit, bounds the listing —
        # otherwise sorting only sees the arbitrary first-N.
        kwargs = self.runner.mddb.search_documents.await_args.kwargs
        self.assertGreater(kwargs["limit"], 50)

    async def test_search_finds_page_beyond_list_window(self):
        docs = [
            self._cms_doc(f"cam-wall-{i}", f"Cam {i}",
                          "2026-10-07T00:00:00+00:00", domain="cam")
            for i in range(30)
        ]
        docs.append(self._cms_doc(
            "voice-dub-demos", "Voice dub demos",
            "2026-09-01T00:00:00+00:00", domain="media",
            summary="Thai voice dubs — Liam clip lives here"))
        self.runner.mddb.search_documents.return_value = docs
        out = await self.runner.execute(
            "cms_read", {"action": "search", "query": "liam dub"})
        self.assertTrue(out["ok"])
        slugs = [p["slug"] for p in out["pages"]]
        self.assertIn("voice-dub-demos", slugs)
        self.assertEqual(out["total_pages"], 31)

    async def test_search_accepts_key_as_term(self):
        # Models sometimes put the term in key/slug instead of query.
        self.runner.mddb.search_documents.return_value = [
            self._cms_doc("pool-notes", "Pool notes",
                          "2026-10-01T00:00:00+00:00"),
        ]
        out = await self.runner.execute(
            "cms_read", {"action": "search", "key": "pool"})
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["pages"][0]["slug"], "pool-notes")

    async def test_search_no_match_carries_note(self):
        self.runner.mddb.search_documents.return_value = [
            self._cms_doc("pool-notes", "Pool notes",
                          "2026-10-01T00:00:00+00:00"),
        ]
        out = await self.runner.execute(
            "cms_read", {"action": "search", "query": "nonexistent"})
        self.assertEqual(out["count"], 0)
        self.assertIn("note", out)
        out = await self.runner.execute("cms_read", {"action": "search"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    async def test_search_matches_summary_text(self):
        self.runner.mddb.search_documents.return_value = [
            self._cms_doc("a-page", "Unrelated title",
                          "2026-10-01T00:00:00+00:00",
                          summary="cached YouTube dubs inventory"),
        ]
        out = await self.runner.execute(
            "cms_read", {"action": "search", "query": "cached videos"})
        self.assertEqual(out["count"], 1)

    def test_index_rows_dedupes_lang_variants(self):
        from backend.tool_runner.cms import cms_index_rows
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)
        docs = [
            self._cms_doc("flood", "Flood report",
                          "2026-10-07T10:00:00+00:00", domain="flood"),
            self._cms_doc("flood", "รายงานน้ำท่วม",
                          "2026-10-08T10:00:00+00:00", lang="th",
                          domain="flood"),
        ]
        rows, overflow = cms_index_rows(docs, now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["slug"], "flood")
        self.assertEqual(rows[0]["title"], "Flood report")  # en wins
        self.assertEqual(rows[0]["updated"],
                         "2026-10-08T10:00:00+00:00")  # freshest variant
        self.assertEqual(sorted(rows[0]["langs"]), ["en", "th"])
        self.assertEqual(overflow, {})

    def test_index_rows_caps_domain_and_reports_overflow(self):
        from backend.tool_runner.cms import cms_index_rows
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)
        docs = [
            self._cms_doc(f"cam-{i}", f"Cam {i}",
                          f"2026-10-{(i % 9) + 1:02d}T00:00:00+00:00",
                          domain="cam")
            for i in range(15)
        ]
        docs.append(self._cms_doc("cached-videos", "Cached videos",
                                  "2026-10-07T00:00:00+00:00",
                                  domain="media"))
        rows, overflow = cms_index_rows(docs, now, domain_cap=10)
        cams = [r for r in rows if r["domain"] == "cam"]
        self.assertEqual(len(cams), 10)
        self.assertIn("cached-videos", [r["slug"] for r in rows])
        self.assertEqual(overflow, {"cam": 5})

    def test_index_rows_drops_dead_and_nonreport_docs(self):
        from backend.tool_runner.cms import cms_index_rows
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)
        docs = [
            self._cms_doc("live", "Live", "2026-10-07T00:00:00+00:00"),
            self._cms_doc("dead", "Dead", "2026-10-07T00:00:00+00:00",
                          status="superseded"),
            self._cms_doc("cfg", "Config", "2026-10-07T00:00:00+00:00",
                          kind="automation-config"),
            self._cms_doc("reports-index", "Index",
                          "2026-10-07T00:00:00+00:00"),
        ]
        rows, _ = cms_index_rows(docs, now)
        self.assertEqual([r["slug"] for r in rows], ["live"])

    async def test_reports_index_marks_capped_domains(self):
        docs = [
            self._cms_doc(f"cam-{i}", f"Cam {i}",
                          "2026-10-07T00:00:00+00:00", domain="cam")
            for i in range(12)
        ]
        docs.append(self._cms_doc("cached-videos", "Cached",
                                  "2026-10-08T00:00:00+00:00",
                                  domain="media"))
        self.runner.mddb.search_documents.return_value = docs
        await self.runner._cms_reports_index()
        args = self.runner.mddb.add_document.await_args.args
        md = args[3]
        self.assertIn("cached-videos", md)
        self.assertIn("cam-", md)
        self.assertIn("_Not shown: +2 cam", md)


class CalendarPlanMergeAliasTests(unittest.IsolatedAsyncioTestCase):
    """tools-merge-calendar-plan (9 -> 3): the eight absorbed names stay
    callable via _ALIASES and route to their canonical parent —
    calendar_read for events/calendars/freebusy, calendar_write for
    create/delete/shift (confirm-gated), plan_day for the daily digest
    and weekly comparison."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.runner.mddb = AsyncMock()
        # no live captures in unit tests — keep the capture_reminder
        # advisory off result dicts (and don't probe the real relay)
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}
        self.runner.mddb.search_documents.return_value = []
        self.runner.mddb.get_document.return_value = None
        from tests.test_calendar_providers import FakeProvider, TZ
        from backend.calendar_providers import CalendarService
        self.provider = FakeProvider("fake")
        self.runner._calendar = CalendarService(
            {"fake": self.provider}, write="fake", tz=TZ)
        self.runner._calendar_loaded = True

    async def test_calendar_readers_alias_route(self):
        out = await self.runner.execute("calendar_list_events", {"day": "today"})
        self.assertIn("events", out)
        out = await self.runner.execute("calendar_list_calendars", {})
        self.assertIn("calendars", out)
        out = await self.runner.execute("calendar_freebusy", {"day": "today"})
        self.assertIn("busy", out)

    async def test_canonical_calendar_read_actions(self):
        out = await self.runner.execute(
            "calendar_read", {"action": "calendars"})
        self.assertIn("calendars", out)
        out = await self.runner.execute("calendar_read", {"action": "bogus"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    async def test_create_alias_keeps_confirm_gate(self):
        args = {"title": "dentist", "start": "2026-10-06T14:00",
                "end": "2026-10-06T15:00"}
        with self.assertRaises(PermissionError):
            await self.runner.execute("calendar_create_event", dict(args))
        self.assertEqual(self.provider.created, [])
        out = await self.runner.execute(
            "calendar_create_event", {**args, "confirmed": True})
        self.assertEqual(out["title"], "dentist")

    async def test_delete_alias_keeps_confirm_gate(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "calendar_delete_event", {"event_id": "fake:primary/abc"})
        out = await self.runner.execute(
            "calendar_delete_event",
            {"event_id": "fake:primary/abc", "confirmed": True})
        self.assertIn("deleted", out["output"])
        self.assertEqual(self.provider.deleted, ["primary/abc"])

    async def test_shift_alias_keeps_confirm_gate(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute("calendar_shift_overdue", {})
        out = await self.runner.execute(
            "calendar_shift_overdue", {"to": "tomorrow", "confirmed": True})
        self.assertIn("moved", out)

    async def test_canonical_calendar_write_dispatch(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "calendar_write",
                {"action": "delete", "event_id": "fake:primary/abc"})
        out = await self.runner.execute(
            "calendar_write", {"action": "bogus", "confirmed": True})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    async def test_daily_summary_alias_returns_bare_digest(self):
        self.runner.mddb.get_document.return_value = {
            "key": "daily-2026-10-04", "contentMd": "talked about floods"}
        out = await self.runner.execute(
            "ada_daily_summary", {"day": "2026-10-04"})
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["summary"], "talked about floods")
        self.assertNotIn("events", out)

    async def test_weekly_comparison_alias_maps_end_to_day(self):
        self.runner.mddb.get_document.return_value = {
            "key": "weekly-2026-10-05", "contentMd": "busy week"}
        out = await self.runner.execute(
            "ada_weekly_comparison", {"end": "2026-10-05", "days": 7})
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["comparison"], "busy week")
        self.runner.mddb.get_document.assert_awaited_with(
            ANY, "weekly-2026-10-05")

    async def test_plan_day_folds_digest_into_day_view(self):
        self.runner.mddb.get_document.return_value = {
            "key": "daily-2026-09-22", "contentMd": "day digest"}
        out = await self.runner.execute("plan_day", {"day": "2026-09-22"})
        self.assertIn("events", out)
        self.assertEqual(out["digest"]["summary"], "day digest")

    async def test_plan_day_week_period(self):
        self.runner.mddb.get_document.return_value = {
            "key": "weekly-2026-10-05", "contentMd": "week text"}
        out = await self.runner.execute(
            "plan_day", {"period": "week", "day": "2026-10-05"})
        self.assertEqual(out["comparison"], "week text")

    async def test_plan_day_no_calendar_returns_digest(self):
        self.runner._calendar = None
        self.runner.mddb.get_document.return_value = {
            "key": "daily-2026-09-22", "contentMd": "only digest"}
        out = await self.runner.execute("plan_day", {"day": "2026-09-22"})
        self.assertEqual(out["summary"], "only digest")
        self.assertEqual(out["calendar"], "not configured")


class DocsDriveMergeAliasTests(unittest.IsolatedAsyncioTestCase):
    """tools-merge-docs-drive (8 -> 2): the eight absorbed names stay
    callable via _ALIASES and route to their canonical parent — docs for
    ada_doc_* (search/get free reads; archive/print confirm-gated), drive
    for drive_* (search/get/show free; update confirm-gated)."""
    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        # doc/drive tools are documents-bank scoped — allow it by default;
        # the denial test overrides.
        self._allow = patch.object(
            self.runner.banks, "bank_allowed", return_value=True)
        self._allow.start()
        self.addCleanup(self._allow.stop)

    async def test_doc_search_alias_routes_and_logs(self):
        self.runner.doc_log = []
        with patch("backend.tool_runner.doc_archive_client.doc_search",
                   new=AsyncMock(return_value=[{"slug": "A-68"}])) as m:
            out = await self.runner.execute(
                "ada_doc_search", {"query": "deed"})
        self.assertEqual(out["output"], [{"slug": "A-68"}])
        m.assert_awaited_once_with("deed", limit=5)
        self.assertEqual(self.runner.doc_log[0]["action"], "search")

    async def test_doc_get_alias_routes(self):
        with patch("backend.tool_runner.doc_archive_client.doc_get",
                   new=AsyncMock(return_value={"slug": "A-68"})) as m:
            out = await self.runner.execute("ada_doc_get", {"slug": "A-68"})
        self.assertEqual(out["slug"], "A-68")
        m.assert_awaited_once_with("A-68")

    async def test_doc_archive_alias_keeps_confirm_gate(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "ada_doc_archive", {"slug": "x", "source_dir": "/tmp/d"})
        with patch("backend.tool_runner.doc_archive_client.doc_archive",
                   new=AsyncMock(return_value={"archive_id": "x"})), \
             patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=["p1.jpg"]), \
             patch("builtins.open",
                   unittest.mock.mock_open(read_data=b"\xff\xd8jpeg")):
            out = await self.runner.execute(
                "ada_doc_archive",
                {"slug": "x", "source_dir": "/tmp/d", "confirmed": True})
        self.assertEqual(out["archive_id"], "x")

    async def test_doc_print_alias_keeps_confirm_gate(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "ada_doc_print", {"slug": "A-68", "pages": "1"})
        with patch("backend.tool_runner.doc_archive_client.doc_print_pdf",
                   new=AsyncMock(return_value={"queue": "ok"})) as m:
            out = await self.runner.execute(
                "ada_doc_print",
                {"slug": "A-68", "pages": "1", "confirmed": True})
        self.assertEqual(out["queue"], "ok")
        m.assert_awaited_once_with("A-68", "1", None)

    async def test_drive_search_and_get_aliases_route(self):
        with patch("backend.tool_runner.doc_archive_client.drive_search",
                   new=AsyncMock(return_value=[{"id": "f1"}])) as m:
            out = await self.runner.execute(
                "drive_search", {"query": "condo", "mime": "image/"})
        self.assertEqual(out["count"], 1)
        m.assert_awaited_once_with("condo", mime="image/", limit=10)
        with patch("backend.tool_runner.doc_archive_client.drive_get",
                   new=AsyncMock(return_value={"id": "f1"})) as m:
            out = await self.runner.execute("drive_get", {"file_id": "f1"})
        self.assertEqual(out["id"], "f1")
        m.assert_awaited_once_with("f1")

    async def test_drive_update_alias_keeps_confirm_gate(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "drive_update", {"file_id": "f1", "content": "x"})
        with patch("backend.tool_runner.doc_archive_client.drive_update",
                   new=AsyncMock(return_value={"updated": True})) as m:
            out = await self.runner.execute(
                "drive_update",
                {"file_id": "f1", "content": "x", "confirmed": True})
        self.assertTrue(out["updated"])
        m.assert_awaited_once_with("f1", "x")

    async def test_drive_show_alias_casts_to_screen(self):
        self.runner.cast_to_screen = AsyncMock(return_value={"ok": True})
        with patch("backend.tool_runner.doc_archive_client.drive_get",
                   new=AsyncMock(return_value={
                       "mimeType": "image/jpeg", "media_url": "/m/f1"})), \
             patch("backend.tool_runner.doc_archive_client.drive_media_url",
                   new=AsyncMock(return_value="http://x/f1")):
            out = await self.runner.execute(
                "drive_show", {"file_id": "f1"})
        self.assertTrue(out["ok"])
        self.runner.cast_to_screen.assert_awaited_once()
        self.assertEqual(
            self.runner.cast_to_screen.await_args.kwargs["action"], "image")

    async def test_canonical_dispatch_and_invalid_actions(self):
        with patch("backend.tool_runner.doc_archive_client.doc_search",
                   new=AsyncMock(return_value=[])):
            out = await self.runner.execute(
                "docs", {"action": "search", "query": "deed"})
        self.assertEqual(out["output"], [])
        # docs/drive hold the confirm-gated seats — the gate runs before
        # action validation for anything that isn't a known free read.
        with self.assertRaises(PermissionError):
            await self.runner.execute("docs", {"action": "bogus"})
        with self.assertRaises(PermissionError):
            await self.runner.execute("drive", {"action": "bogus"})
        out = await self.runner.execute(
            "docs", {"action": "bogus", "confirmed": True})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")
        out = await self.runner.execute(
            "drive", {"action": "bogus", "confirmed": True})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    async def test_docs_and_drive_denied_when_bank_not_allowed(self):
        self._allow.stop()
        with patch.object(
                self.runner.banks, "bank_allowed", return_value=False):
            for tool, args in (
                    ("ada_doc_search", {"query": "x"}),
                    ("drive_search", {"query": "x"}),
                    ("drive_update",
                     {"file_id": "f", "content": "x", "confirmed": True})):
                with self.assertRaises(PermissionError, msg=tool):
                    await self.runner.execute(tool, dict(args))
        self._allow.start()


class MetaVoiceMergeAliasTests(unittest.IsolatedAsyncioTestCase):
    """tools-merge-meta-voice (8 -> 3): the seven absorbed names stay
    callable via _ALIASES and route to their canonical parent —
    ada_persona for the voice actions, ada_ops for the five meta tools,
    ada_enroll_speaker for chaba's guest_register."""
    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.runner.mddb = AsyncMock()
        # no live captures in unit tests — keep the capture_reminder
        # advisory off result dicts (and don't probe the real relay)
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}
        self.runner.mddb.search_documents.return_value = []
        self.runner.memory.mddb = self.runner.mddb
        # Voice preference writes land in a per-instance JSON — point the
        # voice file at a tempdir so set_voice can't touch real config.
        self._voice_tmp = tempfile.mkdtemp()
        self._voice_prev = os.environ.get("ADA_VOICE_FILE")
        os.environ["ADA_VOICE_FILE"] = str(
            Path(self._voice_tmp) / "voice.json")

    async def asyncTearDown(self):
        if self._voice_prev is None:
            os.environ.pop("ADA_VOICE_FILE", None)
        else:
            os.environ["ADA_VOICE_FILE"] = self._voice_prev

    def _confirmed_bank(self, allowed_tools):
        """A writable confirmed-policy bank listing the LEGACY
        ada_outcome name — exercises the alias-normalized allowed_tools
        check (bank configs need no edit for the merge)."""
        path = Path(tempfile.mkdtemp()) / "banks.json"
        path.write_text(json.dumps({"banks": {
            "general": {
                "title": "General", "scope": "shared",
                "mddb_collection": "ada-ha-bank-general",
                "instances": ["test"],
                "writable": True,
                "write_policy": "confirmed",
                "allowed_tools": allowed_tools,
                "status": "active",
            }}}))
        self.runner._banks = MemoryBankRegistry(
            path=str(path), instance="test", notebook_ids={})

    # -- ada_set_voice -> ada_persona (*_voice actions) --

    async def test_set_voice_alias_maps_actions(self):
        # The absorbed name's action=set|show|list collides with persona's
        # own actions — the shim must land them on the *_voice forms.
        out = await self.runner.execute("ada_set_voice", {"action": "list"})
        self.assertEqual(out["verb"], "list_voices")
        self.assertIn("Kore", out["voices"])
        out = await self.runner.execute("ada_set_voice", {"action": "show"})
        self.assertEqual(out["verb"], "show_voice")
        self.assertEqual(out["current_voice"], "Kore")

    async def test_set_voice_alias_set_persists(self):
        out = await self.runner.execute(
            "ada_set_voice", {"action": "set", "voice": "Aoede"})
        self.assertEqual(out["verb"], "set_voice")
        self.assertEqual(out["voice"], "Aoede")
        self.assertEqual(out["previous"], "Kore")
        out = await self.runner.execute("ada_set_voice", {"action": "show"})
        self.assertEqual(out["current_voice"], "Aoede")

    async def test_set_voice_alias_defaults_to_set_voice(self):
        # ada_set_voice{} with no action -> implied action='set_voice'
        # (arg default) — an unknown voice is an honest error, not a
        # persona-style rejection.
        out = await self.runner.execute(
            "ada_set_voice", {"voice": "Notavoice"})
        self.assertIn("error", out)

    # -- ada_* meta tools -> ada_ops action= --

    async def test_usage_summary_alias_routes_to_ops_usage(self):
        out = await self.runner.execute("ada_usage_summary", {})
        self.assertIn("input_tokens", out)

    async def test_mddb_health_alias_routes_to_ops_health(self):
        self.runner.mddb._client = AsyncMock()
        resp = Mock()
        resp.raise_for_status = Mock()
        resp.json.return_value = {"collections": {
            "ada-ha-bank-general": {
                "total_documents": 10, "embedded_documents": 8}}}
        self.runner.mddb._client.get = AsyncMock(return_value=resp)
        self.runner.mddb.base_url = "http://mddb.test"
        out = await self.runner.execute("ada_mddb_health", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["missing_vectors"], 2)
        self.assertEqual(out["lagging"],
                         ["ada-ha-bank-general: 2 missing of 10"])

    async def test_decision_check_alias_is_provider_dispatched(self):
        out = await self.runner.execute(
            "ada_decision_check", {"product": "shower head"})
        self.assertIn("live voice session", out["error"])

    async def test_deep_research_alias_is_provider_dispatched(self):
        out = await self.runner.execute(
            "ada_deep_research", {"topic": "graphene"})
        self.assertIn("live voice session", out["error"])

    async def test_outcome_alias_keeps_confirm_gate(self):
        # ada_outcome held a memory-write seat on confirmed banks — the
        # canonical ada_ops(action='outcome') must keep the same gate.
        self._confirmed_bank(["ada_remember", "ada_forget", "ada_outcome"])
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "ada_outcome",
                {"bank": "general", "key": "k1", "outcome": "good"})
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "ada_ops",
                {"action": "outcome", "bank": "general",
                 "key": "k1", "outcome": "good"})

    async def test_ops_reads_stay_ungated(self):
        # usage/health were never in a gate set — the canonical umbrella
        # must not drag them under the memory-write confirmation gate.
        self._confirmed_bank(["ada_outcome"])
        out = await self.runner.execute("ada_usage_summary", {})
        self.assertIn("input_tokens", out)
        out = await self.runner.execute(
            "ada_ops", {"action": "usage"})
        self.assertIn("input_tokens", out)

    async def test_ops_rejects_unknown_action(self):
        out = await self.runner.execute("ada_ops", {"action": "bogus"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    # -- guest_register -> ada_enroll_speaker who='guest' --

    async def test_guest_register_alias_routes_who_guest(self):
        self.runner.chaba = SimpleNamespace()
        self.runner.chaba.register_pending = Mock(
            return_value={"ok": True, "name": "Nat"})
        out = await self.runner.execute("guest_register", {"name": "Nat"})
        self.runner.chaba.register_pending.assert_called_once_with(
            "Nat", session_id=self.runner.session_id)
        self.assertEqual(out, {"ok": True, "name": "Nat"})


class AclExplainTests(unittest.IsolatedAsyncioTestCase):
    """ada_ops action='acl_explain' (card ada-acl-explain) — one call
    answers 'why can <identity> do <thing>' by naming the deciding ACL
    layer. The runner gates consume the same resolver, so the explain
    verdict is the gate verdict."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}
        path = Path(tempfile.mkdtemp()) / "banks.json"
        path.write_text(json.dumps({
            "banks": {
                "general": {
                    "scope": "shared", "instances": ["test"],
                    "mddb_collection": "c-gen", "writable": True,
                    "write_policy": "confirmed",
                    "allowed_tools": ["ada_remember", "ada_outcome"],
                    "status": "active"},
                "personal": {
                    "scope": "instance", "instances": ["test"],
                    "mddb_collection": "c-p", "writable": True,
                    "allowed_tools": ["ada_remember"],
                    "status": "active"},
            },
            "person_policies": {
                "person.kk": {"allow": ["general"]},
            },
            "control_policies": {
                "person.kk": {"allow_domains": ["light", "switch"]},
                "person.tony": {"full": True},
            },
        }))
        self.runner._banks = MemoryBankRegistry(
            path=str(path), instance="test", notebook_ids={})

    async def test_explain_granted_names_layer(self):
        out = await self.runner.execute("ada_ops", {
            "action": "acl_explain", "identity": "person.kk",
            "entity_id": "light.kitchen"})
        self.assertTrue(out["allowed"])
        self.assertEqual(out["verdict"], "allowed")
        self.assertEqual(out["decided_by"], "control_policies")
        self.assertTrue(out["trace"])

    async def test_explain_denied_names_layer(self):
        out = await self.runner.execute("ada_ops", {
            "action": "acl_explain", "identity": "person.kk",
            "entity_id": "cover.gate"})
        self.assertFalse(out["allowed"])
        self.assertEqual(out["verdict"], "denied")
        self.assertEqual(out["decided_by"], "control_policies")
        self.assertEqual(out["trace"][-1]["rule"], "allow_domains")

    async def test_explain_full_identity(self):
        out = await self.runner.execute("ada_ops", {
            "action": "acl_explain", "identity": "person.tony",
            "entity_id": "cover.gate"})
        self.assertTrue(out["allowed"])
        self.assertEqual(out["trace"][-1]["rule"], "full")

    async def test_explain_bank_write_names_layer(self):
        # kk may read 'general' (policy allow) but ada_forget isn't in
        # the bank's allowed_tools — the write denies at a different
        # layer than the read.
        out = await self.runner.execute("ada_ops", {
            "action": "acl_explain", "identity": "person.kk",
            "bank": "general"})
        self.assertTrue(out["allowed"])
        out = await self.runner.execute("ada_ops", {
            "action": "acl_explain", "identity": "person.kk",
            "bank": "general", "tool": "ada_forget"})
        self.assertFalse(out["allowed"])
        self.assertEqual(out["decided_by"], "bank.allowed_tools")
        out = await self.runner.execute("ada_ops", {
            "action": "acl_explain", "identity": "person.kk",
            "bank": "general", "tool": "ada_ops"})
        # bank config lists the LEGACY name ada_outcome — the alias
        # expansion lets the canonical ada_ops through.
        self.assertTrue(out["allowed"])
        self.assertEqual(out["verdict"], "needs_confirmation")
        # kk's allow list doesn't include 'personal'.
        out = await self.runner.execute("ada_ops", {
            "action": "acl_explain", "identity": "person.kk",
            "bank": "personal", "tool": "ada_remember"})
        self.assertFalse(out["allowed"])
        self.assertEqual(out["decided_by"], "person_policies")

    async def test_explain_requires_subject(self):
        out = await self.runner.execute(
            "ada_ops", {"action": "acl_explain"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    async def test_explain_defaults_identity_to_session(self):
        self.runner.session_caller_name = "user-kk"
        out = await self.runner.execute("ada_ops", {
            "action": "acl_explain", "entity_id": "light.kitchen"})
        self.assertEqual(out["identity"], "user-kk")


def _doc_registry(instance="test") -> MemoryBankRegistry:
    """Hermetic registry WITH a 'documents' bank — the photo/doc flows
    absorbed into chat_send are owner-tier (DRIVE_TOOLS bank gate), so a
    runner on the empty registry would deny them before routing."""
    path = Path(tempfile.mkdtemp()) / "banks.json"
    path.write_text(json.dumps({"banks": {"documents": {
        "instances": [instance],
        "mddb_collection": "ada-docs",
        "scope": "shared",
        "writable": True,
    }}}))
    return MemoryBankRegistry(path=str(path), instance=instance,
                              notebook_ids={})


class TasksStatusMergeAliasTests(unittest.IsolatedAsyncioTestCase):
    """tools-merge-tasks-status (10 -> 3 on the card's count): the
    sixteen absorbed names stay callable via _ALIASES — tasks_* route to
    tasks(action=add|list|done|move) with writes confirm-gated and
    action='list' free; the seven status getters route to
    home_status(what=...); photos_pick/photos_picked plus the three
    chaba-side doc-upload card names route to chat_send
    (photo=pick|picked, doc=show|process|card)."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.ha_client.battery_status.return_value = {"soc": 90}
        self.ha_client.battery_detail.return_value = {"battery": 2}
        self.ha_client.power_summary.return_value = {"solar_kwh": 5}
        self.ha_client.inverter_status.return_value = {"mode": "normal"}
        self.ha_client.pool_status.return_value = {"pump": "off"}
        self.ha_client.dashboard_tab.return_value = {"entities": []}
        self.runner = ToolRunner(
            self.ha_client, habit_state_getter=lambda: {"habits": []},
            instance_id="test")
        self.runner._banks = _doc_registry()
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}
        self.runner.mddb = AsyncMock()
        self.runner.mddb.search_documents.return_value = []
        from tests.test_calendar_providers import FakeProvider, TZ
        from backend.calendar_providers import CalendarService, Task
        from datetime import datetime
        self.provider = FakeProvider("fake")
        self.provider.move_task = AsyncMock(return_value=Task(
            id="@default/t1", title="moved", provider="fake",
            task_list="@default", due="2026-10-07"))
        self.runner._calendar = CalendarService(
            {"fake": self.provider}, write="fake", tz=TZ)
        self.runner._calendar_loaded = True
        # chat_send queues a real relay POST in the background — stub the
        # runner so no test reaches the network.
        self.runner._chat_send_run = AsyncMock()
    # -- alias table + declarations ------------------------------------

    async def test_alias_table_routes_all_absorbed_names(self):
        from backend.tool_runner import _ALIASES
        expected = {
            "tasks_add": "tasks", "tasks_list": "tasks",
            "tasks_complete": "tasks", "tasks_move": "tasks",
            "get_battery_status": "home_status",
            "get_battery_detail": "home_status",
            "get_power_summary": "home_status",
            "get_inverter_status": "home_status",
            "get_pool_status": "home_status",
            "get_dashboard_tab": "home_status",
            "get_habit_status": "home_status",
            "photos_pick": "chat_send", "photos_picked": "chat_send",
            "sys_show_uploaded_document": "chat_send",
            "process_document_upload": "chat_send",
            "doc_upload_card_action": "chat_send",
        }
        for old, new in expected.items():
            self.assertEqual(_ALIASES.get(old), new,
                             f"{old} should alias to {new}")
        provider = (Path(__file__).resolve().parents[1]
                    / "backend/realtime_provider.py").read_text()
        for canonical in ("tasks", "home_status", "chat_send"):
            self.assertIn(f'"name": "{canonical}"', provider)
        # no absorbed name survives as a declaration dict
        for absorbed in expected:
            self.assertNotIn(f'"name": "{absorbed}"', provider)

    # -- tasks family ---------------------------------------------------

    async def test_tasks_list_alias_is_a_free_read(self):
        out = await self.runner.execute("tasks_list", {})
        self.assertIn("tasks", out)
        # canonical read path is equally ungated — action='list' never
        # had a confirmed= requirement.
        out = await self.runner.execute("tasks", {"action": "list"})
        self.assertIn("tasks", out)

    async def test_task_write_aliases_keep_confirm_gate(self):
        for name, args in [
            ("tasks_add", {"title": "buy milk"}),
            ("tasks_complete", {"task_id": "fake:@default/t1"}),
            ("tasks_move", {"task_id": "fake:@default/t1",
                            "due": "tomorrow"}),
            ("tasks", {"action": "add", "title": "x"}),
            ("tasks", {"action": "done", "task_id": "fake:@default/t1"}),
            ("tasks", {"action": "move", "task_id": "fake:@default/t1",
                       "due": "tomorrow"}),
        ]:
            with self.assertRaises(PermissionError, msg=name):
                await self.runner.execute(name, dict(args))
        self.assertEqual(self.provider.tasks, [])
        self.assertEqual(self.provider.completed, [])

    async def test_task_write_aliases_execute_when_confirmed(self):
        out = await self.runner.execute(
            "tasks_add", {"title": "buy milk", "confirmed": True})
        self.assertEqual(out["title"], "buy milk")
        out = await self.runner.execute(
            "tasks_complete",
            {"task_id": "fake:@default/t1", "confirmed": True})
        self.assertIn("completed", out["output"])
        out = await self.runner.execute(
            "tasks_move",
            {"task_id": "fake:@default/t1", "due": "tomorrow",
             "confirmed": True})
        self.assertEqual(out["title"], "moved")
        self.provider.move_task.assert_awaited_once()

    async def test_tasks_rejects_unknown_action(self):
        # gate runs before action validation (same as cms_edit) — a bogus
        # action still needs confirmed to reach the ValueError.
        with self.assertRaises(PermissionError):
            await self.runner.execute("tasks", {"action": "bogus"})
        out = await self.runner.execute(
            "tasks", {"action": "bogus", "confirmed": True})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    # -- home_status family ----------------------------------------------

    async def test_home_status_aliases_route(self):
        await self.runner.execute("get_battery_status", {})
        self.ha_client.battery_status.assert_awaited_once()
        await self.runner.execute(
            "get_battery_detail", {"battery_index": 2})
        self.ha_client.battery_detail.assert_awaited_once_with(2)
        await self.runner.execute("get_power_summary", {"hours": 6})
        self.ha_client.power_summary.assert_awaited_once_with(hours=6)
        await self.runner.execute("get_inverter_status", {})
        self.ha_client.inverter_status.assert_awaited_once()
        await self.runner.execute("get_pool_status", {})
        self.ha_client.pool_status.assert_awaited_once()
        await self.runner.execute("get_dashboard_tab", {"tab": "TPL"})
        self.ha_client.dashboard_tab.assert_awaited_once_with("TPL")
        out = await self.runner.execute("get_habit_status", {})
        self.assertEqual(out, {"habits": [], "ok": True})

    async def test_home_status_canonical_what_dispatch(self):
        out = await self.runner.execute(
            "home_status", {"what": "power", "hours": 12})
        self.assertEqual(out, {"solar_kwh": 5, "ok": True})
        self.ha_client.power_summary.assert_awaited_once_with(hours=12)
        # what='battery' with no index reads the bank, with an index
        # reads one battery (the absorbed get_battery_detail path).
        out = await self.runner.execute("home_status", {"what": "battery"})
        self.assertEqual(out, {"soc": 90, "ok": True})
        out = await self.runner.execute(
            "home_status", {"what": "battery", "battery_index": 2})
        self.assertEqual(out, {"battery": 2, "ok": True})
        out = await self.runner.execute("home_status", {"what": "bogus"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")
        out = await self.runner.execute("home_status", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    # -- chat_send photo/doc flows ---------------------------------------

    async def test_photos_pick_alias_starts_picker_and_queues_send(self):
        self.runner._chat_send_queue = AsyncMock(
            return_value={"status": "queued", "job_id": "chat-x"})
        with patch("backend.tool_runner.doc_archive_client"
                   ".photos_picker_create",
                   new=AsyncMock(return_value={
                       "session_id": "s1",
                       "picker_uri": "https://photos.example/pick"})):
            out = await self.runner.execute("photos_pick", {})
        self.assertEqual(out["session_id"], "s1")
        self.assertEqual(out["send"]["status"], "queued")
        sent = self.runner._chat_send_queue.call_args.kwargs
        self.assertIn("https://photos.example/pick", sent["text"])

    async def test_photos_picked_alias_polls_and_delivers(self):
        self.runner._chat_send_queue = AsyncMock(
            return_value={"status": "queued", "job_id": "chat-x"})
        self.runner.cast_to_screen = AsyncMock(return_value={"ok": True})
        with patch("backend.tool_runner.doc_archive_client"
                   ".photos_picker_poll",
                   new=AsyncMock(return_value={
                       "picked": True,
                       "items": [{"baseUrl": "https://photos.example/i1",
                                  "mimeType": "image/jpeg",
                                  "filename": "a.jpg"}]})):
            out = await self.runner.execute(
                "photos_picked", {"session_id": "s1", "screen": 3})
        self.assertTrue(out["picked"])
        self.runner.cast_to_screen.assert_awaited_once()
        sent = self.runner._chat_send_queue.call_args.kwargs
        self.assertEqual(sent["image_url"],
                         "https://photos.example/i1=w2048")

    async def test_doc_aliases_route_and_gate(self):
        from backend import document_check
        engine = document_check.engine()
        engine._hold("doc/test-1", b"pdf", b"jpg",
                     {"doc_type": "letter", "filename": "deed.pdf"})
        try:
            out = await self.runner.execute(
                "sys_show_uploaded_document", {"intake_key": "doc/test-1"})
            self.assertEqual(out["doc"], "doc/test-1")
            self.assertEqual(out["doc_type"], "letter")
            out = await self.runner.execute(
                "process_document_upload", {})
            self.assertEqual(out["doc"], "doc/test-1")  # newest held
            # The card action's 'archive' button re-dispatches through
            # execute() — ada_doc_archive's confirm gate denial surfaces
            # as a normalized failure result.
            out = await self.runner.execute(
                "doc_upload_card_action",
                {"action": "archive", "intake_key": "doc/test-1"})
            self.assertFalse(out["ok"])
            self.assertEqual(out["error_type"], "PermissionError")
        finally:
            engine._held.pop("doc/test-1", None)

    async def test_doc_alias_without_held_intake_errors(self):
        out = await self.runner.execute(
            "sys_show_uploaded_document", {"intake_key": "doc/nope"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "RuntimeError")

    async def test_chat_send_plain_send_still_queues(self):
        self.runner._chat_send_run = AsyncMock()
        out = await self.runner.execute(
            "chat_send", {"channel": "line", "text": "hello"})
        self.assertEqual(out["status"], "queued")

    async def test_chat_send_rejects_unknown_photo_doc(self):
        out = await self.runner.execute("chat_send", {"photo": "bogus"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")
        out = await self.runner.execute("chat_send", {"doc": "bogus"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    async def test_chat_send_photo_unconfigured_degrades_honestly(self):
        # The doc-archive service 501s when GPHOTO_REFRESH_TOKEN is
        # missing — the model must see 'not configured', not an opaque
        # HTTP code, and nothing is queued to the channel.
        from backend import doc_archive_client
        err = doc_archive_client.PhotosNotConfiguredError(
            "photo sending is not configured")
        with patch("backend.tool_runner.doc_archive_client"
                   ".photos_picker_create",
                   new=AsyncMock(side_effect=err)):
            out = await self.runner.execute(
                "chat_send", {"photo": "pick", "channel": "line"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "PhotosNotConfiguredError")
        self.assertIn("not configured", out["error"])
        with patch("backend.tool_runner.doc_archive_client"
                   ".photos_picker_poll",
                   new=AsyncMock(side_effect=err)):
            out = await self.runner.execute(
                "chat_send", {"photo": "picked", "session_id": "s1"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "PhotosNotConfiguredError")


class YtMergeAliasTests(unittest.IsolatedAsyncioTestCase):
    """tools-merge-yt (4 -> 1): the four absorbed names stay callable via
    _ALIASES and route to yt(action=cast|status|stop|transcript)."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        # _capture_reminder probes the vcast relay on every dict result —
        # stub it so the tests stay hermetic.
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}

    async def test_cast_alias_routes_to_yt_cast_action(self):
        with patch.object(
                ToolRunner, "_yt_api", return_value={"ok": True}) as api:
            out = await self.runner.execute(
                "yt_cast", {"query": "the egg kurzgesagt"})
        self.assertEqual(out, {"ok": True})
        api.assert_called_once_with(
            "/cast", {"q": "the egg kurzgesagt", "lang": "th"})

    async def test_status_and_stop_aliases_route(self):
        with patch.object(
                ToolRunner, "_yt_api", return_value={"ok": True}) as api:
            await self.runner.execute("yt_cast_status", {})
            await self.runner.execute("yt_cast_stop", {})
        self.assertEqual(
            [c.args for c in api.call_args_list],
            [("/status",), ("/stop", {})])

    async def test_transcript_alias_routes_with_url(self):
        proc = SimpleNamespace(
            returncode=0,
            stdout="TITLE: Evening news\nLANG: th\nfull transcript text",
            stderr="")
        with patch("subprocess.run", return_value=proc):
            out = await self.runner.execute(
                "yt_transcript", {"url": "https://youtu.be/abc"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["title"], "Evening news")
        self.assertEqual(out["transcript"], "full transcript text")

    async def test_canonical_dispatch_and_fallbacks(self):
        with patch.object(
                ToolRunner, "_yt_api", return_value={"ok": True}) as api:
            # url= is accepted as the cast target too
            await self.runner.execute(
                "yt", {"action": "cast", "url": "https://youtu.be/x"})
            await self.runner.execute("yt", {"action": "status"})
        self.assertEqual(
            [c.args for c in api.call_args_list],
            [("/cast", {"q": "https://youtu.be/x", "lang": "th"}),
             ("/status",)])

    async def test_canonical_rejects_unknown_action(self):
        out = await self.runner.execute("yt", {"action": "bogus"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")



class ConfirmationGateTests(unittest.IsolatedAsyncioTestCase):
    """The flexible-but-bound confirmation gate: truthy spellings pass,
    denials mint single-use tokens bound to the exact call, and the
    ledger records every proposal/grant/consumption."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.ha_client.entities.return_value = [
            {"entity_id": "cover.gate", "state": "closed", "available": True, "name": "Gate"},
        ]
        self.ha_client.control_cover.return_value = {"ok": True}
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.runner.mddb = AsyncMock()
        # no live captures in unit tests — keep the capture_reminder
        # advisory off result dicts (and don't probe the real relay)
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}
        self.runner.mddb.search_documents.return_value = []
        self.runner.mddb.delete_document.return_value = {"status": "deleted"}
        self.runner.memory.mddb = self.runner.mddb

    async def _deny_for_token(self, tool="cms_delete_page", args=None) -> str:
        args = args or {"slug": "pool-notes"}
        with self.assertRaises(PermissionError) as ctx:
            await self.runner.execute(tool, dict(args))
        match = re.search(r"confirm_token='(cfm-[0-9a-f]+)'", str(ctx.exception))
        self.assertIsNotNone(match, f"no token in denial: {ctx.exception}")
        return match.group(1)

    async def test_truthy_spellings_accepted(self):
        for value in (True, 1, "yes", "true", "confirmed"):
            result = await self.runner.execute(
                "cms_delete_page", {"slug": "pool-notes", "confirmed": value}
            )
            self.assertEqual(result["status"], "deleted", msg=f"confirmed={value!r}")

    async def test_falsey_spellings_rejected(self):
        for value in (None, False, 0, "", "no", "nope"):
            with self.assertRaises(PermissionError, msg=f"confirmed={value!r}"):
                await self.runner.execute(
                    "cms_delete_page", {"slug": "pool-notes", "confirmed": value}
                )

    async def test_denial_mints_bound_token_then_replay_executes(self):
        token = await self._deny_for_token()
        result = await self.runner.execute(
            "cms_delete_page", {"slug": "pool-notes", "confirm_token": token}
        )
        self.assertEqual(result["status"], "deleted")
        self.runner.mddb.delete_document.assert_awaited_once()

    async def test_token_single_use_replay_rejected(self):
        token = await self._deny_for_token()
        await self.runner.execute(
            "cms_delete_page", {"slug": "pool-notes", "confirm_token": token}
        )
        with self.assertRaises(PermissionError) as ctx:
            await self.runner.execute(
                "cms_delete_page", {"slug": "pool-notes", "confirm_token": token}
            )
        self.assertIn("unknown or expired", str(ctx.exception))

    async def test_token_bound_to_exact_args(self):
        token = await self._deny_for_token(args={"slug": "pool-notes"})
        with self.assertRaises(PermissionError) as ctx:
            await self.runner.execute(
                "cms_delete_page", {"slug": "other-page", "confirm_token": token}
            )
        self.assertIn("different arguments", str(ctx.exception))

    async def test_token_bound_to_tool(self):
        token = await self._deny_for_token(args={"slug": "pool-notes"})
        with self.assertRaises(PermissionError) as ctx:
            await self.runner.execute(
                "control_cover",
                {"entity_id": "cover.gate", "action": "open", "confirm_token": token},
            )
        self.assertIn("different tool", str(ctx.exception))
        self.ha_client.control_cover.assert_not_awaited()

    async def test_expired_token_rejected(self):
        token = await self._deny_for_token()
        with patch("backend.tool_runner.CONFIRM_TOKEN_TTL_S", -1):
            with self.assertRaises(PermissionError) as ctx:
                await self.runner.execute(
                    "cms_delete_page", {"slug": "pool-notes", "confirm_token": token}
                )
        self.assertIn("expired", str(ctx.exception))

    async def test_audit_ledger_tracks_proposal_grant_and_consumption(self):
        token = await self._deny_for_token()
        await self.runner.execute(
            "cms_delete_page", {"slug": "pool-notes", "confirm_token": token}
        )
        await self.runner.execute(
            "cms_delete_page", {"slug": "pool-notes", "confirmed": "yes"}
        )
        events = [(e["event"], e["via"]) for e in self.runner.confirmation_audit()]
        self.assertIn(("denied", "unconfirmed"), events)
        self.assertIn(("proposed", "token"), events)
        self.assertIn(("consumed", "token"), events)
        self.assertIn(("granted", "confirmed"), events)
        # every entry is bound to a fingerprint of the exact call
        self.assertTrue(all(len(e["fingerprint"]) == 12 for e in self.runner.confirmation_audit()))

    async def test_confirmed_true_still_arms_dangerous_control(self):
        result = await self.runner.execute(
            "control_cover",
            {"entity_id": "cover.gate", "action": "open", "confirmed": True},
        )
        self.assertTrue(result.get("ok"))  # upstream may add advisory fields (capture_reminder)

    async def test_token_arms_dangerous_control(self):
        with self.assertRaises(PermissionError) as ctx:
            await self.runner.execute(
                "control_cover", {"entity_id": "cover.gate", "action": "open"}
            )
        token = re.search(r"confirm_token='(cfm-[0-9a-f]+)'", str(ctx.exception)).group(1)
        result = await self.runner.execute(
            "control_cover",
            {"entity_id": "cover.gate", "action": "open", "confirm_token": token},
        )
        self.assertTrue(result.get("ok"))  # upstream may add advisory fields (capture_reminder)

    async def test_pending_confirm_tracks_denied_call(self):
        # 2026-10-07 flake: the provider consults pending_confirm to pass
        # the model's confirmed=true resend through — it must match only
        # the exact denied call. Tokens are minted under the RESOLVED
        # name/args (cms_delete_page -> cms_edit action='delete'), which
        # is also what the provider queries after its own alias pass.
        canon = {"action": "delete", "slug": "pool-notes"}
        self.assertFalse(self.runner.pending_confirm("cms_edit", canon))
        await self._deny_for_token()
        self.assertTrue(self.runner.pending_confirm("cms_edit", canon))
        self.assertFalse(self.runner.pending_confirm(
            "cms_edit", {"action": "delete", "slug": "other-page"}))
        self.assertFalse(self.runner.pending_confirm(
            "control_cover",
            {"entity_id": "cover.gate", "action": "open"}))

    async def test_pending_confirm_expires_with_token(self):
        await self._deny_for_token()
        with patch("backend.tool_runner.CONFIRM_TOKEN_TTL_S", -1):
            self.assertFalse(self.runner.pending_confirm(
                "cms_edit", {"action": "delete", "slug": "pool-notes"}))

    async def test_confirmed_grant_burns_pending_token(self):
        # The flag-grant path must retire the outstanding token for the
        # same call — otherwise a minted token could re-arm the write.
        token = await self._deny_for_token()
        result = await self.runner.execute(
            "cms_delete_page", {"slug": "pool-notes", "confirmed": True}
        )
        self.assertEqual(result["status"], "deleted")
        self.assertFalse(self.runner.pending_confirm(
            "cms_edit", {"action": "delete", "slug": "pool-notes"}))
        with self.assertRaises(PermissionError) as ctx:
            await self.runner.execute(
                "cms_delete_page",
                {"slug": "pool-notes", "confirm_token": token},
            )
        self.assertIn("unknown or expired", str(ctx.exception))

    async def test_denial_text_instructs_exact_replay_arg(self):
        with self.assertRaises(PermissionError) as ctx:
            await self.runner.execute("cms_delete_page", {"slug": "pool-notes"})
        text = str(ctx.exception)
        self.assertIn("confirm_token='cfm-", text)
        self.assertIn("replay the SAME call", text)


class DevinJobReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner.mddb = AsyncMock()
        self.runner.mddb.add_document.return_value = {"status": "ok"}

    @staticmethod
    def _job_doc(task_id, status, host="tony-dell", ts="2026-09-27T14:00:00+00:00",
                 body_extra="", question=None):
        meta = {
            "kind": ["job"], "status": [status], "job_id": [task_id],
            "host": [host], "ts": [ts],
        }
        if question:
            meta["question"] = [question]
        return {
            "key": f"job/{task_id}",
            "meta": meta,
            "contentMd": f"Job {task_id} on {host}: {status}.\n\n{body_extra}",
        }

    def _ledger(self):
        return [
            self._job_doc("20260927-215500-active-task", "running"),
            self._job_doc("20260927-040246-spawn-failed", "running",
                          host="mn01"),
            self._job_doc("20260927-110116-finished-ok", "done",
                          host="tony-omen"),
            self._job_doc("resume-20260926-dead-task", "running"),
            self._job_doc("20260926-191209-old-task", "superseded"),
            self._job_doc("20260927-120000-blocked", "awaiting-user",
                          host="tony-omen", question="which repo?"),
        ]

    def _dispatch_rows(self):
        # Spawn failure signature on the dispatch host: ledger says running,
        # unit is inactive/gone, transcript was never written.
        return [
            {"task_id": "20260927-215500-active-task", "state": "active",
             "result": "-", "repo": "ada-pi", "transcript": "never"},
            {"task_id": "resume-20260926-dead-task", "state": "inactive",
             "result": "success", "repo": "resume", "transcript": "never"},
        ]

    async def test_failed_jobs_render_in_dedicated_section(self):
        self.runner.mddb.search_documents.return_value = self._ledger()
        with patch("backend.tool_runner.devin_dispatch_mod.tasks",
                   new=AsyncMock(return_value=self._dispatch_rows())):
            result = await self.runner.execute("devin_job_report", {})
        md = result["markdown"]
        failed_idx = md.index("## Failed jobs")
        done_idx = md.index("## Done")
        active_idx = md.index("## Active jobs")
        stale_idx = md.index("## Stale / superseded")
        self.assertLess(active_idx, failed_idx)
        self.assertLess(failed_idx, done_idx)
        self.assertLess(done_idx, stale_idx)
        # Spawn failure: ledger 'running' + dead unit + no transcript → failed.
        failed_block = md[failed_idx:done_idx]
        self.assertIn("resume-20260926-dead-task", failed_block)
        self.assertIn("no transcript", failed_block)
        self.assertIn("counts", result)
        self.assertEqual(result["counts"]["failed"], 1)
        # Active = the running dispatch task + mn01's 'running' doc (remote
        # host: ledger stays authoritative, can't verify from here) + the
        # awaiting-user job.
        self.assertEqual(result["counts"]["active"], 3)
        self.assertEqual(result["counts"]["done"], 1)
        self.assertEqual(result["counts"]["stale"], 1)

    async def test_ledger_failed_status_goes_to_failed_section(self):
        self.runner.mddb.search_documents.return_value = [
            self._job_doc("20260927-040246-audit-tests", "failed",
                          host="mn01", body_extra="spawn failure, no transcript"),
            self._job_doc("20260927-215500-live-task", "running"),
        ]
        with patch("backend.tool_runner.devin_dispatch_mod.tasks",
                   new=AsyncMock(return_value=[])):
            result = await self.runner.execute("devin_job_report", {})
        md = result["markdown"]
        failed_block = md[md.index("## Failed jobs"):md.index("## Done")]
        self.assertIn("20260927-040246-audit-tests", failed_block)
        self.assertIn("spawn failure", failed_block)
        active_block = md[md.index("## Active jobs"):md.index("## Failed jobs")]
        self.assertIn("20260927-215500-live-task", active_block)
        self.assertNotIn("20260927-040246-audit-tests", active_block)

    async def test_compose_survives_dispatch_status_outage(self):
        self.runner.mddb.search_documents.return_value = self._ledger()
        with patch("backend.tool_runner.devin_dispatch_mod.tasks",
                   new=AsyncMock(side_effect=RuntimeError("ssh down"))):
            result = await self.runner.execute("devin_job_report", {})
        self.assertEqual(result["status"], "composed")
        self.assertIn("## Failed jobs", result["markdown"])

    async def test_publish_requires_confirmed(self):
        self.runner.mddb.search_documents.return_value = self._ledger()
        with patch("backend.tool_runner.devin_dispatch_mod.tasks",
                   new=AsyncMock(return_value=[])):
            out = await self.runner.execute(
                "devin_job_report", {"publish": True})
            self.assertFalse(out["ok"])
            self.assertEqual(out["error_type"], "PermissionError")
            self.runner.mddb.add_document.assert_not_awaited()
            result = await self.runner.execute(
                "devin_job_report", {"publish": True, "confirmed": True})
        self.assertEqual(result["status"], "published")
        args, kwargs = _page_call(self.runner, "devin-job-report")
        self.assertEqual(args[:3], ("ada-cms-pages", "devin-job-report", "en"))
        self.assertIn("## Failed jobs", args[3])


class DevinMergeAliasTests(unittest.IsolatedAsyncioTestCase):
    """tools-merge-devin-mcp (8 -> 2): the eight absorbed names stay
    callable via _ALIASES and route to their canonical parent — devin
    for dispatch/followup/answer (confirm-gated), devin_read for
    status/jobs/pending/report/review."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.runner.mddb = AsyncMock()
        # no live captures in unit tests — keep the capture_reminder
        # advisory off result dicts (and don't probe the real relay)
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}
        self.runner.mddb.search_documents.return_value = []
        self.runner.mddb.add_document.return_value = {"status": "ok"}
        self.runner.mddb.get_document.return_value = None

    async def test_dispatch_alias_keeps_confirm_gate(self):
        args = {"repo": "ada-pi", "task": "do the thing"}
        with self.assertRaises(PermissionError):
            await self.runner.execute("devin_dispatch", dict(args))
        with patch("backend.tool_runner.devin_dispatch_mod.dispatch",
                   new=AsyncMock(return_value={"task_id": "t-1"})) as disp:
            out = await self.runner.execute(
                "devin_dispatch", {**args, "confirmed": True})
        self.assertEqual(out["task_id"], "t-1")
        args, kwargs = disp.await_args
        self.assertEqual(args, ("ada-pi", "do the thing"))
        self.assertIsNone(kwargs.get("playbook"))

    async def test_followup_alias_keeps_confirm_gate(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "devin_followup", {"task_id": "t-1", "message": "hi"})
        with patch("backend.tool_runner.devin_dispatch_mod.followup",
                   new=AsyncMock(return_value="sent")) as fu:
            out = await self.runner.execute(
                "devin_followup",
                {"task_id": "t-1", "message": "hi", "confirmed": True})
        self.assertEqual(out["output"], "sent")
        fu.assert_awaited_once_with("t-1", "hi")

    async def test_answer_alias_keeps_confirm_gate_and_records(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "devin_answer",
                {"task_id": "test-ans-9", "message": "use chaba"})
        self.runner.mddb.add_document.assert_not_awaited()
        out = await self.runner.execute(
            "devin_answer",
            {"task_id": "test-ans-9", "message": "use chaba",
             "confirmed": True})
        self.assertEqual(out["task_id"], "test-ans-9")
        keys = [c.kwargs.get("key")
                for c in self.runner.mddb.add_document.call_args_list]
        self.assertIn("answer/test-ans-9", keys)

    async def test_read_aliases_route_to_devin_read(self):
        with patch("backend.tool_runner.devin_dispatch_mod.status",
                   new=AsyncMock(return_value="two tasks")) as st:
            out = await self.runner.execute("devin_status", {})
        self.assertEqual(out["output"], "two tasks")
        st.assert_awaited_once_with(None)

        out = await self.runner.execute("devin_pending", {})
        self.assertEqual(out["output"], [])
        out = await self.runner.execute("devin_jobs", {"status": "failed"})
        self.assertEqual(out["output"], [])
        fm = self.runner.mddb.search_documents.call_args.kwargs[
            "filter_meta"]
        self.assertEqual(fm["status"], ["failed"])

    async def test_canonical_devin_read_actions(self):
        out = await self.runner.execute("devin_read", {"action": "pending"})
        self.assertEqual(out["output"], [])
        out = await self.runner.execute("devin_read", {"action": "bogus"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")
        # devin is a confirm-gated seat — the gate runs before action
        # validation, same as the absorbed writers did pre-merge.
        with self.assertRaises(PermissionError):
            await self.runner.execute("devin", {"action": "bogus"})
        out = await self.runner.execute(
            "devin", {"action": "bogus", "confirmed": True})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    async def test_canonical_devin_dispatch(self):
        with patch("backend.tool_runner.devin_dispatch_mod.dispatch",
                   new=AsyncMock(return_value={"task_id": "t-9"})) as disp:
            out = await self.runner.execute(
                "devin",
                {"action": "dispatch", "repo": "chaba", "task": "x",
                 "confirmed": True})
        self.assertEqual(out["task_id"], "t-9")
        args, kwargs = disp.await_args
        self.assertEqual(args, ("chaba", "x"))
        self.assertIsNone(kwargs.get("playbook"))

    async def test_report_publish_gate_on_canonical(self):
        with patch("backend.tool_runner.devin_dispatch_mod.tasks",
                   new=AsyncMock(return_value=[])):
            out = await self.runner.execute(
                "devin_read", {"action": "report", "publish": True})
            self.assertFalse(out["ok"])
            self.assertEqual(out["error_type"], "PermissionError")
            self.runner.mddb.add_document.assert_not_awaited()
            out = await self.runner.execute(
                "devin_read",
                {"action": "report", "publish": True, "confirmed": True})
        self.assertEqual(out["status"], "published")

    async def test_review_alias_denied_without_full_policy(self):
        # ada_devteam_review was manifest owner_only — the hermetic
        # registry has no person_policies, so the review action refuses.
        out = await self.runner.execute(
            "ada_devteam_review", {"request": "a tool that counts"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "PermissionError")

    async def test_review_action_owner_allowed(self):
        reg_path = Path(tempfile.mkdtemp()) / "banks.json"
        reg_path.write_text(json.dumps({
            "banks": {},
            "person_policies": {"person.tony": {"full": True}},
        }))
        self.runner._banks = MemoryBankRegistry(
            path=str(reg_path), instance="test", notebook_ids={})
        review = {
            "verdict": "revise", "model": "m",
            "panel": [{"role": "security", "verdict": "ok",
                       "summary": "fine", "comments": []}],
            "spec": "## spec",
        }
        with patch("backend.devteam.review",
                   new=AsyncMock(return_value=review)):
            out = await self.runner.execute(
                "ada_devteam_review", {"request": "a tool that counts"},
                identity="person.tony")
        self.assertTrue(out["ok"])
        self.assertEqual(out["verdict"], "revise")
        key = self.runner.mddb.add_document.call_args.kwargs["key"]
        self.assertTrue(key.startswith("spec/"))


class CastToScreenRouteTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for the 2026-10-01 "camera never changes"
    failure: Ada cast a JPEG snapshot with action='play' (renders a black
    video-vw0 pane) and an invented URL that did not even resolve — the
    screen stayed on the old camera while the tool claimed delivery."""

    async def asyncSetUp(self):
        self.runner = ToolRunner(AsyncMock(), instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.pubbed = []
        self.display_state = "image"
        self.display_detail = "p0:img-nw800 https://img.test/x.jpg"

        def fake_vcast(path, payload=None):
            if path == "/pub":
                self.pubbed.append(payload)
                return {"ok": True, "delivered": 1}
            if path == "/displays":
                return {"screens": [{
                    "screen": 1, "connected": True,
                    "state": self.display_state,
                    "state_detail": self.display_detail}]}
            return {"captures": {}}

        patcher = patch.object(
            ToolRunner, "_vcast_api", staticmethod(fake_vcast))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_play_with_image_url_reroutes_to_image(self):
        # A still image sent to <video> never decodes — the tool must
        # auto-route to the image pane instead of producing black.
        with patch.object(ToolRunner, "_frame_check",
                          staticmethod(lambda url:
                                       {"content_type": "image/jpeg"})):
            out = await self.runner.cast_to_screen(
                1, "play", url="https://img.test/x.jpg")
        self.assertTrue(out["ok"])
        self.assertEqual(self.pubbed[-1]["msg"]["type"], "image")
        self.assertIn("action_fixed", out)

    async def test_dead_url_never_reaches_display(self):
        # An invented/unresolvable URL must hard-fail before the pub —
        # casting it produces a silent black pane.
        with patch.object(ToolRunner, "_frame_check",
                          staticmethod(lambda url:
                                       {"dead": "gaierror: -2"})):
            out = await self.runner.cast_to_screen(
                1, "play", url="https://dead.invalid/x")
        self.assertFalse(out["ok"])
        self.assertIn("unreachable", out["error"])
        self.assertEqual(self.pubbed, [])

    async def test_image_interval_passes_through(self):
        out = await self.runner.cast_to_screen(
            1, "image", url="https://img.test/x.jpg", interval=15)
        self.assertEqual(self.pubbed[-1]["msg"]["type"], "image")
        self.assertEqual(self.pubbed[-1]["msg"]["interval"], 15)
        self.assertNotIn("render_warn", out)

    async def test_dead_render_warns_instead_of_claiming_success(self):
        # video-vw0 = zero decoded frames — the pane is black. The result
        # must flag it so Ada doesn't report success.
        self.display_state = "playing"
        self.display_detail = "p0:video-vw0"
        with patch.object(ToolRunner, "_frame_check",
                          staticmethod(lambda url: {})):
            # screen reports 'playing' -> interrupt gate needs a confirm
            out = await self.runner.cast_to_screen(
                1, "play", url="https://vid.test/x.mp4", confirmed=True)
        self.assertIn("render_warn", out)
        self.assertNotIn("action_fixed", out)


class TvInputMismatchTests(unittest.IsolatedAsyncioTestCase):
    """cast-input-mismatch (2026-10-04): cctv_wall 'delivered' while the
    TV sat on the True STB app for 3min — the wall loaded in the
    backgrounded webOS browser, invisible. Post-cast the runner must read
    the TV's foreground app via HA and, on mismatch, push the URL at the
    TV browser (tv_action nav) and report honestly."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.tv_action.return_value = {"ok": True}
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.pubbed = []
        # TV-hosted screen 1; screen 2 is a plain vcast display.
        reg = Path(tempfile.mkdtemp()) / "cast-screens.json"
        reg.write_text(json.dumps({
            "screens": {"1": "shared", "2": "shared"},
            "tv": {"entity": "media_player.tony_tv",
                   "screens": [1], "ok_apps": ["browser"]},
        }))
        env = patch.dict(os.environ, {
            "ADA_CAST_SCREENS": str(reg),
            # dead port — the camwall manifest read fails fast offline
            "ADA_CAMWALL_BASE": "http://127.0.0.1:9/",
        })
        env.start()
        self.addCleanup(env.stop)

        def fake_vcast(path, payload=None):
            if path == "/pub":
                self.pubbed.append(payload)
                return {"ok": True, "delivered": 1}
            if path == "/displays":
                return {"screens": [
                    {"screen": 1, "name": "screen-1", "label": "TONY-TV",
                     "connected": True, "state": "nav",
                     "state_detail": "camwall ?zone=noble-park"}]}
            return {"captures": {}, "zones": {}}

        p = patch.object(ToolRunner, "_vcast_api", staticmethod(fake_vcast))
        p.start()
        self.addCleanup(p.stop)

    def _tv_state(self, app):
        return {"entity_id": "media_player.tony_tv", "state": "on",
                "attributes": {"app_id": app, "app_name": app,
                               "friendly_name": "TONY-TV"}}

    async def test_wall_on_wrong_input_remediates_and_reports(self):
        # STB on the first read, browser after the remediation nav.
        self.ha_client.get_state.side_effect = [
            self._tv_state("com.truedmp.idtv"),
            self._tv_state("com.webos.app.browser"),
        ]
        out = await self.runner.cctv_wall("start", "noble-park", screen=1)
        self.assertTrue(out.get("tv_input_mismatch"))
        self.assertEqual(out["tv_input"]["app"], "com.truedmp.idtv")
        self.assertTrue(out["tv_remediation"]["ok"])
        # the wall URL is pushed at the TV browser as an absolute URL
        nav = self.ha_client.tv_action.await_args
        self.assertEqual(nav.kwargs.get("cmd") or nav.args[0], "nav")
        self.assertIn("/apps/camwall/", str(nav))
        self.assertTrue(str(nav.args[1] if len(nav.args) > 1
                            else nav.kwargs["text"]).startswith("https://"))
        self.assertIn("now showing", out["note"])
        self.assertNotIn("NOT visible", out["note"])

    async def test_wall_still_wrong_input_reports_not_visible(self):
        self.ha_client.get_state.side_effect = [
            self._tv_state("com.truedmp.idtv"),
            self._tv_state("com.truedmp.idtv"),  # nav didn't switch it
        ]
        out = await self.runner.cctv_wall("start", "noble-park", screen=1)
        self.assertTrue(out.get("tv_input_mismatch"))
        self.assertIn("NOT visible", out["note"])

    async def test_wall_on_right_input_no_remediation(self):
        self.ha_client.get_state.return_value = self._tv_state(
            "com.webos.app.browser")
        out = await self.runner.cctv_wall("start", "noble-park", screen=1)
        self.assertNotIn("tv_input_mismatch", out)
        self.assertEqual(out["tv_input"]["on_cast_app"], True)
        self.ha_client.tv_action.assert_not_awaited()

    async def test_non_tv_screen_skips_the_check(self):
        out = await self.runner.cctv_wall("start", "noble-park", screen=2)
        self.assertNotIn("tv_input", out)
        self.ha_client.get_state.assert_not_awaited()

    async def test_cast_to_screen_on_tv_verifies(self):
        self.ha_client.get_state.return_value = self._tv_state(
            "com.truedmp.idtv")
        out = await self.runner.cast_to_screen(
            1, "image", url="https://img.test/x.jpg")
        self.assertTrue(out.get("tv_input_mismatch"))
        self.ha_client.tv_action.assert_awaited_once()

    async def test_tv_action_nav_appends_input_state(self):
        self.ha_client.get_state.return_value = self._tv_state(
            "com.truedmp.idtv")
        out = await self.runner.tv_action(cmd="nav", text="https://x.test/")
        self.assertEqual(out["tv_input"]["app"], "com.truedmp.idtv")
        self.assertIn("input_warn", out)
        self.assertIn("NOT visible", out["input_warn"])

    async def test_tv_action_nav_clean_input_no_warn(self):
        self.ha_client.get_state.return_value = self._tv_state(
            "com.webos.app.browser")
        out = await self.runner.tv_action(cmd="nav", text="https://x.test/")
        self.assertEqual(out["tv_input"]["on_cast_app"], True)
        self.assertNotIn("input_warn", out)

    async def test_vcast_list_surfaces_tv_input(self):
        self.ha_client.get_state.return_value = self._tv_state(
            "com.truedmp.idtv")
        out = await self.runner.vcast_list()
        self.assertEqual(out["tv_input"]["app"], "com.truedmp.idtv")
        self.assertFalse(out["tv_input"]["on_cast_app"])
        self.assertIn("warning", out["tv_input"])
        self.assertTrue(out["screens"][0].get("on_tv"))

    async def test_ha_unreachable_does_not_block_the_cast(self):
        self.ha_client.get_state.side_effect = RuntimeError("ha down")
        out = await self.runner.cctv_wall("start", "noble-park", screen=1)
        self.assertTrue(out.get("ok"))
        self.assertIsNone(out["tv_input"]["on_cast_app"])
        self.ha_client.tv_action.assert_not_awaited()


class TvGevLaneTests(unittest.IsolatedAsyncioTestCase):
    """tv-cast-gev-path (2026-10-05): 'nav gev' and /apps/gev/ URL navs
    went through shotAndCast — six {ok,cast:200} results while the TV
    showed a frozen PNG. GEV nav targets now rewrite to the live
    tony-omen workspace lane, and cast-bearing results verify the cast
    target actually left off/idle (off-Chromecast 200 no-op class)."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.tv_action.return_value = {
            "ok": True, "cast": "camera.play_stream:200",
            "stream": "tony-omen/index0.m3u8"}
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        # Registry with no 'sources' block — desktop-source ACL is shared.
        reg = Path(tempfile.mkdtemp()) / "cast-screens.json"
        reg.write_text(json.dumps({"screens": {}}))
        env = patch.dict(os.environ, {"ADA_CAST_SCREENS": str(reg)})
        env.start()
        self.addCleanup(env.stop)
        # Keep the verification polls instant.
        slp = patch("asyncio.sleep", new=AsyncMock())
        slp.start()
        self.addCleanup(slp.stop)
        self._ha_states()

    def _ha_states(self, cast_state="playing",
                   select="TONY-TV Cast", tv_app="com.webos.app.browser"):
        def fake(entity_id):
            if entity_id == "input_select.tv_cast_target":
                return {"state": select}
            if entity_id == "media_player.tony_tv_cast":
                return {"state": cast_state, "attributes": {}}
            if entity_id == "media_player.tony_tv":
                return {"entity_id": entity_id, "state": "on",
                        "attributes": {"app_id": tv_app,
                                       "app_name": tv_app}}
            return {"state": "unknown"}
        self.ha_client.get_state.side_effect = fake

    def _sent_text(self):
        call = self.ha_client.tv_action.await_args
        return call.args[1] if len(call.args) > 1 else call.kwargs["text"]

    async def test_nav_gev_key_rewrites_to_live_lane(self):
        out = await self.runner.tv_action(cmd="nav", text="gev")
        self.assertEqual(self._sent_text(), "tony-omen:workspace:4")
        self.assertEqual(out["gev_lane"], "tony-omen:workspace:4")
        self.assertEqual(out["cast_verify"]["state"], "playing")
        self.assertTrue(out["ok"])

    async def test_nav_gev_url_rewrites_too(self):
        out = await self.runner.tv_action(
            cmd="nav",
            text="https://tony-dell.taila0626a.ts.net/apps/gev/")
        self.assertEqual(self._sent_text(), "tony-omen:workspace:4")
        self.assertIn("gev_lane", out)

    async def test_nav_relative_gev_path_rewrites(self):
        await self.runner.tv_action(cmd="nav", text="/apps/gev/")
        self.assertEqual(self._sent_text(), "tony-omen:workspace:4")

    async def test_plain_url_nav_not_rewritten(self):
        out = await self.runner.tv_action(
            cmd="nav", text="https://example.com/")
        self.assertEqual(self._sent_text(), "https://example.com/")
        self.assertNotIn("gev_lane", out)

    async def test_lane_env_override(self):
        with patch.dict(os.environ,
                        {"ADA_GEV_TV_TARGET": "tony-omen:workspace:2"}):
            await self.runner.tv_action(cmd="nav", text="gev")
        self.assertEqual(self._sent_text(), "tony-omen:workspace:2")

    async def test_cast_verify_off_player_fails_loudly(self):
        self._ha_states(cast_state="off")
        out = await self.runner.tv_action(cmd="nav", text="gev")
        self.assertFalse(out["ok"])
        self.assertEqual(out["cast_verify"]["state"], "off")
        self.assertIn("tony_tv_cast", out["error"])
        self.assertIn("nothing reached the TV", out["error"])

    async def test_cast_verify_idle_player_fails(self):
        self._ha_states(cast_state="idle")
        out = await self.runner.tv_action(
            cmd="nav", text="https://example.com/")
        self.assertFalse(out["ok"])

    async def test_cast_verify_wakes_after_first_poll(self):
        # Player still 'off' on the first read, 'playing' on the second —
        # the poll must wait out the wake latency instead of failing.
        states = iter([{"state": "TONY-TV Cast"}])
        player = iter([{"state": "off"}, {"state": "playing"}])

        def fake(entity_id):
            if entity_id == "input_select.tv_cast_target":
                return next(states, {"state": "TONY-TV Cast"})
            if entity_id == "media_player.tony_tv_cast":
                return next(player, {"state": "playing"})
            if entity_id == "media_player.tony_tv":
                return {"state": "on",
                        "attributes": {"app_id": "com.webos.app.browser"}}
            return {"state": "unknown"}
        self.ha_client.get_state.side_effect = fake
        out = await self.runner.tv_action(cmd="nav", text="gev")
        self.assertTrue(out["ok"])
        self.assertEqual(out["cast_verify"]["state"], "playing")

    async def test_verify_inconclusive_does_not_fail(self):
        # HA unreachable -> no verdict; the cast-browser ok stands.
        self.ha_client.get_state.side_effect = RuntimeError("ha down")
        out = await self.runner.tv_action(cmd="nav", text="gev")
        self.assertTrue(out["ok"])
        self.assertNotIn("cast_verify", out)

    async def test_non_cast_result_skips_verify(self):
        # A bare {ok} result carries no cast attempt — nothing to verify.
        self.ha_client.tv_action.return_value = {"ok": True}
        out = await self.runner.tv_action(
            cmd="nav", text="https://example.com/")
        self.assertNotIn("cast_verify", out)


class TvTypeGuardTests(unittest.IsolatedAsyncioTestCase):
    """ada-tv-action-hallucinated-input (2026-10-09): Ada typed the
    literal scaffolds 'your-username'/'your-password' into a TV login
    form — invented placeholder text, not user dictation. cmd=type must
    refuse empty or placeholder-looking payloads; only the exact text
    the user just said may be typed."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.tv_action.return_value = {"ok": True, "typed": "ok"}
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        reg = Path(tempfile.mkdtemp()) / "cast-screens.json"
        reg.write_text(json.dumps({"screens": {}}))
        env = patch.dict(os.environ, {"ADA_CAST_SCREENS": str(reg)})
        env.start()
        self.addCleanup(env.stop)

    async def test_type_with_no_text_refused(self):
        with self.assertRaisesRegex(ValueError, "cmd=type refused"):
            await self.runner.tv_action(cmd="type")
        self.ha_client.tv_action.assert_not_awaited()

    async def test_placeholder_texts_refused(self):
        for bad in ("your-username", "your password", "your_email",
                    "<password>", "<...>", "{otp}", "xxx", "XXXX",
                    "••••••", "******"):
            with self.assertRaisesRegex(ValueError, "cmd=type refused"):
                await self.runner.tv_action(cmd="type", text=bad)
        self.ha_client.tv_action.assert_not_awaited()

    async def test_placeholder_packed_into_cmd_refused(self):
        with self.assertRaisesRegex(ValueError, "cmd=type refused"):
            await self.runner.tv_action(cmd="type your-username")
        self.ha_client.tv_action.assert_not_awaited()

    async def test_real_dictated_text_passes(self):
        for good in ("somchai@gmail.com", "P@ssw0rd!2026",
                     "search for recipe", "1234"):
            out = await self.runner.tv_action(cmd="type", text=good)
            self.assertTrue(out["ok"], good)
        self.assertEqual(self.ha_client.tv_action.await_count, 4)

    async def test_execute_returns_failed_result_with_usage(self):
        with patch.object(ToolRunner, "_vcast_api",
                          staticmethod(lambda *a, **k: {"captures": {}})):
            out = await self.runner.execute(
                "tv_action", {"cmd": "type", "text": "your-password"})
        self.assertFalse(out["ok"])
        self.assertIn("cmd=type refused", out["error"])
        self.assertIn("usage", out)  # tool_guide.yml attached on failure
        self.ha_client.tv_action.assert_not_awaited()

    async def test_other_cmds_not_guarded(self):
        self.ha_client.get_state.return_value = {
            "entity_id": "media_player.tony_tv", "state": "on",
            "attributes": {"app_id": "com.webos.app.browser",
                           "app_name": "com.webos.app.browser"}}
        await self.runner.tv_action(cmd="press", key="Enter")
        await self.runner.tv_action(cmd="scroll", text="down")
        self.assertEqual(self.ha_client.tv_action.await_count, 2)


class HaMergeAliasTests(unittest.IsolatedAsyncioTestCase):
    """tools-merge-ha (21 -> 6): every absorbed name stays callable via
    _ALIASES and routes to its canonical parent — home_search for the
    device/sensor/event finders, get_home_state for ada_ha_get_state,
    home_history for the five history reads, control_entity for the
    three domain controllers, ha_confidence for the confidence pair."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = [
            {"entity_id": "light.office", "state": "on",
             "attributes": {"friendly_name": "Office"}},
            {"entity_id": "cover.gate", "state": "closed",
             "attributes": {"friendly_name": "Gate"}},
        ]
        self.ha_client.sensors.return_value = [
            {"entity_id": "sensor.temp", "state": "23.4", "name": "Temp"},
        ]
        self.ha_client.entities.return_value = [
            {"entity_id": "cover.gate", "state": "closed",
             "available": True, "name": "Gate"},
            {"entity_id": "light.office", "state": "on",
             "available": True, "name": "Office"},
            {"entity_id": "button.gate_my_position", "state": "unknown",
             "available": True, "name": "Gate my position"},
            {"entity_id": "media_player.tony_tv", "state": "idle",
             "available": True, "name": "TV"},
        ]
        self.ha_client.search_entities.return_value = [
            {"entity_id": "light.kitchen", "name": "Kitchen"},
        ]
        self.ha_client.get_state.return_value = {
            "entity_id": "light.office", "state": "on"}
        self.ha_client.control_cover.return_value = {"ok": True}
        self.ha_client.press_button.return_value = {"pressed": True}
        self.ha_client.control_media_player.return_value = {"ok": True}
        self.ha_client.set_power.return_value = {"state": "off"}
        self.ha_client.history.return_value = [[{"state": "22"}]]
        self.ha_client.logbook.return_value = [
            {"entity_id": "cover.gate", "message": "closed"}]
        self.ha_client.state_transitions.return_value = {"transitions": []}
        self.ha_client.recent_events.return_value = {"events": []}
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}
        # memory/events/mddb are real objects in non-chaba mode — swap the
        # network faces so the suite stays hermetic.
        self.runner.mddb = AsyncMock()
        self.runner.mddb.search_documents.return_value = []
        self.runner.mddb.add_document.return_value = {"status": "ok"}
        self.runner.memory = AdaMemoryStore(
            self.ha_client, mddb_client=self.runner.mddb,
            instance_id="test")
        self.runner.events = Mock()
        self.runner.events.collection = "ha-events"
        self.runner.events.status.return_value = {"running": True}
        self.runner.events.recent.return_value = [{"kind": "ha"}]

    # -- home_search --

    async def test_device_finder_aliases_route_to_home_search(self):
        for alias in ("search_home_devices", "search_devices",
                      "ada_ha_search_devices"):
            self.ha_client.search_entities.reset_mock()
            out = await self.runner.execute(alias, {"query": "kitchen"})
            self.assertEqual(out["output"][0]["entity_id"], "light.kitchen")
            self.ha_client.search_entities.assert_awaited_once()

    async def test_list_home_devices_alias_lists_bounded(self):
        out = await self.runner.execute("list_home_devices", {})
        self.assertEqual(len(out["output"]), 4)
        self.ha_client.entities.assert_awaited_once()

    async def test_sensor_finder_aliases_route_to_home_search(self):
        for alias in ("search_sensors", "ada_ha_search_sensors"):
            self.ha_client.sensors.reset_mock()
            out = await self.runner.execute(alias, {"query": "temp"})
            self.assertEqual(out["output"][0]["entity_id"], "sensor.temp")
            self.ha_client.sensors.assert_awaited_once()
        out = await self.runner.execute("list_sensors", {})
        self.assertEqual(out["output"][0]["entity_id"], "sensor.temp")

    async def test_event_search_alias_uses_recorder_and_mddb(self):
        out = await self.runner.execute(
            "ada_ha_search_events", {"query": "gate"})
        self.assertEqual(out["events"], [{"kind": "ha"}])
        self.runner.events.recent.assert_called_once()

    async def test_home_search_canonical_kinds(self):
        out = await self.runner.execute(
            "home_search", {"query": "kitchen", "kind": "device"})
        self.assertEqual(out["output"][0]["entity_id"], "light.kitchen")
        out = await self.runner.execute(
            "home_search", {"kind": "sensor"})
        self.assertEqual(out["output"][0]["entity_id"], "sensor.temp")
        out = await self.runner.execute("home_search", {"kind": "bogus"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    # -- get_home_state --

    async def test_ada_ha_get_state_alias_routes_to_memory_overview(self):
        sentinel = {"controllable": 4, "sensors": 1}
        self.runner.memory.overview = AsyncMock(return_value=sentinel)
        out = await self.runner.execute("ada_ha_get_state", {})
        self.assertEqual(out, {**sentinel, "ok": True})
        out = await self.runner.execute(
            "get_home_state", {"domain": "memory"})
        self.assertEqual(out, {**sentinel, "ok": True})

    async def test_get_home_state_entity_and_domain_reads(self):
        out = await self.runner.execute(
            "get_home_state", {"entity_id": "light.office"})
        self.assertEqual(out["state"], "on")
        self.ha_client.get_state.assert_awaited_once_with("light.office")
        out = await self.runner.execute(
            "get_home_state", {"domain": "light"})
        self.assertEqual(
            [d["entity_id"] for d in out["output"]], ["light.office"])

    # -- home_history --

    async def test_history_aliases_route_to_home_history(self):
        out = await self.runner.execute("get_logbook", {"hours": 6})
        self.assertEqual(out["count"], 1)
        out = await self.runner.execute(
            "get_sensor_history", {"entity_id": "sensor.temp"})
        self.assertEqual(out["output"], [[{"state": "22"}]])
        out = await self.runner.execute(
            "get_entity_events", {"entity_id": "cover.gate"})
        self.assertIn("transitions", out)
        out = await self.runner.execute("get_recent_events", {})
        self.assertIn("events", out)
        out = await self.runner.execute("ada_ha_history", {})
        self.assertEqual(out["output"], [])
        self.runner.mddb.search_documents.assert_awaited()

    async def test_home_history_default_kind_resolution(self):
        # entity sensor -> series, other entity -> timeline, query -> events,
        # bare -> logbook, domain='memory' -> snapshots.
        await self.runner.execute(
            "home_history", {"entity_id": "sensor.temp"})
        self.ha_client.history.assert_awaited_once()
        await self.runner.execute(
            "home_history", {"entity_id": "cover.gate"})
        self.ha_client.state_transitions.assert_awaited_once()
        await self.runner.execute(
            "home_history", {"query": "gate"})
        self.ha_client.recent_events.assert_awaited_once()
        await self.runner.execute("home_history", {})
        self.ha_client.logbook.assert_awaited_once()
        out = await self.runner.execute(
            "home_history", {"kind": "bogus"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    # -- control_entity --

    async def test_control_aliases_dispatch_by_domain(self):
        # cover.* -> control_cover (dangerous: needs confirmed)
        out = await self.runner.execute(
            "control_cover",
            {"entity_id": "cover.gate", "action": "open",
             "confirmed": True})
        self.assertEqual(out, {"ok": True})
        self.ha_client.control_cover.assert_awaited_once_with(
            "cover.gate", "open")
        # button.* -> press_button (inherits gate danger via prefix)
        out = await self.runner.execute(
            "press_button",
            {"entity_id": "button.gate_my_position", "confirmed": True})
        self.assertEqual(out, {"pressed": True, "ok": True})
        self.ha_client.press_button.assert_awaited_once_with(
            "button.gate_my_position")
        # media_player.* -> control_media_player
        out = await self.runner.execute(
            "control_media_player",
            {"entity_id": "media_player.tony_tv", "action": "turn_on"})
        self.assertEqual(out, {"ok": True})
        self.ha_client.control_media_player.assert_awaited_once_with(
            "media_player.tony_tv", "turn_on", None)

    async def test_control_entity_canonical_dispatch(self):
        out = await self.runner.execute(
            "control_entity",
            {"entity_id": "light.office", "on": False})
        self.assertIn("Turned off light.office", out["output"])
        self.ha_client.set_power.assert_awaited_once_with(
            "light.office", False)
        # action=on|off shims the absorbed on= surface
        await self.runner.execute(
            "control_entity",
            {"entity_id": "light.office", "action": "on"})
        self.ha_client.set_power.assert_awaited_with("light.office", True)
        # domain dispatch fires even via the canonical name
        await self.runner.execute(
            "control_entity",
            {"entity_id": "cover.gate", "action": "stop",
             "confirmed": True})
        self.ha_client.control_cover.assert_awaited_with(
            "cover.gate", "stop")
        await self.runner.execute(
            "control_entity",
            {"entity_id": "media_player.tony_tv", "action": "mute"})
        self.ha_client.control_media_player.assert_awaited_with(
            "media_player.tony_tv", "mute", None)
        # press_button absorbed action='press' shim
        await self.runner.execute(
            "control_entity",
            {"entity_id": "button.gate_my_position",
             "action": "press", "confirmed": True})
        self.ha_client.press_button.assert_awaited_with(
            "button.gate_my_position")
        out = await self.runner.execute(
            "control_entity", {"entity_id": "light.office"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")

    async def test_control_aliases_keep_the_danger_gate(self):
        # The absorbed names resolve to control_entity before the gate —
        # dangerous entities still demand confirmed=true under the alias.
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "control_cover",
                {"entity_id": "cover.gate", "action": "open"})
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "press_button",
                {"entity_id": "button.gate_my_position"})

    # -- ha_confidence --

    async def test_confidence_aliases_read_and_set(self):
        groups = await self.runner.execute(
            "ada_ha_get_device_confidence", {})
        self.assertIn("trusted_working", groups)
        out = await self.runner.execute("ha_confidence", {})
        self.assertIn("needs_integration", out)
        # single-entity read
        out = await self.runner.execute(
            "ha_confidence", {"entity_id": "cover.gate"})
        self.assertEqual(out["entity_id"], "cover.gate")
        # absorbed set alias + canonical set path write through memory
        out = await self.runner.execute(
            "ada_ha_set_device_confidence",
            {"entity_id": "cover.gate", "status": "trusted_working",
             "safety": "safe"})
        self.assertIn("trusted_working", str(out))
        self.assertEqual(
            self.runner.memory._confidence["cover.gate"],
            "trusted_working")
        self.assertEqual(
            self.runner.memory._safety["cover.gate"], "safe")
        out = await self.runner.execute(
            "ha_confidence",
            {"entity_id": "light.office", "status": "learning"})
        self.assertIn("learning", str(out))
        # status is required on any write — safety-only still fails
        out = await self.runner.execute(
            "ha_confidence",
            {"entity_id": "light.office", "safety": "safe"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "ValueError")


class ControlEntityVerifyTests(unittest.IsolatedAsyncioTestCase):
    """control-entity-verify-state (2026-10-09): HA accepts a service
    call the device then ignores — 2026-10-08 transcript 9e96b5a5bc had
    volume_down x4 on a paused cast player, every call ok:true while
    the level never moved. Adjustable verbs now read the watched attr
    before+after and come back ok:false with the delta when nothing
    moved; a state target or cover position move counts as landed."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client.entities.return_value = [
            {"entity_id": "cover.gate", "state": "closed",
             "available": True, "name": "Gate"},
            {"entity_id": "media_player.tony_tv_cast", "state": "paused",
             "available": True, "name": "TV Cast"},
        ]
        # Two fake entities the actuate mocks can mutate to simulate a
        # device that responded. get_state always reads live values.
        self._mp = {"entity_id": "media_player.tony_tv_cast",
                    "state": "paused",
                    "attributes": {"volume_level": 0.4,
                                   "is_volume_muted": False,
                                   "source": "HDMI 1"}}
        self._gate = {"entity_id": "cover.gate", "state": "closed",
                      "attributes": {"current_position": 0}}

        def fake_get_state(entity_id):
            if entity_id == "media_player.tony_tv_cast":
                return dict(self._mp)
            if entity_id == "cover.gate":
                return dict(self._gate)
            return {"state": "unknown"}
        self.ha_client.get_state.side_effect = fake_get_state
        self.ha_client.control_media_player.return_value = {"ok": True}
        self.ha_client.control_cover.return_value = {"ok": True}
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.runner._vcast_api = lambda *a, **k: {"captures": {}}
        self.runner.mddb = AsyncMock()
        self.runner.mddb.search_documents.return_value = []
        self.runner.mddb.add_document.return_value = {"status": "ok"}
        self.runner.memory = AdaMemoryStore(
            self.ha_client, mddb_client=self.runner.mddb,
            instance_id="test")
        self.runner.events = Mock()
        # Keep the verification polls instant.
        slp = patch("asyncio.sleep", new=AsyncMock())
        slp.start()
        self.addCleanup(slp.stop)

    def _mp_call(self, action, **extra):
        return self.runner.execute(
            "control_entity",
            {"entity_id": "media_player.tony_tv_cast",
             "action": action, **extra})

    async def test_volume_down_moved_is_ok_with_delta(self):
        async def actuate(entity_id, action, source):
            self._mp["attributes"]["volume_level"] = 0.35
            return {"ok": True}
        self.ha_client.control_media_player.side_effect = actuate
        out = await self._mp_call("volume_down")
        self.assertTrue(out["ok"])
        self.assertEqual(out["verify"]["ok"], True)
        self.assertEqual(out["verify"]["before"], 0.4)
        self.assertEqual(out["verify"]["after"], 0.35)
        self.assertAlmostEqual(out["verify"]["delta"], -0.05)

    async def test_volume_down_noop_fails_loudly(self):
        # The 2026-10-08 bug: HA ok'd every volume_down on a paused
        # cast player while the level never moved.
        out = await self._mp_call("volume_down")
        self.assertFalse(out["ok"])
        self.assertEqual(out["verify"]["before"], 0.4)
        self.assertEqual(out["verify"]["after"], 0.4)
        self.assertEqual(out["verify"]["delta"], 0.0)
        self.assertIn("never moved", out["error"])
        self.assertIn("Do NOT claim", out["error"])

    async def test_volume_mute_toggle_verified(self):
        async def actuate(entity_id, action, source):
            self._mp["attributes"]["is_volume_muted"] = True
            return {"ok": True}
        self.ha_client.control_media_player.side_effect = actuate
        out = await self._mp_call("volume_mute")
        self.assertTrue(out["ok"])
        self.assertEqual(out["verify"]["attr"], "is_volume_muted")

    async def test_dead_player_fails_fast(self):
        self._mp["state"] = "off"
        self._mp["attributes"] = {}
        out = await self._mp_call("volume_down")
        self.assertFalse(out["ok"])
        self.assertEqual(out["verify"]["state"], "off")

    async def test_select_source_reaches_requested(self):
        async def actuate(entity_id, action, source):
            self._mp["attributes"]["source"] = "YouTube"
            return {"ok": True}
        self.ha_client.control_media_player.side_effect = actuate
        out = await self._mp_call("select_source", source="youtube")
        self.assertTrue(out["ok"])

    async def test_select_source_wrong_input_fails(self):
        out = await self._mp_call("select_source", source="Netflix")
        self.assertFalse(out["ok"])
        self.assertIn("Netflix", out["error"])

    async def test_cover_open_state_transition_ok(self):
        async def actuate(entity_id, action):
            self._gate["state"] = "opening"
            return {"ok": True}
        self.ha_client.control_cover.side_effect = actuate
        out = await self.runner.execute(
            "control_entity",
            {"entity_id": "cover.gate", "action": "open",
             "confirmed": True})
        self.assertTrue(out["ok"])
        self.assertEqual(out["verify"]["state"], "opening")

    async def test_cover_open_position_delta_ok(self):
        # A cover that moves without a state transition still counts.
        async def actuate(entity_id, action):
            self._gate["attributes"]["current_position"] = 12
            return {"ok": True}
        self.ha_client.control_cover.side_effect = actuate
        out = await self.runner.execute(
            "control_entity",
            {"entity_id": "cover.gate", "action": "open",
             "confirmed": True})
        self.assertTrue(out["ok"])
        self.assertEqual(out["verify"]["delta"], 12.0)

    async def test_cover_open_swallowed_fails(self):
        out = await self.runner.execute(
            "control_entity",
            {"entity_id": "cover.gate", "action": "open",
             "confirmed": True})
        self.assertFalse(out["ok"])
        self.assertEqual(out["verify"]["state"], "closed")
        self.assertIn("never moved", out["error"])

    async def test_cover_stop_still_moving_fails(self):
        self._gate["state"] = "opening"
        self._gate["attributes"]["current_position"] = 30
        out = await self.runner.execute(
            "control_entity",
            {"entity_id": "cover.gate", "action": "stop",
             "confirmed": True})
        self.assertFalse(out["ok"])
        self.assertIn("still 'opening'", out["error"])

    async def test_unverified_verbs_pass_through(self):
        # State verbs carry no adjustable attr — no verify block.
        out = await self._mp_call("media_play")
        self.assertTrue(out["ok"])
        self.assertNotIn("verify", out)

    async def test_ha_unreachable_never_fails_the_call(self):
        self.ha_client.get_state.side_effect = RuntimeError("ha down")
        out = await self._mp_call("volume_down")
        self.assertTrue(out["ok"])
        self.assertNotIn("verify", out)

    async def test_missing_attr_is_inconclusive(self):
        # A player that does not expose volume_level cannot be verified
        # — inconclusive never flips the call to ok:false.
        self._mp["attributes"] = {}
        out = await self._mp_call("volume_down")
        self.assertTrue(out["ok"])
        self.assertNotIn("verify", out)

class DisplayMergeAliasTests(unittest.IsolatedAsyncioTestCase):
    """tools-merge-display (8 -> 2 canonical seats): vcast_say/vcast_list/
    vcast_status/vcast_shortcut are absorbed into cast_to_screen's action=
    seats (say|list|status|shortcut, plus content-routed 'cast') and stay
    callable via _ALIASES; capture_frame folds into vcast_snapshot, which
    keeps its own name for the verify-after-act loop; vcast_gesture is a
    distinct subsystem and is NOT absorbed."""

    async def asyncSetUp(self):
        self.runner = ToolRunner(AsyncMock(), instance_id="test")
        self.runner._banks = _hermetic_registry()
        self.pubbed = []
        self.display_state = "idle"
        self.display_detail = ""

        def fake_vcast(path, payload=None):
            if path == "/pub":
                self.pubbed.append(payload)
                return {"ok": True, "delivered": 1}
            if path == "/displays":
                return {"screens": [
                    {"screen": 1, "name": "lab", "connected": True,
                     "state": self.display_state,
                     "state_detail": self.display_detail,
                     "panes": 1},
                    {"screen": 2, "name": "ipad", "connected": False,
                     "state": "idle", "state_detail": "", "panes": 1}]}
            if path == "/camwall":
                return {"zones": {}}
            return {"captures": {}}

        patcher = patch.object(
            ToolRunner, "_vcast_api", staticmethod(fake_vcast))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_alias_resolution(self):
        from backend.tool_runner import _resolve_alias
        for legacy, action in (("vcast_say", "say"), ("vcast_list", "list"),
                               ("vcast_status", "status"),
                               ("vcast_shortcut", "shortcut")):
            name, implied = _resolve_alias(legacy)
            self.assertEqual(name, "cast_to_screen", legacy)
            self.assertEqual(implied, {"action": action}, legacy)
        # capture_frame folds into vcast_snapshot — the name that stays
        # declared for the verify-after-act loop.
        name, implied = _resolve_alias("capture_frame")
        self.assertEqual(name, "vcast_snapshot")
        # vcast_gesture is a distinct subsystem — it must NOT be absorbed.
        name, implied = _resolve_alias("vcast_gesture")
        self.assertEqual(name, "vcast_gesture")
        self.assertEqual(implied, {})

    async def test_vcast_list_alias_routes(self):
        out = await self.runner.execute("vcast_list", {})
        self.assertIn("screens", out)
        self.assertEqual(out["screens"][0]["screen"], 1)
        self.assertEqual(out["screens"][1]["online"], False)

    async def test_vcast_say_alias_routes(self):
        out = await self.runner.execute(
            "vcast_say", {"screen": 1, "text": "hello there"})
        self.assertTrue(out["ok"])
        self.assertEqual(self.pubbed[-1]["msg"]["type"], "speak")
        self.assertEqual(self.pubbed[-1]["msg"]["text"], "hello there")

    async def test_vcast_status_alias_routes(self):
        self.display_state = "nav"
        self.display_detail = "p0:nav https://x.test/page"
        out = await self.runner.execute("vcast_status", {"screen": 1})
        self.assertTrue(out["ok"])
        self.assertEqual(out["screen"], 1)
        self.assertEqual(out["state"], "nav")
        out = await self.runner.execute("vcast_status", {"screen": 9})
        self.assertFalse(out["ok"])
        self.assertIn("not a registered display", out["error"])

    async def test_vcast_shortcut_alias_maps_name_to_url(self):
        # The census-only name's arg spellings (name/app/shortcut) funnel
        # into url — an app short name resolves to its /apps/<name>/ page.
        out = await self.runner.execute(
            "vcast_shortcut", {"screen": 1, "name": "gev"})
        self.assertTrue(out["ok"])
        self.assertEqual(self.pubbed[-1]["msg"]["type"], "nav")
        self.assertEqual(self.pubbed[-1]["msg"]["url"], "/apps/gev/")

    async def test_canonical_merged_actions(self):
        out = await self.runner.execute(
            "cast_to_screen", {"action": "list"})
        self.assertIn("screens", out)
        out = await self.runner.execute(
            "cast_to_screen", {"action": "status", "screen": 1})
        self.assertTrue(out["ok"])
        self.assertEqual(out["screen"], 1)
        out = await self.runner.execute(
            "cast_to_screen",
            {"action": "say", "screen": 1, "text": "check one"})
        self.assertEqual(self.pubbed[-1]["msg"]["type"], "speak")
        out = await self.runner.execute(
            "cast_to_screen",
            {"action": "shortcut", "screen": 1, "shortcut": "camwall"})
        self.assertEqual(self.pubbed[-1]["msg"]["url"], "/apps/camwall/")

    async def test_cast_action_routes_by_content(self):
        cases = [({"content_type": "image/png"}, "image"),
                 ({"content_type": "video/mp4"}, "play"),
                 ({"content_type": "audio/mpeg"}, "audio"),
                 ({"content_type": "application/vnd.apple.mpegurl"},
                  "play"),
                 ({"content_type": "text/html"}, "nav")]
        for probe, want in cases:
            with self.subTest(probe=probe), patch.object(
                    ToolRunner, "_frame_check",
                    staticmethod(lambda url, p=probe: p)):
                out = await self.runner.cast_to_screen(
                    1, "cast", url="https://x.test/a")
            self.assertEqual(self.pubbed[-1]["msg"]["type"], want)
            self.assertIn("action_routed", out)

    async def test_cast_action_routes_video_url_spellings(self):
        # YouTube watch pages and .mp4/.m3u8 paths route to 'play' even
        # when the probe reports a generic content type.
        for url in ("https://youtube.com/watch?v=abc",
                    "https://x.test/stream.m3u8"):
            with self.subTest(url=url), patch.object(
                    ToolRunner, "_frame_check",
                    staticmethod(lambda u: {})):
                await self.runner.cast_to_screen(1, "cast", url=url)
            self.assertEqual(self.pubbed[-1]["msg"]["type"], "play")

    async def test_cast_action_requires_url(self):
        with self.assertRaises(ValueError):
            await self.runner.cast_to_screen(1, "cast")

    async def test_cast_action_keeps_interrupt_gate(self):
        # A 'cast' onto a busy screen must confirm like any content cast —
        # the auto-route happens after the busy check.
        self.display_state = "playing"
        self.display_detail = "p0:video-live"
        with patch.object(ToolRunner, "_frame_check",
                          staticmethod(lambda url: {})):
            out = await self.runner.cast_to_screen(
                1, "cast", url="https://x.test/v.mp4")
        self.assertFalse(out["ok"])
        self.assertIn("needs_confirm", out)
        self.assertEqual(self.pubbed, [])

    async def test_absorbed_seats_keep_prior_access(self):
        # vcast_say/vcast_list were never secondary-blocked or
        # control-gated — merged into the CONTROL_TOOLS member
        # cast_to_screen they must keep exactly that access on a
        # secondary (guest) turn, while a real cast stays blocked.
        with patch.object(ToolRunner, "_is_secondary_turn",
                          return_value=True):
            out = await self.runner.execute("vcast_list", {})
            self.assertIn("screens", out)
            out = await self.runner.execute(
                "vcast_say", {"screen": 1, "text": "hi"})
            self.assertTrue(out["ok"])
            with self.assertRaises(PermissionError):
                await self.runner.execute(
                    "cast_to_screen",
                    {"action": "nav", "screen": 1,
                     "url": "https://x.test/"})

    async def test_uplink_still_capture_gated_on_secondary(self):
        # The merged tool's camera-capture seat keeps the
        # CAPTURE_CONFIRMED_TOOLS gate for a guest voice.
        with patch.object(ToolRunner, "_is_secondary_turn",
                          return_value=True):
            # secondary block fires first — cast_to_screen is a control
            # tool and 'uplink' is not an ungated seat.
            with self.assertRaises(PermissionError):
                await self.runner.execute(
                    "cast_to_screen",
                    {"action": "uplink", "screen": 1})

    def test_devin_confirmed_tools_unchanged(self):
        # Card rule: confirm gating keys off the resolved canonical name;
        # the DEVIN_CONFIRMED_TOOLS set itself is untouched.
        from backend.tool_runner import DEVIN_CONFIRMED_TOOLS
        # tools-merge-devin-mcp: the set holds the canonical seat —
        # absorbed names route through _ALIASES before the gate sees them.
        self.assertEqual(DEVIN_CONFIRMED_TOOLS, {"devin"})


class GevTourTests(unittest.IsolatedAsyncioTestCase):
    """gev_command's tour= branch — named flyover tours must be
    discoverable and return an executable card. The tours lived only
    inside scenario yaml files until 2026-10-02, so 'play the South
    Africa tour' came up empty. tools-merge-gev (2026-10-05) folded the
    standalone gev_tour tool into gev_command's tour= param."""

    def setUp(self):
        self.runner = ToolRunner.__new__(ToolRunner)

    async def test_list_all_tours(self):
        out = await self.runner.gev_command(tour="")
        self.assertIn("za", out["tours"])
        self.assertIn("bkk", out["tours"])

    async def test_bare_call_lists_tours(self):
        # No name AND no tour — the discovery mode, same answer the
        # absorbed gev_tour() gave with no args.
        out = await self.runner.gev_command()
        self.assertIn("za", out["tours"])

    async def test_alias_resolution_thai_and_english(self):
        za = await self.runner.gev_command(tour="แอฟริกาใต้")
        self.assertEqual(za["output"]["id"], "za")
        self.assertTrue(za["output"]["stops"])
        ct = await self.runner.gev_command(tour="cape town")
        self.assertEqual(ct["output"]["id"], "capetown")

    async def test_unknown_tour_lists_choices(self):
        out = await self.runner.gev_command(tour="atlantis")
        self.assertIn("error", out)
        self.assertIn("za", out["tours"])


class GevMergeAliasTests(unittest.IsolatedAsyncioTestCase):
    """tools-merge-gev (2 -> 1): the absorbed gev_tour name stays callable
    via _ALIASES and routes to gev_command's tour= branch — the same arg
    name, so no arg-default or shim mapping is involved."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()

    async def test_tour_alias_routes_to_canonical(self):
        out = await self.runner.execute("gev_tour", {"tour": "za"})
        self.assertEqual(out["output"]["id"], "za")

    async def test_tour_alias_lists_when_empty(self):
        out = await self.runner.execute("gev_tour", {})
        self.assertIn("za", out["tours"])

    async def test_canonical_tour_param_thai_alias(self):
        out = await self.runner.execute(
            "gev_command", {"tour": "แอฟริกาใต้"})
        self.assertEqual(out["output"]["id"], "za")

    async def test_canonical_bare_call_lists(self):
        out = await self.runner.execute("gev_command", {})
        self.assertIn("za", out["tours"])


class GevArgWhitelistTests(unittest.IsolatedAsyncioTestCase):
    """gev-command-whitelist: known GEV commands get their args checked
    against the tools.json schema BEFORE the ws relay round-trip — bad
    shapes return the expected schema locally instead of surfacing as a
    cryptic client error (the 0bb8e9b wrap-bug class). Commands with no
    map entry pass through untouched."""

    def setUp(self):
        # Dead relay: a call that passes validation fails fast with the
        # "gev command relay" error; a schema rejection never touches
        # the network at all.
        self.runner = ToolRunner.__new__(ToolRunner)
        self._old_url = os.environ.get("GEV_CMD_URL")
        os.environ["GEV_CMD_URL"] = "http://127.0.0.1:9/"

    def tearDown(self):
        if self._old_url is None:
            os.environ.pop("GEV_CMD_URL", None)
        else:
            os.environ["GEV_CMD_URL"] = self._old_url

    async def test_unknown_arg_rejected_with_schema(self):
        out = await self.runner.gev_command(
            name="fly_to_location",
            args={"query": "Bangkok", "altitude": 5000})
        self.assertFalse(out["ok"])
        self.assertIn("unknown args ['altitude']", out["error"])
        self.assertIn("fly_to_location", out["error"])
        self.assertIn("query", out["error"])  # expected schema echoed
        self.assertIn("locationId", out["error"])

    async def test_missing_required_arg_rejected(self):
        out = await self.runner.gev_command(
            name="set_layer_visibility", args={"layerId": "flights"})
        self.assertFalse(out["ok"])
        self.assertIn("missing required args ['enabled']", out["error"])
        self.assertIn("enabled*", out["error"])

    async def test_no_args_at_all_reports_every_required(self):
        out = await self.runner.gev_command(name="track_entity")
        self.assertFalse(out["ok"])
        self.assertIn("missing required args ['query']", out["error"])

    async def test_non_dict_args_rejected(self):
        out = await self.runner.gev_command(
            name="fly_to_location", args="Bangkok")
        self.assertFalse(out["ok"])
        self.assertIn("args must be an object", out["error"])

    async def test_fly_to_location_needs_a_target(self):
        out = await self.runner.gev_command(
            name="fly_to_location", args={"viewMode": "close"})
        self.assertFalse(out["ok"])
        self.assertIn("needs one of", out["error"])
        self.assertIn("latitude+longitude", out["error"])

    async def test_annotate_map_requires_annotations(self):
        out = await self.runner.gev_command(
            name="annotate_map", args={"persist": True})
        self.assertFalse(out["ok"])
        self.assertIn("missing required args ['annotations']",
                      out["error"])

    async def test_nested_envelope_unwraps_then_validates(self):
        # The 0bb8e9b unwrap stays: a wrapped envelope whose inner args
        # are valid must NOT be schema-rejected — it must reach the
        # relay (which, blackholed here, returns the relay error).
        out = await self.runner.gev_command(
            name="fly_to_location",
            args={"name": "fly_to_location",
                  "args": {"query": "Bangkok"}})
        self.assertFalse(out["ok"])
        self.assertIn("gev command relay", out["error"])

    async def test_unknown_command_passthrough(self):
        # No map entry — never rejected locally, goes straight to relay.
        out = await self.runner.gev_command(
            name="some_future_command", args={"bogus": 1})
        self.assertFalse(out["ok"])
        self.assertIn("gev command relay", out["error"])

    async def test_rejection_surfaces_through_execute(self):
        runner = ToolRunner(AsyncMock(), instance_id="test")
        runner._banks = _hermetic_registry()
        out = await runner.execute(
            "gev_command",
            {"name": "annotate_map", "args": {"flub": 1}})
        self.assertFalse(out["ok"])
        self.assertIn("rejected before relay", out["error"])


class ResultContractTests(unittest.IsolatedAsyncioTestCase):
    """tool-error-contract (2026-10-05): every ToolRunner.execute()
    result is a dict with top-level ok: bool — {ok: True, ...} on
    success, {ok: False, error: str, ...} on failure. The legacy shapes
    (bare values, {error}, {err}, status-based failure, needs_confirm,
    all-failed fan-out responses, method raises) normalize at the
    boundary; dispatch-layer denials still raise — a refusal is not a
    tool result."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()

    def _stub_gev(self, value=None, exc=None):
        """Swap gev_command for a stub that returns/raises `value` — the
        execute() boundary must normalize whatever the method does."""
        async def fake(**_kwargs):
            if exc is not None:
                raise exc
            return value
        self.runner.gev_command = fake

    async def _gev(self):
        return await self.runner.execute("gev_command", {"name": "x"})

    async def test_non_dict_return_wraps_in_output(self):
        self._stub_gev("sent")
        out = await self._gev()
        self.assertEqual(out, {"ok": True, "output": "sent"})

    async def test_error_dict_is_top_level_failure(self):
        self._stub_gev({"error": "relay down"})
        out = await self._gev()
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "relay down")

    async def test_err_dict_surfaces_canonical_error(self):
        self._stub_gev({"err": "bad args"})
        out = await self._gev()
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad args")

    async def test_status_failures_are_top_level_failures(self):
        for status in ("error", "failed", "not_found", "denied"):
            self._stub_gev({"status": status, "slug": "x"})
            out = await self._gev()
            self.assertFalse(out["ok"], status)

    async def test_needs_confirm_is_a_failure(self):
        self._stub_gev({"needs_confirm": "open the gate?"})
        out = await self._gev()
        self.assertFalse(out["ok"])

    async def test_ok_dict_passes_through(self):
        self._stub_gev({"ok": True, "slug": "x"})
        self.assertEqual(await self._gev(), {"ok": True, "slug": "x"})
        self._stub_gev({"ok": False, "error": "nope"})
        out = await self._gev()
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "nope")

    async def test_plain_dict_gains_ok_true(self):
        self._stub_gev({"slug": "x", "count": 3})
        out = await self._gev()
        self.assertTrue(out["ok"])
        self.assertEqual(out["slug"], "x")

    async def test_all_failed_fanout_lifts_to_top_level(self):
        # The gev_command pattern (0bb8e9b), generalized at the boundary:
        # every client response ok:false means the command failed.
        self._stub_gev({"delivered": 2, "responses": [
            {"client": "a", "response": {"ok": False, "error": "nav failed"}},
            {"client": "b", "response": {"ok": False}},
        ]})
        out = await self._gev()
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "nav failed")

    async def test_mixed_fanout_is_not_top_level_failure(self):
        self._stub_gev({"delivered": 2, "responses": [
            {"client": "a", "response": {"ok": True}},
            {"client": "b", "response": {"ok": False, "error": "nav failed"}},
        ]})
        out = await self._gev()
        self.assertTrue(out["ok"])

    async def test_method_exception_is_a_tool_failure(self):
        self._stub_gev(exc=RuntimeError("relay exploded"))
        out = await self._gev()
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "RuntimeError")
        self.assertIn("relay exploded", out["error"])

    async def test_forced_failure_fixture_reports_top_level_ok_false(self):
        # The card's verify step: a real tool failing for real — the
        # unconfigured calendar raises inside the method — surfaces as
        # ok:false at top level, not a raise.
        self.runner._calendar = None
        self.runner._calendar_loaded = True
        out = await self.runner.execute("calendar_list_events", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_type"], "RuntimeError")
        self.assertIn("not configured", out["error"])

    async def test_gate_denial_still_raises_not_a_result(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "cms_publish_page",
                {"slug": "x", "title": "x", "content": "x", **_PAGE_META})

    async def test_unknown_tool_still_raises(self):
        with self.assertRaises(KeyError):
            await self.runner.execute("no_such_tool", {})


class StormBreakerTests(unittest.IsolatedAsyncioTestCase):
    """ada-tool-retry-storm (journal 16:31:03-16:32:18 ICT — a dead
    calendar token produced ~75s of identical calendar_read retries):
    the same tool failing TOOL_BREAKER_TRIP times with the same
    error_class inside the window opens a per-session circuit — further
    calls get a synthesized 'do not retry — it is broken' result instead
    of executing. Covers real tool exceptions (CalendarAuthError,
    ReadTimeout) and gate ACL denials (PermissionError); confirm
    proposals and arg typos never count."""

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.ha_client._states.return_value = []
        self.ha_client.sensors.return_value = []
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner._banks = _hermetic_registry()

    def _stub_gev(self, exc=None, value=None):
        calls = {"n": 0}

        async def fake(**_kwargs):
            calls["n"] += 1
            if exc is not None:
                raise exc() if isinstance(exc, type) else exc
            return value
        self.runner.gev_command = fake
        return calls

    async def _gev(self, **kw):
        return await self.runner.execute("gev_command", {"name": "x"}, **kw)

    async def test_same_class_failure_twice_opens_circuit(self):
        calls = self._stub_gev(exc=RuntimeError("relay down"))
        for _ in range(2):
            out = await self._gev()
            self.assertFalse(out["ok"])
            self.assertEqual(out["error_type"], "RuntimeError")
        out = await self._gev()
        self.assertTrue(out.get("circuit_open"))
        self.assertEqual(out["error_type"], "CircuitOpen")
        self.assertEqual(out["error_class"], "RuntimeError")
        self.assertIn("DO NOT RETRY", out["error"])
        # The tool itself was never invoked a third time.
        self.assertEqual(calls["n"], 2)

    async def test_different_error_classes_do_not_combine(self):
        seq = [TimeoutError("timed out"), RuntimeError("nope"),
               TimeoutError("timed out")]
        calls = {"n": 0}

        async def fake(**_kw):
            calls["n"] += 1
            raise seq[min(calls["n"], 3) - 1]
        self.runner.gev_command = fake
        for _ in range(3):
            out = await self._gev()
            self.assertFalse(out.get("circuit_open"), out)
        self.assertEqual(calls["n"], 3)

    async def test_read_timeout_class_trips(self):
        # ReadTimeout is one of the card's named error classes.
        class ReadTimeout(Exception):
            pass
        calls = self._stub_gev(exc=ReadTimeout("read timed out"))
        await self._gev()
        await self._gev()
        out = await self._gev()
        self.assertTrue(out.get("circuit_open"))
        self.assertEqual(out["error_class"], "ReadTimeout")
        self.assertEqual(calls["n"], 2)

    async def test_calendar_auth_error_class_trips(self):
        from backend.calendar_providers import CalendarAuthError
        calls = self._stub_gev(
            exc=CalendarAuthError("google: token revoked"))
        await self._gev()
        await self._gev()
        out = await self._gev()
        self.assertTrue(out.get("circuit_open"))
        self.assertEqual(out["error_class"], "CalendarAuthError")
        self.assertEqual(calls["n"], 2)

    async def test_outage_is_announced_once_then_quiet(self):
        self._stub_gev(exc=RuntimeError("down"))
        await self._gev()
        await self._gev()
        first = await self._gev()
        self.assertFalse(first["already_announced"])
        self.assertIn("tell the user ONCE", first["error"])
        again = await self._gev()
        self.assertTrue(again["already_announced"])
        self.assertIn("already told the user", again["error"])

    async def test_breaker_scope_is_per_session(self):
        calls = self._stub_gev(exc=RuntimeError("down"))
        await self._gev(session="sess-a")
        await self._gev(session="sess-a")
        tripped = await self._gev(session="sess-a")
        self.assertTrue(tripped.get("circuit_open"))
        # Another session's calls still execute against the same runner.
        self._stub_gev(value={"ok": True, "pong": 1})
        out = await self._gev(session="sess-b")
        self.assertTrue(out["ok"])
        self.assertNotIn("circuit_open", out)

    async def test_ownerless_calls_share_one_bucket(self):
        self._stub_gev(exc=RuntimeError("down"))
        await self._gev()
        await self._gev()
        out = await self._gev()
        self.assertTrue(out.get("circuit_open"))

    async def test_arg_typos_never_trip(self):
        calls = self._stub_gev(exc=ValueError("invalid action 'bogus'"))
        for _ in range(3):
            out = await self._gev()
            self.assertFalse(out.get("circuit_open"))
        self.assertEqual(calls["n"], 3)

    async def test_needs_confirm_results_never_trip(self):
        calls = self._stub_gev(value={"needs_confirm": "do it?"})
        for _ in range(3):
            out = await self._gev()
            self.assertFalse(out.get("circuit_open"))
        self.assertEqual(calls["n"], 3)

    async def test_success_resets_failure_counts(self):
        seq = [RuntimeError("x"), None, RuntimeError("y"), RuntimeError("z")]
        calls = {"n": 0}

        async def fake(**_kw):
            calls["n"] += 1
            exc = seq[calls["n"] - 1]
            if exc:
                raise exc
            return {"ok": True}
        self.runner.gev_command = fake
        for _ in range(4):
            out = await self._gev()
            self.assertFalse(out.get("circuit_open"), out)
        self.assertEqual(calls["n"], 4)

    async def test_half_open_probe_after_cooldown(self):
        calls = self._stub_gev(exc=RuntimeError("down"))
        await self._gev()
        await self._gev()
        out = await self._gev()
        self.assertTrue(out.get("circuit_open"))
        # Age the open record past TOOL_BREAKER_OPEN_S — the next call is
        # a real probe, not another synthesized result.
        for rec in self.runner._storm_open.values():
            rec["until"] = 0
        calls2 = self._stub_gev(value={"ok": True, "back": True})
        out = await self._gev()
        self.assertTrue(out["ok"])
        self.assertEqual(calls2["n"], 1)

    async def test_acl_denial_storm_opens_breaker(self):
        # Secondary-speaker ACL denials raise PermissionError with no
        # confirm token — a terminal denial, counted by the breaker.
        kw = dict(speaker="person.kk", owner="person.tony")
        for _ in range(2):
            with self.assertRaises(PermissionError):
                await self.runner.execute(
                    "ada_forget", {"key": "x", "bank": "general"}, **kw)
        # Third try short-circuits into the synthesized outage result
        # instead of raising again.
        out = await self.runner.execute(
            "ada_forget", {"key": "x", "bank": "general"}, **kw)
        self.assertTrue(out.get("circuit_open"))
        self.assertEqual(out["error_class"], "PermissionError")

    async def test_confirm_proposal_denials_never_trip(self):
        # needs-confirmation denials carry a minted cfm- token — the
        # confirm flow working, not an outage. Unlimited retries allowed.
        for _ in range(3):
            with self.assertRaises(PermissionError):
                await self.runner.execute(
                    "cms_publish_page",
                    {"slug": "x", "title": "x", "content": "x",
                     **_PAGE_META})
        self.assertEqual(self.runner._storm_open, {})

    async def test_calendar_auth_error_shape_trips(self):
        # The journal case: the unconfigured calendar raises inside the
        # method — twice lands the synthesized outage result.
        self.runner._calendar = None
        self.runner._calendar_loaded = True
        await self.runner.execute("calendar_read", {"action": "events"})
        await self.runner.execute("calendar_read", {"action": "events"})
        out = await self.runner.execute(
            "calendar_read", {"action": "events"})
        self.assertTrue(out.get("circuit_open"))
        self.assertEqual(out["error_class"], "RuntimeError")


class NormalizeToolResultTests(unittest.TestCase):
    """Direct unit coverage of the normalizer's shape table."""

    def test_shapes(self):
        from backend.tool_runner import normalize_tool_result as n
        self.assertEqual(n("x"), {"ok": True, "output": "x"})
        self.assertEqual(n(None), {"ok": True, "output": None})
        self.assertEqual(n([1]), {"ok": True, "output": [1]})
        self.assertFalse(n({"error": "e"})["ok"])
        self.assertFalse(n({"err": "e"})["ok"])
        self.assertEqual(n({"err": "e"})["error"], "e")
        self.assertFalse(n({"status": "not_found"})["ok"])
        self.assertFalse(n({"needs_confirm": "ok?"})["ok"])
        self.assertTrue(n({"status": "published"})["ok"])
        self.assertTrue(n({"a": 1})["ok"])
        # explicit ok wins over everything else in the payload
        self.assertTrue(n({"ok": True, "error": "warn"})["ok"])
        # all-failed fan-out lifts; partial failure does not
        all_bad = {"responses": [{"response": {"ok": False, "error": "x"}},
                                 {"response": {"ok": False}}]}
        self.assertFalse(n(all_bad)["ok"])
        partial = {"responses": [{"response": {"ok": True}},
                                 {"response": {"ok": False}}]}
        self.assertTrue(n(partial)["ok"])
        self.assertTrue(n({"responses": []})["ok"])


if __name__ == "__main__":
    unittest.main()
