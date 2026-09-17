import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from backend.event_recorder import HaEventRecorder


def _changed(entity_id, new_state, old_state, friendly_name=None, device_class=None, at=None):
    if at is None:
        at = datetime.now(timezone.utc).isoformat()
    return {
        "time_fired": at,
        "data": {
            "new_state": {
                "entity_id": entity_id,
                "state": new_state,
                "attributes": {
                    "friendly_name": friendly_name or entity_id,
                    "device_class": device_class,
                },
            },
            "old_state": {"entity_id": entity_id, "state": old_state},
        },
    }


class HaEventRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.mddb_client = AsyncMock()
        self.mddb_client.add_document.return_value = {}
        self.recorder = HaEventRecorder(self.ha_client, mddb_client=self.mddb_client, instance_id="test")

    async def test_record_keeps_real_state_changes(self):
        entry = self.recorder.record(_changed(
            "binary_sensor.front_door", "on", "off",
            friendly_name="Front door", device_class="door",
        ))
        self.assertIsNotNone(entry)
        self.assertEqual(entry["from"], "off")
        self.assertEqual(entry["to"], "on")
        self.assertEqual(entry["name"], "Front door")

    async def test_record_skips_attribute_only_changes(self):
        self.assertIsNone(self.recorder.record(_changed("light.office", "on", "on")))

    async def test_record_skips_unwatched_domains(self):
        self.assertIsNone(self.recorder.record(_changed("sensor.outdoor_temp", "31", "30")))

    async def test_recent_filters_by_query(self):
        self.recorder.record(_changed("binary_sensor.front_door", "on", "off", friendly_name="Front door"))
        self.recorder.record(_changed("cover.gate_motor", "open", "closed", friendly_name="Gate"))
        self.assertEqual(len(self.recorder.recent(query="gate")), 1)
        self.assertEqual(len(self.recorder.recent()), 2)

    async def test_recent_excludes_events_older_than_window(self):
        self.recorder.record(_changed(
            "binary_sensor.front_door", "on", "off", at="2020-01-01T00:00:00+00:00",
        ))
        self.assertEqual(self.recorder.recent(hours=1), [])

    async def test_flush_persists_batch_to_mddb(self):
        self.recorder.record(_changed("binary_sensor.front_door", "on", "off", friendly_name="Front door"))
        await self.recorder._flush()
        kwargs = self.mddb_client.add_document.await_args.kwargs
        self.assertEqual(kwargs["collection"], self.recorder.collection)
        self.assertIn("Front door", kwargs["content_md"])
        self.assertIn("off → on", kwargs["content_md"])
        self.assertEqual(kwargs["meta"]["kind"], ["events"])
        self.assertEqual(kwargs["meta"]["count"], ["1"])

    async def test_stop_flushes_pending_events(self):
        self.recorder.record(_changed("cover.gate_motor", "open", "closed"))
        await self.recorder.stop()
        self.mddb_client.add_document.assert_awaited_once()
        self.assertEqual(self.recorder._pending, [])


if __name__ == "__main__":
    unittest.main()
