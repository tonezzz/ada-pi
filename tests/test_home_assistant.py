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

    async def test_entities_keeps_actuator_sharing_object_id_with_switch(self):
        # Tuya curtain/gate motors expose cover.gate_motor beside a
        # switch.gate_motor sibling. The object_id dedup must only collapse
        # simple on/off loads — an actuator domain is never a duplicate, so
        # the cover stays findable and controllable.
        client = AsyncMock()
        client.get.return_value = FakeResponse([
            {"entity_id": "switch.gate_motor", "state": "unavailable",
             "attributes": {}},
            {"entity_id": "cover.gate_motor", "state": "unknown",
             "attributes": {"friendly_name": "Gate motor"}},
        ])
        home = HomeAssistantClient(client=client)
        entities = await home.entities()
        self.assertEqual(
            sorted(item["entity_id"] for item in entities),
            ["cover.gate_motor", "switch.gate_motor"],
        )

    async def test_search_entities_finds_unknown_and_unavailable_devices(self):
        # cover.gate_motor rests at 'unknown' permanently (no position
        # feedback); a flapping Tuya device reads 'unavailable' in bursts.
        # Neither may be filtered out — they return flagged available:False.
        client = AsyncMock()
        client.get.return_value = FakeResponse([
            {"entity_id": "cover.gate_motor", "state": "unknown",
             "attributes": {"friendly_name": "Gate motor"}},
            {"entity_id": "switch.gate_light", "state": "unavailable",
             "attributes": {"friendly_name": "Gate light"}},
            {"entity_id": "light.office", "state": "on",
             "attributes": {"friendly_name": "Office"}},
        ])
        home = HomeAssistantClient(client=client)
        results = await home.search_entities("gate")
        by_id = {item["entity_id"]: item for item in results}
        self.assertIn("cover.gate_motor", by_id)
        self.assertIn("switch.gate_light", by_id)
        self.assertFalse(by_id["cover.gate_motor"]["available"])
        self.assertFalse(by_id["switch.gate_light"]["available"])
        self.assertEqual(by_id["cover.gate_motor"]["state"], "unknown")
        self.assertEqual(by_id["switch.gate_light"]["state"], "unavailable")

    async def test_search_entities_ranks_unavailable_below_available(self):
        # Unavailable entities stay mentionable but rank behind an
        # available entity at the same match score.
        client = AsyncMock()
        client.get.return_value = FakeResponse([
            {"entity_id": "light.gate_flap", "state": "unavailable",
             "attributes": {"friendly_name": "Gate flap"}},
            {"entity_id": "light.gate_spot", "state": "on",
             "attributes": {"friendly_name": "Gate spot"}},
        ])
        home = HomeAssistantClient(client=client)
        results = await home.search_entities("gate")
        self.assertEqual(
            [item["entity_id"] for item in results],
            ["light.gate_spot", "light.gate_flap"],
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
        self.assertIn("/api/logbook?", url)
        self.assertIn("entity=binary_sensor.front_door", url)
        self.assertIn("period=", url)
        self.assertIn("end_time=", url)

    async def test_logbook_falls_back_to_period_endpoint_on_404(self):
        client = AsyncMock()
        client.get.side_effect = [
            FakeResponse([], status_code=404),
            FakeResponse([{"name": "Front door", "message": "was opened"}]),
        ]
        home = HomeAssistantClient(client=client)
        entries = await home.logbook(entity_id="binary_sensor.front_door", hours=2)
        self.assertEqual(len(entries), 1)
        url = client.get.await_args_list[1].args[0]
        self.assertIn("/api/logbook/period/", url)
        self.assertIn("entity=binary_sensor.front_door", url)

    async def test_persons_lists_person_entities(self):
        client = AsyncMock()
        client.get.return_value = FakeResponse([
            {"entity_id": "person.tony", "state": "home",
             "attributes": {"friendly_name": "Tony"}},
            {"entity_id": "person.kk", "state": "not_home",
             "attributes": {"friendly_name": "KK"}},
            {"entity_id": "light.office", "state": "on",
             "attributes": {"friendly_name": "Office"}},
        ])
        home = HomeAssistantClient(client=client)
        people = await home.persons()
        self.assertEqual(
            [p["entity_id"] for p in people],
            ["person.kk", "person.tony"],  # sorted by name
        )
        self.assertEqual(people[0]["name"], "KK")

    async def test_resolve_person_by_name_and_entity(self):
        client = AsyncMock()
        client.get.return_value = FakeResponse([
            {"entity_id": "person.tony", "state": "home",
             "attributes": {"friendly_name": "Tony"}},
            {"entity_id": "person.kk", "state": "home",
             "attributes": {"friendly_name": "KK"}},
        ])
        home = HomeAssistantClient(client=client)
        self.assertEqual(
            (await home.resolve_person("KK"))["entity_id"], "person.kk"
        )
        self.assertEqual(
            (await home.resolve_person("person.tony"))["entity_id"], "person.tony"
        )
        # Slug fallback: 'Some One' -> person.some_one
        client.get.return_value = FakeResponse([
            {"entity_id": "person.some_one", "state": "home",
             "attributes": {"friendly_name": "Friend"}},
        ])
        home = HomeAssistantClient(client=client)
        self.assertEqual(
            (await home.resolve_person("Some One"))["entity_id"], "person.some_one"
        )
        self.assertIsNone(await home.resolve_person("nobody"))

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
