import unittest
from unittest.mock import AsyncMock

from backend.tool_runner import AdaMemoryStore


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
        store = AdaMemoryStore(self.ha_client, mddb_client=self.mddb_client)
        await store.refresh()

        groups = store.confidence_groups()
        by_id = {dev["entity_id"]: dev for group in groups.values() for dev in group}
        self.assertEqual(by_id["cover.gate"]["safety"], "dangerous")
        self.assertEqual(by_id["light.office"]["safety"], "caution")

    async def test_set_confidence_and_safety_persists_both_to_mddb(self):
        self._controllable(
            {"entity_id": "cover.gate", "state": "closed", "available": True, "name": "Gate"},
        )
        store = AdaMemoryStore(self.ha_client, mddb_client=self.mddb_client)
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
        store = AdaMemoryStore(self.ha_client, mddb_client=self.mddb_client)
        await store.refresh()

        # Default safety for cover is dangerous
        self.assertEqual(store._safety.get("cover.gate"), "dangerous")

        await store.set_confidence("cover.gate", "trusted_working")

        self.assertEqual(store._confidence.get("cover.gate"), "trusted_working")
        self.assertEqual(store._safety.get("cover.gate"), "dangerous")


if __name__ == "__main__":
    unittest.main()
