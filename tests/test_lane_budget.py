"""lane_budget (card ada-gemini-live-spend-cap): per-lane monthly spend
enforcement in front of the paid Gemini Live connect — the configured
USD ceiling, the provider billing-cap latch (a learned 1011 spend-cap or
402 fails later connects locally for the rest of the billing month), the
hold semantics the reconnect loops read, and the connect() wiring that
makes it all fail fast and free.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GEMINI_API_KEY", "x")

from backend import lane_budget  # noqa: E402
from backend.lane_budget import (  # noqa: E402
    LaneBudgetExceeded, ProviderCapLatched)
from backend.usage_tracker import UsageLedger  # noqa: E402

CAP_MSG = ("1011 None. Your project has exceeded its monthly spending "
           "cap. Please go to AI Studio at https://ai.studio/spend")


def _cap_exc():
    return RuntimeError(CAP_MSG)


def _usage_file(lines):
    """Write ledger-style recs to a temp file, return its path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for rec in lines:
            f.write(json.dumps(rec) + "\n")
    return path


def _rec(lane="test", in_audio=0, out_audio=0, in_tok=None, out_tok=None,
         ts=None):
    ts = ts or datetime.now(timezone.utc).isoformat()
    return {
        "ts": ts, "source": "live", "session_id": "s",
        "lane": lane,
        "in": in_tok if in_tok is not None else in_audio,
        "out": out_tok if out_tok is not None else out_audio,
        "in_mod": {"audio": in_audio} if in_audio else {},
        "out_mod": {"audio": out_audio} if out_audio else {},
        "cached": 0, "tool_use": 0,
    }


class BudgetEnv(unittest.TestCase):
    """Each test owns a clean state file + usage log."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self._tmp.name, "lane-budget.json")
        self.usage = os.path.join(self._tmp.name, "usage.jsonl")
        lane_budget.reset()

    def tearDown(self):
        lane_budget.reset()
        self._tmp.cleanup()

    def env(self, **kw):
        base = {
            "ADA_LANE_BUDGET_STATE": self.state,
            "ADA_USAGE_LOG": self.usage,
            "ADA_LANE_MONTHLY_BUDGET_USD": "",
            "ADA_LANE_BUDGET_WARN_PCT": "",
            "ADA_LANE_BUDGET_RECHECK_S": "",
            "ADA_LANE_CAP_LATCH": "",
        }
        base.update(kw)
        return patch.dict(os.environ, base)


class CapDetectionTests(BudgetEnv):

    def test_monthly_spend_cap_text_matches(self):
        self.assertTrue(lane_budget.is_billing_cap_error(_cap_exc()))

    def test_prepaid_depleted_text_matches(self):
        self.assertTrue(lane_budget.is_billing_cap_error(RuntimeError(
            "402 prepayment credits depleted")))

    def test_http_402_code_matches(self):
        exc = RuntimeError("payment required")
        exc.status_code = 402
        self.assertTrue(lane_budget.is_billing_cap_error(exc))

    def test_bare_1011_without_cap_text_does_not_match(self):
        self.assertFalse(lane_budget.is_billing_cap_error(
            RuntimeError("1011 internal error")))

    def test_ordinary_error_does_not_match(self):
        self.assertFalse(lane_budget.is_billing_cap_error(
            RuntimeError("connection reset by peer")))


class LatchTests(BudgetEnv):

    def test_billing_error_latches_key_for_the_month(self):
        with self.env():
            self.assertTrue(lane_budget.note_connect_failure(
                _cap_exc(), api_key="key-A"))
            state = json.loads(open(self.state).read())
            fp = lane_budget.key_fingerprint("key-A")
            self.assertIn(fp, state["caps"])
            self.assertEqual(state["caps"][fp]["period"],
                             lane_budget.current_period())
            self.assertEqual(state["caps"][fp]["lane"], "test")

    def test_latched_key_fails_check_locally(self):
        with self.env():
            lane_budget.note_connect_failure(_cap_exc(), api_key="key-A")
            with self.assertRaises(ProviderCapLatched) as ctx:
                lane_budget.check(api_key="key-A")
            self.assertEqual(ctx.exception.code, "provider_cap")
            self.assertIn("billing cap", str(ctx.exception))

    def test_rekeyed_key_is_not_latched(self):
        with self.env():
            lane_budget.note_connect_failure(_cap_exc(), api_key="key-A")
            lane_budget.check(api_key="key-B")  # no raise

    def test_non_billing_error_does_not_latch(self):
        with self.env():
            self.assertFalse(lane_budget.note_connect_failure(
                RuntimeError("503 service unavailable"), api_key="key-A"))
            lane_budget.check(api_key="key-A")

    def test_latch_disabled_by_env(self):
        with self.env(ADA_LANE_CAP_LATCH="off"):
            self.assertFalse(lane_budget.note_connect_failure(
                _cap_exc(), api_key="key-A"))
            self.assertFalse(os.path.exists(self.state))

    def test_stale_period_latch_ignored(self):
        with self.env():
            fp = lane_budget.key_fingerprint("key-A")
            os.makedirs(os.path.dirname(self.state), exist_ok=True)
            with open(self.state, "w") as f:
                json.dump({"caps": {fp: {"lane": "test",
                                         "period": "1999-01",
                                         "at": "x", "reason": "old"}}}, f)
            lane_budget.check(api_key="key-A")  # no raise

    def test_latch_persists_across_calls(self):
        """The latch is on disk — a new process state still holds it."""
        with self.env():
            lane_budget.note_connect_failure(_cap_exc(), api_key="key-A")
        # Simulate restart: fresh module state would re-read the file.
        lane_budget.reset()
        with self.env():
            with self.assertRaises(ProviderCapLatched):
                lane_budget.check(api_key="key-A")


class SpendAccountingTests(BudgetEnv):

    def test_month_spend_sums_this_lane_only(self):
        path = _usage_file([
            _rec(lane="test", in_audio=1_000_000),           # $3.00 in
            _rec(lane="other", in_audio=9_000_000),          # not mine
            _rec(lane="test", ts="1999-01-15T00:00:00+00:00",
                 in_audio=9_000_000),                        # other month
        ])
        with self.env(ADA_USAGE_LOG=path):
            self.assertAlmostEqual(
                lane_budget.month_spend_usd("test"), 3.0, places=3)

    def test_unlabeled_rows_count_as_every_lane(self):
        # Legacy/pre-tag lines can't be attributed — conservative: they
        # count toward the checking lane.
        rec = _rec(lane=None, in_audio=1_000_000)
        rec.pop("lane")
        path = _usage_file([rec])
        with self.env(ADA_USAGE_LOG=path):
            self.assertAlmostEqual(
                lane_budget.month_spend_usd("test"), 3.0, places=3)

    def test_usage_log_off_means_zero_spend(self):
        with self.env(ADA_USAGE_LOG="off"):
            self.assertEqual(lane_budget.month_spend_usd("test"), 0.0)

    def test_missing_usage_file_means_zero_spend(self):
        with self.env(ADA_USAGE_LOG=os.path.join(
                self._tmp.name, "nope.jsonl")):
            self.assertEqual(lane_budget.month_spend_usd("test"), 0.0)


class BudgetGateTests(BudgetEnv):

    def test_no_budget_configured_is_noop(self):
        with self.env():
            lane_budget.check(api_key="key-A")

    def test_over_budget_raises_lane_budget(self):
        # $3.00 of audio-in against a $1 cap.
        path = _usage_file([_rec(in_audio=1_000_000)])
        with self.env(ADA_USAGE_LOG=path,
                      ADA_LANE_MONTHLY_BUDGET_USD="1.00"):
            with self.assertRaises(LaneBudgetExceeded) as ctx:
                lane_budget.check(api_key="key-A")
            self.assertEqual(ctx.exception.code, "lane_budget")
            self.assertIn("monthly live budget", str(ctx.exception))
            self.assertIn("test", str(ctx.exception))

    def test_under_budget_passes(self):
        path = _usage_file([_rec(in_audio=1000)])  # ~$0.003
        with self.env(ADA_USAGE_LOG=path,
                      ADA_LANE_MONTHLY_BUDGET_USD="10.00"):
            lane_budget.check(api_key="key-A")

    def test_warn_threshold_emits_once(self):
        events = []
        path = _usage_file([_rec(in_audio=3_000_000)])  # $9 > 80% of $10
        env = self.env(ADA_USAGE_LOG=path,
                       ADA_LANE_MONTHLY_BUDGET_USD="10.00")
        with env:
            lane_budget.check(api_key="key-A",
                              emit=lambda t, d: events.append(t))
            lane_budget.check(api_key="key-A",
                              emit=lambda t, d: events.append(t))
        self.assertEqual(events, ["lane_budget_warn"])

    def test_block_event_emitted_on_trip(self):
        events = []
        path = _usage_file([_rec(in_audio=1_000_000)])
        with self.env(ADA_USAGE_LOG=path,
                      ADA_LANE_MONTHLY_BUDGET_USD="1.00"):
            with self.assertRaises(LaneBudgetExceeded):
                lane_budget.check(api_key="key-A",
                                  emit=lambda t, d: events.append(t))
        self.assertEqual(events, ["lane_budget_block"])


class HoldSemanticsTests(BudgetEnv):

    def test_hold_seconds_on_enforcement_errors(self):
        with self.env(ADA_LANE_BUDGET_RECHECK_S="600"):
            exc = LaneBudgetExceeded("x")
            self.assertEqual(lane_budget.hold_seconds(exc), 600.0)
            self.assertEqual(
                lane_budget.hold_seconds(ProviderCapLatched("x")), 600.0)

    def test_hold_seconds_none_for_normal_errors(self):
        self.assertIsNone(lane_budget.hold_seconds(RuntimeError("boom")))
        self.assertIsNone(lane_budget.hold_seconds(ValueError("x")))

    def test_period_end_rolls_month_and_year(self):
        self.assertEqual(lane_budget.period_end("2026-10"), "2026-11-01")
        self.assertEqual(lane_budget.period_end("2026-12"), "2027-01-01")


class StatusTests(BudgetEnv):

    def test_status_snapshot(self):
        path = _usage_file([_rec(in_audio=1_000_000)])
        with self.env(ADA_USAGE_LOG=path,
                      ADA_LANE_MONTHLY_BUDGET_USD="5.00"):
            st = lane_budget.status()
        self.assertEqual(st["lane"], "test")
        self.assertEqual(st["budget_usd"], 5.0)
        self.assertAlmostEqual(st["spent_usd"], 3.0, places=3)
        self.assertAlmostEqual(st["remaining_usd"], 2.0, places=3)
        self.assertEqual(st["latched_caps"], {})


class UsageLedgerLaneTests(unittest.TestCase):
    """record() stamps the lane on each ledger line for the spend scan."""

    def test_record_stamps_lane(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "usage.jsonl")
            ledger = UsageLedger()
            ledger.configure(path)
            ledger.record("live", session_id="s",
                          input_tokens=10, output_tokens=5)
            rec = json.loads(open(path).read().splitlines()[0])
            self.assertEqual(rec["lane"], "test")

    def test_record_explicit_lane(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "usage.jsonl")
            ledger = UsageLedger()
            ledger.configure(path)
            ledger.record("live", lane="custom")
            rec = json.loads(open(path).read().splitlines()[0])
            self.assertEqual(rec["lane"], "custom")


class ProviderConnectGateTests(BudgetEnv):
    """The enforcement call inside GeminiLiveProvider.connect."""

    def _provider(self):
        from backend.realtime_provider import create_provider
        return create_provider(session_id="t-budget")

    def test_connect_blocked_before_genai_when_latched(self):
        from backend import realtime_provider as rp
        with self.env():
            lane_budget.note_connect_failure(_cap_exc(), api_key="x")
            provider = self._provider()
            sentinel = MagicMock(side_effect=AssertionError(
                "genai.Client must not be built on a latched key"))
            with patch.object(rp.genai, "Client", sentinel):
                with self.assertRaises(ProviderCapLatched):
                    asyncio.run(provider.connect())

    def test_billing_failure_during_connect_latches(self):
        from backend import realtime_provider as rp

        class _FailCtx:
            async def __aenter__(self):
                raise _cap_exc()

            async def __aexit__(self, *a):
                return False

        with self.env():
            provider = self._provider()
            client = MagicMock()
            client.aio.live.connect = MagicMock(return_value=_FailCtx())
            with patch.object(rp.genai, "Client", return_value=client), \
                 patch.object(rp.GeminiLiveProvider, "_session_config",
                              AsyncMock(return_value={})):
                with self.assertRaises(RuntimeError) as ctx:
                    asyncio.run(provider.connect())
            self.assertIn("spending cap", str(ctx.exception))
            # The failure was learned — the next connect fails locally.
            with self.assertRaises(ProviderCapLatched):
                lane_budget.check(api_key="x")


if __name__ == "__main__":
    unittest.main()
