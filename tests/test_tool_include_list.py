import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.tool_runner import ToolRunner


def _fake_client():
    fake_session = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fake_session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    client = MagicMock()
    client.aio.live.connect = MagicMock(return_value=ctx)
    return client


async def _connect(env_extra):
    from backend.realtime_provider import GeminiLiveProvider

    runner = ToolRunner(AsyncMock(), instance_id="test")
    client = _fake_client()
    env = {"GEMINI_API_KEY": "test-key", **env_extra}
    with patch.dict(os.environ, env), \
         patch("backend.realtime_provider.genai.Client", return_value=client):
        provider = GeminiLiveProvider(tool_runner=runner)
        await provider.connect()
    return client.aio.live.connect.call_args.kwargs["config"]


def _declared(config):
    return {fd["name"] for fd in config["tools"][0]["function_declarations"]}


class IncludedToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_include_list_keeps_only_listed_tools(self):
        keep = {"web_search", "ada_persona", "cms_read", "voice_fx"}
        config = await _connect({"ADA_INCLUDED_TOOLS": ",".join(sorted(keep))})
        names = _declared(config)
        self.assertTrue(names <= keep, names - keep)
        # Kept builtin AND kept tools.d declarations survive.
        self.assertIn("web_search", names)
        self.assertIn("voice_fx", names)

    async def test_include_list_strips_family_instructions(self):
        config = await _connect({"ADA_INCLUDED_TOOLS": "web_search,ada_persona"})
        instr = config["system_instruction"]
        self.assertNotIn("calendar and task tools backed by", instr)
        self.assertNotIn("You maintain the user's miniapp", instr)
        self.assertNotIn("Google Drive", instr)

    async def test_excluded_intersects_included(self):
        # Both vars set — intersection wins (belt & suspenders).
        config = await _connect({
            "ADA_INCLUDED_TOOLS": "web_search,cms_read",
            "ADA_EXCLUDED_TOOLS": "cms_read",
        })
        names = _declared(config)
        self.assertEqual(names, {"web_search"})

    async def test_include_list_drops_late_merged_declarations(self):
        # The camera declaration and tools.d entries are appended after
        # the strip point — the keep-list must still apply to them.
        keep = {"ada_camera_snapshot", "ada_market_quote"}
        config = await _connect({"ADA_INCLUDED_TOOLS": ",".join(sorted(keep))})
        names = _declared(config)
        self.assertTrue(names <= keep, names - keep)
        self.assertIn("ada_camera_snapshot", names)
        self.assertIn("ada_market_quote", names)


if __name__ == "__main__":
    unittest.main()
