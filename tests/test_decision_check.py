import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.decision_check import (
    DecisionCheckEngine,
    _detect_adapter,
    _extract_json,
    decode_image,
)
from backend.memory_banks import MemoryBankRegistry

REGISTRY = {
    "banks": {
        "purchase": {
            "title": "Purchase checks",
            "scope": "shared",
            "instances": ["tony"],
            "mddb_collection": "ada-ha-bank-purchase",
            "kinds": ["fact", "preference", "note", "check"],
            "writable": True,
            "write_policy": "confirmed",
            "allowed_tools": ["ada_remember", "ada_forget"],
            "status": "active",
        },
    },
}


def _registry() -> MemoryBankRegistry:
    path = Path(tempfile.mkdtemp()) / "banks.json"
    path.write_text(json.dumps(REGISTRY))
    return MemoryBankRegistry(path=str(path), instance="tony", notebook_ids={})


def _gemini_client(responses: list[str]):
    """Fake genai client; each generate_content call pops one .text response."""
    client = MagicMock()
    calls = []

    async def _generate(**kwargs):
        calls.append(kwargs)
        resp = MagicMock()
        resp.text = responses.pop(0)
        return resp

    client.aio.models.generate_content = _generate
    client.calls = calls
    return client


PRODUCT_JSON = json.dumps({
    "name": "Anker 737 Power Bank 24000mAh",
    "brand": "Anker",
    "model": "A1289",
    "price": 1299.0,
    "currency": "THB",
    "shop_name": "GadgetShop99",
    "shop_rating": 4.2,
    "sold_count": 87,
    "marketplace": "shopee",
    "url": "https://shopee.co.th/product/123/456",
})

VERDICT_JSON = json.dumps({
    "verdict": "caution",
    "confidence": 0.7,
    "summary": "Real product, but the shop rating is below your 4.7 threshold.",
    "reasons": ["Genuine Anker A1289 exists", "Price is roughly market rate"],
    "flags": ["Shop rating 4.2 < 4.7", "Not a Shopee Mall seller"],
    "price_assessment": "fair",
    "price_reference": "Official Anker store ~1,590 THB",
    "criteria_results": [
        {"criterion": "shop rating >= 4.7", "pass": False, "note": "4.2"},
        {"criterion": "warranty stated", "pass": True, "note": "18mo brand"},
    ],
    "alternatives": [
        {"name": "Anker 737 (official)", "source": "Anker Shopee Mall",
         "price": "1,590 THB", "why": "Brand warranty, trusted seller"},
    ],
})


class DecisionCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = _registry()
        self.mddb = AsyncMock()
        self.mddb.search_documents = AsyncMock(return_value=[
            {"key": "criteria/shop-rating", "contentMd": "shops >= 4.7",
             "meta": {"kind": ["preference"], "status": ["active"]}},
            {"key": "check/old", "contentMd": "old check",
             "meta": {"kind": ["check"], "status": ["active"]}},
        ])
        self.mddb.add_document = AsyncMock(return_value={"ok": True})
        self.mddb.get_document = AsyncMock(return_value=None)

    def _engine(self, responses):
        engine = DecisionCheckEngine(
            self.mddb, self.registry, "tony",
            client=_gemini_client(list(responses)),
        )
        # Don't let the event SSH call actually run in tests.
        engine._emit_event = AsyncMock()
        return engine

    async def test_quick_check_end_to_end(self):
        engine = self._engine([PRODUCT_JSON, VERDICT_JSON])
        result = await engine.check(
            text="Anker 737 ฿1,299 https://shopee.co.th/product/123/456",
            mode="quick",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.verdict, "caution")
        self.assertEqual(result.adapter, "shopee")
        self.assertEqual(result.product["brand"], "Anker")
        self.assertTrue(result.persisted)
        self.assertIn("normalize_ms", result.durations_ms)
        self.assertIn("verify_ms", result.durations_ms)

        # criteria loader skipped the kind=check history doc
        verify_prompt = engine.client.calls[1]["contents"][0].text
        self.assertIn("shops >= 4.7", verify_prompt)
        self.assertNotIn("old check", verify_prompt)

        # persisted doc carries the check meta (add_document is positional)
        add = self.mddb.add_document.await_args
        self.assertEqual(add.args[0], "ada-ha-bank-purchase")
        self.assertIn("check/", add.args[1])
        self.assertEqual(add.args[4]["kind"], ["check"])
        self.assertEqual(add.args[4]["verdict"], ["caution"])

    async def test_deep_mode_prompt_differs(self):
        engine = self._engine([PRODUCT_JSON, VERDICT_JSON])
        await engine.check(text="item", mode="deep")
        prompt = engine.client.calls[1]["contents"][0].text
        self.assertIn("DEEP mode", prompt)
        engine2 = self._engine([PRODUCT_JSON, VERDICT_JSON])
        await engine2.check(text="item", mode="quick")
        self.assertIn("QUICK mode", engine2.client.calls[1]["contents"][0].text)

    async def test_requires_input(self):
        engine = self._engine([])
        with self.assertRaises(ValueError):
            await engine.check()

    async def test_persist_failure_does_not_fail_check(self):
        self.mddb.add_document = AsyncMock(return_value=None)
        engine = self._engine([PRODUCT_JSON, VERDICT_JSON])
        result = await engine.check(text="item")
        self.assertTrue(result.ok)
        self.assertFalse(result.persisted)
        self.assertIsNone(result.doc_key)

    async def test_missing_bank_degrades(self):
        empty = Path(tempfile.mkdtemp()) / "b.json"
        empty.write_text(json.dumps({"banks": {}}))
        registry = MemoryBankRegistry(path=str(empty), instance="tony", notebook_ids={})
        engine = DecisionCheckEngine(
            self.mddb, registry, "tony",
            client=_gemini_client([PRODUCT_JSON, VERDICT_JSON]),
        )
        engine._emit_event = AsyncMock()
        result = await engine.check(text="item")
        self.assertTrue(result.ok)
        self.assertFalse(result.persisted)

    async def test_history_lists_checks(self):
        engine = self._engine([])
        checks = await engine.history()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["key"], "check/old")
        # criteria doc was excluded by the kind=check filter in the request
        filt = self.mddb.search_documents.await_args.kwargs["filter_meta"]
        self.assertEqual(filt["kind"], ["check"])


class JsonExtractTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(_extract_json('here:\n```json\n{"a": 2}\n```'), {"a": 2})

    def test_prose_wrapped(self):
        self.assertEqual(_extract_json('Sure! {"a": 3} hope that helps'), {"a": 3})

    def test_invalid_raises(self):
        with self.assertRaises(Exception):
            _extract_json("no json here")


class InputTests(unittest.TestCase):
    def test_shopee_url_detected(self):
        self.assertEqual(
            _detect_adapter("https://shopee.co.th/x", "").name, "shopee"
        )

    def test_generic_fallback(self):
        self.assertEqual(_detect_adapter("", "a kettle").name, "generic")

    def test_decode_image(self):
        data, mime = decode_image({
            "image_b64": base64.b64encode(b"jpg").decode(),
            "image_mime": "image/png",
        })
        self.assertEqual(data, b"jpg")
        self.assertEqual(mime, "image/png")

    def test_decode_image_rejects_non_image(self):
        with self.assertRaises(ValueError):
            decode_image({
                "image_b64": base64.b64encode(b"x").decode(),
                "image_mime": "text/plain",
            })


if __name__ == "__main__":
    unittest.main()
