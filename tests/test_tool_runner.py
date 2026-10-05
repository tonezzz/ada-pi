import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch, ANY

from backend.memory_banks import MemoryBankRegistry
from backend.tool_runner import AdaMemoryStore, ToolRunner


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
        with self.assertRaises(ValueError):
            await self.runner.execute(
                "ada_memory_search", {"query": "x", "scope": "bogus"})


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
                {"slug": "pool-notes", "title": "Pool", "content": "# hi"},
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
                 "content": "# hi", "confirmed": True},
            )
        self.runner.mddb.add_document.assert_not_awaited()

    async def test_publish_with_confirmed_writes_page_doc(self):
        # Two-step handshake: first call registers pending, the confirmed
        # resubmit publishes.
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "cms_publish_page",
                {"slug": "Pool Notes", "title": "Pool notes",
                 "content": "# Pool\npH 7.4"},
            )
        result = await self.runner.execute(
            "cms_publish_page",
            {
                "slug": "Pool Notes",
                "title": "Pool notes",
                "content": "# Pool\npH 7.4",
                "confirmed": True,
            },
        )
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["slug"], "pool-notes")
        args, kwargs = _page_call(self.runner, "pool-notes")
        self.assertEqual(args[:3], ("ada-cms-pages", "pool-notes", "en"))
        meta = kwargs["meta"]
        self.assertEqual(meta["kind"], ["page"])
        self.assertEqual(meta["slug"], ["pool-notes"])
        self.assertEqual(meta["format"], ["markdown"])

    async def test_publish_rejects_bad_slug_and_format(self):
        # Register pending first so the calls reach validation.
        for bad in ({"slug": "../evil", "title": "x", "content": "x"},
                    {"slug": "ok", "title": "x", "content": "x", "format": "exe"}):
            with self.assertRaises(PermissionError):
                await self.runner.execute("cms_publish_page", bad)
        with self.assertRaises(ValueError):
            await self.runner.execute(
                "cms_publish_page",
                {"slug": "../evil", "title": "x", "content": "x", "confirmed": True},
            )
        with self.assertRaises(ValueError):
            await self.runner.execute(
                "cms_publish_page",
                {"slug": "ok", "title": "x", "content": "x", "format": "exe", "confirmed": True},
            )

    async def test_list_and_get_are_not_gated(self):
        self.runner.mddb.search_documents.return_value = [
            {"key": "pool-notes", "meta": {"slug": ["pool-notes"], "title": ["Pool"], "format": ["markdown"], "updated": ["2026-09-22T10:00:00+00:00"]}},
        ]
        pages = await self.runner.execute("cms_list_pages", {})
        self.assertEqual(pages[0]["slug"], "pool-notes")
        self.assertEqual(pages[0]["title"], "Pool")

        self.runner.mddb.get_document.return_value = {
            "key": "pool-notes",
            "contentMd": "# Pool",
            "meta": {"slug": ["pool-notes"], "title": ["Pool"], "format": ["markdown"]},
        }
        page = await self.runner.execute("cms_get_page", {"slug": "pool-notes"})
        self.assertEqual(page["content"], "# Pool")

    async def test_delete_requires_confirmed(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute("cms_delete_page", {"slug": "pool-notes"})
        result = await self.runner.execute(
            "cms_delete_page", {"slug": "pool-notes", "confirmed": True}
        )
        self.assertEqual(result["status"], "deleted")
        self.runner.mddb.delete_document.assert_awaited_once_with("ada-cms-pages", "pool-notes")

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
                {"slug": "flood-report", "title": "Flood", "content": "# Flood\nnew"},
            )
        await self.runner.execute(
            "cms_publish_page",
            {"slug": "flood-report", "title": "Flood",
             "content": "# Flood\nnew", "confirmed": True},
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
            with self.assertRaises(ValueError):
                await self.runner.execute("cms_automation", bad)

    async def test_bad_action_and_empty_set(self):
        with self.assertRaises(ValueError):
            await self.runner.execute(
                "cms_automation",
                {"action": "bogus", "slug": "flood-report", "confirmed": True})
        with self.assertRaises(ValueError):
            await self.runner.execute(
                "cms_automation",
                {"action": "set", "slug": "flood-report", "confirmed": True})


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
        self.runner.mddb.search_documents.return_value = []
        self.runner.mddb.add_document.return_value = {"status": "ok"}
        self.runner.mddb.get_document.return_value = None
        self.runner.mddb.delete_document.return_value = {"status": "deleted"}
        # write paths schedule a reports-index regen — stub it out
        self.runner._cms_reports_index = AsyncMock()

    async def test_list_pages_alias_routes_to_read_list(self):
        pages = await self.runner.execute("cms_list_pages", {})
        self.assertEqual(pages, [])
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
        with self.assertRaises(ValueError):
            await self.runner.execute("cms_read", {"action": "bogus"})
        # cms_edit is a write-tool seat — the confirm gate runs before
        # action validation, same as cms_automation did pre-merge.
        with self.assertRaises(ValueError):
            await self.runner.execute(
                "cms_edit", {"action": "bogus", "confirmed": True})

    async def test_canonical_read_and_edit_dispatch(self):
        pages = await self.runner.execute("cms_read", {"action": "list"})
        self.assertEqual(pages, [])
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
        with self.assertRaises(ValueError):
            await self.runner.execute("calendar_read", {"action": "bogus"})

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
        self.assertIn("deleted", out)
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
        with self.assertRaises(ValueError):
            await self.runner.execute(
                "calendar_write", {"action": "bogus", "confirmed": True})

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
            with self.assertRaises(PermissionError):
                await self.runner.execute(
                    "devin_job_report", {"publish": True})
            self.runner.mddb.add_document.assert_not_awaited()
            result = await self.runner.execute(
                "devin_job_report", {"publish": True, "confirmed": True})
        self.assertEqual(result["status"], "published")
        args, kwargs = _page_call(self.runner, "devin-job-report")
        self.assertEqual(args[:3], ("ada-cms-pages", "devin-job-report", "en"))
        self.assertIn("## Failed jobs", args[3])


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


if __name__ == "__main__":
    unittest.main()
