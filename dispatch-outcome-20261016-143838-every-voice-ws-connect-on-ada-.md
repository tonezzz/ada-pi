# Dispatch outcome — ada-gemini-live-spend-cap (attempt 3)

Session: 20261016-143838-every-voice-ws-connect-on-ada-
Branch: dispatch/20261016-143838-every-voice-ws-connect-on-ada- (commit 690e013, not pushed)

## What this run found

The card's actual deliverable — per-lane budget enforcement — was
already built by the Oct-15 dispatch and merged into this worktree at
`18c0a01` (commits `7d317b4` + `0d53bc9`): `backend/lane_budget.py`
(lane USD cap from the usage ledger + provider-cap latch keyed by key
fingerprint), wired through `GeminiLiveProvider.connect()`,
`live_manager` (`budget_hold` status + `live_budget_hold` broadcast),
`pwa_server` reconnect loop, both frontends, and
`conversation_health()["lane_budget"]`. Trail doc:
docs/ssot/jobs/ada/2026-10-15-lane-budget-enforcement.yml.

The reason attempt 2 "did not verify cleanly" was almost certainly NOT
the lane-budget code: the deploy gate `pytest tests/ -x -q` failed on
`test_line_relay_group.py::test_unmapped_member_refused_once` — an
uptime-dependent bug in the relays, not a regression from the merge
(neither file was touched by it).

## Root cause of the verify failure (fixed this run)

`_refuse_group_member` rate-limiter used `self._refused.get(key, 0.0)`
as the "never refused" sentinel, then compared
`time.monotonic() - last < _REFUSAL_GAP_S` (3600s). `time.monotonic()`
is seconds since boot, so on any host with uptime < 1h the first-time
sender computes `uptime - 0 < 3600` → True → the refusal is silently
dropped. tony-dell had been up ~19 min when the suite ran. Same
sentinel pattern existed in `tg_relay.py` (inverted comparison, same
failure mode).

Fix (commit `690e013`, 4 changed lines): default `last` to `None` and
only apply the gap when a prior refusal timestamp actually exists —
`line_relay.py:1186` and `tg_relay.py:553`.

## Result

- `pytest tests/test_line_relay_group.py` — 28 passed (was 27+1 fail)
- `pytest tests/test_lane_budget.py` — 29 passed
- Full suite `pytest tests/ -x -q` — **1489 passed, 7 skipped** (was
  failing at test 504 on the refusal test)
- `python3 scripts/tool-lint.py` — clean (61 warnings, all pre-existing)
- Deploy gates now pass on this tree regardless of host uptime.

## Ops note (unchanged from attempt 1)

Enforcement stays OFF per lane until `ADA_LANE_MONTHLY_BUDGET_USD` is
set in each lane's env (`~/.config/secrets` on idc03); the provider-cap
latch is active regardless. Recommended: just under the AI Studio hard
cap. Smoke on ada-dev per the jobs YAML `ops_pending` section.

## Board comms

127.0.0.1:8787 was connection-refused for the whole session — no card
comms could be read or posted. This file is the record.

lessons:
- `time.monotonic()` counts from boot, not epoch — a `.get(key, 0.0)` sentinel on a monotonic timestamp silently suppresses first-use behavior for the whole rate-limit window on any host up < the gap. Use `None`.
- `pytest tests/ -x -q` in the deploy gate makes ambient, unrelated test failures (like a fresh-boot uptime flake) a hard stop for unrelated merges — verify failures on a dispatch run may be environmental, check `uptime`/env before assuming the merged diff broke it.
- Board API (127.0.0.1:8787) was down this session; `docs/ssot/kanban/` and `scripts/ci/card-pipeline.py` don't exist in the ada-voice worktree — card comms and CI opt-in live outside the repo.
- Pre-existing cosmetic bug spotted, left alone: `self._client = genai.Client(...)` is duplicated back-to-back at realtime_provider.py:4950-4951 (present in base 5dc7665, harmless — constructor makes no calls).
