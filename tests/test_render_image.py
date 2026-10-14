"""Unit tests for backend/tools.d/ada_render_image.py.

gemini_pool.generate and traffic_camera.publish_relay are faked — no
network, no real image model call. Covers prompt validation, aspect
mapping, ref_url shapes, the render->save->publish flow, and manifest
wiring.
"""

from __future__ import annotations

import asyncio
import base64
import os
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import google.genai  # noqa: F401
except ImportError:
    import sys as _sys
    import types as _types
    _genai = _types.ModuleType("google.genai")
    _genai.types = _types.ModuleType("google.genai.types")
    _sys.modules.setdefault("google.genai", _genai)
    _sys.modules.setdefault("google.genai.types", _genai.types)

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "ada_render_image.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "tools_d.ada_render_image_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


render = _load_module()
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 600
JPEG_BYTES = b"\xff\xd8jpeg-fake\xff\xd9"


def _resp_with_image(data: bytes = PNG_BYTES, text: str = ""):
    """Fake generate_content response: one candidate, inline image."""
    part = MagicMock()
    part.inline_data = MagicMock()
    part.inline_data.data = data
    part.inline_data.mime_type = "image/png"
    part.text = None
    cand = MagicMock()
    cand.content.parts = [part]
    resp = MagicMock()
    resp.candidates = [cand]
    return resp


class TestAdaRenderImage(unittest.TestCase):

    def _run(self, **kw):
        return asyncio.run(render.run(MagicMock(), **kw))

    def test_no_prompt_is_honest(self):
        out = self._run()
        self.assertFalse(out["ok"])
        self.assertIn("prompt", out["error"])

    def test_aspect_aliases(self):
        for raw, want in [("", "1:1"), ("square", "1:1"),
                          ("landscape", "16:9"), ("portrait", "9:16"),
                          ("16x9", "16:9"), ("3:4", "3:4")]:
            got, err = render._aspect(raw)
            self.assertIsNone(err, raw)
            self.assertEqual(got, want)
        bad, err = render._aspect("cinematic")
        self.assertIsNone(bad)
        self.assertIn("unknown aspect", err)

    def test_happy_path_publishes_image_url(self):
        import backend.gemini_pool as gp
        with tempfile.TemporaryDirectory() as td, \
             patch.dict("os.environ", {"ADA_RENDER_DIR": td}), \
             patch.object(gp, "generate", new_callable=AsyncMock,
                          return_value=_resp_with_image()), \
             patch.object(gp, "configured_keys", return_value=["k1"]), \
             patch("backend.traffic_camera.publish_relay",
                   return_value="https://x/frame?token=render:abc"):
            out = self._run(prompt="a cat in a hat", aspect="landscape")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["image_url"], "https://x/frame?token=render:abc")
        self.assertEqual(out["aspect"], "16:9")
        self.assertTrue(Path(out["file"]).name.startswith("render-img-"))

    def test_ref_url_refuses_bad_shape(self):
        out = self._run(prompt="edit it", ref_url="ftp://nope")
        self.assertFalse(out["ok"])
        self.assertIn("ref_url", out["error"])

    def test_ref_url_http_fetch_and_edit(self):
        import httpx
        fake = httpx.Response(200, content=JPEG_BYTES,
                              headers={"content-type": "image/jpeg"})
        with tempfile.TemporaryDirectory() as td, \
             patch.dict("os.environ", {"ADA_RENDER_DIR": td}), \
             patch.object(httpx.AsyncClient, "get",
                          new_callable=AsyncMock, return_value=fake), \
             patch("backend.traffic_camera.publish_relay",
                   return_value="https://x/frame?token=render:def"):
            import backend.gemini_pool as gp
            with patch.object(gp, "generate", new_callable=AsyncMock,
                              return_value=_resp_with_image()) as gen, \
                 patch.object(gp, "configured_keys",
                              return_value=["k1"]):
                out = self._run(prompt="make it red",
                                ref_url="https://x/in.jpg")
        self.assertTrue(out["ok"], out)
        # the ref went into the request as inline_data
        contents = gen.call_args[0][1]
        parts = contents[0]["parts"]
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[1]["inline_data"]["mime_type"],
                         "image/jpeg")

    def test_no_image_in_response_is_honest(self):
        resp = MagicMock()
        resp.candidates = []
        resp.prompt_feedback = None
        import backend.gemini_pool as gp
        with patch.object(gp, "generate", new_callable=AsyncMock,
                          return_value=resp), \
             patch.object(gp, "configured_keys", return_value=["k1"]):
            out = self._run(prompt="nothing")
        self.assertFalse(out["ok"])
        self.assertIn("no image", out["error"])

    def test_size_param_reaches_image_config(self):
        with patch("backend.traffic_camera.publish_relay",
                   return_value="https://x/frame?token=render:ghi"):
            import backend.gemini_pool as gp
            with patch.object(gp, "generate", new_callable=AsyncMock,
                              return_value=_resp_with_image()) as gen,                  patch.object(gp, "configured_keys",
                              return_value=["k1"]):
                out = self._run(prompt="big", size="4K")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["size"], "4K")
        cfg = gen.call_args[1].get("config") or gen.call_args[0][2]
        icfg = getattr(cfg, "image_config", None) or cfg["image_config"]
        val = getattr(icfg, "image_size", None) or icfg["image_size"]
        self.assertEqual(str(val).upper(), "4K")

    def test_gate_file_refuses(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            Path(d, "ada_render_image.closed").touch()
            with patch.dict(os.environ, {"ADA_GATES_DIR": d}):
                out = self._run(prompt="anything")
        self.assertFalse(out["ok"])
        self.assertIn("switched off", out["error"])

    def test_lane_gate_only_blocks_that_lane(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            Path(d, "ada_render_image.kk.closed").touch()
            env = {"ADA_GATES_DIR": d, "ADA_INSTANCE_ID": "kk"}
            with patch.dict(os.environ, env):
                out = self._run(prompt="anything")
            self.assertFalse(out["ok"])
            env2 = {"ADA_GATES_DIR": d, "ADA_INSTANCE_ID": "chaba"}
            with patch.dict(os.environ, env2), \
                 patch("backend.traffic_camera.publish_relay",
                       return_value="https://x/frame?token=render:jkl"):
                import backend.gemini_pool as gp
                with patch.object(gp, "generate", new_callable=AsyncMock,
                                  return_value=_resp_with_image()),                      patch.object(gp, "configured_keys",
                                  return_value=["k1"]):
                    out2 = self._run(prompt="anything")
            self.assertTrue(out2["ok"], out2)

    def test_manifest_entry(self):
        import yaml
        m = yaml.safe_load(open(
            Path(__file__).resolve().parent.parent
            / "backend" / "tools.d" / "manifest.yml"))
        ent = m["tools"]["ada_render_image"]
        self.assertEqual(ent["module"], "ada_render_image")
        self.assertEqual(ent["policy"], "read")


if __name__ == "__main__":
    unittest.main()
