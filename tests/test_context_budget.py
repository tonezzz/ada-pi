"""Context-budget + role-marker-leak regression (card ada-context-budget).

Replays the session-d6cbf6e6e7 shape: ~78k input tokens/turn, 1.5M
cumulative in 12 minutes, and the model degenerated into emitting raw
scaffold role tags — 'ยืนยัน—user\nบันทึกเลยmodel' — fabricating the
user's reply inside Ada's own turn. The confirm gate held (publish
denied), but the fabricated text still reached the transcript.

The fix must (a) strip role-marker leak text before the transcript like
a tool-call leak, and (b) rotate the live session once per-turn input
tokens exceed the budget, clearing the resumption handle so the new
session starts with a clean window.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.realtime_provider import (
    GeminiLiveProvider,
    _GENERIC_TOOL_LEAK_RE,
    _ROLE_LEAK_RE,
)


def _provider() -> GeminiLiveProvider:
    # GeminiLiveProvider() without connect() — enough surface for the
    # sanitizer and usage hooks under test.
    return GeminiLiveProvider(instructions="test", session_id="t-ctx")


def _usage(in_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_token_count=in_tokens,
        response_token_count=10,
        candidates_token_count=None,
        prompt_tokens_details=[],
        response_tokens_details=[],
        candidates_tokens_details=[],
        cached_content_token_count=0,
        tool_use_prompt_token_count=0,
    )


class RoleLeakRegexTests(unittest.TestCase):
    def test_fabricated_user_turn_stripped(self):
        # The exact leaked tail: scaffold role tags after em-dash/newline.
        self.assertIsNotNone(_ROLE_LEAK_RE.search("ยืนยัน—user"))
        self.assertIsNotNone(_ROLE_LEAK_RE.search("บันทึกเลย\nmodel"))
        self.assertIsNotNone(
            _ROLE_LEAK_RE.search("ยืนยัน—user\nบันทึกเลยmodel"))

    def test_delta_that_is_only_a_tag(self):
        for tag in ("user", "model", "system", "assistant", "Model"):
            with self.subTest(tag=tag):
                self.assertIsNotNone(_ROLE_LEAK_RE.search(tag))
                self.assertIsNotNone(_ROLE_LEAK_RE.search("\n" + tag))

    def test_legit_speech_not_stripped(self):
        for text in (
            "the model is a Honda CR-V",
            "model ของรถคันนี้คือ CR-V ค่ะ",
            "the user asked about the car",
            "user interface looks fine",
            "ใช้ model ตัวใหม่นะคะ",   # mid-sentence
            "assistant coach — nice",
            "system ของบ้านทำงานดีค่ะ",
            "— user experience is good",  # em-dash + two-word tail: kept
        ):
            with self.subTest(text=text):
                self.assertIsNone(_ROLE_LEAK_RE.search(text))


class RoleLeakSanitizerTests(unittest.TestCase):
    def test_role_leak_suppresses_rest_of_turn(self):
        p = _provider()
        p._emit_ops_event = MagicMock()
        out = p._strip_tool_leak("ยืนยันไหมคะ—user\nบันทึกเลยmodel")
        self.assertEqual(out, "ยืนยันไหมคะ")
        self.assertTrue(p._leak_active)
        # Subsequent deltas in the same turn are suppressed entirely.
        self.assertEqual(p._strip_tool_leak("anything after"), "")
        kinds = [c.args[0] for c in p._emit_ops_event.call_args_list]
        self.assertIn("leak_detected", kinds)

    def test_plain_thai_passes(self):
        p = _provider()
        self.assertEqual(
            p._strip_tool_leak("ยืนยันไหมคะ จะบันทึกเลยไหม"),
            "ยืนยันไหมคะ จะบันทึกเลยไหม")
        self.assertFalse(p._leak_active)


class ContextBudgetTests(unittest.TestCase):
    def test_over_budget_schedules_one_rotate(self):
        p = _provider()
        p._context_turn_budget = 45_000
        p._emit_ops_event = MagicMock()
        p._record_usage(_usage(30_000))
        self.assertFalse(p._rotate_scheduled)
        p._record_usage(_usage(78_365))   # session d6cbf6e6e7 scale
        self.assertTrue(p._rotate_scheduled)
        # second over-budget turn does not re-schedule
        p._record_usage(_usage(80_000))
        kinds = [c.args[0] for c in p._emit_ops_event.call_args_list]
        self.assertEqual(kinds.count("context_rotate"), 1)

    def test_budget_default_from_env(self):
        import os
        with patch.dict(os.environ, {"ADA_CONTEXT_TURN_BUDGET": "12345"}):
            p = _provider()
            self.assertEqual(p._context_turn_budget, 12345)


if __name__ == "__main__":
    unittest.main()
