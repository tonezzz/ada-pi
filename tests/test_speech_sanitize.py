"""Markup-artifact scrub for speech-bound text (card tts-artifact-clean).

Transcript 519088cb6d (2026-10-04): Ada spoke "&nbsp;ชัดเจน" and
"][พาสเจอร์ไรซ์]" — HTML entities and markdown remnants reached speech.
sanitize_speech strips them from the Live output-transcription stream
(via _strip_speech_artifacts + a partial-token holdback) and from
vcast_say text before synthesis.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.speech_sanitize import (  # noqa: E402
    ARTIFACT_TAIL_RE,
    sanitize_speech,
    split_artifact_tail,
)
from backend.realtime_provider import GeminiLiveProvider  # noqa: E402
from backend.tool_runner import ToolRunner  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixtures"
           / "tts-artifact-clean-transcript.txt")


class SanitizeSpeechTests(unittest.TestCase):

    def test_observed_transcript_artifacts(self):
        # The two verbatim leaks from transcript 519088cb6d.
        self.assertEqual(sanitize_speech("&nbsp;ชัดเจน"), " ชัดเจน")
        self.assertEqual(
            sanitize_speech("][พาสเจอร์ไรซ์]"), " พาสเจอร์ไรซ์")

    def test_html_entities(self):
        self.assertEqual(sanitize_speech("clear&nbsp;done"), "clear done")
        self.assertEqual(sanitize_speech("a &amp; b"), "a & b")
        self.assertEqual(sanitize_speech("5 &lt; 6"), "5 < 6")
        # Unknown entities are still markup junk — drop them.
        self.assertEqual(sanitize_speech("&fake; junk"), " junk")
        # ...but never decode legacy bare forms mid-word.
        self.assertEqual(
            sanitize_speech("b &not that"), "b &not that")

    def test_markdown_links_keep_link_text(self):
        self.assertEqual(
            sanitize_speech("see [the report](https://x) now"),
            "see the report now")
        self.assertEqual(
            sanitize_speech("image ![cat](http://x) here"),
            "image cat here")
        self.assertEqual(
            sanitize_speech("ref [a][1] done"), "ref a done")

    def test_emphasis_and_html_tags(self):
        self.assertEqual(
            sanitize_speech("**bold** and `code`"), "bold and code")
        self.assertEqual(
            sanitize_speech("a <b>tag</b> b"), "a tag b")

    def test_legit_ampersand_survives(self):
        self.assertEqual(sanitize_speech("R&D is fine"), "R&D is fine")
        self.assertEqual(sanitize_speech("R&D"), "R&D")

    def test_truncated_partial_entity_dropped(self):
        self.assertEqual(sanitize_speech("x&nbs"), "x")

    def test_fixture_is_clean(self):
        text = FIXTURE.read_text()
        self.assertNotRegex(text, r"&nbsp;")
        self.assertNotRegex(text, r"\]\[")


class TailSplitTests(unittest.TestCase):

    def test_partial_artifact_held(self):
        for frag in ("abc&nbs", "abc&", "a[lin", "a](htt", "a][re",
                     "a]", "a!"):
            emit, hold = split_artifact_tail(frag)
            self.assertTrue(hold, frag)
            self.assertFalse(ARTIFACT_TAIL_RE.search(emit), frag)

    def test_complete_text_emits_all(self):
        self.assertEqual(
            split_artifact_tail("a](x) done"), ("a](x) done", ""))
        self.assertEqual(
            split_artifact_tail("plain text"), ("plain text", ""))


class ProviderDeltaTests(unittest.TestCase):
    """_strip_speech_artifacts handles tokens split across deltas."""

    def setUp(self):
        self.p = GeminiLiveProvider()

    def drain(self, *deltas):
        out = "".join(self.p._strip_speech_artifacts(d) for d in deltas)
        out += self.p._flush_artifact_hold()
        return out

    def test_entity_split_across_deltas(self):
        out = self.drain("clear &nbs", "p;done")
        self.assertNotIn("&", out)
        self.assertIn("done", out)

    def test_link_split_across_deltas(self):
        out = self.drain("see [the rep", "ort](u) now")
        self.assertEqual(out, "see the report now")

    def test_bracket_junk_across_deltas(self):
        out = self.drain("ok][พาสเจอร์ไร", "ซ์] done")
        self.assertEqual(out, "ok พาสเจอร์ไรซ์ done")

    def test_plain_text_untouched(self):
        self.assertEqual(self.drain("R&D report"), "R&D report")
        self.assertEqual(self.drain("done!"), "done!")

    def test_leak_suppression_still_wins(self):
        # tool-call leak truncates the rest of the turn — the artifact
        # hold must not resurrect text after it.
        self.p._leak_active = True
        self.assertEqual(self.p._strip_tool_leak("anything"), "")


class VcastSaySanitizeTests(unittest.IsolatedAsyncioTestCase):

    async def test_vcast_say_text_is_sanitized(self):
        runner = ToolRunner(AsyncMock(), instance_id="test")
        pubbed = []

        def fake_vcast(path, payload=None):
            if path == "/pub":
                pubbed.append(payload)
                return {"ok": True}
            return {}

        with patch.object(ToolRunner, "_vcast_api",
                          staticmethod(fake_vcast)):
            await runner.vcast_say(
                1, "note: [flood report](http://x)&nbsp;done")
        self.assertEqual(
            pubbed[-1]["msg"]["text"], "note: flood report done")

    async def test_vcast_say_all_artifact_is_rejected(self):
        runner = ToolRunner(AsyncMock(), instance_id="test")
        with self.assertRaises(ValueError):
            await runner.vcast_say(1, "&nbsp;][ ][")


if __name__ == "__main__":
    unittest.main()
