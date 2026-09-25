"""ada_doc_* tools — confirmed gate, dispatch, and client helpers."""
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ADA_MEMORY_BANKS_FILE", "/nonexistent-banks.json")

from backend.tool_runner import ToolRunner, DOC_CONFIRMED_TOOLS  # noqa: E402
from backend import doc_archive_client  # noqa: E402


class DocConfirmedGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runner = ToolRunner(AsyncMock(), instance_id="test")
        # Default: documents bank allowed for this identity (Tony's keys are
        # {full: true} in person_policies). Individual tests override.
        self._allow = unittest.mock.patch.object(
            self.runner.banks, "bank_allowed", return_value=True)
        self._allow.start()
        self.addCleanup(self._allow.stop)

    async def test_all_doc_tools_denied_when_bank_not_allowed(self):
        self._allow.stop()
        with unittest.mock.patch.object(
                self.runner.banks, "bank_allowed", return_value=False):
            for tool in ("ada_doc_search", "ada_doc_get",
                         "ada_doc_archive", "ada_doc_print"):
                with self.assertRaises(PermissionError, msg=tool):
                    await self.runner.execute(
                        tool, {"query": "x", "slug": "x",
                               "source_dir": "/tmp", "confirmed": True})
        self._allow.start()

    async def test_archive_denied_without_confirmed(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "ada_doc_archive", {"slug": "x", "source_dir": "/tmp"})

    async def test_print_denied_without_confirmed(self):
        with self.assertRaises(PermissionError):
            await self.runner.execute(
                "ada_doc_print", {"slug": "A-68", "pages": "1"})

    async def test_search_and_get_are_read_only(self):
        self.assertNotIn("ada_doc_search", DOC_CONFIRMED_TOOLS)
        self.assertNotIn("ada_doc_get", DOC_CONFIRMED_TOOLS)

    async def test_doc_log_records_search_and_archive(self):
        self.runner.doc_log = []
        with patch.object(doc_archive_client, "doc_search",
                          new=AsyncMock(return_value=[{"slug": "A-68"}])):
            await self.runner.execute("ada_doc_search", {"query": "deed"})
        with patch.object(doc_archive_client, "doc_archive",
                          new=AsyncMock(return_value={"archive_id": "x"})), \
             patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=["p1.jpg"]), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"\xff\xd8jpeg")):
            await self.runner.execute(
                "ada_doc_archive",
                {"slug": "x", "source_dir": "/tmp/d", "confirmed": True})
        self.assertEqual([i["action"] for i in self.runner.doc_log],
                         ["search", "archive"])
        self.assertEqual(self.runner.doc_log[0]["found"], ["A-68"])
        self.assertEqual(self.runner.doc_log[1]["pages"], 1)

    async def test_archive_passes_confirmed(self):
        with patch.object(doc_archive_client, "doc_archive",
                          new=AsyncMock(return_value={"archive_id": "x"})), \
             patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=["p1.jpg"]), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"\xff\xd8jpeg")):
            out = await self.runner.execute(
                "ada_doc_archive",
                {"slug": "x", "source_dir": "/tmp/d", "confirmed": True})
        self.assertEqual(out["archive_id"], "x")

    async def test_archive_needs_a_source(self):
        with self.assertRaises(RuntimeError):
            await self.runner.execute(
                "ada_doc_archive", {"slug": "x", "confirmed": True})

    async def test_bad_true_size_mm(self):
        with self.assertRaises(RuntimeError):
            await self.runner.execute(
                "ada_doc_print",
                {"slug": "A-68", "true_size_mm": "abc", "confirmed": True})


class PageSpecTest(unittest.TestCase):
    def test_all(self):
        self.assertEqual(doc_archive_client._pagespec(5, None), [1, 2, 3, 4, 5])
        self.assertEqual(doc_archive_client._pagespec(5, "all"), [1, 2, 3, 4, 5])

    def test_range_and_list(self):
        self.assertEqual(doc_archive_client._pagespec(5, "1-3"), [1, 2, 3])
        self.assertEqual(doc_archive_client._pagespec(5, "2,4"), [2, 4])
        self.assertEqual(doc_archive_client._pagespec(3, "1-9"), [1, 2, 3])


class DocSearchShapeTest(unittest.IsolatedAsyncioTestCase):
    async def test_search_maps_mddb_docs(self):
        fake = [{
            "key": "A-68",
            "contentMd": "# Archived document set: A-68\n- type: condo-sale\n"
                        "- identifiers: deeds 070547/070548",
            "meta": {"doc_type": ["condo-sale"],
                     "drive_path": ["ada-documents/2026/A-68"],
                     "archived_at": ["2026-09-23T19:01:32"],
                     "page_names": ["p1.jpg", "p2.jpg"]},
        }]
        with patch.object(doc_archive_client, "_post", return_value=fake):
            out = await doc_archive_client.doc_search("condo deed")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["slug"], "A-68")
        self.assertEqual(out[0]["doc_type"], "condo-sale")
        self.assertIn("070547", out[0]["summary"])


if __name__ == "__main__":
    unittest.main()
