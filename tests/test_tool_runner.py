import os
import unittest
from unittest.mock import AsyncMock

from backend.tool_runner import AdaMemoryStore, ToolRunner


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


if __name__ == "__main__":
    unittest.main()
