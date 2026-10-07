# Dispatch outcome — ada-remember-confirm-flake

Task: fix the ada_remember confirm-gate flake (voice session
2026-10-07-56d2e4d167: 6/8 writes NOT EXECUTED because the model resends
confirmed:true instead of confirm_token; Ada narrated "saved" over the
failed results).

## Root cause

`GeminiLiveProvider` strips self-asserted `confirmed=true` whenever the
user's transcript doesn't affirm (`_user_confirmed`). Each stripped
resend hit `_require_confirmation`, was denied, and minted a fresh
confirm_token — which Gemini never emits — so identical writes looped
NOT EXECUTED. Separately, `phantom_write_claim` only fired on
zero-tool-call turns and `_PHANTOM_CLAIM_RE` lacked the verb "saved",
so narrating success over failed results was undetected.

## What changed

- `backend/tool_runner/__init__.py`
  - `pending_confirm(tool, args)` — true when a live (unexpired)
    confirm_token covers the exact tool+args fingerprint.
  - `_burn_pending_confirms()` — a confirmed-grant now retires the
    outstanding token for the same call so it can't re-arm the write.
  - Denial text now instructs the replay arg prominently:
    "TO EXECUTE: replay the SAME call adding confirm_token='cfm-…' —
    or resend the identical call with confirmed=true."
- `backend/realtime_provider.py`
  - `_confirm_retry_pending()` filters the confirm envelope keys and
    asks the runner whether this exact call is pending.
  - The confirmed-strip branch now passes `confirmed=true` through when
    an identical call already has a live token (the model's actual
    retry form — card option a) and emits a `confirm_retry_accept` ops
    event; otherwise it strips as before (`confirm_strip`).
  - `failed_results_this_turn` counts ok:false results;
    `phantom_write_claim` now fires when a write claim rides on any
    failed result, not only on zero tool calls.
  - `_PHANTOM_CLAIM_RE` gained save-verbs
    (saved|stored|memorized|jotted down|kept a note|บันทึกแล้ว|จำไว้|เก็บไว้).
- `tests/scenarios/confirm_token_replay.yaml` (new) — token replay E2E:
  deny → token replay executes → recall finds the write → consumed
  token refused → confirmed=true resend executes → pending token burned
  → audit ledger.
- `tests/test_tool_runner.py` — pending_confirm tracking/expiry,
  grant-burns-token, denial-text assertions.
- `tests/test_provider_events.py` — `_confirm_retry_pending` envelope
  filtering/fail-closed, phantom regex save-verb coverage.
- `docs/ssot/jobs/ada/2026-10-07-ada-remember-confirm-flake.yml` —
  decision record (chose card option a; trade-off documented).

## Result

Verified live in-process: first identical call denied + token minted,
`pending_confirm` true for identical args only, confirmed=true resend
executes (verb: create), pending token burned, different args still
strip/deny. The propose → deny → retry protocol is preserved — only the
retry of the exact proposed call is accepted without voice affirmation.

## How to verify

```
~/CascadeProjects/ada-pi/.venv/bin/python -m pytest \
  tests/test_tool_runner.py tests/test_provider_events.py \
  tests/test_scenarios.py tests/test_ops_events.py \
  tests/test_memory_banks.py tests/test_tools_loader.py \
  tests/test_recall_gate.py tests/test_decision_check.py \
  tests/test_memory_recall_extras.py -q
```

All green (380+ tests). Note: `tests/test_tool_audit.py` has a
pre-existing lint failure on the untouched baseline (ada_track_device
descsize + ada_device_acl coverage) — not caused by this change.

Watch in prod: `confirm_retry_accept` vs `confirm_strip` ops events in
ada-ha-events-<instance>, and a possible initial rise in
phantom_write_claim volume (now counts failed-result turns too).
