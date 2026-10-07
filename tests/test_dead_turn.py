"""Dead-turn guard regression (card ada-dead-turn-guard).

Replays the session-56d2e4d167 shape: user asks "รถ KK ทะเบียนอะไร",
ada_memory_search returns hits already containing the plate 5ขว 6249,
and the model's whole answer is the degenerate literal "response:" —
the user heard nothing and had to repeat the question.

The guard must (a) emit a dead_turn ops event, (b) never persist the
scaffold as the answer, (c) deterministically retry once with the tool
result handed back, and (d) when the retry lands dead too, synthesize a
fallback that speaks the actual result ("พบแล้วค่ะ ทะเบียน 5ขว 6249").
Also covers the generic tool-call-text sanitizer (leak_detected) and the
ada_memory_search hit-content cap.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from google.genai import types

from backend.realtime_provider import GeminiLiveProvider, _dead_turn_text
from backend.tool_runner.memory import MemoryMixin, MEMORY_HIT_CONTENT_MAX

PLATE_CONTENT = (
    "KK (คุณกุ้ง) drives a white Honda CR-V — ทะเบียน 5ขว 6249.\n"
    "Nickname for Ada: แก้วตา. Prefers Thai replies."
)


def _search_result(degraded: bool = False) -> dict:
    return {
        "ok": True,
        "bank": "all",
        "scope": "all",
        "count": 1,
        "hits": [{
            "bank": "people", "key": "people/kk", "score": 0.92,
            "subject": "KK", "content": PLATE_CONTENT,
        }],
        "degraded": degraded,
    }


class _PlateQuestionSession:
    """Stubbed Gemini Live session replaying the dead-turn incident.

    After the dead "response:" turn, a retry turn only exists if the
    provider actually sent the retry nudge (send_client_content) — so a
    missing retry can't be masked by a stubbed answer.
    """

    def __init__(self, provider: GeminiLiveProvider, retry_answer: str):
        self.provider = provider
        self.retry_answer = retry_answer
        self.client_content: list[dict] = []
        self.tool_responses: list = []

    async def receive(self):
        yield types.LiveServerMessage(
            server_content=types.LiveServerContent(
                input_transcription=types.Transcription(
                    text="รถ KK ทะเบียนอะไร")))
        yield types.LiveServerMessage(
            tool_call=types.LiveServerToolCall(function_calls=[
                types.FunctionCall(
                    id="ms-1", name="ada_memory_search",
                    args={"query": "ทะเบียนรถ KK"})]))
        yield types.LiveServerMessage(
            server_content=types.LiveServerContent(
                output_transcription=types.Transcription(text="response:"),
                turn_complete=True))
        if self.client_content:  # the dead-turn retry nudge was sent
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    output_transcription=types.Transcription(
                        text=self.retry_answer),
                    turn_complete=True))
        self.provider._closed = True

    async def send_tool_response(self, *, function_responses):
        self.tool_responses.extend(function_responses)

    async def send_client_content(self, **kwargs):
        self.client_content.append(kwargs)


def _provider_with_search(result: dict):
    runner = MagicMock()
    runner.current_speaker_ha_person = None
    runner.banks.banks.return_value = {}
    runner.execute = AsyncMock(return_value=result)
    provider = GeminiLiveProvider(tool_runner=runner, session_id="dead-t")
    ops: list[tuple] = []
    provider._emit_ops_event = lambda ev_type, detail, tool=None: (
        ops.append((ev_type, detail, tool)))
    return provider, ops


def _assistant_turns(provider) -> list[str]:
    return [t["text"] for t in provider.conversation.turns()
            if t["role"] == "assistant"]


class DeadTurnTextTests(unittest.TestCase):
    def test_degenerate_shapes_are_dead(self):
        for text in ("", "   \n ", "response:", "Response:", "response :",
                     "RESPONSE:", "assistant:", "คำตอบ:", "response",
                     "<response>", "</output>", "```", "---", "***",
                     "response: <output>", "<code_output>"):
            self.assertTrue(_dead_turn_text(text), repr(text))

    def test_real_answers_are_not_dead(self):
        for text in ("พบแล้วค่ะ ทะเบียน 5ขว 6249", "5ขว 6249",
                     "response: พบแล้วค่ะ", "The plate is 5ขว 6249",
                     "ค่ะ", "Noted."):
            self.assertFalse(_dead_turn_text(text), repr(text))


class DeadTurnGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_dead_turn_retries_once_and_answer_carries_plate(self):
        provider, ops = _provider_with_search(_search_result())
        session = _PlateQuestionSession(
            provider, retry_answer="พบแล้วค่ะ ทะเบียน 5ขว 6249")
        provider._session = session

        events = [e async for e in provider.events()]

        # The retry nudge went out as a new user-role turn carrying the
        # tool-result digest — the model cannot miss the plate again.
        self.assertEqual(len(session.client_content), 1)
        nudge = session.client_content[0]
        self.assertTrue(nudge["turn_complete"])
        nudge_text = nudge["turns"].parts[0].text
        self.assertIn("5ขว 6249", nudge_text)
        # Ops event fired, and the retry produced the real answer.
        self.assertIn("dead_turn", [t[0] for t in ops])
        assistant = _assistant_turns(provider)
        self.assertEqual(len(assistant), 1)
        self.assertIn("5ขว 6249", assistant[0])
        # The degenerate scaffold is never persisted as the answer.
        self.assertNotIn("response:", assistant[0])
        self.assertEqual(events[-1].type, "response_completed")
        self.assertFalse(provider._dead_retried)

    async def test_double_dead_turn_synthesizes_fallback_with_plate(self):
        provider, ops = _provider_with_search(_search_result())
        session = _PlateQuestionSession(provider, retry_answer="response:")
        provider._session = session

        events = [e async for e in provider.events()]

        # One retry only — the second dead turn takes the fallback.
        self.assertEqual(len(session.client_content), 1)
        self.assertEqual([t[0] for t in ops], ["dead_turn", "dead_turn"])
        deltas = [e for e in events
                  if e.type == "assistant_transcript_delta"]
        spoken = "".join(e.data["text"] for e in deltas)
        self.assertIn("พบแล้วค่ะ", spoken)
        self.assertIn("5ขว 6249", spoken)
        assistant = _assistant_turns(provider)
        self.assertEqual(len(assistant), 1)
        self.assertIn("5ขว 6249", assistant[0])
        self.assertNotIn("response:", assistant[0])
        self.assertFalse(provider._dead_retried)

    async def test_degraded_search_fallback_still_says_degraded(self):
        provider, _ops = _provider_with_search(_search_result(degraded=True))
        session = _PlateQuestionSession(provider, retry_answer="response:")
        provider._session = session
        events = [e async for e in provider.events()]
        spoken = "".join(
            e.data["text"] for e in events
            if e.type == "assistant_transcript_delta")
        self.assertIn("5ขว 6249", spoken)
        self.assertIn("degraded", spoken)

    async def test_healthy_turn_never_touches_the_guard(self):
        class HealthySession(_PlateQuestionSession):
            async def receive(self):
                yield types.LiveServerMessage(
                    server_content=types.LiveServerContent(
                        input_transcription=types.Transcription(
                            text="รถ KK ทะเบียนอะไร")))
                yield types.LiveServerMessage(
                    tool_call=types.LiveServerToolCall(function_calls=[
                        types.FunctionCall(
                            id="ms-1", name="ada_memory_search",
                            args={"query": "ทะเบียนรถ KK"})]))
                yield types.LiveServerMessage(
                    server_content=types.LiveServerContent(
                        output_transcription=types.Transcription(
                            text="พบแล้วค่ะ ทะเบียน 5ขว 6249"),
                        turn_complete=True))
                self.provider._closed = True

        provider, ops = _provider_with_search(_search_result())
        session = HealthySession(provider, retry_answer="unused")
        provider._session = session
        events = [e async for e in provider.events()]
        # No dead-turn machinery may fire on a healthy turn.
        self.assertEqual(ops, [])
        self.assertEqual(session.client_content, [])
        self.assertEqual(_assistant_turns(provider),
                         ["พบแล้วค่ะ ทะเบียน 5ขว 6249"])
        self.assertEqual(events[-1].type, "response_completed")


class ToolCallShapeSanitizerTests(unittest.TestCase):
    """Guard 2 — `name{key:value}` text must be stripped before the
    transcript/TTS path even when the name is not a declared tool."""

    def setUp(self):
        self.provider = GeminiLiveProvider()
        self.ops: list[tuple] = []
        self.provider._emit_ops_event = lambda ev_type, detail, tool=None: (
            self.ops.append((ev_type, detail, tool)))

    def test_declared_tool_call_shape_stripped(self):
        # Provider not connected → _tool_leak_re is None; the generic
        # shape detector still catches name{key:value}.
        out = self.provider._strip_tool_leak(
            'พบแล้วค่ะ set_facial_expression{expression:"sassy"}')
        self.assertEqual(out, "พบแล้วค่ะ ")
        self.assertTrue(self.provider._leak_active)
        self.assertEqual(self.ops[0][0], "leak_detected")

    def test_undeclared_tool_call_shape_stripped(self):
        out = self.provider._strip_tool_leak(
            'กำลังค้นหา imaginer{query:"plate"}')
        self.assertEqual(out, "กำลังค้นหา ")
        self.assertEqual(self.ops[0][0], "leak_detected")

    def test_leak_suppresses_rest_of_turn(self):
        self.provider._strip_tool_leak('a foo{x:1} tail')
        self.assertEqual(self.provider._strip_tool_leak("more text"), "")
        self.provider._leak_active = False

    def test_plain_speech_passes_through(self):
        for text in ("พบแล้วค่ะ ทะเบียน 5ขว 6249",
                     "The plate is 5ขว 6249.",
                     "ค่ะ เดี๋ยวดูให้นะคะ"):
            self.assertEqual(self.provider._strip_tool_leak(text), text)
        self.assertEqual(self.ops, [])


class _FakeChaba:
    def __init__(self, hits):
        self._hits = hits

    def recall(self, query, session_id=None, limit=10):
        return self._hits


class _FakeRunner(MemoryMixin):
    def __init__(self, chaba=None, mddb=None):
        self.chaba = chaba
        self.mddb = mddb
        self.banks = MagicMock()
        self.session_id = "t"

    def _memory_identity(self):
        return None


class MemoryHitTruncationTests(unittest.IsolatedAsyncioTestCase):
    """Guard 3 — a fat hit body can never reach the live context whole;
    key/score/meta fields survive intact and degraded stays announced."""

    async def test_guest_scope_hit_content_capped(self):
        big = "ทะเบียน 5ขว 6249 " + "x" * 9000
        runner = _FakeRunner(chaba=_FakeChaba([{
            "key": "car", "name": "KK", "score": 0.9,
            "text": big, "at": "2026-10-07"}]))
        out = await runner.ada_memory_search(
            "ทะเบียน", limit=5, scope="guest")
        hit = out["hits"][0]
        self.assertLess(len(hit["content"]), MEMORY_HIT_CONTENT_MAX + 100)
        self.assertTrue(hit["content"].startswith("ทะเบียน 5ขว 6249"))
        self.assertIn("truncated", hit["content"])
        self.assertTrue(hit["content_truncated"])
        # key/subject/score survive the cap.
        self.assertEqual(hit["key"], "car")
        self.assertEqual(hit["subject"], "KK")
        self.assertEqual(hit["score"], 0.9)

    async def test_banks_scope_cap_is_boundary_not_upstream(self):
        # Even if the source (memory_ops) regressed and returned a huge
        # body, the runner boundary still caps it.
        big_hit = {
            "key": "people/kk", "score": 0.92, "subject": "KK",
            "content": "plate 5ขว 6249 " + "y" * 9000,
            "kind": "person",
        }
        with patch(
            "backend.memory_ops.memory_search",
            AsyncMock(return_value={"hits": [big_hit], "degraded": True}),
        ):
            runner = _FakeRunner(mddb=MagicMock())
            out = await runner.ada_memory_search(
                "plate", bank="all", limit=5, scope="banks")
        hit = out["hits"][0]
        self.assertLess(len(hit["content"]), MEMORY_HIT_CONTENT_MAX + 100)
        self.assertTrue(hit["content_truncated"])
        self.assertEqual(hit["key"], "people/kk")
        self.assertEqual(hit["kind"], "person")
        self.assertTrue(out["degraded"])

    async def test_small_hits_pass_through_untouched(self):
        runner = _FakeRunner(chaba=_FakeChaba([{
            "key": "car", "name": "KK", "score": 0.9,
            "text": "plate 5ขว 6249", "at": "x"}]))
        out = await runner.ada_memory_search("plate", scope="guest")
        hit = out["hits"][0]
        self.assertEqual(hit["content"], "plate 5ขว 6249")
        self.assertNotIn("content_truncated", hit)
        self.assertFalse(out["degraded"])


if __name__ == "__main__":
    unittest.main()
