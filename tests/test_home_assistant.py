import os
import unittest
from unittest.mock import AsyncMock

from backend.home_assistant import HomeAssistantClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class HomeAssistantClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_token = os.environ.get("HOME_ASSISTANT_TOKEN")
        os.environ["HOME_ASSISTANT_TOKEN"] = "test-token"

    async def asyncTearDown(self):
        if self.previous_token is None:
            os.environ.pop("HOME_ASSISTANT_TOKEN", None)
        else:
            os.environ["HOME_ASSISTANT_TOKEN"] = self.previous_token

    async def test_entities_exposes_only_simple_power_domains(self):
        client = AsyncMock()
        client.get.return_value = FakeResponse([
            {"entity_id": "light.office", "state": "on", "attributes": {"friendly_name": "Office"}},
            {"entity_id": "switch.monitor", "state": "unavailable", "attributes": {}},
            {"entity_id": "sensor.temperature", "state": "72", "attributes": {}},
        ])
        home = HomeAssistantClient(client=client)
        entities = await home.entities()
        self.assertEqual([item["entity_id"] for item in entities], ["light.office", "switch.monitor"])
        self.assertFalse(entities[1]["available"])

    async def test_entities_deduplicates_same_object_across_domains(self):
        client = AsyncMock()
        client.get.return_value = FakeResponse([
            {"entity_id": "switch.office_lamp", "state": "on", "attributes": {"friendly_name": "Office lamp"}},
            {"entity_id": "light.office_lamp", "state": "on", "attributes": {"friendly_name": "Office lamp"}},
            {"entity_id": "switch.office_lamp_usb", "state": "off", "attributes": {"friendly_name": "Office lamp USB"}},
        ])
        home = HomeAssistantClient(client=client)
        entities = await home.entities()
        self.assertEqual(
            [item["entity_id"] for item in entities],
            ["light.office_lamp", "switch.office_lamp_usb"],
        )

    async def test_power_calls_allow_listed_home_assistant_service(self):
        client = AsyncMock()
        client.post.return_value = FakeResponse([])
        home = HomeAssistantClient(client=client)
        result = await home.set_power("light.office", True)
        client.post.assert_awaited_once_with("/api/services/light/turn_on", json={"entity_id": "light.office"})
        self.assertEqual(result["state"], "on")

    async def test_power_rejects_unsupported_domain(self):
        home = HomeAssistantClient(client=AsyncMock())
        with self.assertRaises(ValueError):
            await home.set_power("lock.front_door", True)

    async def test_logbook_hits_logbook_endpoint_with_entity_filter(self):
        client = AsyncMock()
        client.get.return_value = FakeResponse([{"name": "Front door", "message": "was opened"}])
        home = HomeAssistantClient(client=client)
        entries = await home.logbook(entity_id="binary_sensor.front_door", hours=2)
        self.assertEqual(len(entries), 1)
        url = client.get.await_args.args[0]
        self.assertIn("/api/logbook/period/", url)
        self.assertIn("entity=binary_sensor.front_door", url)

    async def test_state_transitions_collapses_and_computes_durations(self):
        def get(url):
            if url == "/api/states":
                return FakeResponse([
                    {"entity_id": "binary_sensor.front_door", "state": "off",
                     "attributes": {"friendly_name": "Front door", "device_class": "door"}},
                ])
            return FakeResponse([[
                {"entity_id": "binary_sensor.front_door", "state": "off",
                 "last_changed": "2026-09-16T08:00:00+00:00"},
                {"state": "on", "last_changed": "2026-09-16T09:00:00+00:00"},
                {"state": "on", "last_changed": "2026-09-16T09:00:05+00:00"},
                {"state": "off", "last_changed": "2026-09-16T09:30:00+00:00"},
            ]])

        client = AsyncMock()
        client.get.side_effect = get
        home = HomeAssistantClient(client=client)
        result = await home.state_transitions("binary_sensor.front_door", hours=24)

        entity = result["entities"][0]
        self.assertEqual(entity["name"], "Front door")
        self.assertEqual(entity["device_class"], "door")
        self.assertEqual(entity["changes"], 3)
        # The duplicate "on" row is collapsed; the open period lasted 30 min.
        states = [t["state"] for t in entity["transitions"]]
        self.assertEqual(states, ["off", "on", "off"])
        self.assertEqual(entity["transitions"][1]["duration_seconds"], 1800)

    async def test_recent_events_filters_domains_and_query(self):
        def get(url):
            if url == "/api/states":
                return FakeResponse([
                    {"entity_id": "binary_sensor.front_door", "state": "off",
                     "attributes": {"friendly_name": "Front door", "device_class": "door"}},
                    {"entity_id": "sensor.outdoor_temp", "state": "30",
                     "attributes": {"friendly_name": "Outdoor"}},
                    {"entity_id": "cover.gate_motor", "state": "closed",
                     "attributes": {"friendly_name": "Gate"}},
                ])
            return FakeResponse([
                [{"entity_id": "binary_sensor.front_door", "state": "off",
                  "last_changed": "2026-09-16T08:00:00+00:00"},
                 {"state": "on", "last_changed": "2026-09-16T09:00:00+00:00"}],
                [{"entity_id": "cover.gate_motor", "state": "closed",
                  "last_changed": "2026-09-16T08:30:00+00:00"}],
            ])

        client = AsyncMock()
        client.get.side_effect = get
        home = HomeAssistantClient(client=client)

        result = await home.recent_events(hours=24)
        history_url = [c.args[0] for c in client.get.await_args_list if "history" in c.args[0]][0]
        self.assertIn("binary_sensor.front_door", history_url)
        self.assertIn("cover.gate_motor", history_url)
        self.assertNotIn("sensor.outdoor_temp", history_url)
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["events"][0]["state"], "on")  # newest first
        self.assertEqual(result["events"][0]["name"], "Front door")

        result = await home.recent_events(hours=24, query="gate")
        self.assertEqual(result["entities_scanned"], 1)


if __name__ == "__main__":
    unittest.main()
