"""Output-hygiene regressions (card ada-output-hygiene-tags).

Session 67b02417a8 (2026-10-09): Ada literally spoke "(ha_safety:
caution)" and "(kanban list review)" — parenthesized internal
tool/safety annotations reached speech. Session e9837ae922: she slipped
to English 3x mid-Thai-conversation ("ทำไมเปลี่ยนภาษา").

Under test:

1. sanitize_speech strips parenthesized internal annotations — the
   technical shape (>=2 all-lowercase [a-z0-9_] tokens joined by
   space/colon/comma) — from transcript text and vcast_say input, while
   natural-language parentheticals ("(draft)", "(I think)", Thai)
   survive.

2. The artifact-tail holdback keeps a '(' tag fragment from emitting
   mid-delta.

3. The language-drift guard: a Latin-dominant reply while the user is
   Thai-locked fires a language_drift ops event and injects a silent
   (turn_complete=False) language-lock context note — capped per session
   so a stubborn model can't grow the context.
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.genai import types  # noqa: E402

from backend.realtime_provider import (  # noqa: E402
    GeminiLiveProvider,
    _assistant_drifted_english,
    _count_scripts,
)
from backend.speech_sanitize import (  # noqa: E402
    sanitize_speech,
    split_artifact_tail,
)


def _provider():
    runner = MagicMock()
    runner.current_speaker_ha_person = None
    runner.execute = AsyncMock(return_value={"ok": True})
    provider = GeminiLiveProvider(
        tool_runner=runner, session_id="hygiene-t")
    ops: list[tuple] = []
    provider._emit_ops_event = lambda ev_type, detail, tool=None: (
        ops.append((ev_type, detail, tool)))
    return provider, ops


def _deltas(events) -> str:
    return "".join(
        e.data["text"] for e in events
        if e.type == "assistant_transcript_delta")


class InternalTagStripTests(unittest.TestCase):

    def test_observed_leaks(self):
        # The two verbatim leaks from session 67b02417a8.
        self.assertEqual(
            sanitize_speech("ได้เลยค่ะ (ha_safety: caution) เปิดไฟแล้วค่ะ"),
            "ได้เลยค่ะ เปิดไฟแล้วค่ะ")
        self.assertEqual(
            sanitize_speech("(kanban list review) เดี๋ยวดูให้ค่ะ"),
            " เดี๋ยวดูให้ค่ะ")

    def test_no_space_variant(self):
        self.assertEqual(
            sanitize_speech("note (ha_safety:caution) done"), "note done")

    def test_unclosed_tail_tag_drops(self):
        self.assertEqual(sanitize_speech("ok (ha_safety: caution"), "ok ")

    def test_natural_parentheticals_survive(self):
        # Single lowercase word, capitalized aside, Thai aside — none
        # are internal annotations.
        self.assertEqual(
            sanitize_speech("the file (draft) is ready"),
            "the file (draft) is ready")
        self.assertEqual(
            sanitize_speech("done (I think) yes"), "done (I think) yes")
        self.assertEqual(
            sanitize_speech("ตอบแล้ว (โอเค) ค่ะ"), "ตอบแล้ว (โอเค) ค่ะ")
        self.assertEqual(
            sanitize_speech("(system) keep this"), "(system) keep this")

    def test_paren_fragment_held(self):
        emit, hold = split_artifact_tail("abc (kanb")
        self.assertEqual((emit, hold), ("abc ", "(kanb"))
        emit, hold = split_artifact_tail("abc (")
        self.assertEqual(hold, "(")
        # A complete parenthetical is not a tail.
        self.assertEqual(
            split_artifact_tail("abc (draft) do"), ("abc (draft) do", ""))


class ProviderTagDeltaTests(unittest.TestCase):
    """A tag split across output-transcription deltas never reaches the
    transcript stream."""

    def setUp(self):
        self.p, _ = _provider()

    def drain(self, *deltas):
        out = "".join(self.p._strip_speech_artifacts(d) for d in deltas)
        out += self.p._flush_artifact_hold()
        return out

    def test_tag_split_across_deltas(self):
        out = self.drain("ได้เลยค่ะ (ha_safe", "ty: caution) เปิดไฟ")
        self.assertNotIn("ha_safety", out)
        self.assertNotIn("(", out)
        self.assertIn("เปิดไฟ", out)

    def test_unclosed_tag_at_turn_end_drops(self):
        out = self.drain("โอเคค่ะ (kanban list rev")
        self.assertNotIn("kanban", out)
        self.assertEqual(out.strip(), "โอเคค่ะ")


class DriftDetectorTests(unittest.TestCase):

    def test_count_scripts(self):
        # Combining vowel marks (ั ี) live in the Thai block too.
        self.assertEqual(_count_scripts("สวัสดี kanban"), (6, 6))
        self.assertEqual(_count_scripts(""), (0, 0))

    def test_english_clause_is_drift(self):
        self.assertTrue(_assistant_drifted_english(
            "Here is what is on the board right now for you."))

    def test_thai_reply_is_not_drift(self):
        self.assertFalse(_assistant_drifted_english(
            "เดี๋ยวเช็คบอร์ดให้ค่ะ มีการ์ด kanban สามใบพร้อมรีวิว"))

    def test_short_english_terms_not_drift(self):
        # A Thai sentence with English terms must not trip the guard.
        self.assertFalse(_assistant_drifted_english(
            "เปิดไฟแล้วค่ะ ใช้ Home Assistant"))


class _DriftSession:
    """A Thai user turn answered in English — the exact e9837ae922
    failure. After turn_complete the guard must inject a silent
    (turn_complete=False) language-lock note."""

    def __init__(self, provider):
        self.provider = provider
        self.client_content: list[dict] = []

    async def receive(self):
        yield types.LiveServerMessage(
            server_content=types.LiveServerContent(
                input_transcription=types.Transcription(
                    text="บอร์ดมีการ์ดอะไรรอรีวิวบ้าง")))
        yield types.LiveServerMessage(
            server_content=types.LiveServerContent(
                output_transcription=types.Transcription(
                    text="There are three cards waiting for review "
                         "on the board right now."),
                turn_complete=True))
        self.provider._closed = True

    async def send_client_content(self, **kwargs):
        self.client_content.append(kwargs)


class LanguageLockTests(unittest.IsolatedAsyncioTestCase):

    async def test_drift_locks_language_silently(self):
        provider, ops = _provider()
        session = _DriftSession(provider)
        provider._session = session

        events = [e async for e in provider.events()]

        # The English reply lands (transcript is post-hoc — the audio is
        # already out); the guard flags it for the audit…
        self.assertIn("three cards", _deltas(events))
        self.assertIn("language_drift", [t[0] for t in ops])
        self.assertTrue(provider._last_user_thai)
        self.assertEqual(provider._lang_locks_sent, 1)

        # …and re-locks silently: a user-role context note with
        # turn_complete=False — it must NOT prompt a reply.
        self.assertEqual(len(session.client_content), 1)
        note = session.client_content[0]
        self.assertFalse(note["turn_complete"])
        text = note["turns"].parts[0].text
        self.assertIn("Language lock", text)

    async def test_thai_reply_no_lock(self):
        provider, ops = _provider()

        class ThaiReply(_DriftSession):
            async def receive(self):
                yield types.LiveServerMessage(
                    server_content=types.LiveServerContent(
                        input_transcription=types.Transcription(
                            text="บอร์ดมีการ์ดอะไรรอรีวิวบ้าง")))
                yield types.LiveServerMessage(
                    server_content=types.LiveServerContent(
                        output_transcription=types.Transcription(
                            text="มีสามการ์ดรอรีวิวค่ะ เดี๋ยวสรุปให้นะคะ"),
                        turn_complete=True))
                self.provider._closed = True

        session = ThaiReply(provider)
        provider._session = session
        events = [e async for e in provider.events()]
        self.assertIn("สามการ์ด", _deltas(events))
        self.assertNotIn("language_drift", [t[0] for t in ops])
        self.assertEqual(provider._lang_locks_sent, 0)
        self.assertFalse(session.client_content)

    async def test_lock_note_capped_per_session(self):
        provider, _ops = _provider()
        session = _DriftSession(provider)
        provider._session = session
        provider._lang_locks_sent = 2  # cap reached earlier this session
        [e async for e in provider.events()]
        # Drift still flags, but no third context note grows the window.
        self.assertFalse(session.client_content)

    async def test_filler_turn_does_not_unlock_thai(self):
        provider, _ops = _provider()

        class FillerThenThai(_DriftSession):
            async def receive(self):
                # Turn 1: Thai question answered in Thai.
                yield types.LiveServerMessage(
                    server_content=types.LiveServerContent(
                        input_transcription=types.Transcription(
                            text="สวัสดีค่ะ วันนี้เป็นไงบ้าง")))
                yield types.LiveServerMessage(
                    server_content=types.LiveServerContent(
                        output_transcription=types.Transcription(
                            text="สบายดีค่ะ"),
                        turn_complete=True))
                # Turn 2: a bare "ok" — must not re-key the session to
                # English; the Thai lock survives filler.
                yield types.LiveServerMessage(
                    server_content=types.LiveServerContent(
                        input_transcription=types.Transcription(
                            text="ok")))
                yield types.LiveServerMessage(
                    server_content=types.LiveServerContent(
                        output_transcription=types.Transcription(
                            text="Let me check the board for you right "
                                 "now and report back."),
                        turn_complete=True))
                self.provider._closed = True

        session = FillerThenThai(provider)
        provider._session = session
        [e async for e in provider.events()]
        self.assertTrue(provider._last_user_thai)
        self.assertEqual(provider._lang_locks_sent, 1)


if __name__ == "__main__":
    unittest.main()
