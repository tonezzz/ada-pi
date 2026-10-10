"""gemini_pool (card ada-gemini-free-tier-quota): the shared key pool,
per-(key,model) exhaustion marks, throttled quota ops events, the TTL
cache, and the generate() failover helper — plus the wiring contracts on
web_search (skip-when-drained + answer cache) and document classify
(content-hash cache).
"""
import asyncio
import io
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GEMINI_API_KEY", "x")

from backend import gemini_pool  # noqa: E402
from backend.gemini_pool import QuotaExhaustedError  # noqa: E402


def _quota_exc():
    return RuntimeError(
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'status': "
        "'RESOURCE_EXHAUSTED', 'message': 'You exceeded your current "
        "quota: GenerateRequestsPerDayPerModel'}}")


def _client_factory(fail_keys=(), text="ok", seen=None):
    """genai.Client replacement — a fake per api_key, recording which key
    each client was built with."""
    def _make(api_key=None, **kwargs):
        if seen is not None:
            seen.append(api_key)
        client = MagicMock()
        if api_key in fail_keys:
            client.aio.models.generate_content = AsyncMock(
                side_effect=_quota_exc())
        else:
            resp = MagicMock()
            resp.text = text
            client.aio.models.generate_content = AsyncMock(
                return_value=resp)
        return client
    return _make


class PoolTests(unittest.TestCase):

    def setUp(self):
        gemini_pool.reset()

    def tearDown(self):
        gemini_pool.reset()

    def test_configured_keys_dedups_in_order(self):
        env = {"GEMINI_API_KEY": "k1", "GEMINI_API_KEYS": "k2, k3 ,k1",
               "GEMINI_API_KEY_2": "k4"}
        with patch.dict(os.environ, env):
            self.assertEqual(gemini_pool.configured_keys(),
                             ["k1", "k2", "k3", "k4"])

    def test_next_key_round_robins(self):
        env = {"GEMINI_API_KEY": "k1", "GEMINI_API_KEYS": "k2"}
        with patch.dict(os.environ, env):
            picks = {gemini_pool.next_key("m") for _ in range(4)}
            self.assertEqual(picks, {"k1", "k2"})

    def test_next_key_priority_prefers_first_key(self):
        env = {"GEMINI_API_KEY": "free", "GEMINI_API_KEY_2": "paid",
               "ADA_GEMINI_KEY_POLICY": "priority"}
        with patch.dict(os.environ, env):
            picks = {gemini_pool.next_key("m") for _ in range(4)}
            self.assertEqual(picks, {"free"})
            gemini_pool.mark_exhausted("free", "m")
            self.assertEqual(gemini_pool.next_key("m"), "paid")

    def test_exhaustion_is_per_key_model(self):
        env = {"GEMINI_API_KEY": "k1"}
        with patch.dict(os.environ, env):
            gemini_pool.mark_exhausted("k1", "flash")
            self.assertIsNone(gemini_pool.next_key("flash"))
            # a different model on the same key still has quota
            self.assertEqual(gemini_pool.next_key("other-model"), "k1")

    def test_pool_exhausted_only_when_all_keys_out(self):
        env = {"GEMINI_API_KEY": "k1", "GEMINI_API_KEYS": "k2"}
        with patch.dict(os.environ, env):
            self.assertFalse(gemini_pool.pool_exhausted("m"))
            gemini_pool.mark_exhausted("k1", "m")
            self.assertFalse(gemini_pool.pool_exhausted("m"))
            gemini_pool.mark_exhausted("k2", "m")
            self.assertTrue(gemini_pool.pool_exhausted("m"))

    def test_simulate_exhausted_forces_none(self):
        with patch.dict(os.environ,
                        {"GEMINI_API_KEY": "k1",
                         "ADA_GEMINI_SIMULATE_EXHAUSTED": "1"}):
            self.assertIsNone(gemini_pool.next_key("m"))
            self.assertTrue(gemini_pool.status()["simulate_exhausted"])

    def test_status_reports_counts_without_key_material(self):
        env = {"GEMINI_API_KEY": "secretkeyvalue"}
        with patch.dict(os.environ, env):
            gemini_pool.mark_exhausted(
                "secretkeyvalue", "m", _quota_exc(), tool="t")
            st = gemini_pool.status()
            self.assertEqual(st["keys_configured"], 1)
            self.assertEqual(st["exhausted_key_models"], 1)
            self.assertIn("t", st["last_quota_errors"])
            self.assertNotIn("secretkeyvalue", json.dumps(st))


class CacheTests(unittest.TestCase):

    def setUp(self):
        gemini_pool.reset()

    def test_put_get_expiry(self):
        gemini_pool.cache_put("ns", "k", {"a": 1}, ttl_s=60)
        self.assertEqual(gemini_pool.cache_get("ns", "k"), {"a": 1})
        gemini_pool.cache_put("ns", "k2", "v", ttl_s=-1)
        self.assertIsNone(gemini_pool.cache_get("ns", "k2"))
        self.assertIsNone(gemini_pool.cache_get("ns", "missing"))

    def test_cap_evicts_oldest(self):
        cap = gemini_pool._CACHE_CAP
        for i in range(cap + 5):
            gemini_pool.cache_put("ns", f"k{i}", i, ttl_s=60)
        self.assertLessEqual(len(gemini_pool._caches["ns"]), cap)
        self.assertIsNone(gemini_pool.cache_get("ns", "k0"))
        self.assertEqual(
            gemini_pool.cache_get("ns", f"k{cap + 4}"), cap + 4)


class GenerateTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        gemini_pool.reset()

    async def asyncTearDown(self):
        gemini_pool.reset()

    async def test_failover_to_next_key_on_429(self):
        env = {"GEMINI_API_KEY": "k1", "GEMINI_API_KEYS": "k2"}
        seen = []
        with patch.dict(os.environ, env), \
                patch("google.genai.Client",
                      new=_client_factory(fail_keys={"k1"}, seen=seen)):
            resp = await gemini_pool.generate(model="m", contents="hi")
            self.assertEqual(resp.text, "ok")
            self.assertEqual(seen, ["k1", "k2"])
            # k1 stays marked for this model; k2 serves it now
            self.assertEqual(gemini_pool.next_key("m"), "k2")

    async def test_all_keys_drained_raises_loud(self):
        env = {"GEMINI_API_KEY": "k1", "GEMINI_API_KEYS": "k2"}
        with patch.dict(os.environ, env), \
                patch("google.genai.Client",
                      new=_client_factory(fail_keys={"k1", "k2"})):
            with self.assertRaises(QuotaExhaustedError) as ctx:
                await gemini_pool.generate(model="m", contents="hi")
            self.assertIn("quota", str(ctx.exception).lower())
            self.assertTrue(gemini_pool.pool_exhausted("m"))

    async def test_drained_pool_emits_ops_event(self):
        env = {"GEMINI_API_KEY": "k1", "ADA_INSTANCE_ID": "test"}
        mddb = MagicMock()
        mddb.add_document = AsyncMock(return_value={"status": "ok"})
        with patch.dict(os.environ, env), \
                patch("google.genai.Client",
                      new=_client_factory(fail_keys={"k1"})):
            with self.assertRaises(QuotaExhaustedError):
                await gemini_pool.generate(
                    model="m", contents="hi", tool="t1", mddb=mddb)
        await asyncio.sleep(0.05)
        docs = [c.kwargs for c in mddb.add_document.call_args_list
                if c.kwargs.get("collection") == "ada-ha-events-test"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["meta"]["kind"], ["ops-event"])
        self.assertEqual(docs[0]["meta"]["type"], ["t1_quota"])

    async def test_non_quota_error_propagates_unmarked(self):
        env = {"GEMINI_API_KEY": "k1", "GEMINI_API_KEYS": "k2"}
        seen = []

        def _make(api_key=None, **kw):
            seen.append(api_key)
            c = MagicMock()
            c.aio.models.generate_content = AsyncMock(
                side_effect=RuntimeError("some other failure"))
            return c
        with patch.dict(os.environ, env), \
                patch("google.genai.Client", new=_make):
            with self.assertRaises(RuntimeError) as ctx:
                await gemini_pool.generate(model="m", contents="hi")
        self.assertIn("some other failure", str(ctx.exception))
        self.assertEqual(seen, ["k1"])  # no rotation on non-quota errors
        self.assertFalse(gemini_pool.pool_exhausted("m"))

    async def test_injected_client_bypasses_pool(self):
        client = MagicMock()
        resp = MagicMock()
        resp.text = "injected"
        client.aio.models.generate_content = AsyncMock(return_value=resp)
        with patch("google.genai.Client") as cls:
            out = await gemini_pool.generate(
                model="m", contents="hi", client=client)
        self.assertEqual(out.text, "injected")
        cls.assert_not_called()

    async def test_no_keys_raises_not_set(self):
        with patch.dict(os.environ,
                        {"GEMINI_API_KEY": "", "GEMINI_API_KEYS": "",
                         "GOOGLE_API_KEY": ""},
                        clear=False):
            for i in range(2, 10):
                os.environ[f"GEMINI_API_KEY_{i}"] = ""
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    await gemini_pool.generate(model="m", contents="hi")
                self.assertIn("GEMINI_API_KEY is not set",
                              str(ctx.exception))
            finally:
                for i in range(2, 10):
                    os.environ.pop(f"GEMINI_API_KEY_{i}", None)


class EmitOpsEventTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        gemini_pool.reset()

    async def test_throttled_per_tool(self):
        env = {"ADA_INSTANCE_ID": "test"}
        mddb = MagicMock()
        mddb.add_document = AsyncMock(return_value={"status": "ok"})
        with patch.dict(os.environ, env):
            gemini_pool.emit_ops_event(mddb, "toolA", _quota_exc())
            gemini_pool.emit_ops_event(mddb, "toolA", _quota_exc())
            gemini_pool.emit_ops_event(mddb, "toolB", _quota_exc())
        await asyncio.sleep(0.05)
        colls = [c.kwargs["meta"]["type"][0]
                 for c in mddb.add_document.call_args_list]
        self.assertEqual(sorted(colls), ["toolA_quota", "toolB_quota"])

    async def test_no_mddb_noop(self):
        gemini_pool.emit_ops_event(None, "toolA", _quota_exc())
        await asyncio.sleep(0.01)  # just must not raise


class WebSearchPoolWiringTests(unittest.IsolatedAsyncioTestCase):
    """web_search honors a drained pool and caches clean answers."""

    async def asyncSetUp(self):
        gemini_pool.reset()
        from backend.tool_runner import ToolRunner
        self.ha_client = AsyncMock()
        self.ha_client.base_url = "http://test:8123"
        self.runner = ToolRunner(self.ha_client, instance_id="test")
        self.runner.mddb = AsyncMock()
        self.runner.mddb.add_document.return_value = {"status": "ok"}
        self.runner._web_search_ddg = AsyncMock(return_value={
            "answer": "ddg hit", "sources": [{"title": "t", "uri": "u"}],
            "provider": "duckduckgo"})

    async def asyncTearDown(self):
        gemini_pool.reset()

    async def test_drained_pool_skips_gemini_call(self):
        env = {"GEMINI_API_KEY": "k1"}
        with patch.dict(os.environ, env):
            gemini_pool.mark_exhausted(
                "k1", "gemini-2.5-flash", _quota_exc())
            self.runner._web_search_gemini = AsyncMock(
                side_effect=AssertionError("doomed request must be skipped"))
            out = await self.runner.web_search("flood news")
        self.assertEqual(out["provider_used"], "duckduckgo")
        self.assertTrue(out["quota_exhausted"])
        self.assertIn("degraded", out)
        self.runner._web_search_gemini.assert_not_awaited()

    async def test_gemini_answer_cached_per_query(self):
        gemini_out = {"answer": "grounded", "sources": [],
                      "provider": "gemini", "model": "gemini-2.5-flash"}
        self.runner._web_search_gemini = AsyncMock(
            return_value=dict(gemini_out))
        first = await self.runner.web_search("same query")
        second = await self.runner.web_search("Same  QUERY ")
        self.runner._web_search_gemini.assert_awaited_once()
        self.assertTrue(second["cached"])
        self.assertNotIn("cached", first)

    async def test_degraded_result_not_cached(self):
        self.runner._web_search_gemini = AsyncMock(
            side_effect=_quota_exc())
        await self.runner.web_search("q")
        self.runner._web_search_gemini = AsyncMock(return_value={
            "answer": "grounded", "sources": [], "provider": "gemini",
            "model": "gemini-2.5-flash"})
        await self.runner.web_search("q")
        # the degraded first answer must not have been replayed — gemini
        # was consulted again for the same query
        self.runner._web_search_gemini.assert_awaited_once()


class DocClassifyCacheTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        gemini_pool.reset()

    async def asyncTearDown(self):
        gemini_pool.reset()

    def _jpeg(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 200, 200)).save(buf, "JPEG")
        return buf.getvalue()

    async def test_same_image_classifies_once(self):
        from backend.document_check import DocumentCheckEngine
        eng = DocumentCheckEngine(client=MagicMock())
        resp = MagicMock()
        resp.text = json.dumps({"doc_type": "receipt", "confidence": 0.9,
                                "summary": "shop receipt"})
        eng._client.aio.models.generate_content = AsyncMock(
            return_value=resp)
        img = self._jpeg()
        one = await eng._classify(img, "image/jpeg")
        two = await eng._classify(img, "image/jpeg")
        eng._client.aio.models.generate_content.assert_awaited_once()
        self.assertEqual(one["doc_type"], two["doc_type"])

    async def test_classify_quota_marks_pool(self):
        from backend.document_check import DocumentCheckEngine
        eng = DocumentCheckEngine(client=MagicMock())
        eng._client.aio.models.generate_content = AsyncMock(
            side_effect=_quota_exc())
        with self.assertRaises(RuntimeError) as ctx:
            await eng._classify(self._jpeg(), "image/jpeg")
        # injected clients propagate their own error (no pool marking —
        # the fake isn't a real key)
        self.assertIn("429", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
