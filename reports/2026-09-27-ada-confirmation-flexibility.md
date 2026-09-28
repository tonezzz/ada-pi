# Ada confirmation gate — more flexible, not looser (Before/After)

Date: 2026-09-27 · Session: dispatch `20260927-215445-improve-ada-s-confirmation-dec`

Source: ideas bank `ideas/confirmation-improvement` (ada-ha-bank-ideas-tony) —
"1. Ada drafts Before/After report. 2. Implement audit scenario. 3. Benchmark
key steps." Ref extract `extract-2026-09-27-fd98e6e49c-1`.

## Where the logic lives

- `backend/tool_runner.py` — the actual confirmation/decision gate. Every
  mutating tool family (`CONTROL_TOOLS`, `MEMORY_WRITE_TOOLS`,
  `CALENDAR_WRITE_TOOLS`, `CMS_WRITE_TOOLS`, `DEVIN_CONFIRMED_TOOLS`,
  `DOC_CONFIRMED_TOOLS`) is gated server-side in `execute()` via the
  `_check_*_allowed` methods.
- `backend/decision_check.py` — purchase-verification pipeline (unrelated to
  the write gates; read-only, no confirmation needed).
- `backend/realtime_provider.py` — voice tool schemas + prompt instructions
  telling the model when `confirmed=true` is required.
- `pwa_server.py` — REST path `POST /api/home-assistant/entities/{id}/power`
  that reuses `_check_control_allowed` directly.

## Before

- `confirmed` had to be the strict JSON boolean `true`. The model emits
  `"yes"`, `"true"`, `1` often enough that denials turned into re-ask loops —
  flexible spelling friction for zero security gain (the flag is
  self-asserted either way).
- `confirmed=true` was **unbound**: nothing tied a confirmation to the action
  it was shown. It was replayable forever and could arm a different call than
  the one the user agreed to — the "bare 'yes' answers the wrong question"
  class of failure noted in `tests/scenarios-live/tv_power.yaml`.
- No structured audit trail — proposals and grants existed only as log lines.

## After

Three changes in `tool_runner.py`, all enforced at the same single gate:

1. **Truthy parsing** (`_confirmed_truthy`): `confirmed` accepts `true`, `1`,
   `"yes"`, `"y"`, `"true"`, `"confirm"`, `"confirmed"` (case-insensitive).
   Falsey spellings still refuse. Same self-asserted semantics, fewer
   spurious denials.
2. **Bound one-shot tokens** (`confirm_token`): every denial mints
   `cfm-<hex>` bound to `sha256(tool + canonical-args)`, TTL 120s
   (`ADA_CONFIRM_TOKEN_TTL_S`), single-use, capped at 64 pending. Replaying
   the exact call with the token executes it; the same token with different
   args, a different tool, an expiry, or a second use is refused. This is
   strictly *tighter* than `confirmed=true` for the propose→retry flow and
   also gives REST/non-voice callers a real challenge-response.
   `confirm_token` is declared next to `confirmed` in all 17 gated voice tool
   schemas (they use `additionalProperties: false`, so an undeclared arg
   would never reach the runner).
3. **Structured audit ledger** (`confirmation_audit()`): bounded deque (200)
   recording `denied | proposed | granted | consumed` with tool name,
   args-fingerprint prefix, via (`confirmed`/`token`), and identity — the
   auditable answer to "which exact action did the user approve?".

## Benchmark (key steps, this host, CPython 3.14)

| Path | Cost/call |
|---|---|
| Grant via `confirmed="yes"` → `cms_delete_page` executes | ~106 µs |
| Deny → fingerprint + token mint + `PermissionError` | ~126 µs |
| Token consume → executes (median, 500 samples) | ~135 µs |

The new gate adds ~1 SHA-256 + dict ops over the old `is not True` check —
unmeasurable in the voice path (a denied→confirmed round trip is seconds,
dominated by the model + user). Audit deque cap verified: 200 retained after
4 500 events. Offline scenario steps all complete in <1 ms.

## Audit scenario

`tests/scenarios/confirmation_audit.yaml` exercises the full contract
end-to-end through `runner.execute`: bare write refused + token minted →
same token + different args refused → exact replay executes → second replay
refused (single-use) → `confirmed: "yes"` writes → `audit: confirmations`
asserts the ledger shows denied/proposed/consumed/granted.

Scenario engine gained two reusable primitives for this (and future
scenarios): `capture: {name: regex}` + `{name}` arg substitution, and the
`audit: confirmations` step kind; every step now reports `ms`.

## Files changed

- `backend/tool_runner.py` — truthy parsing, confirm_token mint/consume,
  `_require_confirmation` shared gate, audit ledger; all six `_check_*`
  families route through it; dangerous-device gate included.
- `backend/realtime_provider.py` — `confirm_token` declared in all 17 gated
  tool schemas.
- `pwa_server.py` — REST power endpoint forwards `confirm_token`.
- `tests/scenario_engine.py` — capture/substitution, `audit:` step, per-step
  timing.
- `tests/test_tool_runner.py` — new `ConfirmationGateTests` (10 tests);
  also made `ControlGateTests` hermetic against the ambient
  `~/.config/ada/memory-banks.json` (its `control_policies` denied `cover`
  for anonymous identity on this host and was already failing pre-change).
- `tests/scenarios/confirmation_audit.yaml` — new audit scenario.

## Not done / notes

- `confirmed=true` remains accepted — dropping it would force a deny→retry
  round trip even when the user's request already *is* the confirmation
  (e.g. "remember that…"), which the prompt explicitly allows. The token
  path is available where stronger binding is wanted.
- The ledger is in-memory like the rate-limit counters; persisting it to
  MDDB/events would be the follow-up if cross-restart audit is needed.
- `decision_check.py` unchanged — already instruments `durations_ms` per
  stage; it is verification-only and needs no write gate.

## Verify

```bash
.venv/bin/python -m unittest tests.test_tool_runner tests.test_scenarios -v
.venv/bin/python -m unittest discover -s tests   # 320 tests
```
