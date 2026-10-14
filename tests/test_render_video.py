"""Unit tests for backend/tools.d/ada_render_video.py.

httpx.AsyncClient and traffic_camera.publish_relay are faked — no
network, no real Veo call. Covers the submit->poll->download->publish
flow, aspect/secs mapping, ref_url resolution, the operation= resume
path, pending timeout honesty, and manifest wiring.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

try:
    import google.genai  # noqa: F401
except ImportError:
    # Dep-less CI host — document_check's import chain needs the module
    # to exist, not to work (no calls reach it in these tests).
    import sys as _sys
    import types as _types
    _genai = _types.ModuleType("google.genai")
    _genai_types = _types.ModuleType("google.genai.types")
    _genai.types = _genai_types
    _sys.modules.setdefault("google.genai", _genai)
    _sys.modules.setdefault("google.genai.types", _genai_types)
    try:
        import google as _google
        _google.genai = _genai
    except ImportError:
        pass

from backend import tools_loader  # noqa: E402
from backend import document_check  # noqa: E402

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "ada_render_video.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "tools_d.ada_render_video_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


render = _load_module()

OP_NAME = "models/veo-3.1-fast-generate-preview/operations/abc123"
VIDEO_URI = ("https://generativelanguage.googleapis.com/v1beta/files/"
             "vid1:download?alt=media")
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 800
JPEG_BYTES = b"\xff\xd8jpeg-fake\xff\xd9"


class FakeResp:
    def __init__(self, status=200, payload=None, content=b"",
                 content_type="application/json"):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.content = content
        self.headers = {"content-type": content_type}

    def json(self):
        return self._payload


def _submit_resp(name=OP_NAME, status=200, payload=None):
    return FakeResp(status, payload if payload is not None
                    else {"name": name})


def _op_pending():
    return FakeResp(200, {"name": OP_NAME, "done": False})


def _op_done(uri=VIDEO_URI, filtered=None):
    gvr = {"generatedSamples": [{"video": {"uri": uri}}]}
    if filtered:
        gvr["raiMediaFilteredReasons"] = filtered
    return FakeResp(200, {"name": OP_NAME, "done": True,
                          "response": {"generateVideoResponse": gvr}})


class FakeClient:
    """httpx.AsyncClient stand-in: canned post, get answers pop from
    get_queue then fall back to get_resp (last wins for downloads)."""

    def __init__(self, post_resp=None, get_resp=None, get_queue=None,
                 fail=None, *a, **kw):
        self.post_resp = post_resp
        self.get_resp = get_resp
        self.get_queue = list(get_queue or [])
        self.fail = fail
        self.posts = []
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        self.posts.append((url, kw))
        if self.fail is not None:
            raise self.fail
        return self.post_resp or FakeResp(500, {"error": "unset"})

    async def get(self, url, **kw):
        self.gets.append((url, kw))
        if self.fail is not None:
            raise self.fail
        if self.get_queue:
            return self.get_queue.pop(0)
        return self.get_resp or FakeResp(404, {"error": "not found"})


def _runner():
    return MagicMock()


def _client(post_resp=None, get_resp=None, get_queue=None, fail=None):
    client = FakeClient(post_resp=post_resp, get_resp=get_resp,
                        get_queue=get_queue, fail=fail)

    def factory(*a, **kw):
        return client
    return factory, client


class RenderVideoTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Keep the render_job board log inert: an empty env means
        # "not configured" so no test can post to a real board even on
        # a host that has wp-boards.env. RenderJobWpLogTest patches it
        # back per-test.
        self._wp_env = patch("backend.wp_client.env", return_value={})
        self._wp_env.start()
        self.addCleanup(self._wp_env.stop)
        self.env = {"GEMINI_API_KEY": "test-key",
                    "ADA_RENDER_DIR": self.tmp.name,
                    "ADA_RENDER_VIDEO_OPEN": "1",
                    "LINE_MEDIA_ARCHIVE_DIR": self.tmp.name + "/arch",
                    "ADA_RELAY_PUBLIC_BASE": "https://x/relay-img",
                    "ADA_RENDER_VIDEO_POLL_S": "0"}

    async def test_res_reaches_submit_params(self):
        factory, client = _client(
            post_resp=_submit_resp(),
            get_queue=[_op_done(),
                       FakeResp(200, content=MP4_BYTES,
                                content_type="video/mp4")])
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env), \
                patch("backend.traffic_camera.publish_relay",
                      return_value="https://x/frame?screen=0&token=r"):
            out = await render.run(_runner(), prompt="p", res="1080p")
        self.assertTrue(out["ok"], out)
        params = client.posts[0][1]["json"]["parameters"]
        self.assertEqual(params["resolution"], "1080p")

    async def test_res_1080p_portrait_is_honest_error(self):
        with patch.dict("os.environ", {"ADA_RENDER_VIDEO_OPEN": "1"}), \
                patch.object(render.httpx, "AsyncClient",
                          side_effect=AssertionError("no http expected")):
            out = await render.run(_runner(), prompt="p", res="1080p",
                                   aspect="9:16")
        self.assertFalse(out["ok"])
        self.assertIn("landscape", out["error"])

    async def test_closed_lane_refuses(self):
        with patch.dict("os.environ",
                        {"ADA_RENDER_VIDEO_OPEN": "0"}):
            out = await render.run(_runner(), prompt="a boat")
        self.assertFalse(out["ok"])
        self.assertIn("owner-only", out["error"])

    async def test_happy_path_returns_playable_url(self):
        factory, client = _client(
            post_resp=_submit_resp(),
            get_queue=[_op_pending(), _op_done(),
                       FakeResp(200, content=MP4_BYTES,
                                content_type="video/mp4")])
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env), \
                patch("backend.traffic_camera.publish_relay",
                      return_value="https://pub.example/api/input-bridge/"
                                   "frame?screen=0&token=render:boat-1") as pub:
            out = await render.run(_runner(), prompt="a paper boat")
        self.assertTrue(out["ok"])
        # arch publish is primary (no size cap); /frame is fallback
        self.assertIn("/relay-img/arch/renders/", out["video_url"])
        self.assertTrue(out["video_url"].endswith(".mp4"))
        self.assertTrue(Path(out["file"]).is_file())
        self.assertEqual(Path(out["file"]).read_bytes(), MP4_BYTES)
        self.assertEqual(Path(out["file"]).suffix, ".mp4")
        self.assertEqual(out["secs"], 8)
        self.assertEqual(out["aspect"], "16:9")
        self.assertEqual(out["model"], "veo-3.1-fast-generate-preview")
        # arch publish succeeded — the /frame fallback never ran
        pub.assert_not_called()
        # submit shape: predictLongRunning, api key header, instances
        url, kw = client.posts[0]
        self.assertTrue(url.endswith(
            "/v1beta/models/veo-3.1-fast-generate-preview:"
            "predictLongRunning"))
        self.assertEqual(kw["headers"]["x-goog-api-key"], "test-key")
        body = kw["json"]
        self.assertEqual(body["instances"][0]["prompt"], "a paper boat")
        self.assertEqual(body["parameters"]["aspectRatio"], "16:9")
        self.assertEqual(body["parameters"]["durationSeconds"], 8)
        # poll GETs hit the operation URL with the api key
        poll_url, poll_kw = client.gets[0]
        self.assertTrue(poll_url.endswith(f"/v1beta/{OP_NAME}"))
        self.assertEqual(poll_kw["headers"]["x-goog-api-key"], "test-key")
        # download GET hits the file uri with the api key
        dl_url, dl_kw = client.gets[-1]
        self.assertEqual(dl_url, VIDEO_URI)
        self.assertEqual(dl_kw["headers"]["x-goog-api-key"], "test-key")

    async def test_secs_and_aspect_mapping(self):
        factory, client = _client(
            post_resp=_submit_resp(),
            get_queue=[_op_done(),
                       FakeResp(200, content=MP4_BYTES)])
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env), \
                patch("backend.traffic_camera.publish_relay",
                      return_value="https://x/frame?screen=0&token=r"):
            out = await render.run(_runner(), prompt="p", secs="4",
                                   aspect="portrait")
        self.assertTrue(out["ok"])
        params = client.posts[0][1]["json"]["parameters"]
        self.assertEqual(params["durationSeconds"], 4)
        self.assertEqual(params["aspectRatio"], "9:16")

    async def test_secs_invalid_is_honest_error(self):
        with patch.dict("os.environ", {"ADA_RENDER_VIDEO_OPEN": "1"}), \
                patch.object(render.httpx, "AsyncClient",
                          side_effect=AssertionError("no http expected")):
            out = await render.run(_runner(), prompt="p", secs=5)
        self.assertFalse(out["ok"])
        self.assertIn("4, 6, or 8", out["error"])

    async def test_aspect_square_is_honest_error(self):
        with patch.dict("os.environ", {"ADA_RENDER_VIDEO_OPEN": "1"}), \
                patch.object(render.httpx, "AsyncClient",
                          side_effect=AssertionError("no http expected")):
            out = await render.run(_runner(), prompt="p", aspect="1:1")
        self.assertFalse(out["ok"])
        self.assertIn("no square", out["error"])

    async def test_prompt_required(self):
        with patch.dict("os.environ", {"ADA_RENDER_VIDEO_OPEN": "1"}), \
                patch.object(render.httpx, "AsyncClient",
                          side_effect=AssertionError("no http expected")):
            out = await render.run(_runner())
        self.assertFalse(out["ok"])
        self.assertIn("prompt", out["error"])

    async def test_missing_api_key_is_honest(self):
        with patch.dict("os.environ",
                        {"ADA_RENDER_DIR": self.tmp.name,
                         "ADA_RENDER_VIDEO_OPEN": "1"},
                        clear=False), \
                patch.dict("os.environ") as env:
            env.pop("GEMINI_API_KEY", None)
            env.pop("GOOGLE_API_KEY", None)
            out = await render.run(_runner(), prompt="a boat")
        self.assertFalse(out["ok"])
        self.assertIn("GEMINI_API_KEY", out["error"])

    async def test_submit_http_error_is_honest(self):
        factory, _ = _client(post_resp=_submit_resp(
            status=429, payload={"error": {"message": "quota exceeded"}}))
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env):
            out = await render.run(_runner(), prompt="a boat")
        self.assertFalse(out["ok"])
        self.assertIn("HTTP 429", out["error"])
        self.assertIn("quota", out["error"])

    async def test_submit_no_operation_name_is_honest(self):
        factory, _ = _client(post_resp=FakeResp(200, {}))
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env):
            out = await render.run(_runner(), prompt="a boat")
        self.assertFalse(out["ok"])
        self.assertIn("no operation name", out["error"])

    async def test_poll_timeout_returns_pending_operation(self):
        env = dict(self.env)
        env["ADA_RENDER_VIDEO_WAIT_S"] = "0.001"
        factory, _ = _client(post_resp=_submit_resp(),
                             get_resp=_op_pending())
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", env):
            out = await render.run(_runner(), prompt="a boat")
        self.assertFalse(out["ok"])
        self.assertTrue(out["pending"])
        self.assertEqual(out["operation"], OP_NAME)
        self.assertIn("still rendering", out["error"])

    async def test_operation_resume_skips_submit(self):
        factory, client = _client(
            get_queue=[_op_done(),
                       FakeResp(200, content=MP4_BYTES)])
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env), \
                patch("backend.traffic_camera.publish_relay",
                      return_value="https://x/frame?screen=0&token=r"):
            out = await render.run(_runner(), operation=OP_NAME)
        self.assertTrue(out["ok"])
        self.assertEqual(client.posts, [])  # no new render billed
        self.assertIn("video_url", out)

    async def test_operation_bad_shape_rejected(self):
        with patch.dict("os.environ", {"ADA_RENDER_VIDEO_OPEN": "1"}), \
                patch.object(render.httpx, "AsyncClient",
                          side_effect=AssertionError("no http expected")):
            out = await render.run(_runner(), operation="not-an-op")
        self.assertFalse(out["ok"])
        self.assertIn("operation", out["error"])

    async def test_op_error_field_is_honest(self):
        factory, _ = _client(
            post_resp=_submit_resp(),
            get_queue=[FakeResp(200, {
                "name": OP_NAME, "done": True,
                "error": {"message": "content policy violation"}})])
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env):
            out = await render.run(_runner(), prompt="a boat")
        self.assertFalse(out["ok"])
        self.assertIn("render failed", out["error"])
        self.assertIn("policy", out["error"])

    async def test_done_without_clip_is_honest(self):
        factory, _ = _client(
            post_resp=_submit_resp(),
            get_queue=[FakeResp(200, {
                "name": OP_NAME, "done": True,
                "response": {"generateVideoResponse": {
                    "generatedSamples": []}}})])
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env):
            out = await render.run(_runner(), prompt="a boat")
        self.assertFalse(out["ok"])
        self.assertIn("no clip", out["error"])

    async def test_filtered_reason_surfaced(self):
        factory, _ = _client(
            post_resp=_submit_resp(),
            get_queue=[_op_done(filtered=["audio blocked by policy"]),
                       FakeResp(200, content=MP4_BYTES)])
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env), \
                patch("backend.traffic_camera.publish_relay",
                      return_value="https://x/frame?screen=0&token=r"):
            out = await render.run(_runner(), prompt="a boat")
        self.assertTrue(out["ok"])
        self.assertIn("audio blocked", out["note"])

    async def test_download_failure_is_honest(self):
        factory, _ = _client(
            post_resp=_submit_resp(),
            get_queue=[_op_done(), FakeResp(502, {})])
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env):
            out = await render.run(_runner(), prompt="a boat")
        self.assertFalse(out["ok"])
        self.assertIn("download", out["error"])
        self.assertEqual(out["operation"], OP_NAME)

    async def test_publish_failure_keeps_artifact_and_errors(self):
        factory, _ = _client(
            post_resp=_submit_resp(),
            get_queue=[_op_done(), FakeResp(200, content=MP4_BYTES)])
        env = dict(self.env)
        env["LINE_MEDIA_ARCHIVE_DIR"] = "/proc/no-such-dir-x"
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", env), \
                patch("backend.traffic_camera.publish_relay",
                      return_value=None):
            out = await render.run(_runner(), prompt="a boat")
        self.assertFalse(out["ok"])
        self.assertTrue(Path(out["file"]).is_file())
        self.assertIn("publish", out["error"])

    async def test_ref_url_intake_key_uses_engine(self):
        held = document_check._Held(
            pdf=b"pdf", preview=b"prev", meta={"archive_jpg": JPEG_BYTES})
        factory, client = _client(
            post_resp=_submit_resp(),
            get_queue=[_op_done(), FakeResp(200, content=MP4_BYTES)])
        engine = MagicMock()
        engine.held.return_value = held
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env), \
                patch("backend.document_check.engine",
                      return_value=engine), \
                patch("backend.traffic_camera.publish_relay",
                      return_value="https://x/frame?screen=0&token=r"):
            out = await render.run(
                _runner(), prompt="animate it",
                ref_url="doc/20261008-120000-photo-ab12cd")
        self.assertTrue(out["ok"])
        engine.held.assert_called_once_with(
            "doc/20261008-120000-photo-ab12cd")
        inst = client.posts[0][1]["json"]["instances"][0]
        self.assertEqual(inst["image"]["mimeType"], "image/jpeg")
        self.assertEqual(base64.b64decode(
            inst["image"]["bytesBase64Encoded"]), JPEG_BYTES)

    async def test_ref_url_http_fetch(self):
        factory, client = _client(
            post_resp=_submit_resp(),
            get_queue=[FakeResp(200, content=b"\x89PNG-ref",
                                content_type="image/png"),
                       _op_done(),
                       FakeResp(200, content=MP4_BYTES)])
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env), \
                patch("backend.traffic_camera.publish_relay",
                      return_value="https://x/frame?screen=0&token=r"):
            out = await render.run(
                _runner(), prompt="animate it",
                ref_url="https://example.com/ref.png")
        self.assertTrue(out["ok"])
        self.assertEqual(client.gets[0][0], "https://example.com/ref.png")
        inst = client.posts[0][1]["json"]["instances"][0]
        self.assertEqual(base64.b64decode(
            inst["image"]["bytesBase64Encoded"]), b"\x89PNG-ref")
        self.assertEqual(inst["image"]["mimeType"], "image/png")

    async def test_ref_url_non_image_rejected(self):
        factory, _ = _client(
            get_resp=FakeResp(200, content=b"<html>",
                              content_type="text/html"))
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env):
            out = await render.run(_runner(), prompt="animate it",
                                   ref_url="https://example.com/page")
        self.assertFalse(out["ok"])
        self.assertIn("isn't an image", out["error"])

    async def test_ref_url_bad_scheme_rejected(self):
        with patch.object(render.httpx, "AsyncClient",
                          side_effect=AssertionError("no http expected")):
            out = await render.run(_runner(), prompt="x",
                                   ref_url="file:///etc/passwd")
        self.assertFalse(out["ok"])


WP_ENV = {"WP_SITE_URL": "https://b.example/wp", "WP_APP_USER": "u",
          "WP_APP_PASSWORD": "p"}


async def _drain_wp():
    """Let fire-and-forget render_job posts run to completion."""
    tasks = list(render._WP_TASKS)
    if tasks:
        await asyncio.gather(*tasks)


def _wp_patches(create):
    """Stub wp_client so render_job logging sees a configured board."""
    return (patch("backend.wp_client.env", return_value=dict(WP_ENV)),
            patch("backend.wp_client.configured", return_value=True),
            patch("backend.wp_client.create_post", create))


class RenderJobWpLogTest(unittest.IsolatedAsyncioTestCase):
    """render_job board logging — best-effort, never blocks the render."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"GEMINI_API_KEY": "test-key",
                    "ADA_RENDER_DIR": self.tmp.name,
                    "ADA_RENDER_VIDEO_OPEN": "1",
                    "LINE_MEDIA_ARCHIVE_DIR": self.tmp.name + "/arch",
                    "ADA_RELAY_PUBLIC_BASE": "https://x/relay-img",
                    "ADA_RENDER_VIDEO_POLL_S": "0"}

    async def test_success_posts_render_job_meta(self):
        factory, _ = _client(
            post_resp=_submit_resp(),
            get_queue=[_op_done(),
                       FakeResp(200, content=MP4_BYTES,
                                content_type="video/mp4")])
        create = AsyncMock(return_value={"ok": True, "id": 7,
                                         "link": "https://b/7"})
        p_env, p_cfg, p_create = _wp_patches(create)
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env), \
                p_env, p_cfg, p_create:
            out = await render.run(_runner(), prompt="a paper boat")
            await _drain_wp()
        self.assertTrue(out["ok"])
        create.assert_awaited_once()
        args, kw = create.await_args
        self.assertEqual(kw["post_type"], "render_job")
        title = args[2]
        self.assertRegex(
            title, r"^Render — a paper boat \d{4}-\d{2}-\d{2}$")
        meta = kw["meta"]
        self.assertEqual(meta["kind"], "video")
        self.assertEqual(meta["prompt"], "a paper boat")
        self.assertEqual(meta["model"], "veo-3.1-fast-generate-preview")
        self.assertEqual(meta["secs"], 8)
        self.assertEqual(meta["aspect"], "16:9")
        self.assertEqual(meta["res"], "720p")
        self.assertEqual(meta["ref_url"], "")
        self.assertEqual(meta["operation"], OP_NAME)
        self.assertGreaterEqual(meta["duration_s"], 0)
        self.assertTrue(meta["host"])
        self.assertEqual(meta["backend"], "veo")
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(meta["output_url"], out["video_url"])
        self.assertEqual(meta["source"], "ada_render_video")
        self.assertTrue(meta["requested_by"])
        self.assertAlmostEqual(meta["est_cost_usd"], 0.80)
        self.assertEqual(meta["parent_job"], "")
        self.assertEqual(meta["chain_index"], 0)
        self.assertNotIn("error", meta)

    async def test_failure_posts_error_meta(self):
        factory, _ = _client(post_resp=_submit_resp(
            status=429, payload={"error": {"message": "quota exceeded"}}))
        create = AsyncMock(return_value={"ok": True})
        p_env, p_cfg, p_create = _wp_patches(create)
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env), \
                p_env, p_cfg, p_create:
            out = await render.run(_runner(), prompt="a boat")
            await _drain_wp()
        self.assertFalse(out["ok"])
        create.assert_awaited_once()
        meta = create.await_args.kwargs["meta"]
        self.assertEqual(meta["status"], "error")
        self.assertIn("HTTP 429", meta["error"])
        self.assertEqual(meta["output_url"], "")
        self.assertEqual(meta["operation"], "")

    async def test_pending_posts_pending_status(self):
        env = dict(self.env)
        env["ADA_RENDER_VIDEO_WAIT_S"] = "0.001"
        factory, _ = _client(post_resp=_submit_resp(),
                             get_resp=_op_pending())
        create = AsyncMock(return_value={"ok": True})
        p_env, p_cfg, p_create = _wp_patches(create)
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", env), \
                p_env, p_cfg, p_create:
            out = await render.run(_runner(), prompt="a boat")
            await _drain_wp()
        self.assertTrue(out["pending"])
        meta = create.await_args.kwargs["meta"]
        self.assertEqual(meta["status"], "pending")
        self.assertEqual(meta["operation"], OP_NAME)

    async def test_resume_posts_same_operation(self):
        factory, _ = _client(
            get_queue=[_op_done(),
                       FakeResp(200, content=MP4_BYTES)])
        create = AsyncMock(return_value={"ok": True})
        p_env, p_cfg, p_create = _wp_patches(create)
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env), \
                p_env, p_cfg, p_create:
            out = await render.run(_runner(), operation=OP_NAME)
            await _drain_wp()
        self.assertTrue(out["ok"])
        meta = create.await_args.kwargs["meta"]
        self.assertEqual(meta["operation"], OP_NAME)
        self.assertEqual(meta["status"], "ok")

    async def test_missing_wp_env_skips_silently(self):
        factory, _ = _client(
            post_resp=_submit_resp(),
            get_queue=[_op_done(),
                       FakeResp(200, content=MP4_BYTES)])
        create = AsyncMock(return_value={"ok": True})
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env), \
                patch("backend.wp_client.env", return_value={}), \
                patch("backend.wp_client.create_post", create):
            out = await render.run(_runner(), prompt="a boat")
            await _drain_wp()
        self.assertTrue(out["ok"])
        create.assert_not_awaited()

    async def test_wp_failure_never_breaks_render(self):
        factory, _ = _client(
            post_resp=_submit_resp(),
            get_queue=[_op_done(),
                       FakeResp(200, content=MP4_BYTES)])
        create = AsyncMock(side_effect=RuntimeError("wp down"))
        p_env, p_cfg, p_create = _wp_patches(create)
        with patch.object(render.httpx, "AsyncClient", factory), \
                patch.dict("os.environ", self.env), \
                p_env, p_cfg, p_create:
            out = await render.run(_runner(), prompt="a boat")
            await _drain_wp()
        self.assertTrue(out["ok"])
        self.assertIn("video_url", out)


class DeclarationTest(unittest.TestCase):

    def test_description_is_slim(self):
        desc = render.DECLARATION["description"]
        self.assertLessEqual(len(desc), 300)

    def test_manifest_wiring(self):
        reg = tools_loader.load()  # real backend/tools.d
        self.assertIn("ada_render_video", reg.tools, reg.errors)
        tool = reg.tools["ada_render_video"]
        self.assertEqual(tool.policy, "read")
        self.assertFalse(tool.secondary_allowed)
        self.assertEqual(tool.declaration["name"], "ada_render_video")
        self.assertGreaterEqual(tool.timeout_s, 120)


if __name__ == "__main__":
    unittest.main()
