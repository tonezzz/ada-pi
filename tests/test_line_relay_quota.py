"""Push-class + monthly-quota policy for the LINE relay
(card linepush-quota-audit).

class=bulk /send payloads park durably and flush bundled after
LINE_BULK_COALESCE_S (or ride the next inbound replyToken free); while a
confirmed 429 'monthly limit' block is set they never reach the LINE API
at all. Urgent payloads always attempt — a 200 clears the block.
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend import line_relay


class _R:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


def _relay(tmp):
    line_relay.PARKED_DIR = Path(tmp)
    relay = line_relay.LineRelay()
    return relay


def _parked(tmp, chat="U1"):
    p = Path(tmp) / f"{chat}.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines()
            if l.strip()]


class PushClass(unittest.TestCase):

    def test_default_urgent(self):
        self.assertEqual(line_relay._push_class({"text": "x"}), "urgent")

    def test_class_and_priority_alias(self):
        self.assertEqual(line_relay._push_class({"class": "bulk"}), "bulk")
        self.assertEqual(line_relay._push_class({"priority": "bulk"}),
                         "bulk")
        self.assertEqual(line_relay._push_class({"class": "BULK"}),
                         "bulk")

    def test_bulk_lanes_force(self):
        with patch.dict(os.environ, {"LINE_BULK_LANES": "digest,nag"}):
            self.assertEqual(
                line_relay._push_class({"instance": "nag"}), "bulk")
            self.assertEqual(
                line_relay._push_class({"instance": "ada"}), "urgent")
            # explicit class wins even off-list
            self.assertEqual(
                line_relay._push_class({"instance": "ada",
                                        "class": "bulk"}), "bulk")

    def test_is_monthly_limit(self):
        self.assertTrue(line_relay._is_monthly_limit(
            _R(429, '{"message":"You have reached your monthly limit."}')))
        self.assertFalse(line_relay._is_monthly_limit(_R(429, "slow down")))
        self.assertFalse(line_relay._is_monthly_limit(_R(500, "monthly")))


class QuotaBlock(unittest.TestCase):

    def test_monthly_429_sets_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _relay(tmp)
            relay._quota_alert = AsyncMock()
            relay.line = AsyncMock(return_value=_R(
                429, '{"message":"You have reached your monthly limit."}'))
            st = asyncio.run(relay._deliver(
                "U1", [{"type": "text", "text": "hi"}]))
            self.assertEqual(st, "parked")
            self.assertGreater(relay._quota_blocked, time.time())
            self.assertEqual(len(_parked(tmp)), 1)

    def test_bulk_parks_without_api_call_when_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _relay(tmp)
            relay._quota_blocked = time.time() + 3600
            relay.line = AsyncMock()
            st = asyncio.run(relay._deliver(
                "U1", [{"type": "text", "text": "digest"}], cls="bulk"))
            self.assertEqual(st, "parked")
            relay.line.assert_not_called()
            self.assertEqual(_parked(tmp)[0]["text"], "digest")

    def test_urgent_still_attempts_when_blocked_and_200_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _relay(tmp)
            relay._quota_blocked = time.time() + 3600
            relay.line = AsyncMock(return_value=_R(200))
            st = asyncio.run(relay._deliver(
                "U1", [{"type": "text", "text": "sos"}]))
            self.assertEqual(st, "delivered")
            self.assertEqual(relay._quota_blocked, 0.0)
            self.assertEqual(_parked(tmp), [])

    def test_rate_limit_429_does_not_set_monthly_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _relay(tmp)
            relay.line = AsyncMock(return_value=_R(429, "rate limited"))
            st = asyncio.run(relay._deliver(
                "U1", [{"type": "text", "text": "hi"}]))
            self.assertEqual(st, "parked")
            self.assertEqual(relay._quota_blocked, 0.0)


class BulkFlush(unittest.TestCase):

    def test_flush_bundles_parked_into_one_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _relay(tmp)
            calls = []

            async def fake_line(path, payload):
                calls.append((path, payload))
                return _R(200)
            relay.line = AsyncMock(side_effect=fake_line)
            line_relay._parked_add(
                "U1", [{"type": "text", "text": "a"},
                       {"type": "text", "text": "b"}])

            async def go():
                with patch.object(line_relay, "LINE_BULK_COALESCE_S",
                                  0.01):
                    relay._bulk_arm("U1")
                    await asyncio.sleep(0.2)
            asyncio.run(go())
            self.assertEqual(len(calls), 1)
            path, payload = calls[0]
            self.assertEqual(path, "push")
            # two text bubbles merged into one quota message
            self.assertEqual(len(payload["messages"]), 1)
            self.assertIn("a", payload["messages"][0]["text"])
            self.assertIn("b", payload["messages"][0]["text"])
            self.assertEqual(_parked(tmp), [])

    def test_flush_skips_when_quota_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _relay(tmp)
            relay._quota_blocked = time.time() + 3600
            relay.line = AsyncMock()
            line_relay._parked_add("U1", [{"type": "text", "text": "a"}])

            async def go():
                with patch.object(line_relay, "LINE_BULK_COALESCE_S",
                                  0.01):
                    relay._bulk_arm("U1")
                    await asyncio.sleep(0.2)
            asyncio.run(go())
            relay.line.assert_not_called()
            self.assertEqual(len(_parked(tmp)), 1)

    def test_arm_debounces(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = _relay(tmp)

            async def go():
                relay._bulk_arm("U1")
                t1 = relay._bulk_flush["U1"]
                relay._bulk_arm("U1")  # second arm inside window: same task
                self.assertIs(relay._bulk_flush["U1"], t1)
                t1.cancel()
            asyncio.run(go())


class SendEndpointClass(unittest.TestCase):
    """/send honors class=bulk: payload parks on disk and reports parked
    instead of synchronously pushing."""

    def _app(self, env=None):
        import uvicorn
        captured = {}

        class _Srv:
            def __init__(self, config):
                captured["app"] = config.app

            async def serve(self):
                return None

        relay = line_relay.LineRelay()
        relay._deliver = AsyncMock(return_value="delivered")
        relay.http = MagicMock()
        with patch.dict(os.environ, env or {}), \
                patch.object(line_relay, "CHANNEL_SECRET", "x"), \
                patch.object(uvicorn, "Server", _Srv):
            asyncio.run(relay._webhook_server())
        return captured["app"], relay

    def test_send_bulk_parks(self):
        from starlette.testclient import TestClient
        with tempfile.TemporaryDirectory() as tmp:
            line_relay.PARKED_DIR = Path(tmp)
            app, relay = self._app()
            relay._quota_blocked = time.time() + 3600
            client = TestClient(app)
            r = client.post("/send", json={"to": "U1", "text": "digest",
                                           "class": "bulk"})
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertTrue(body.get("parked"))
            relay._deliver.assert_not_called()
            self.assertEqual(_parked(tmp)[0]["text"], "digest")

    def test_send_urgent_delivers(self):
        from starlette.testclient import TestClient
        with tempfile.TemporaryDirectory() as tmp:
            line_relay.PARKED_DIR = Path(tmp)
            app, relay = self._app()
            client = TestClient(app)
            r = client.post("/send", json={"to": "U1", "text": "now"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])
            self.assertEqual(relay._deliver.await_args.args[0], "U1")
            self.assertEqual(_parked(tmp), [])

    def test_quota_route(self):
        from starlette.testclient import TestClient
        with tempfile.TemporaryDirectory() as tmp:
            line_relay.PARKED_DIR = Path(tmp)
            app, relay = self._app()
            relay._quota_probe = AsyncMock(
                return_value={"limit": 300, "used": 300, "remaining": 0})
            line_relay._parked_add("U1", [{"type": "text", "text": "a"},
                                          {"type": "text", "text": "b"}])
            client = TestClient(app)
            r = client.get("/quota")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["quota"]["remaining"], 0)
            self.assertEqual(body["parked"], {"U1": 2})
            self.assertFalse(body["quota_blocked"])


if __name__ == "__main__":
    unittest.main()
