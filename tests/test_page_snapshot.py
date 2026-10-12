"""Unit tests for backend/tools.d/page_snapshot.py.

httpx.AsyncClient and traffic_camera.publish_relay are faked — no real
playlived or input-bridge traffic. Covers url validation, the
session->navigate->shot->publish flow, the blank-shot guard,
publish-failure honesty, and manifest wiring.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "page_snapshot.py")
MANIFEST = (Path(__file__).resolve().parent.parent
            / "backend" / "tools.d" / "manifest.yml")


def _load():
    spec = importlib.util.spec_from_file_location(
        "tools_d.page_snapshot_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


snap = _load()

_PNG = base64.b64encode(b"\x89PNG" + b"\x00" * 2000).decode()


class FakeResp:
    def __init__(self, status, data=None, url_path="/"):
        self.status_code = status
        self._data = data or {}
        import httpx
        self.request = MagicMock()
        self.request.url.path = url_path
        self.text = "fake-error-body"

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=self.request,
                response=self)


class FakeClient:
    """Routes canned responses by request path."""

    def __init__(self, routes=None, fail=None, *a, **kw):
        self.routes = routes or {}
        self.fail = fail
        self.posts = []
        self.deleted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _route(self, url):
        best = None
        for pat, resp in self.routes.items():
            if url.endswith(pat) and (best is None or len(pat) > len(best)):
                best = pat
        return self.routes[best] if best else FakeResp(404, {}, url)

    async def post(self, url, **kw):
        self.posts.append((url, kw))
        if self.fail is not None:
            raise self.fail
        return self._route(url)

    async def delete(self, url, **kw):
        self.deleted.append(url)
        return FakeResp(200, {}, url)


def _ok_routes(png_b64=_PNG):
    return {
        "/sessions": FakeResp(200, {"ok": True, "session_id": "s1"}),
        "/navigate": FakeResp(200, {"ok": True, "title": "T",
                                    "url": "u"}),
        "/screenshot": FakeResp(200, {"ok": True, "base64": png_b64,
                                      "mimeType": "image/png"}),
    }


def _client(routes=None, fail=None):
    client = FakeClient(routes=routes, fail=fail)

    def factory(*a, **kw):
        return client
    return factory, client


class TestPageSnapshot(unittest.TestCase):
    def _run(self, routes=None, fail=None, **args):
        factory, client = _client(routes, fail)
        with patch.object(snap.httpx, "AsyncClient", factory), \
                patch("backend.traffic_camera.publish_relay",
                      return_value="https://pub/frame?token=x"):
            out = asyncio.run(snap.run(MagicMock(), **args))
        return out, client

    def test_happy_path(self):
        out, client = self._run(routes=_ok_routes(),
                                url="https://x.test/p.html")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["image_url"], "https://pub/frame?token=x")
        self.assertEqual(out["mime"], "image/png")
        self.assertIn("verbatim", out["note"])
        self.assertIn("/sessions/s1", client.deleted[0])

    def test_rejects_non_http(self):
        out, _ = self._run(url="sa-beach-photos")
        self.assertFalse(out["ok"])
        self.assertIn("http", out["error"])

    def test_playlived_500(self):
        routes = {"/sessions": FakeResp(500, {}, "/sessions")}
        out, _ = self._run(routes=routes, url="https://x.test/")
        self.assertFalse(out["ok"])
        self.assertIn("500", out["error"])

    def test_playlived_unreachable(self):
        out, _ = self._run(fail=RuntimeError("conn refused"),
                           url="https://x.test/")
        self.assertFalse(out["ok"])
        self.assertIn("unreachable", out["error"])

    def test_blank_shot_guard(self):
        tiny = base64.b64encode(b"\x89PNG").decode()
        out, _ = self._run(routes=_ok_routes(tiny),
                           url="https://x.test/")
        self.assertFalse(out["ok"])
        self.assertIn("blank", out["error"])

    def test_publish_failure_honest(self):
        factory, client = _client(_ok_routes())
        with patch.object(snap.httpx, "AsyncClient", factory), \
                patch("backend.traffic_camera.publish_relay",
                      return_value=None):
            out = asyncio.run(
                snap.run(MagicMock(), url="https://x.test/"))
        self.assertFalse(out["ok"])
        self.assertIn("publish", out["error"])

    def test_manifest_wired(self):
        man = yaml.safe_load(MANIFEST.read_text())
        entry = man["tools"]["page_snapshot"]
        self.assertEqual(entry["module"], "page_snapshot")
        self.assertEqual(entry["policy"], "read")

    def test_declaration_shape(self):
        d = snap.DECLARATION
        self.assertEqual(d["name"], "page_snapshot")
        self.assertIn("url", d["parameters"]["required"])
        self.assertFalse(d["parameters"]["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
