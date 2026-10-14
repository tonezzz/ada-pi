# Dispatch outcome — ada-gemini-live-spend-cap (ADA-0632)

Session: 20261015-063912-every-voice-ws-connect-on-ada-
Branch: dispatch/20261015-063912-every-voice-ws-connect-on-ada- (commit 7d317b4, not pushed)

## What the card actually asked

The spec is the incident report (voice /ws dead on ada-ha-tony from the
Gemini paid project monthly spend cap, 1011 at provider connect). Tony's
board answer (Oct15 06:26, marked ★): "queue for dispatch — build
per-lane budget enforcement". So the deliverable is software-side
enforcement, not a console/top-up action (that stays on the
gemini-paid-spend-cap backlog card — split projects + $10/mo caps).

## What changed

New `backend/lane_budget.py` — two enforcement layers in front of the
only paid-Live choke point, `GeminiLiveProvider.connect()`:

1. **Lane budget** — `ADA_LANE_MONTHLY_BUDGET_USD` per lane. Estimated
   monthly spend is summed from the durable usage ledger
   (`ADA_USAGE_LOG`; `usage_tracker.record()` rows now carry a `lane`
   tag from `ADA_INSTANCE_ID`, new `entry_cost_usd()` rates each record
   with the existing modality rates). At/over cap → `LaneBudgetExceeded`
   raised before `genai.Client` is even constructed. `WARN_PCT` (80)
   fires one `lane_budget_warn` ops event per lane-month. Unset = gate
   off — opt-in per lane env.
2. **Provider-cap latch** — a billing-class connect failure (monthly
   spending cap / ai.studio/spend / prepayment depleted text, or HTTP
   402) writes the key's sha256 fingerprint into
   `data/lane-budget.json` for the billing month (Pacific boundary).
   Later connects raise `ProviderCapLatched` locally — no Google
   round-trip, no paid-for doomed handshakes. Self-clears on month
   rollover, on rekey (different fingerprint), or by deleting the state
   file; `ADA_LANE_CAP_LATCH=off` disables.

Both exceptions carry `.hold_s` (`ADA_LANE_BUDGET_RECHECK_S`, 900s).
`live_manager` parks the reconnect loop at `status="budget_hold"`,
broadcasts `live_budget_hold`, and `_ready_provider` fails a waiting
/ws client immediately with the hold reason instead of a 10s timeout.
The pwa per-session reconnect loop parks the same way and notifies the
client; the `provider_connect` error payload gains a `code` field
(`lane_budget`/`provider_cap`). `frontend/app.js` (kiosk) and
`pwa/app.js` render "Voice suspended — monthly budget" instead of an
endless reconnect spinner. `conversation_health()` gains a
`lane_budget` block (spent/remaining/latches — fingerprints only, no
key material).

## Verify

- `pytest tests/test_lane_budget.py` — 29 passed (cap detection, latch
  lifecycle incl. rekey/rollover/disable, lane-isolated spend
  accounting, gate + warn-once, hold semantics, connect wiring: latch
  blocks before genai.Client; a billing failure latches the key).
- Full suite: 1489 passed, 7 skipped. `scripts/tool-lint.py` clean.
- Post-deploy ops step: set `ADA_LANE_MONTHLY_BUDGET_USD` per lane env
  (~/.config/secrets on idc03) — recommended just under the AI Studio
  hard cap. To smoke it on ada-dev: set the budget under the current
  month's spend, restart, open /ws → clean error + `budget_hold` status.
  Trail: docs/ssot/jobs/ada/2026-10-15-lane-budget-enforcement.yml

lessons:
- ada-voice has no .venv in dispatch worktrees; system pytest 9.0.2 works (dep_stubs covers genai/fastapi).
- All paid-Live connects funnel through GeminiLiveProvider.connect — one gate covers kiosk live_manager, pwa sessions, and reconnects; relay text is OpenRouter/?mode=text and never touches Live.
- usage_tracker.py usage.jsonl may be shared across lanes on idc03 (same checkout cwd) — new records carry a `lane` field; unlabeled rows count toward every lane (conservative).
- Prior "attempt 2" flag had no failed-run trail in card comms — the real directive was Tony's ★ answer, not the incident spec.
