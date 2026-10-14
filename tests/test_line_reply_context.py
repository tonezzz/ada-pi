"""line-tool-reply-routing (2026-10-13): tool replies stay in the
originating chat.

Before this fix a bare chat_send had no notion of WHICH chat the turn
came from — the relay /send fell back to _recent_chat (keyed by persona
name while readers query by ADA_INSTANCE_ID — dead map), then
LINE_LANE_DEFAULT, then the first allowed user: answers landed in the
lane's default room or the owner's DM instead of the asking chat.

Now the relay binds each backend session to its originating chat
(&chat_id= on the ws connect), the runner records it in SESSION_CHAT,
and chat_send defaults `to` to that chat. Explicit to= still wins and
LINE_LANE_ALLOW/LOCK vet the bound target like any other.
"""
from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend import line_relay
from backend.tool_runner import ToolRunner
from backend.tool_runner.common import (
    SESSION_CHAT, _CALLER_SESSION, session_chat_id)


class _Resp:
    """urllib.request.urlopen stand-in — context manager + read()."""

    status = 200

    def __init__(self, payload=None):
        self._b = json.dumps(payload or {"ok": True}).encode()

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _runner():
    ha = AsyncMock()
    ha.base_url = "http://test:8123"
    r = ToolRunner(ha, instance_id="kk")
    r.session_id = None
    return r


class SessionChatBindingTests(unittest.TestCase):
    """common.SESSION_CHAT — the session -> originating-chat registry."""

    def setUp(self):
        SESSION_CHAT.clear()

    def tearDown(self):
        SESSION_CHAT.clear()

    def test_bound_session_resolves_chat_per_channel(self):
        SESSION_CHAT["s1"] = {"channel": "line", "chat_id": "C822x"}
        r = _runner()
        tok = _CALLER_SESSION.set("s1")
        try:
            self.assertEqual(session_chat_id(r, "line"), "C822x")
            # The LINE chat id never leaks into a telegram send.
            self.assertEqual(session_chat_id(r, "telegram"), "")
        finally:
            _CALLER_SESSION.reset(tok)

    def test_unbound_and_unknown_sessions_return_empty(self):
        r = _runner()
        self.assertEqual(session_chat_id(r, "line"), "")
        SESSION_CHAT["s1"] = {"channel": "line", "chat_id": "C822x"}
        tok = _CALLER_SESSION.set("other")
        try:
            self.assertEqual(session_chat_id(r, "line"), "")
        finally:
            _CALLER_SESSION.reset(tok)

    def test_runner_field_is_the_fallback_scope(self):
        # REST/test callers never set _CALLER_SESSION — the runner's own
        # session_id still binds the send to its session's chat.
        r = _runner()
        r.session_id = "s9"
        SESSION_CHAT["s9"] = {"channel": "line", "chat_id": "Udm9"}
        self.assertEqual(session_chat_id(r, "line"), "Udm9")


class ChatSendContextTests(unittest.IsolatedAsyncioTestCase):
    """chat_send defaults `to` to the session's originating chat."""

    def setUp(self):
        SESSION_CHAT.clear()

    def tearDown(self):
        SESSION_CHAT.clear()

    async def _run_send(self, runner, to=""):
        posts = []

        def _capture(req, timeout=0):
            posts.append({"url": req.full_url,
                          "body": json.loads(req.data.decode())})
            return _Resp({"ok": True})

        with patch("urllib.request.urlopen", side_effect=_capture):
            await runner._chat_send_run(
                "job-1", channel="line", text="hi", image_url="",
                video_url="", camera="", to=to)
        return posts[0]

    async def test_bare_send_answers_in_originating_chat(self):
        r = _runner()
        SESSION_CHAT["s1"] = {"channel": "line", "chat_id": "C822x"}
        tok = _CALLER_SESSION.set("s1")
        try:
            send = await self._run_send(r)
        finally:
            _CALLER_SESSION.reset(tok)
        self.assertEqual(send["url"], "http://127.0.0.1:8912/send")
        self.assertEqual(send["body"]["to"], "C822x")
        self.assertEqual(send["body"]["chat_id"], "C822x")

    async def test_explicit_to_wins_over_binding(self):
        r = _runner()
        SESSION_CHAT["s1"] = {"channel": "line", "chat_id": "C822x"}
        tok = _CALLER_SESSION.set("s1")
        try:
            send = await self._run_send(r, to="Uother9")
        finally:
            _CALLER_SESSION.reset(tok)
        self.assertEqual(send["body"]["to"], "Uother9")

    async def test_unbound_session_sends_no_context(self):
        r = _runner()
        send = await self._run_send(r)
        self.assertEqual(send["body"]["to"], "")


class RelayWsBindingTests(unittest.IsolatedAsyncioTestCase):
    """The relay hands the originating chat to the backend at connect."""

    async def test_ensure_url_carries_chat_id(self):
        relay = line_relay.LineRelay()
        sess = line_relay.ChatSession(
            "U1", relay, ws_url="ws://127.0.0.1:8007/ws",
            caller=None, chat_id="C822x")

        class _WS:
            def __aiter__(self):
                async def _g():
                    return
                    yield  # pragma: no cover
                return _g()

            async def send(self, data):
                pass

            async def close(self):
                pass

        urls = []

        async def _connect(url, **kw):
            urls.append(url)
            return _WS()

        with patch.object(line_relay.websockets, "connect", _connect):
            await sess.ensure()
        self.assertEqual(len(urls), 1)
        self.assertIn("channel=line", urls[0])
        self.assertIn("chat_id=C822x", urls[0])
        await sess.close()

    async def test_group_turn_binds_room_chat(self):
        """_session() must pass the room chat_id — in a room the session
        is keyed (chat_id, sender, persona) and its tool sends answer in
        that room."""
        relay = line_relay.LineRelay()

        class _S:
            def __init__(self, user_id, relay, ws_url=None, caller=None,
                         chat_id=""):
                self.chat_id = chat_id
                self.ws_url = ws_url

        with patch.object(line_relay, "ChatSession", _S):
            sess = relay._session(("C822x", "Ukk1", "kaewta"), "Ukk1",
                                  "ws://x/ws", "user-kk", "C822x")
        self.assertEqual(sess.chat_id, "C822x")


class SendEndpointContextTests(unittest.TestCase):
    """/send records deliveries under the instance key so the in-context
    fallback (_recent_chat) is keyed the same way readers query it —
    ADA_INSTANCE_ID, not persona name."""

    def test_send_records_recent_chat_by_instance(self):
        import uvicorn
        from starlette.testclient import TestClient

        captured = {}

        class _Srv:
            def __init__(self, config):
                captured["app"] = config.app

            async def serve(self):
                return None

        env = {"LINE_LANE_DEFAULT": "kk:C822x",
               "LINE_ALLOWED_USER_IDS": "Utony9"}
        relay = line_relay.LineRelay()
        relay._deliver = AsyncMock()
        relay.http = MagicMock()
        with patch.dict(os.environ, env), \
                patch.object(line_relay, "CHANNEL_SECRET", "x"), \
                patch.object(uvicorn, "Server", _Srv):
            asyncio.run(relay._webhook_server())
            client = TestClient(captured["app"])
            r = client.post("/send", json={"instance": "kk",
                                           "text": "hello"})
            self.assertEqual(r.status_code, 200)
            # Lane default resolved it, and the delivery keyed the
            # fallback map by INSTANCE so the next bare send lands there.
            self.assertEqual(relay._deliver.await_args.args[0], "C822x")
            self.assertEqual(relay._recent_chat["kk"][0], "C822x")
            # A second bare send reuses the delivered-to chat — never
            # the owner-DM fallback.
            relay._deliver.reset_mock()
            r = client.post("/send", json={"instance": "kk",
                                           "text": "again"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(relay._deliver.await_args.args[0], "C822x")


if __name__ == "__main__":
    unittest.main()
