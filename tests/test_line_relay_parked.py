"""Parked-outbound queue for LINE push-quota (OPS-0651).

A push rejected with 429 parks the payload under LINE_PARKED_DIR; the
next inbound's free replyToken flushes parked msgs merged ahead of the
turn's own reply. Reply failure restores the parked batch untouched.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend import line_relay


class _R:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


def _relay(tmp):
    line_relay.PARKED_DIR = Path(tmp)
    relay = line_relay.LineRelay()
    return relay


class ParkedQueue(unittest.TestCase):

    def test_push_429_parks(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _relay(tmp)
            relay.line = AsyncMock(return_value=_R(429, "monthly limit"))
            asyncio.run(relay._deliver("U1", [{"type": "text", "text": "hi"}]))
            parked = list(Path(tmp).glob("*.jsonl"))
            self.assertEqual(len(parked), 1)
            lines = parked[0].read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["text"], "hi")

    def test_push_200_no_park(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _relay(tmp)
            relay.line = AsyncMock(return_value=_R(200))
            asyncio.run(relay._deliver("U1", [{"type": "text", "text": "hi"}]))
            self.assertEqual(list(Path(tmp).glob("*.jsonl")), [])

    def test_parked_rides_next_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _relay(tmp)
            line_relay._parked_add("U1", [{"type": "text", "text": "old1"},
                                          {"type": "image", "u": 1}])
            calls = []

            async def fake_line(path, payload):
                calls.append((path, payload))
                return _R(200)
            relay.line = AsyncMock(side_effect=fake_line)
            asyncio.run(relay._deliver(
                "U1", [{"type": "text", "text": "new"}],
                reply_token="RT"))
            self.assertEqual(len(calls), 1)
            path, payload = calls[0]
            self.assertEqual(path, "reply")
            self.assertEqual(payload["replyToken"], "RT")
            msgs = payload["messages"]
            # order preserved: parked text, parked image, current text
            kinds = [m["type"] for m in msgs]
            self.assertEqual(kinds, ["text", "image", "text"])
            self.assertIn("old1", msgs[0]["text"])
            self.assertIn("new", msgs[2]["text"])
            # queue consumed
            self.assertEqual(list(Path(tmp).glob("*.jsonl")), [])

    def test_reply_failure_restores_parked(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _relay(tmp)
            line_relay._parked_add("U1", [{"type": "text", "text": "old"}])
            seq = [_R(500, "reply err"), _R(200)]

            async def fake_line(path, payload):
                return seq.pop(0)
            relay.line = AsyncMock(side_effect=fake_line)
            asyncio.run(relay._deliver(
                "U1", [{"type": "text", "text": "new"}],
                reply_token="RT"))
            # parked msg restored (push of the CURRENT msg succeeded, no park)
            parked = Path(tmp) / "U1.jsonl"
            lines = parked.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["text"], "old")

    def test_overflow_reparks(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _relay(tmp)
            # 6 parked media msgs + 1 new text -> reply takes 5, rest park
            line_relay._parked_add(
                "U1", [{"type": "image", "n": i} for i in range(6)])
            sent = []

            async def fake_line(path, payload):
                sent.append(payload["messages"])
                return _R(200)
            relay.line = AsyncMock(side_effect=fake_line)
            asyncio.run(relay._deliver(
                "U1", [{"type": "image", "n": 99}], reply_token="RT"))
            self.assertEqual(len(sent[0]), 5)
            parked = Path(tmp) / "U1.jsonl"
            self.assertEqual(len(parked.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
