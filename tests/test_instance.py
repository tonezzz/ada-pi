import os
import unittest
from unittest.mock import AsyncMock, patch

from backend.event_recorder import HaEventRecorder
from backend.instance import ada_instance_id
from backend.tool_runner import AdaMemoryStore


class InstanceIdTests(unittest.TestCase):
    def test_missing_env_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                ada_instance_id()

    def test_invalid_env_raises(self):
        for bad in ["Tony", "tony ha", "../x", ""]:
            with patch.dict(os.environ, {"ADA_INSTANCE_ID": bad}, clear=True):
                with self.assertRaises(RuntimeError, msg=bad):
                    ada_instance_id()

    def test_valid_env_stripped(self):
        with patch.dict(os.environ, {"ADA_INSTANCE_ID": "  tony-2 "}, clear=True):
            self.assertEqual(ada_instance_id(), "tony-2")

    def test_collections_use_instance_id(self):
        ha_client = AsyncMock()
        ha_client.base_url = "http://test:8123"
        store = AdaMemoryStore(ha_client, instance_id="michael")
        self.assertEqual(store._collection, "ada-ha-snapshots-michael")
        self.assertEqual(store._confidence_collection, "ada-ha-device-confidence-michael")
        self.assertEqual(store._safety_collection, "ada-ha-device-safety-michael")
        recorder = HaEventRecorder(ha_client, instance_id="michael")
        self.assertEqual(recorder.collection, "ada-ha-events-michael")

    def test_constructors_fail_fast_without_env(self):
        ha_client = AsyncMock()
        ha_client.base_url = "http://test:8123"
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                AdaMemoryStore(ha_client)
            with self.assertRaises(RuntimeError):
                HaEventRecorder(ha_client)


if __name__ == "__main__":
    unittest.main()
