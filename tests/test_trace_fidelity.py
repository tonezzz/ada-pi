"""Tool-result trace fidelity (card ada-trace-full-result).

The ws tool_result event stays a stump (scalars + _safe_args-truncated
strings) but now carries id + result_hash — sha256 of the canonical JSON
of the normalized result. The complete payload lands in two places:

- the call-log function_result line's `result_full` field (always on —
  the audit tier-2 trace doc keyed by session+call id), and
- a tool_result_full ws event when ADA_TRACE_FULL=1 (debug runs, so
  scenario-live can assert on ground truth instead of the stump).
"""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from google.genai import types

from backend.realtime_provider import (
    GeminiLiveProvider, _canonical_result_json, _full_result_payload,
    _json_safe, _result_hash)


class CanonicalJsonTests(unittest.TestCase):
    def test_key_order_invariant(self):
        a = {"b": 1, "a": {"y": [2, 3], "x": "s"}}
        b = {"a": {"x": "s", "y": [2, 3]}, "b": 1}
        self.assertEqual(_canonical_result_json(a),
                         _canonical_result_json(b))
        self.assertEqual(_result_hash(a), _result_hash(b))

    def test_hash_is_sha256_of_canonical_json(self):
        result = {"ok": True, "n": 3}
        want = hashlib.sha256(
            _canonical_result_json(result).encode("utf-8")).hexdigest()
        self.assertEqual(_result_hash(result), want)

    def test_content_change_changes_hash(self):
        self.assertNotEqual(_result_hash({"ok": True}),
                            _result_hash({"ok": False}))

    def test_non_dict_and_non_json_values(self):
        # Sets/objects stringify; scalars and containers survive.
        safe = _json_safe({"s": frozenset({1}), "t": (1, 2)})
        self.assertIsInstance(safe["s"], str)
        self.assertEqual(safe["t"], [1, 2])
        # Non-dict results hash fine — normalize_tool_result wraps them
        # upstream, but the helper itself must never raise.
        self.assertTrue(_result_hash("plain string"))
        self.assertTrue(_result_hash([1, {"a": 2}]))


class FullResultPayloadTests(unittest.TestCase):
    def test_under_cap_passes_through(self):
        result = {"ok": True, "hits": [{"content": "x" * 5000}]}
        self.assertEqual(_full_result_payload(result), result)

    def test_over_cap_stores_truncated_marker(self):
        result = {"ok": True, "blob": "y" * 5000}
        with patch(
                "backend.realtime_provider._RESULT_FULL_CAP", 100):
            out = _full_result_payload(result)
        self.assertTrue(out["_truncated"])
        self.assertGreater(out["chars"], 100)
        self.assertEqual(len(out["head"]), 100)


class _RunnerResultSession:
    """One tool_call for a runner-dispatched tool, then close."""

    def __init__(self, provider: GeminiLiveProvider):
        self.provider = provider
        self.responses = []

    async def receive(self):
        yield types.LiveServerMessage(
            tool_call=types.LiveServerToolCall(function_calls=[
                types.FunctionCall(
                    id="call-1", name="ada_test_tool",
                    args={"q": "x"})]))
        self.provider._closed = True

    async def send_tool_response(self, *, function_responses):
        self.responses.extend(function_responses)


def _provider(result: dict, trace_full: bool = False):
    runner = MagicMock()
    runner.current_speaker_ha_person = None
    runner.banks.banks.return_value = {}
    runner.execute = AsyncMock(return_value=result)
    env = {"ADA_TRACE_FULL": "1"} if trace_full else {"ADA_TRACE_FULL": "0"}
    with patch.dict(os.environ, env):
        provider = GeminiLiveProvider(
            tool_runner=runner, session_id="trace-t")
    return provider


LONG_TEXT = "plate-content-" + "x" * 600 + "-TAILMARKER"
RESULT = {
    "ok": True,
    "hits": [{"content": LONG_TEXT, "meta": {"nested": [1, {"deep": "v"}]}}],
}


class ToolResultTraceTests(unittest.IsolatedAsyncioTestCase):
    async def _events(self, provider, tmpdir):
        provider._call_log_dir = Path(tmpdir)
        session = _RunnerResultSession(provider)
        provider._session = session
        return [e async for e in provider.events()]

    async def test_tool_result_event_carries_id_and_hash_stump(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _provider(dict(RESULT))
            events = await self._events(provider, td)
        tr = [e for e in events if e.type == "tool_result"]
        self.assertEqual(len(tr), 1)
        data = tr[0].data
        self.assertEqual(data["id"], "call-1")
        self.assertEqual(data["name"], "ada_test_tool")
        self.assertEqual(
            data["result_hash"], _result_hash(RESULT))
        # Stump unchanged: nested containers still str()[:300] — the
        # tail marker sits past the cut, ground truth lives elsewhere.
        hits = data["result"]["hits"]
        self.assertIsInstance(hits, str)
        self.assertLessEqual(len(hits), 300)
        self.assertNotIn("TAILMARKER", hits)

    async def test_call_log_result_full_is_ground_truth(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _provider(dict(RESULT))
            await self._events(provider, td)
            lines = []
            for p in Path(td).glob("*.jsonl"):
                lines += [json.loads(x) for x in
                          p.read_text().splitlines() if x.strip()]
        results = [x for x in lines
                   if x.get("event") == "function_result"]
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertEqual(row["id"], "call-1")
        self.assertEqual(row["session_id"], "trace-t")
        self.assertEqual(row["result_hash"], _result_hash(RESULT))
        # Full payload, untruncated — the audit join on session+call id.
        self.assertEqual(row["result_full"], RESULT)
        self.assertEqual(
            row["result_full"]["hits"][0]["content"], LONG_TEXT)
        # The legacy `result` stump field is still there too.
        self.assertIsInstance(row["result"]["hits"], str)

    async def test_session_events_anchor_the_hash(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _provider(dict(RESULT))
            await self._events(provider, td)
        calls = [i for i in provider.conversation.session_items
                 if i.get("kind") == "tool_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["result_hash"], _result_hash(RESULT))

    async def test_trace_full_flag_emits_ground_truth_event(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _provider(dict(RESULT), trace_full=True)
            events = await self._events(provider, td)
        full = [e for e in events if e.type == "tool_result_full"]
        self.assertEqual(len(full), 1)
        data = full[0].data
        self.assertEqual(data["id"], "call-1")
        self.assertEqual(data["result_hash"], _result_hash(RESULT))
        self.assertEqual(data["result"], RESULT)
        self.assertEqual(
            data["result"]["hits"][0]["content"], LONG_TEXT)

    async def test_no_full_event_without_flag(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _provider(dict(RESULT))
            events = await self._events(provider, td)
        self.assertFalse(
            [e for e in events if e.type == "tool_result_full"])

    async def test_failed_result_still_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _provider({"ok": False, "error": "denied"})
            events = await self._events(provider, td)
        tr = [e for e in events if e.type == "tool_result"][0]
        self.assertEqual(
            tr.data["result_hash"],
            _result_hash({"ok": False, "error": "denied"}))
        self.assertFalse(tr.data["result"]["ok"])


if __name__ == "__main__":
    unittest.main()
