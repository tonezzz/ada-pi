"""Unit tests for backend/tools.d/eye.py (ada_look).

runner.mddb is faked — no network. Covers the live / stale / idle
honesty contract (card ada-look-tool) and manifest wiring.
"""

from __future__ import annotations

import importlib.util
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from backend import tools_loader

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "eye.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "tools_d.eye_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


eye = _load_module()


def _md(payload: dict, raw_block: bool = True) -> dict:
    body = json.dumps(payload)
    text = f"# eye page\n```json\n{body}\n```\n" if raw_block else body
    return {"key": "eye/latest", "contentMd": text, "meta": {}}


class FakeMddb:
    def __init__(self, doc=None):
        self.doc = doc

    async def get_document(self, collection, key, lang="en"):
        assert collection == "ada-ha-scenario-reports"
        assert key == "eye/latest"
        return self.doc


def _runner(doc=None, mddb_missing=False):
    r = MagicMock()
    r.mddb = None if mddb_missing else FakeMddb(doc)
    return r


FRESH = {"ts": time.time(), "pub_s": 5, "src": "cam:c100",
         "model": "yolov8n-int8", "ua": "Mozilla/5.0 (iPhone)",
         "detections": [
             {"cls": "person", "score": 0.9, "box": [0, 0, 1, 1]},
             {"cls": "person", "score": 0.8, "box": [2, 0, 1, 1]},
             {"cls": "dog", "score": 0.7, "box": [4, 0, 1, 1]},
         ]}


class IdleTest(unittest.IsolatedAsyncioTestCase):

    async def test_no_doc_is_honest_empty(self):
        out = await eye.run(_runner(None))
        self.assertTrue(out["ok"])
        self.assertFalse(out["live"])
        self.assertEqual(out["count"], 0)
        self.assertIn("no eye page is publishing",
                      out["summary"].lower())

    async def test_doc_without_json_block_is_honest_empty(self):
        out = await eye.run(_runner({"key": "eye/latest",
                                     "contentMd": "hello world",
                                     "meta": {}}))
        self.assertTrue(out["ok"])
        self.assertFalse(out["live"])

    async def test_mddb_unavailable_is_error(self):
        out = await eye.run(_runner(mddb_missing=True))
        self.assertFalse(out["ok"])

    async def test_malformed_json_is_error(self):
        doc = {"key": "eye/latest", "meta": {},
               "contentMd": "```json\n{not json\n```"}
        out = await eye.run(_runner(doc))
        self.assertFalse(out["ok"])
        self.assertIn("couldn't read", out["error"])


class LiveTest(unittest.IsolatedAsyncioTestCase):

    async def test_fresh_doc_counts_classes(self):
        out = await eye.run(_runner(_md(FRESH)))
        self.assertTrue(out["ok"])
        self.assertTrue(out["live"])
        self.assertFalse(out["stale"])
        self.assertEqual(out["objects"], {"person": 2, "dog": 1})
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["src"], "cam:c100")
        self.assertEqual(out["model"], "yolov8n-int8")
        self.assertIn("2 people", out["summary"])
        self.assertIn("1 dog", out["summary"])

    async def test_bare_json_doc_accepted(self):
        out = await eye.run(_runner(_md(FRESH, raw_block=False)))
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 3)

    async def test_iso_timestamp_accepted(self):
        doc = dict(FRESH)
        doc["ts"] = datetime.now(timezone.utc).isoformat()
        out = await eye.run(_runner(_md(doc)))
        self.assertTrue(out["ok"])
        self.assertFalse(out["stale"])

    async def test_empty_detections_is_live_but_empty(self):
        doc = dict(FRESH, detections=[])
        out = await eye.run(_runner(_md(doc)))
        self.assertTrue(out["ok"])
        self.assertFalse(out["stale"])
        self.assertEqual(out["count"], 0)
        self.assertIn("nothing", out["summary"].lower())

    async def test_pub_alias_accepted(self):
        doc = dict(FRESH, pub=5)
        del doc["pub_s"]
        out = await eye.run(_runner(_md(doc)))
        self.assertTrue(out["ok"])
        self.assertFalse(out["stale"])
        self.assertEqual(out["pub_s"], 5.0)


class StaleTest(unittest.IsolatedAsyncioTestCase):

    async def test_old_doc_flagged_stale_with_age(self):
        doc = dict(FRESH, ts=time.time() - 600, pub_s=5)
        out = await eye.run(_runner(_md(doc)))
        self.assertTrue(out["ok"])
        self.assertTrue(out["stale"])
        self.assertGreaterEqual(out["age_s"], 599)
        self.assertIn("minutes ago", out["summary"])
        self.assertIn("person", out["objects"])

    async def test_within_3x_pub_is_fresh(self):
        doc = dict(FRESH, ts=time.time() - 14, pub_s=5)
        out = await eye.run(_runner(_md(doc)))
        self.assertTrue(out["ok"])
        self.assertFalse(out["stale"])

    async def test_missing_ts_reports_unknown_freshness(self):
        doc = dict(FRESH)
        del doc["ts"]
        out = await eye.run(_runner(_md(doc)))
        self.assertTrue(out["ok"])
        self.assertEqual(out["freshness"], "unknown")
        self.assertNotIn("stale", out)


class ManifestTest(unittest.TestCase):

    def test_manifest_registers_tool(self):
        reg = tools_loader.load()
        self.assertIn("ada_look", reg.tools,
                      msg=f"load errors: {reg.errors}")
        spec = reg.tools["ada_look"]
        self.assertEqual(spec.module, "eye")
        self.assertEqual(spec.policy, "read")
        self.assertEqual(spec.declaration["name"], "ada_look")
        self.assertEqual(
            spec.declaration["parameters"],
            {"type": "object", "properties": {}})


if __name__ == "__main__":
    unittest.main()
