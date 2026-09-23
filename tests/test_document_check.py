import asyncio
import base64
import io
import json
import unittest
from unittest.mock import MagicMock

from PIL import Image

from backend.document_check import (
    DocumentCheckEngine,
    decode_image,
    enhance_for_print,
    measure,
    render_a4,
    split_spread,
    trim_border,
    A4_PX,
    DPI,
)


def _img(w=1200, h=1600, gray=210, dark_patch=True):
    """Synthetic 'scanned page': gray paper with a dark text block."""
    im = Image.new("RGB", (w, h), (gray, gray, gray))
    if dark_patch:
        # solid dark block ~35% of the image -> realistic contrast
        im.paste(Image.new("RGB", (w // 2, h // 2), (30, 30, 30)),
                 (w // 4, h // 4))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return im, buf.getvalue()


def _engine(classify_json=None):
    eng = DocumentCheckEngine(client=MagicMock())
    resp = MagicMock()
    resp.text = json.dumps(classify_json or {
        "doc_type": "deed", "confidence": 0.9,
        "summary": "Thai condo title deed", "orientation": "portrait",
    })
    async def _gen(**kwargs):
        return resp
    eng._client.aio.models.generate_content = _gen
    return eng


class MeasureTests(unittest.TestCase):
    def test_measure_ok_image(self):
        im, _ = _img()
        m = measure(im)
        self.assertEqual(m["width"], 1200)
        self.assertEqual(m["warnings"], [])

    def test_low_resolution_warns(self):
        im, _ = _img(w=500, h=700)
        m = measure(im)
        self.assertTrue(any("low resolution" in w for w in m["warnings"]))

    def test_flat_image_warns(self):
        im, _ = _img(dark_patch=False)
        m = measure(im)
        self.assertTrue(any("low contrast" in w for w in m["warnings"]))


class ImagingTests(unittest.TestCase):
    def test_render_a4_fit(self):
        im, _ = _img()
        page = enhance_for_print(im)
        canvas, placement = render_a4(page)
        self.assertEqual(canvas.size, A4_PX)
        self.assertEqual(canvas.mode, "L")
        self.assertEqual(placement["mode"], "fit_a4")
        self.assertEqual(placement["dpi"], 300)
        # fits inside the 12mm margins (12mm = ~142px at 300dpi)
        self.assertLessEqual(placement["placed_px"][0], A4_PX[0] - 2 * 141)
        self.assertLessEqual(placement["placed_px"][1], A4_PX[1] - 2 * 141)

    def test_render_a4_true_size_id_card(self):
        card = Image.new("RGB", (640, 400), (200, 200, 190))
        page = enhance_for_print(card)
        canvas, placement = render_a4(page, true_size_mm=(85.6, 54.0))
        self.assertEqual(placement["mode"], "true_size")
        # ID-1 at 300dpi = 1011 x 638 px
        self.assertEqual(placement["placed_px"], [1011, 638])
        self.assertAlmostEqual(placement["placed_mm"][0], 85.6, places=0)
        self.assertAlmostEqual(placement["placed_mm"][1], 54.0, places=0)

    def test_trim_border(self):
        im = Image.new("RGB", (800, 1000), (255, 255, 255))
        inner = Image.new("RGB", (400, 500), (40, 40, 40))
        im.paste(inner, (200, 250))
        t = trim_border(im)
        self.assertLessEqual(t.width, 800)
        self.assertGreaterEqual(t.width, 380)

    def test_split_spread_detects_gutter(self):
        # two white pages with a dark vertical gutter in the middle
        im = Image.new("RGB", (1600, 700), (230, 230, 230))
        for x in range(795, 805):
            for y in range(0, 700):
                im.putpixel((x, y), (20, 20, 20))
        halves = split_spread(im)
        self.assertIsNotNone(halves)
        self.assertEqual(halves[0].width, 795)  # split at gutter's left edge

    def test_split_spread_portrait_none(self):
        im, _ = _img(w=800, h=1600)
        self.assertIsNone(split_spread(im))


class IntakeTests(unittest.TestCase):
    def test_intake_happy_path(self):
        _, blob = _img()
        eng = _engine()
        r = asyncio.run(eng.intake(blob, "image/jpeg", filename="deed.jpg"))
        self.assertTrue(r.ok)
        self.assertEqual(r.doc_type, "deed")
        self.assertEqual(r.confidence, 0.9)
        self.assertIn("/pdf", r.to_dict()["pdf_url"])
        held = eng.held(r.key)
        self.assertIsNotNone(held)
        # held pdf is a real PDF
        self.assertTrue(held.pdf[:5] == b"%PDF-")
        self.assertEqual(len(held.preview) > 1000, True)

    def test_intake_id_card_true_size(self):
        _, blob = _img(w=700, h=450)
        eng = _engine({"doc_type": "id_card", "confidence": 0.95,
                       "summary": "Thai national ID", "orientation": "landscape"})
        r = asyncio.run(eng.intake(blob, "image/jpeg"))
        self.assertTrue(r.ok)
        self.assertEqual(r.plan["output"], "true_size")
        self.assertEqual(r.plan["placed_mm"], [85.6, 54.0])

    def test_intake_bad_bytes(self):
        eng = _engine()
        r = asyncio.run(eng.intake(b"not an image", "image/jpeg"))
        self.assertFalse(r.ok)
        self.assertIn("decode", r.error)

    def test_intake_classify_failure_degrades(self):
        _, blob = _img()
        eng = DocumentCheckEngine(client=MagicMock())
        async def _boom(**kwargs):
            raise RuntimeError("gemini down")
        eng._client.aio.models.generate_content = _boom
        r = asyncio.run(eng.intake(blob, "image/jpeg"))
        self.assertTrue(r.ok)  # render still succeeds without classification
        self.assertEqual(r.doc_type, "other")


class DecodeTests(unittest.TestCase):
    def test_data_url_prefix(self):
        raw = base64.b64encode(b"abc").decode()
        data, mime = decode_image({"image_b64": f"data:image/png;base64,{raw}"})
        self.assertEqual(data, b"abc")
        self.assertEqual(mime, "image/jpeg")

    def test_rejects_oversize(self):
        big = base64.b64encode(b"x" * (16 * 1024 * 1024)).decode()
        with self.assertRaises(ValueError):
            decode_image({"image_b64": big})

    def test_rejects_nonimage_mime(self):
        with self.assertRaises(ValueError):
            decode_image({"image_b64": base64.b64encode(b"x").decode(),
                          "image_mime": "text/html"})


if __name__ == "__main__":
    unittest.main()
