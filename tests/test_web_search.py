"""web_search quota fallback (card web-search-quota-fallback): a
429/quota-shaped gemini error auto-retries duckduckgo once and the
result is annotated (provider_used / quota_exhausted / degraded) so
Ada can say "live search is degraded" — plus a web_search_quota ops
event lands in ada-ha-events-<instance> so quota burn shows in the
digest before it reaches zero."""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GEMINI_API_KEY", "x")

from backend.tool_runner import ToolRunner  # noqa: E402
from backend.tool_runner.web import _is_quota_error  # noqa: E402


def _quota_exc():
    return RuntimeError(
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'status': "
        "'RESOURCE_EXHAUSTED', 'message': 'You exceeded your current "
        "quota: GenerateRequestsPerDayPerModel'}}")


def _plain_exc():
    return RuntimeError("web search returned no answer")


_DDG_RESULT = {"answer": "ddg synthesized top hit",
               "sources": [{"title": "t", "uri": "https://x"}],
               "provider": "duckduckgo"}

_GEMINI_RESULT = {"answer": "grounded answer",
                  "sources": [], "provider": "gemini",
                  "model": "gemini-2.5-flash"}


class QuotaShapeTests(unittest.TestCase):

    def test_429_text_is_quota(self):
        self.assertTrue(_is_quota_error(_quota_exc()))

    def test_structured_code_is_quota(self):
        e = RuntimeError("no marker text")
        e.code = 429
        self.assertTrue(_is_quota_error(e))

    def test_resource_exhausted_status_is_quota(self):
        e = RuntimeError("x")
        e.status = "RESOURCE_EXHAUSTED"
        self.assertTrue(_is_quota_error(e))

    def test_rate_limit_text_is_quota(self):
        self.assertTrue(_is_quota_error(RuntimeError("Rate limit hit")))

    def test_plain_error_is_not_quota(self):
        self.assertFalse(_is_quota_error(_plain_exc()))
        self.assertFalse(_is_quota_error(TimeoutError("timed out")))


class WebSearchFallbackTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner.mddb = AsyncMock()
        self.runner.mddb.add_document.return_value = {"status": "ok"}
        self.runner._web_search_ddg = AsyncMock(return_value=dict(_DDG_RESULT))

    async def _settle(self):
        await asyncio.sleep(0.05)

    def _ops_docs(self):
        return [c.kwargs for c in self.runner.mddb.add_document.call_args_list
                if c.kwargs.get("collection") == "ada-ha-events-test"]

    async def test_429_falls_back_and_annotates(self):
        self.runner._web_search_gemini = AsyncMock(side_effect=_quota_exc())
        out = await self.runner.web_search("bangkok flood")
        self.assertEqual(out["provider_used"], "duckduckgo")
        self.assertEqual(out["provider"], "duckduckgo")
        self.assertTrue(out["quota_exhausted"])
        self.assertIn("degraded", out)
        self.assertIn("429", out["fallback_error"])
        self.runner._web_search_ddg.assert_awaited_once_with("bangkok flood")

    async def test_429_posts_quota_ops_event(self):
        self.runner._web_search_gemini = AsyncMock(side_effect=_quota_exc())
        await self.runner.web_search("q")
        await self._settle()
        docs = self._ops_docs()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["meta"]["kind"], ["ops-event"])
        self.assertEqual(docs[0]["meta"]["type"], ["web_search_quota"])
        self.assertEqual(docs[0]["meta"]["tool"], ["web_search"])
        self.assertIn("ops-", docs[0]["key"])

    async def test_quota_event_throttled_per_window(self):
        self.runner._web_search_gemini = AsyncMock(side_effect=_quota_exc())
        await self.runner.web_search("q1")
        await self.runner.web_search("q2")
        await self._settle()
        self.assertEqual(len(self._ops_docs()), 1)

    async def test_non_quota_error_still_falls_back_without_event(self):
        self.runner._web_search_gemini = AsyncMock(side_effect=_plain_exc())
        out = await self.runner.web_search("q")
        self.assertEqual(out["provider_used"], "duckduckgo")
        self.assertFalse(out["quota_exhausted"])
        self.assertIn("degraded", out)
        await self._settle()
        self.assertEqual(self._ops_docs(), [])

    async def test_explicit_gemini_raises_but_still_reports_quota(self):
        self.runner._web_search_gemini = AsyncMock(side_effect=_quota_exc())
        with self.assertRaises(RuntimeError):
            await self.runner.web_search("q", provider="gemini")
        await self._settle()
        self.assertEqual(len(self._ops_docs()), 1)
        self.runner._web_search_ddg.assert_not_awaited()

    async def test_ddg_failure_reraises_gemini_error(self):
        self.runner._web_search_gemini = AsyncMock(side_effect=_quota_exc())
        self.runner._web_search_ddg = AsyncMock(
            side_effect=RuntimeError("duckduckgo returned no results"))
        with self.assertRaises(RuntimeError) as ctx:
            await self.runner.web_search("q")
        self.assertIn("429", str(ctx.exception))

    async def test_gemini_success_is_annotated_clean(self):
        self.runner._web_search_gemini = AsyncMock(
            return_value=dict(_GEMINI_RESULT))
        out = await self.runner.web_search("q")
        self.assertEqual(out["provider_used"], "gemini")
        self.assertFalse(out["quota_exhausted"])
        self.assertNotIn("degraded", out)
        self.runner._web_search_ddg.assert_not_awaited()
        await self._settle()
        self.assertEqual(self._ops_docs(), [])

    async def test_explicit_duckduckgo_marks_provider(self):
        self.runner._web_search_gemini = AsyncMock()
        out = await self.runner.web_search("q", provider="duckduckgo")
        self.assertEqual(out["provider_used"], "duckduckgo")
        self.assertFalse(out["quota_exhausted"])
        self.runner._web_search_gemini.assert_not_awaited()

    async def test_no_mddb_means_no_event_no_crash(self):
        self.runner.mddb = None
        self.runner._web_search_gemini = AsyncMock(side_effect=_quota_exc())
        out = await self.runner.web_search("q")
        self.assertTrue(out["quota_exhausted"])


if __name__ == "__main__":
    unittest.main()
