"""Unit tests for backend/tools.d/ada_board_write.py.

httpx is faked — no network. Covers happy paths, error honesty, the
respond owner-gate, and manifest wiring.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from backend import tools_loader

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "ada_board_write.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "tools_d.ada_board_write_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


board = _load_module()


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


def _runner(owner=True):
    r = MagicMock()
    r.policy_identity.return_value = "person.tony"
    r.banks.policy_for.return_value = {"full": True} if owner else {}
    return r


def _client(routes=None, fail=None):
    def factory(*a, **kw):
        return FakeClient(routes=routes, fail=fail)
    return factory


class CommentTest(unittest.IsolatedAsyncioTestCase):

    async def test_comment_posts_as_ada(self):
        client = FakeClient({"/comment": FakeResp(200, {"ok": True,
                                                      "message": "comment added"})})
        with patch.object(board.httpx, "AsyncClient", return_value=client):
            out = await board.run(_runner(), action="comment",
                                  id="kanban-selftest", text="hello board")
        self.assertTrue(out["ok"])
        method, url, kw = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/apps/board-api/comment"))
        self.assertEqual(kw["json"]["from"], "ada")
        self.assertEqual(kw["json"]["id"], "kanban-selftest")
        self.assertEqual(kw["json"]["text"], "hello board")

    async def test_comment_requires_text(self):
        with patch.object(board.httpx, "AsyncClient",
                          side_effect=AssertionError("no http expected")):
            out = await board.run(_runner(), action="comment",
                                  id="kanban-selftest", text="  ")
        self.assertFalse(out["ok"])
        self.assertIn("text", out["error"])

    async def test_comment_rejects_bad_card_id(self):
        with patch.object(board.httpx, "AsyncClient",
                          side_effect=AssertionError("no http expected")):
            out = await board.run(_runner(), action="comment",
                                  id="../etc/passwd", text="x")
        self.assertFalse(out["ok"])

    async def test_comment_surfaces_server_error(self):
        client = FakeClient({"/comment": FakeResp(400, {"error": "no card nope"})})
        with patch.object(board.httpx, "AsyncClient", return_value=client):
            out = await board.run(_runner(), action="comment",
                                  id="nope", text="x")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "no card nope")

    async def test_unreachable_is_honest(self):
        client = FakeClient(fail=httpx.ConnectError("refused"))
        with patch.object(board.httpx, "AsyncClient", return_value=client):
            out = await board.run(_runner(), action="comment",
                                  id="kanban-selftest", text="x")
        self.assertFalse(out["ok"])
        self.assertIn("couldn't reach", out["error"])


class RespondTest(unittest.IsolatedAsyncioTestCase):

    async def test_respond_owner_succeeds(self):
        client = FakeClient({"/respond": FakeResp(200, {"ok": True,
                                                      "message": "answer saved"})})
        with patch.object(board.httpx, "AsyncClient", return_value=client):
            out = await board.run(_runner(owner=True), action="respond",
                                  id="c1", request_id="r1", answer="do A")
        self.assertTrue(out["ok"])
        self.assertIn("note", out)  # tony-attribution caveat surfaced
        method, url, kw = client.calls[0]
        self.assertEqual(kw["json"]["request_id"], "r1")
        self.assertEqual(kw["json"]["answer"], "do A")

    async def test_respond_non_owner_denied(self):
        with patch.object(board.httpx, "AsyncClient",
                          side_effect=AssertionError("no http expected")):
            out = await board.run(_runner(owner=False), action="respond",
                                  id="c1", request_id="r1", answer="x")
        self.assertFalse(out["ok"])
        self.assertIn("owner", out["error"])

    async def test_respond_requires_request_id_and_answer(self):
        with patch.object(board.httpx, "AsyncClient",
                          side_effect=AssertionError("no http expected")):
            out = await board.run(_runner(), action="respond", id="c1",
                                  request_id="", answer="x")
            self.assertFalse(out["ok"])
            out = await board.run(_runner(), action="respond", id="c1",
                                  request_id="r1", answer=" ")
            self.assertFalse(out["ok"])


class ReadTest(unittest.IsolatedAsyncioTestCase):

    PAYLOAD = {
        "columns": [{"id": "backlog"}, {"id": "doing"}, {"id": "done"}],
        "cards": [
            {"id": "a", "title": "card A", "column": "doing",
             "updated": "2026-10-04 10:00",
             "requests": [{"id": "r1", "status": "open"}],
             "comms": [{"from": "devin", "text": "working on it"}]},
            {"id": "b", "title": "card B", "column": "backlog",
             "updated": "2026-10-04 12:00"},
            {"id": "c", "title": "card C", "column": "done",
             "updated": "2026-10-03 09:00",
             "requests": [{"id": "r2", "status": "answered"}]},
        ],
    }

    async def test_read_compacts_board(self):
        client = FakeClient({"/cards": FakeResp(200, dict(self.PAYLOAD))})
        with patch.object(board.httpx, "AsyncClient", return_value=client):
            out = await board.run(_runner(), action="read")
        self.assertTrue(out["ok"])
        self.assertEqual(out["columns"], {"doing": 1, "backlog": 1,
                                         "done": 1})
        self.assertEqual(out["open_requests"], 1)
        self.assertEqual(out["total"], 3)
        # sorted by updated desc
        self.assertEqual([c["id"] for c in out["cards"]], ["b", "a", "c"])
        a = next(c for c in out["cards"] if c["id"] == "a")
        self.assertEqual(a["open_requests"], 1)
        self.assertEqual(a["last_comm"], "devin: working on it")
        self.assertNotIn("open_requests",
                         next(c for c in out["cards"] if c["id"] == "b"))

    async def test_read_column_filter_and_limit(self):
        payload = dict(self.PAYLOAD)
        client = FakeClient({"/cards": FakeResp(200, payload)})
        with patch.object(board.httpx, "AsyncClient", return_value=client):
            out = await board.run(_runner(), action="read",
                                  column="doing", limit=5)
        self.assertEqual([c["id"] for c in out["cards"]], ["a"])
        self.assertEqual(out["total"], 1)
        # columns counts are still the whole board
        self.assertEqual(out["columns"]["backlog"], 1)

    async def test_read_unreachable_is_honest(self):
        client = FakeClient(fail=httpx.ConnectError("refused"))
        with patch.object(board.httpx, "AsyncClient", return_value=client):
            out = await board.run(_runner(), action="read")
        self.assertFalse(out["ok"])
        self.assertIn("couldn't reach", out["error"])


class MiscTest(unittest.IsolatedAsyncioTestCase):

    async def test_unknown_action(self):
        client = FakeClient()
        with patch.object(board.httpx, "AsyncClient", return_value=client):
            out = await board.run(_runner(), action="explode")
        self.assertFalse(out["ok"])
        self.assertIn("unknown action", out["error"])

    async def test_base_url_env_override(self):
        client = FakeClient({"/comment": FakeResp(200, {"ok": True})})
        with patch.object(board.httpx, "AsyncClient", return_value=client), \
                patch.dict("os.environ",
                           {"ADA_BOARD_API_URL": "http://127.0.0.1:8787"}):
            out = await board.run(_runner(), action="comment",
                                  id="c", text="x")
        self.assertTrue(out["ok"])
        self.assertEqual(client.calls[0][1],
                         "http://127.0.0.1:8787/comment")


class ManifestWiringTest(unittest.TestCase):

    def test_tool_loads_from_real_manifest(self):
        reg = tools_loader.load()  # real backend/tools.d
        self.assertIn("ada_board_write", reg.tools, reg.errors)
        tool = reg.tools["ada_board_write"]
        self.assertEqual(tool.policy, "read")
        self.assertFalse(tool.secondary_allowed)
        self.assertEqual(tool.declaration["name"], "ada_board_write")


if __name__ == "__main__":
    unittest.main()
