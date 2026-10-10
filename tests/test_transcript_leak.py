"""Transcript-leak storm regression (card ada-transcript-leak-storm).

Two guarantees under test:

1. Earliest-position stripping. _strip_tool_leak emits the text BEFORE the
   match verbatim, so the match must be the earliest POSITION across all
   three detectors — not the first detector that happens to match. A
   declared-name leak (ada_remember{...}) later in a delta used to shadow
   an earlier generic name{key: or role-tag leak, letting the earlier
   leak ride the emitted prefix into assistant_transcript_delta.

2. Tool-result text never reaches assistant_transcript_delta. The
   observed echo wrapper ('Tool ada_memory_search returned: {...}',
   '<code_output>…') is a declared-detector shape; once it fires, the
   rest of the turn — the raw hits JSON included — is suppressed. The
   result itself still reaches the UI on its own tool_result event.
"""
import re
import unittest
from unittest.mock import AsyncMock, MagicMock

from google.genai import types

from backend.realtime_provider import GeminiLiveProvider

PLATE_CONTENT = (
    "KK (คุณกุ้ง) drives a white Honda CR-V — ทะเบียน 5ขว 6249.\n"
    "Nickname for Ada: แก้วตา. Prefers Thai replies."
)


def _search_result() -> dict:
    return {
        "ok": True,
        "bank": "all",
        "scope": "all",
        "count": 1,
        "hits": [{
            "bank": "people", "key": "people/kk", "score": 0.92,
            "subject": "KK", "content": PLATE_CONTENT,
        }],
        "degraded": False,
    }


def _declared_leak_re() -> re.Pattern:
    """Mirror of the filter connect() builds — declared names plus the
    observed tool-result echo wrappers."""
    return re.compile(
        r"\b(?:ada_memory_search|ada_remember|set_facial_expression)\s*\{"
        r"|<code_output>|\bTool\s+\w+\s+returned\s*:")


def _provider(result: dict | None = None):
    runner = MagicMock()
    runner.current_speaker_ha_person = None
    runner.banks.banks.return_value = {}
    runner.execute = AsyncMock(return_value=result or _search_result())
    provider = GeminiLiveProvider(tool_runner=runner, session_id="leak-t")
    provider._tool_leak_re = _declared_leak_re()
    ops: list[tuple] = []
    provider._emit_ops_event = lambda ev_type, detail, tool=None: (
        ops.append((ev_type, detail, tool)))
    return provider, ops


def _deltas(events) -> str:
    return "".join(
        e.data["text"] for e in events
        if e.type == "assistant_transcript_delta")


class LeakOrderingTests(unittest.TestCase):
    """The emitted prefix must stop at the earliest leak of ANY detector,
    not the earliest match of the declared-name detector."""

    def test_generic_leak_before_declared_name(self):
        provider, _ops = _provider()
        out = provider._strip_tool_leak(
            "ok foo{a:1} then ada_remember{content:'x'}")
        # Old order returned 'ok foo{a:1} then ' — the generic leak rode
        # the prefix into the transcript.
        self.assertEqual(out, "ok ")

    def test_role_tag_before_declared_name(self):
        provider, _ops = _provider()
        out = provider._strip_tool_leak("พบแล้ว\nuser\nada_remember{x:1}")
        # The '\nuser\n' scaffold tag is the earliest leak — emitting up to
        # the ada_remember match would ship the fabricated role tag.
        self.assertEqual(out, "พบแล้ว")

    def test_code_output_wrapper_before_generic(self):
        provider, _ops = _provider()
        out = provider._strip_tool_leak(
            "done. <code_output>Tool x returned: foo{a:1}")
        self.assertEqual(out, "done. ")

    def test_single_declared_match_unchanged(self):
        provider, ops = _provider()
        out = provider._strip_tool_leak(
            'พบแล้วค่ะ ada_remember{content:"x"}')
        self.assertEqual(out, "พบแล้วค่ะ ")
        self.assertTrue(provider._leak_active)
        self.assertEqual(ops[0][0], "leak_detected")


class _StormSession:
    """Replays a leak storm: a real ada_memory_search call lands, then the
    model answers by echoing the tool-result wrapper + raw hits JSON,
    followed by more leaked call shapes — across several deltas."""

    def __init__(self, provider: GeminiLiveProvider):
        self.provider = provider
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
        # Mid-task boundary: results dispatched, answer segment pending.
        yield types.LiveServerMessage(
            server_content=types.LiveServerContent(turn_complete=True))
        # The answer segment arrives as a storm of leak shapes — each a
        # separate output_transcription delta, like the real stream.
        storm_deltas = [
            "พบแล้วค่ะ ",
            'Tool ada_memory_search returned: {"ok": true, "hits": '
            '[{"key": "people/kk", "content": "' + PLATE_CONTENT + '"}]}',
            "and then ada_remember{content: 'plate'} to save it",
        ]
        for i, delta in enumerate(storm_deltas):
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    output_transcription=types.Transcription(text=delta),
                    turn_complete=(i == len(storm_deltas) - 1)))
        self.provider._closed = True

    async def send_tool_response(self, *, function_responses):
        self.tool_responses.extend(function_responses)

    async def send_client_content(self, **kwargs):
        self.client_content.append(kwargs)


class ToolResultTranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_echo_never_reaches_transcript_delta(self):
        provider, ops = _provider()
        session = _StormSession(provider)
        provider._session = session

        events = [e async for e in provider.events()]
        spoken = _deltas(events)

        # The clean prefix survives; every tool-result byte is suppressed.
        self.assertEqual(spoken.strip(), "พบแล้วค่ะ")
        for leaked in ('"hits"', '"key"', "returned:", "{", "}",
                       "ada_remember{", PLATE_CONTENT, "people/kk"):
            self.assertNotIn(leaked, spoken, leaked)

        # The result is not lost — it travels on its own tool_result
        # event, never through the transcript stream.
        tool_results = [e for e in events if e.type == "tool_result"]
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(tool_results[0].data["name"], "ada_memory_search")

        # One leak_detected ops event per turn (first match reports).
        self.assertIn("leak_detected", [t[0] for t in ops])

        # The stored conversation line is clean too — nothing is
        # persisted that the transcript never showed.
        assistant = [t["text"] for t in provider.conversation.turns()
                     if t["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        for leaked in ('"hits"', "{", "returned:", "5ขว 6249"):
            self.assertNotIn(leaked, assistant[0], leaked)

    async def test_fully_leaked_turn_is_dead_not_silent(self):
        """A turn whose whole output is a spoken tool call is stripped to
        nothing — on a text-channel session (no audio) the dead-turn
        guard must still produce the result, not silence."""
        provider, ops = _provider()

        class AllLeakSession(_StormSession):
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
                        turn_complete=True))
                # The entire answer is the leaked call shape — after
                # stripping, the turn is dead (no audio on text channel).
                yield types.LiveServerMessage(
                    server_content=types.LiveServerContent(
                        output_transcription=types.Transcription(
                            text='ada_memory_search{query:"plate"}'),
                        turn_complete=True))
                if self.client_content:  # dead-turn retry nudge sent
                    yield types.LiveServerMessage(
                        server_content=types.LiveServerContent(
                            output_transcription=types.Transcription(
                                text="พบแล้วค่ะ ทะเบียน 5ขว 6249"),
                            turn_complete=True))
                self.provider._closed = True

        session = AllLeakSession(provider)
        provider._session = session
        events = [e async for e in provider.events()]
        spoken = _deltas(events)

        self.assertIn("dead_turn", [t[0] for t in ops])
        self.assertNotIn("{", spoken)
        self.assertNotIn("ada_memory_search{", spoken)
        # The retry — or the fallback — speaks the actual result.
        self.assertIn("5ขว 6249", spoken)


if __name__ == "__main__":
    unittest.main()
