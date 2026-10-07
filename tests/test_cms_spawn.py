"""Unit tests for POST /api/cms/spawn (report→session loop §1b).

The route is invoked as a coroutine with a mocked Request — the app is
imported for real (module-level FastAPI wiring), MDDB page reads and the
board-api HTTP call are faked. No network.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ADA_INSTANCE_ID", "test")
os.environ.setdefault("GEMINI_API_KEY", "x")

import httpx
from fastapi import HTTPException

import pwa_server
from backend import board_client


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeClient:
    """httpx.AsyncClient stand-in recording requests; keyed canned replies."""

    def __init__(self, routes=None, fail=None, *a, **kw):
        self.routes = routes or {}
        self.fail = fail
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.fail is not None:
            raise self.fail
        for key, resp in self.routes.items():
            if url.endswith(key):
                return resp
        return FakeResp(404, {"error": "not found"})


PAGE = {"slug": "flood-report", "title": "Flood — Bangkok Situation",
        "format": "markdown", "lang": "en", "content": "# flood\n"}

KEYS = {
    "viewer": {"key": "view-only-key", "device": "*", "apps": ["view"]},
    "tony": {"key": "tony-dispatch-key", "device": "*",
             "apps": ["view", "dispatch"]},
    "legacy": {"key": "legacy-key", "device": "*"},   # no apps -> voice+chat
}


def _request(key=None, body=None):
    req = MagicMock()
    req.headers = {"x-api-key": key} if key else {}
    req.query_params = {}
    req.cookies = {}
    req.json = AsyncMock(
        return_value=body if body is not None else {"slug": "flood-report"})
    return req


class SpawnTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._keys_tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False)
        json.dump(KEYS, self._keys_tmp)
        self._keys_tmp.close()
        self._env = patch.dict(os.environ, {
            "ADA_KEYS_FILE": self._keys_tmp.name,
            "ADA_API_KEY": "",
            "ADA_API_KEYS": "",
        })
        self._env.start()
        self._page_mock = AsyncMock(return_value=dict(PAGE))
        self._page = patch.object(
            pwa_server.tool_runner, "cms_get_page", new=self._page_mock)
        self._page.start()
        self.addCleanup(self._page.stop)
        self.addCleanup(self._env.stop)
        self.addCleanup(os.unlink, self._keys_tmp.name)

    async def _spawn(self, **kw):
        return await pwa_server.cms_spawn_session(_request(**kw))

    async def test_no_key_401(self):
        with self.assertRaises(HTTPException) as cm:
            await self._spawn()
        self.assertEqual(cm.exception.status_code, 401)

    async def test_view_only_key_403_no_board_call(self):
        client = FakeClient()
        with patch.object(board_client.httpx, "AsyncClient",
                          return_value=client):
            with self.assertRaises(HTTPException) as cm:
                await self._spawn(key="view-only-key")
        self.assertEqual(cm.exception.status_code, 403)
        self.assertIn("dispatch", cm.exception.detail)
        self.assertEqual(client.calls, [])          # never reaches board
        self._page_mock.assert_not_awaited()         # gate precedes read

    async def test_legacy_key_403(self):
        with self.assertRaises(HTTPException) as cm:
            await self._spawn(key="legacy-key")
        self.assertEqual(cm.exception.status_code, 403)

    async def test_spawn_posts_armed_card(self):
        client = FakeClient({"/card": FakeResp(
            200, {"ok": True,
                  "message": "card flood-bangkok-a1b2c3 created in backlog"})})
        with patch.object(board_client.httpx, "AsyncClient",
                          return_value=client):
            out = await self._spawn(key="tony-dispatch-key")
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "queued")
        self.assertEqual(out["card_id"], "flood-bangkok-a1b2c3")
        self.assertTrue(out["board_url"].endswith("/apps/board/"))
        method, url, kw = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/apps/board-api/card"))
        body = kw["json"]
        self.assertEqual(body["title"], "Flood — Bangkok Situation")
        self.assertEqual(body["report"], "flood-report")
        self.assertEqual(body["action"],
                         {"type": "dispatch", "repo": "chaba"})
        self.assertTrue(body["queue"])
        self.assertEqual(body["on_exists"], "queue")
        self.assertEqual(body["tags"], ["report-session-loop", "cms"])
        self.assertIn("flood-report", body["brief"])

    async def test_env_admin_key_is_unscoped(self):
        with patch.dict(os.environ, {"ADA_API_KEY": "admin-key"}):
            client = FakeClient({"/card": FakeResp(
                200, {"ok": True, "message": "card x-y1z2 created in backlog"})})
            with patch.object(board_client.httpx, "AsyncClient",
                              return_value=client):
                out = await self._spawn(key="admin-key")
        self.assertTrue(out["ok"])

    async def test_page_not_found_404(self):
        self._page_mock.return_value = None
        client = FakeClient()
        with patch.object(board_client.httpx, "AsyncClient",
                          return_value=client):
            with self.assertRaises(HTTPException) as cm:
                await self._spawn(key="tony-dispatch-key")
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(client.calls, [])

    async def test_bad_slug_422(self):
        with self.assertRaises(HTTPException) as cm:
            await self._spawn(key="tony-dispatch-key",
                              body={"slug": "Bad Slug!"})
        self.assertEqual(cm.exception.status_code, 422)

    async def test_board_unreachable_502(self):
        client = FakeClient(fail=httpx.ConnectError("refused"))
        with patch.object(board_client.httpx, "AsyncClient",
                          return_value=client):
            with self.assertRaises(HTTPException) as cm:
                await self._spawn(key="tony-dispatch-key")
        self.assertEqual(cm.exception.status_code, 502)

    async def test_board_409_passes_through(self):
        client = FakeClient({"/card": FakeResp(
            409, {"error": "card already running"})})
        with patch.object(board_client.httpx, "AsyncClient",
                          return_value=client):
            with self.assertRaises(HTTPException) as cm:
                await self._spawn(key="tony-dispatch-key")
        self.assertEqual(cm.exception.status_code, 409)

    async def test_already_active_status(self):
        client = FakeClient({"/card": FakeResp(
            200, {"ok": True, "message": "already running"})})
        with patch.object(board_client.httpx, "AsyncClient",
                          return_value=client):
            out = await self._spawn(key="tony-dispatch-key")
        self.assertEqual(out["status"], "already_active")


class BoardClientTest(unittest.IsolatedAsyncioTestCase):

    async def test_post_preserves_error_body(self):
        client = FakeClient({"/card": FakeResp(400, {"error": "title required"})})
        with patch.object(board_client.httpx, "AsyncClient",
                          return_value=client):
            status, data = await board_client.post("/card", {"title": ""})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "title required")

    async def test_post_unreachable_zero(self):
        client = FakeClient(fail=httpx.ConnectError("refused"))
        with patch.object(board_client.httpx, "AsyncClient",
                          return_value=client):
            status, data = await board_client.post("/card", {})
        self.assertEqual(status, 0)
        self.assertIn("error", data)

    def test_board_page_url(self):
        with patch.dict(os.environ,
                        {"ADA_BOARD_API_URL":
                         "https://x.ts.net/apps/board-api"}):
            self.assertEqual(board_client.board_page_url(),
                             "https://x.ts.net/apps/board/")
        with patch.dict(os.environ,
                        {"ADA_BOARD_API_URL": "http://127.0.0.1:8787"}):
            self.assertTrue(board_client.board_page_url()
                            .endswith("/apps/board/"))


if __name__ == "__main__":
    unittest.main()
