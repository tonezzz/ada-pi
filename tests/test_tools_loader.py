import asyncio
import collections
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from backend import tools_loader
from backend.tool_runner import ToolRunner

GOOD_TOOL = """
DECLARATION = {
    "name": "%NAME%",
    "description": "test tool",
    "parameters": {"type": "object", "properties": {}},
}

async def run(runner, **args):
    return {"ok": True, "echo": args}
"""


class _ToolsDir:
    """Context helper: a temp tools.d dir with manifest + modules."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def write(self, name: str, body: str, manifest: dict):
        (self.dir / f"{name}.py").write_text(textwrap.dedent(body))
        merged = {"tools": {}}
        mp = self.dir / "manifest.yml"
        if mp.exists():
            merged = yaml.safe_load(mp.read_text()) or merged
        merged.setdefault("tools", {}).update(manifest)
        mp.write_text(yaml.safe_dump(merged))
        return self

    def cleanup(self):
        self._tmp.cleanup()


class LoadTest(unittest.TestCase):

    def setUp(self):
        self.td = _ToolsDir()
        self.addCleanup(self.td.cleanup)

    def test_loads_valid_tool(self):
        self.td.write("ada_x", GOOD_TOOL.replace("%NAME%", "ada_x"),
                      {"ada_x": {"module": "ada_x", "policy": "read"}})
        reg = tools_loader.load(self.td.dir)
        self.assertIn("ada_x", reg.tools)
        self.assertEqual(reg.tools["ada_x"].policy, "read")
        self.assertFalse(reg.errors)

    def test_bad_module_skipped_not_fatal(self):
        self.td.write("ada_bad", "def not_python(: pass\n",
                      {"ada_bad": {"module": "ada_bad"}})
        self.td.write("ada_ok", GOOD_TOOL.replace("%NAME%", "ada_ok"),
                      {"ada_ok": {"module": "ada_ok"}})
        reg = tools_loader.load(self.td.dir)
        self.assertIn("ada_ok", reg.tools)
        self.assertNotIn("ada_bad", reg.tools)
        self.assertEqual(len(reg.errors), 1)

    def test_missing_declaration_rejected(self):
        self.td.write("ada_nodecl",
                      "async def run(runner, **args): return {}\n",
                      {"ada_nodecl": {"module": "ada_nodecl"}})
        reg = tools_loader.load(self.td.dir)
        self.assertNotIn("ada_nodecl", reg.tools)
        self.assertTrue(reg.errors)

    def test_sync_run_rejected(self):
        self.td.write("ada_sync",
                      'DECLARATION = {"name": "ada_sync", "description": "x",'
                      ' "parameters": {}}\n'
                      'def run(runner, **args): return {}\n',
                      {"ada_sync": {"module": "ada_sync"}})
        reg = tools_loader.load(self.td.dir)
        self.assertNotIn("ada_sync", reg.tools)

    def test_name_mismatch_rejected(self):
        self.td.write("ada_a", GOOD_TOOL.replace("%NAME%", "ada_b"),
                      {"ada_a": {"module": "ada_a"}})
        reg = tools_loader.load(self.td.dir)
        self.assertNotIn("ada_a", reg.tools)

    def test_bad_policy_rejected(self):
        self.td.write("ada_p", GOOD_TOOL.replace("%NAME%", "ada_p"),
                      {"ada_p": {"module": "ada_p", "policy": "yolo"}})
        reg = tools_loader.load(self.td.dir)
        self.assertNotIn("ada_p", reg.tools)

    def test_declarations_respect_exclusion(self):
        self.td.write("ada_x", GOOD_TOOL.replace("%NAME%", "ada_x"),
                      {"ada_x": {"module": "ada_x"}})
        reg = tools_loader.load(self.td.dir)
        self.assertEqual(len(reg.declarations()), 1)
        self.assertEqual(reg.declarations({"ada_x"}), [])

    def test_no_manifest_empty(self):
        reg = tools_loader.load(self.td.dir)
        self.assertEqual(reg.tools, {})
        self.assertEqual(reg.errors, [])


def _runner() -> ToolRunner:
    runner = ToolRunner.__new__(ToolRunner)
    runner.mddb = MagicMock()
    runner._denials = {}
    runner.session_id = None
    runner.session_owner_identity = "person.tony"
    runner.current_speaker_ha_person = "person.tony"
    runner.event_log = None
    runner._confirm_tokens = {}
    runner._confirm_audit = collections.deque(maxlen=10)
    runner._banks = None
    runner._instance_id = "test"
    return runner


class ExecutePathTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.td = _ToolsDir()
        self.addCleanup(self.td.cleanup)

    async def test_dynamic_tool_executes(self):
        self.td.write("ada_x", GOOD_TOOL.replace("%NAME%", "ada_x"),
                      {"ada_x": {"module": "ada_x"}})
        reg = tools_loader.load(self.td.dir)
        runner = _runner()
        with patch("backend.tool_runner.tools_loader.registry",
                   return_value=reg), \
                patch.object(ToolRunner, "_is_secondary_turn",
                             return_value=False):
            out = await runner.execute("ada_x", {"foo": 1},
                                       identity="person.tony")
        self.assertEqual(out, {"ok": True, "echo": {"foo": 1}})

    async def test_unknown_tool_still_raises(self):
        reg = tools_loader.load(self.td.dir)
        runner = _runner()
        with patch("backend.tool_runner.tools_loader.registry",
                   return_value=reg):
            with self.assertRaises(KeyError):
                await runner.execute("nope_nope", {}, identity="person.tony")

    async def test_secondary_turn_denied_by_default(self):
        self.td.write("ada_x", GOOD_TOOL.replace("%NAME%", "ada_x"),
                      {"ada_x": {"module": "ada_x"}})
        reg = tools_loader.load(self.td.dir)
        runner = _runner()
        with patch("backend.tool_runner.tools_loader.registry",
                   return_value=reg), \
                patch.object(ToolRunner, "_is_secondary_turn",
                             return_value=True), \
                patch.object(ToolRunner, "_current_speaker",
                             return_value="person.kk"):
            with self.assertRaises(PermissionError):
                await runner.execute("ada_x", {}, identity="person.kk")

    async def test_confirmed_policy_requires_confirm(self):
        self.td.write("ada_c", GOOD_TOOL.replace("%NAME%", "ada_c"),
                      {"ada_c": {"module": "ada_c", "policy": "confirmed"}})
        reg = tools_loader.load(self.td.dir)
        runner = _runner()
        with patch("backend.tool_runner.tools_loader.registry",
                   return_value=reg), \
                patch.object(ToolRunner, "_is_secondary_turn",
                             return_value=False):
            with self.assertRaises(PermissionError):
                await runner.execute("ada_c", {}, identity="person.tony")
            out = await runner.execute("ada_c", {"confirmed": True},
                                       identity="person.tony")
        self.assertTrue(out["ok"])

    async def test_timeout_enforced(self):
        self.td.write("ada_slow", """
import asyncio
DECLARATION = {"name": "ada_slow", "description": "x",
               "parameters": {"type": "object", "properties": {}}}
async def run(runner, **args):
    await asyncio.sleep(5)
    return {"ok": True}
""", {"ada_slow": {"module": "ada_slow", "timeout_s": 0.05}})
        reg = tools_loader.load(self.td.dir)
        runner = _runner()
        with patch("backend.tool_runner.tools_loader.registry",
                   return_value=reg), \
                patch.object(ToolRunner, "_is_secondary_turn",
                             return_value=False):
            with self.assertRaises(asyncio.TimeoutError):
                await runner.execute("ada_slow", {}, identity="person.tony")


if __name__ == "__main__":
    unittest.main()
