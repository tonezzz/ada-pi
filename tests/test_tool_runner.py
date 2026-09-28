import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.memory_banks import MemoryBankRegistry
from backend.tool_runner import AdaMemoryStore, ToolRunner


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
        self.assertEqual(result, {"ok": True})
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

    async def test_publish_with_confirmed_writes_page_doc(self):
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
        args, kwargs = self.runner.mddb.add_document.call_args
        self.assertEqual(args[:3], ("ada-cms-pages", "pool-notes", "en"))
        meta = kwargs["meta"]
        self.assertEqual(meta["kind"], ["page"])
        self.assertEqual(meta["slug"], ["pool-notes"])
        self.assertEqual(meta["format"], ["markdown"])

    async def test_publish_rejects_bad_slug_and_format(self):
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
        self.assertEqual(result, {"ok": True})

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
        self.assertEqual(result, {"ok": True})


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
        args, kwargs = self.runner.mddb.add_document.call_args
        self.assertEqual(args[:3], ("ada-cms-pages", "devin-job-report", "en"))
        self.assertIn("## Failed jobs", args[3])


if __name__ == "__main__":
    unittest.main()
