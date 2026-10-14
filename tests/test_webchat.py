"""webchat-lane-boards (2026-10-14): the boards WP chat screen.

Covers the server side of the web channel:

- webchat_deliver pushes a chat_push frame to every live session bound
  (?channel=web&chat_id=) to the target chat, and parks offline pushes
  (OPS-0651 pattern — quota-free, offline is the only failure).
- _webchat_flush_parked drains the park queue into a connecting ws.
- POST /api/webchat/send is the relay-style loopback endpoint.
- chat_send channel=web routes to the web deliver path with the
  in-context chat default, same as the relay channels.
"""
from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pwa_server
from backend.tool_runner import ToolRunner
from backend.tool_runner.common import SESSION_CHAT, _CALLER_SESSION


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.fail = False

    async def send_text(self, data):
        if self.fail:
            raise RuntimeError("ws closed")
        self.sent.append(json.loads(data))


class _Req:
    """Request stub — only .json() is read by api_webchat_send."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _runner():
    ha = AsyncMock()
    ha.base_url = "http://test:8123"
    r = ToolRunner(ha, instance_id="kk")
    r.session_id = None
    return r


class WebchatDeliverTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._live = dict(pwa_server._live_sessions)
        pwa_server._live_sessions.clear()
        pwa_server._WEBCHAT_PARKED.clear()
        SESSION_CHAT.clear()

    def tearDown(self):
        pwa_server._live_sessions.clear()
        pwa_server._live_sessions.update(self._live)
        pwa_server._WEBCHAT_PARKED.clear()
        SESSION_CHAT.clear()

    def _bind(self, sid, chat_id="web-kk"):
        ws = _FakeWS()
        SESSION_CHAT[sid] = {"channel": "web", "chat_id": chat_id}
        pwa_server._live_sessions[sid] = {
            "ws": ws, "connected_at": 1.0, "client": "test"}
        return ws

    async def test_delivers_to_bound_live_session(self):
        ws = self._bind("s1")
        res = await pwa_server.webchat_deliver(
            "web-kk", text="hello", image_url="http://x/i.jpg")
        self.assertTrue(res["ok"])
        self.assertEqual(res["delivered"], 1)
        self.assertEqual(ws.sent, [{
            "type": "chat_push", "text": "hello",
            "image_url": "http://x/i.jpg", "video_url": ""}])
        self.assertNotIn("web-kk", pwa_server._WEBCHAT_PARKED)

    async def test_offline_parks_for_next_connect(self):
        res = await pwa_server.webchat_deliver("web-kk", text="ping")
        self.assertTrue(res["ok"])
        self.assertEqual(res["delivered"], 0)
        self.assertTrue(res["parked"])
        self.assertEqual(len(pwa_server._WEBCHAT_PARKED["web-kk"]), 1)

    async def test_wrong_channel_or_chat_never_receives(self):
        ws = self._bind("s1", chat_id="web-tony")     # wrong chat_id
        line_ws = _FakeWS()
        SESSION_CHAT["s2"] = {"channel": "line", "chat_id": "web-kk"}
        pwa_server._live_sessions["s2"] = {
            "ws": line_ws, "connected_at": 1.0}     # wrong channel
        res = await pwa_server.webchat_deliver("web-kk", text="hi")
        self.assertEqual(res["delivered"], 0)
        self.assertEqual(ws.sent, [])
        self.assertEqual(line_ws.sent, [])

    async def test_flush_parked_on_connect(self):
        await pwa_server.webchat_deliver("web-kk", text="one")
        await pwa_server.webchat_deliver("web-kk", text="two")
        ws = _FakeWS()
        sent = await pwa_server._webchat_flush_parked("web-kk", ws)
        self.assertEqual(sent, 2)
        self.assertEqual([m["text"] for m in ws.sent], ["one", "two"])
        self.assertNotIn("web-kk", pwa_server._WEBCHAT_PARKED)

    async def test_flush_keeps_remainder_on_dead_ws(self):
        await pwa_server.webchat_deliver("web-kk", text="kept")
        ws = _FakeWS()
        ws.fail = True
        sent = await pwa_server._webchat_flush_parked("web-kk", ws)
        self.assertEqual(sent, 0)
        self.assertEqual(len(pwa_server._WEBCHAT_PARKED["web-kk"]), 1)

    async def test_chat_id_required(self):
        res = await pwa_server.webchat_deliver("", text="x")
        self.assertFalse(res["ok"])


class WebchatEndpointTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        pwa_server._WEBCHAT_PARKED.clear()

    def tearDown(self):
        pwa_server._WEBCHAT_PARKED.clear()

    async def _call(self, body):
        with patch.object(pwa_server, "_require_api_key", lambda r: None):
            return await pwa_server.api_webchat_send(_Req(body))

    async def test_send_parks_offline_push(self):
        res = await self._call({"chat_id": "web-kk", "text": "hi"})
        self.assertTrue(res["ok"])
        self.assertTrue(res["parked"])

    async def test_to_alias_and_validation(self):
        res = await self._call({"to": "web-kk",
                                "image_url": "http://x/i.jpg"})
        self.assertTrue(res["ok"])
        with self.assertRaises(pwa_server.HTTPException) as cm:
            await self._call({"text": "no chat"})
        self.assertEqual(cm.exception.status_code, 422)
        with self.assertRaises(pwa_server.HTTPException) as cm:
            await self._call({"chat_id": "web-kk"})
        self.assertEqual(cm.exception.status_code, 422)


class ChatSendWebTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        SESSION_CHAT.clear()

    def tearDown(self):
        SESSION_CHAT.clear()

    async def test_run_routes_web_and_defaults_to_bound_chat(self):
        r = _runner()
        SESSION_CHAT["s1"] = {"channel": "web", "chat_id": "web-kk"}
        r._chat_send_web = AsyncMock(
            return_value={"ok": True, "delivered": 1})
        tok = _CALLER_SESSION.set("s1")
        try:
            await r._chat_send_run(
                "job-1", channel="web", text="hi", image_url="",
                video_url="", camera="", to="")
        finally:
            _CALLER_SESSION.reset(tok)
        r._chat_send_web.assert_awaited_once_with(
            "web-kk", text="hi", image_url="", video_url="")

    async def test_explicit_to_wins(self):
        r = _runner()
        r._chat_send_web = AsyncMock(
            return_value={"ok": True, "delivered": 0, "parked": True})
        await r._chat_send_run(
            "job-1", channel="web", text="hi", image_url="",
            video_url="", camera="", to="web-nhong")
        r._chat_send_web.assert_awaited_once_with(
            "web-nhong", text="hi", image_url="", video_url="")

    async def test_web_deliver_in_process(self):
        r = _runner()
        with patch.object(pwa_server, "webchat_deliver",
                          AsyncMock(return_value={"ok": True,
                                                  "delivered": 1})
                          ) as mock_del:
            res = await r._chat_send_web(
                "web-kk", text="hi", image_url="", video_url="")
        self.assertTrue(res["ok"])
        mock_del.assert_awaited_once_with(
            "web-kk", text="hi", image_url="", video_url="")

    async def test_web_deliver_loopback_fallback(self):
        r = _runner()
        posts = []

        class _Resp:
            def read(self, *a):
                return json.dumps({"ok": True, "delivered": 1}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _capture(req, timeout=0):
            posts.append({"url": req.full_url,
                          "body": json.loads(req.data.decode()),
                          "key": req.headers.get("X-api-key")})
            return _Resp()

        with patch.dict(sys.modules, {"pwa_server": None}), \
                patch("urllib.request.urlopen", side_effect=_capture):
            res = await r._chat_send_web(
                "web-kk", text="hi", image_url="http://x/i.jpg",
                video_url="")
        self.assertTrue(res["ok"])
        self.assertEqual(posts[0]["url"],
                         "http://127.0.0.1:8002/api/webchat/send")
        self.assertEqual(posts[0]["body"]["chat_id"], "web-kk")
        self.assertEqual(posts[0]["body"]["image_url"],
                         "http://x/i.jpg")

    async def test_no_chat_id_errors(self):
        r = _runner()
        res = await r._chat_send_web("", text="hi",
                                     image_url="", video_url="")
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
