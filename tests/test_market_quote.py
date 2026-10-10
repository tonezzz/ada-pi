"""Unit tests for backend/tools.d/ada_market_quote.py.

httpx is faked — no network. Covers each kind's quoting path, the
as_of/stale honesty contract, error honesty, and manifest wiring.
"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from backend import tools_loader

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "ada_market_quote.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "tools_d.ada_market_quote_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


market = _load_module()


def _d(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("empty body")
        return self._payload


class FakeClient:
    """httpx.AsyncClient stand-in; routes keyed by url substring."""

    def __init__(self, routes=None, fail=None, *a, **kw):
        self.routes = routes or {}
        self.fail = fail
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        self.calls.append((url, kw))
        if self.fail is not None:
            raise self.fail
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return FakeResp(404, {"error": "not found"})


def _runner():
    r = MagicMock()
    r.policy_identity.return_value = "person.tony"
    r.banks.policy_for.return_value = {"full": True}
    return r


def _client(routes=None, fail=None):
    def factory(*a, **kw):
        return FakeClient(routes=routes, fail=fail)
    return factory


FX_ROWS = {"data": [
    {"date": _d(6), "base_currency": "USD", "quote_currency": "THB",
     "rate": 33.5, "close": 33.5},
    {"date": _d(1), "base_currency": "USD", "quote_currency": "THB",
     "rate": 33.9, "close": 33.9},
]}

GOLD_ROWS = {"data": [
    {"date": _d(9), "commodity": "GOLD", "symbol": "XAU-THB",
     "price": 68000.0, "unit": "baht"},
    {"date": _d(8), "commodity": "GOLD", "symbol": "XAU-THB",
     "price": 67000.0, "unit": "baht"},
    {"date": _d(2), "commodity": "GOLD", "symbol": "GOLD",
     "price": 4100.0, "unit": "oz"},
    {"date": _d(3), "commodity": "GOLD", "symbol": "GOLD",
     "price": 4000.0, "unit": "oz"},
]}

DXY_ROWS = {"data": [
    {"date": _d(5), "value": 100.5},
    {"date": _d(1), "value": 101.2},
]}

OIL_ROWS = {"data": [
    {"date": _d(4), "commodity": "OIL", "symbol": "WTI",
     "price": 90.0, "unit": "barrel"},
    {"date": _d(3), "commodity": "OIL", "symbol": "WTI",
     "price": 95.0, "unit": "barrel"},
    {"date": _d(3), "commodity": "OIL", "symbol": "BRENT",
     "price": 99.0, "unit": "barrel"},
]}


class FxTest(unittest.IsolatedAsyncioTestCase):

    async def test_fx_thb_latest_and_change(self):
        client = FakeClient({"/exchange_rates/THB": FakeResp(200, FX_ROWS)})
        with patch.object(market.httpx, "AsyncClient", return_value=client):
            out = await market.run(_runner(), kind="fx", currency="thb")
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["quotes"]), 1)
        q = out["quotes"][0]
        self.assertEqual(q["label"], "USD/THB")
        self.assertEqual(q["value"], 33.9)
        self.assertEqual(q["prev_value"], 33.5)
        self.assertAlmostEqual(q["change_pct"], 1.19, places=2)
        self.assertNotIn("stale", q)  # 1 day old — fresh
        url, kw = client.calls[0]
        self.assertIn("/exchange_rates/THB", url)
        self.assertIn("start_date", kw["params"])

    async def test_fx_defaults_to_thb(self):
        client = FakeClient({"/exchange_rates/THB": FakeResp(200, FX_ROWS)})
        with patch.object(market.httpx, "AsyncClient", return_value=client):
            out = await market.run(_runner(), kind="fx")
        self.assertTrue(out["ok"])
        self.assertIn("/exchange_rates/THB", client.calls[0][0])

    async def test_fx_bad_currency_rejected_without_http(self):
        with patch.object(market.httpx, "AsyncClient",
                          side_effect=AssertionError("no http expected")):
            out = await market.run(_runner(), kind="fx", currency="dollar")
        self.assertFalse(out["ok"])

    async def test_fx_empty_data_is_honest(self):
        client = FakeClient({"/exchange_rates/THB": FakeResp(200,
                                                            {"data": []})})
        with patch.object(market.httpx, "AsyncClient", return_value=client):
            out = await market.run(_runner(), kind="fx", currency="XTS")
            # XTS routes to its own path -> 404 -> honest error
            out2 = await market.run(_runner(), kind="fx", currency="THB")
            # THB route returns {"data": []} -> honest "no data" error
        self.assertFalse(out["ok"])
        self.assertFalse(out2["ok"])
        self.assertIn("no recent", out2["error"])


class GoldTest(unittest.IsolatedAsyncioTestCase):

    async def test_gold_returns_thai_and_usd(self):
        client = FakeClient({"/commodity_prices/GOLD": FakeResp(200,
                                                              GOLD_ROWS)})
        with patch.object(market.httpx, "AsyncClient", return_value=client):
            out = await market.run(_runner(), kind="gold")
        self.assertTrue(out["ok"])
        labels = {q["label"]: q for q in out["quotes"]}
        self.assertIn("thai_gold_bar", labels)
        self.assertIn("gold_spot", labels)
        self.assertEqual(labels["thai_gold_bar"]["value"], 67000.0)
        self.assertEqual(labels["thai_gold_bar"]["unit"],
                         "THB per baht-weight")
        self.assertTrue(labels["thai_gold_bar"]["stale"])  # 8 days old
        self.assertEqual(labels["gold_spot"]["value"], 4100.0)
        self.assertIn("note", out)


class DxyOilTest(unittest.IsolatedAsyncioTestCase):

    async def test_dxy(self):
        client = FakeClient({"/dollar_index": FakeResp(200, DXY_ROWS)})
        with patch.object(market.httpx, "AsyncClient", return_value=client):
            out = await market.run(_runner(), kind="dxy")
        self.assertTrue(out["ok"])
        q = out["quotes"][0]
        self.assertEqual(q["value"], 101.2)
        self.assertEqual(q["prev_value"], 100.5)

    async def test_oil_wti_and_brent(self):
        client = FakeClient({"/commodity_prices/OIL": FakeResp(200,
                                                             OIL_ROWS)})
        with patch.object(market.httpx, "AsyncClient", return_value=client):
            out = await market.run(_runner(), kind="oil")
        self.assertTrue(out["ok"])
        labels = {q["label"] for q in out["quotes"]}
        self.assertEqual(labels, {"wti_crude", "brent_crude"})


class HonestyTest(unittest.IsolatedAsyncioTestCase):

    async def test_unknown_kind(self):
        with patch.object(market.httpx, "AsyncClient",
                          side_effect=AssertionError("no http expected")):
            out = await market.run(_runner(), kind="stock")
        self.assertFalse(out["ok"])
        self.assertIn("unknown kind", out["error"])

    async def test_unreachable_is_honest(self):
        client = FakeClient(fail=httpx.ConnectError("refused"))
        runner = _runner()
        with patch.object(market.httpx, "AsyncClient", return_value=client):
            out = await market.run(runner, kind="dxy")
        self.assertFalse(out["ok"])
        self.assertIn("couldn't reach", out["error"])
        # transport outage -> one ops event via the context facade
        emit = runner.context.emit_ops_event
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0], "market_quote_api_down")
        self.assertEqual(emit.call_args.kwargs["tool"],
                         "ada_market_quote")

    async def test_server_error_surfaced(self):
        client = FakeClient({"/dollar_index": FakeResp(
            500, {"error": "db gone"})})
        runner = _runner()
        with patch.object(market.httpx, "AsyncClient", return_value=client):
            out = await market.run(runner, kind="dxy")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "db gone")
        # HTTP>=400 is app-level, not a transport outage — no ops event
        runner.context.emit_ops_event.assert_not_called()

    async def test_stale_flag_and_note(self):
        rows = {"data": [{"date": _d(30), "value": 99.0},
                         {"date": _d(10), "value": 100.0}]}
        client = FakeClient({"/dollar_index": FakeResp(200, rows)})
        with patch.object(market.httpx, "AsyncClient", return_value=client):
            out = await market.run(_runner(), kind="dxy")
        self.assertTrue(out["ok"])
        self.assertTrue(out["quotes"][0]["stale"])
        self.assertEqual(out["quotes"][0]["age_days"], 10)
        self.assertIn("as_of", out["note"])


class ManifestTest(unittest.TestCase):

    def test_manifest_registers_tool(self):
        reg = tools_loader.load()
        self.assertIn("ada_market_quote", reg.tools,
                      msg=f"load errors: {reg.errors}")
        spec = reg.tools["ada_market_quote"]
        self.assertEqual(spec.policy, "read")
        self.assertTrue(spec.secondary_allowed)
        self.assertEqual(spec.declaration["name"], "ada_market_quote")
        self.assertIn("kind", spec.declaration["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
