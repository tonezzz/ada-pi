import unittest

from google.genai import types

from backend.realtime_provider import GeminiLiveProvider


class TextTurnSession:
    """TEXT-modality reply: model_turn parts carry .text, no inline audio."""

    def __init__(self, provider: GeminiLiveProvider) -> None:
        self.provider = provider

    async def receive(self):
        yield types.LiveServerMessage(
            server_content=types.LiveServerContent(
                model_turn=types.Content(
                    parts=[types.Part(text="Hello"), types.Part(text=" there.")],
                ),
                turn_complete=True,
            )
        )
        self.provider._closed = True


class TextChannelTests(unittest.IsolatedAsyncioTestCase):
    def test_channel_marks_text_only(self) -> None:
        self.assertTrue(GeminiLiveProvider(channel="telegram").text_only)
        self.assertTrue(GeminiLiveProvider(channel="line").text_only)
        self.assertFalse(GeminiLiveProvider().text_only)

    def test_text_instructions_drop_avatar_face(self) -> None:
        p = GeminiLiveProvider(channel="telegram")
        self.assertNotIn("set_facial_expression", p.instructions)
        self.assertIn("text chat session", p.instructions)
        p = GeminiLiveProvider()
        self.assertIn("set_facial_expression", p.instructions)

    async def test_text_parts_emit_transcript_deltas(self) -> None:
        provider = GeminiLiveProvider(channel="telegram")
        provider._session = TextTurnSession(provider)
        events = [event async for event in provider.events()]
        kinds = [e.type for e in events]
        self.assertEqual(
            kinds,
            ["response_started", "assistant_transcript_delta",
             "assistant_transcript_delta", "response_completed"],
        )
        text = "".join(
            e.data["text"] for e in events
            if e.type == "assistant_transcript_delta"
        )
        self.assertEqual(text, "Hello there.")


if __name__ == "__main__":
    unittest.main()
