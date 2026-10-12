"""Unit tests for backend/input_polish — the noise-gated typed-lane polish.

Gate parity with chaba scripts/micro-bench noise_dict is the contract:
same select/skip behavior the bench measured.
"""

import unittest
from unittest.mock import AsyncMock, patch

from backend import input_polish


class ShouldPolishTests(unittest.TestCase):
    def test_clean_english_skipped(self):
        ok, reason = input_polish.should_polish("turn on the kitchen lights")
        self.assertFalse(ok)
        self.assertIn("clean", reason)

    def test_typo_selected(self):
        ok, reason = input_polish.should_polish("trun on the kicthen light")
        self.assertTrue(ok)
        self.assertIn("unknown-ratio", reason)

    def test_gibberish_selected(self):
        ok, _ = input_polish.should_polish("asdkfj alskdjf qwioe")
        self.assertTrue(ok)

    def test_mixed_script_selected(self):
        ok, reason = input_polish.should_polish("เปิด light ในครัว")
        self.assertTrue(ok)
        self.assertIn("mixed-script", reason)

    def test_thai_dominant_selected(self):
        ok, reason = input_polish.should_polish("เปิดไฟครัวหน่อย")
        self.assertTrue(ok)
        self.assertIn("thai", reason)

    def test_system_note_skipped(self):
        ok, reason = input_polish.should_polish(
            "(system) The user just spoke but you produced no reply")
        self.assertFalse(ok)
        self.assertIn("system", reason)

    def test_too_long_skipped(self):
        ok, reason = input_polish.should_polish("x " * 500 + "qwioe")
        self.assertFalse(ok)
        self.assertIn("long", reason)

    def test_real_word_mishearing_passes_gate(self):
        # documented blind spot — 'kitten lice' are real words; only ASR
        # confidence could catch this class. Lock the behavior in.
        ok, _ = input_polish.should_polish("turn on the kitten lice")
        self.assertFalse(ok)


class _Resp:
    def __init__(self, text):
        self.text = text


class MaybePolishTests(unittest.IsolatedAsyncioTestCase):
    async def test_flag_off_passthrough(self):
        with patch.object(input_polish, "_ENABLED", False):
            out = await input_polish.maybe_polish("trun on teh light")
        self.assertEqual(out, "trun on teh light")

    async def test_clean_input_never_calls_model(self):
        gen = AsyncMock()
        with patch.object(input_polish, "_ENABLED", True), \
                patch("backend.gemini_pool.generate", gen):
            out = await input_polish.maybe_polish("turn on the kitchen light")
        self.assertEqual(out, "turn on the kitchen light")
        gen.assert_not_called()

    async def test_noisy_input_polished(self):
        gen = AsyncMock(return_value=_Resp("turn on the kitchen light"))
        with patch.object(input_polish, "_ENABLED", True), \
                patch("backend.gemini_pool.generate", gen):
            out = await input_polish.maybe_polish("trun on the kicthen light")
        self.assertEqual(out, "turn on the kitchen light")
        gen.assert_awaited_once()

    async def test_translation_drift_rejected(self):
        # weak-polish failure mode from the bench: EN -> TH translation.
        gen = AsyncMock(return_value=_Resp("เปิดไฟครัว"))
        with patch.object(input_polish, "_ENABLED", True), \
                patch("backend.gemini_pool.generate", gen):
            out = await input_polish.maybe_polish("trun on the kicthen light")
        self.assertEqual(out, "trun on the kicthen light")

    async def test_empty_polish_passthrough(self):
        gen = AsyncMock(return_value=_Resp("  "))
        with patch.object(input_polish, "_ENABLED", True), \
                patch("backend.gemini_pool.generate", gen):
            out = await input_polish.maybe_polish("trun on the kicthen light")
        self.assertEqual(out, "trun on the kicthen light")

    async def test_model_failure_fails_open(self):
        gen = AsyncMock(side_effect=RuntimeError("quota"))
        with patch.object(input_polish, "_ENABLED", True), \
                patch("backend.gemini_pool.generate", gen):
            out = await input_polish.maybe_polish("trun on the kicthen light")
        self.assertEqual(out, "trun on the kicthen light")

    async def test_system_note_never_calls_model(self):
        gen = AsyncMock()
        with patch.object(input_polish, "_ENABLED", True), \
                patch("backend.gemini_pool.generate", gen):
            out = await input_polish.maybe_polish("(system) stall note")
        self.assertEqual(out, "(system) stall note")
        gen.assert_not_called()
