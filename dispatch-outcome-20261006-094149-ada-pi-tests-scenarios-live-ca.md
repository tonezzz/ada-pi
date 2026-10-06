# Dispatch outcome — vcast-scenarios (ada-pi cast suite gap coverage)

Second dispatch on this card. The first run (20261006-081312) built and
live-verified the work but ended with a dirty worktree — auto-merge blocked
on uncommitted files. This run ported that diff into a fresh worktree,
re-verified live, and committed it.

## What changed (ported from dispatch-081312, reviewed)

- `scripts/scenario-live.py` — new `vcast_display` turn primitive: the
  driver claims a REAL screen on the live input-bridge (pending → claim →
  paired → registered, tailnet-trusted + relay `ADA_ADMIN_KEY`), then can
  `flap` (ws drop/re-register), `drop` (stay-down; asserts the registry
  marks the screen offline — the flap-detection assert), `reattach`
  (same-screen re-register + lastCast replay), `pub` (driver-side cast
  into the room while the display is down — exercises the
  remember-and-replay path deterministically), `release` (unclaim +
  revoke; auto-run in cleanup). Claimed screen number is exposed as the
  `{flap_screen}` runtime token. Claim failure aborts as INFRA (exit 3).
  Also: `{real_screen2}` token (VCAST_REAL_SCREEN2, default 7), `or_state`
  list form (every entry must hold for the call-assertion waiver) and an
  `or_state` `contains_any` variant.

- New scenarios (`tests/scenarios-live/`):
  - `cast_multi_device.yaml` — one ask casts to two real displays
    ({real_screen}+{real_screen2} = 6+7); or_state list asserts BOTH
    registries show the page; teardown idles both.
  - `cast_during_ha_restart.yaml` — ada-ha restart window via the
    `reconnect` turn (same ws-drop shape); the cast must survive on relay
    ground truth and Ada must report it honestly post-reconnect.
  - `vcast_flap_recovery.yaml` — claim → Ada casts → flap x3 → drop
    detected → Ada must not claim a cast landed while down → deferred
    driver pub replays on reattach → teardown + release.

- `tests/scenarios-live/cast_interrupt_gate.yaml` — T3 stale expectation
  fixed: an explicit wall ask isn't confirm-gated, so "yes" may have
  nothing to execute; now carries `or_state {vcast_screen: 4, contains:
  rama9}` — pass iff the wall is actually up on either path.

- `tests/benchmark.yml` — `casting` suite + `write_allowed_in` gained the
  three missing named scenarios + the three new ones (all 10 gate
  scenarios now listed).

- `docs/ssot/jobs/ada/2026-10-06-vcast-scenario-coverage.yml` — decision
  record (restart window → reconnect turn; flap e2e → vcast_display).

## Result — live verification vs ada-lab (idc02:8012 via ssh fwd :18012)

This session: `cast_multi_device` PASS, `cast_during_ha_restart` PASS,
`cam_to_screen` PASS (existing-suite regression spot check).

`vcast_flap_recovery` correctly aborted INFRA (exit 3): relay `/claim`
needs ada auth at idc01:8001 but `ada-ha-tony` was stopped 09:07 (clean
shutdown — reason unknown, looks like an outage; the other ada scenario
units on idc01 are also failed). First dispatch verified the same code
fully green ~30 min before that outage (claim → cast → flap x3 → drop
detect → deferred pub → reattach replay → release). Relay registry left
clean after my probe — no pending/stale entries.

Prior dispatch: all 3 new scenarios PASS; 6/7 existing named scenarios
PASS; `camera_cast_permission` T3 red on lab's unconfigured calendar —
environment-dependent, not touched by this change.

## How to verify

```bash
ssh -N -L 18012:127.0.0.1:8012 idc02 &
ADA_API_KEY=<lab key> python3 scripts/scenario-live.py \
    tests/scenarios-live/<name>.yaml --url ws://127.0.0.1:18012/ws
# flap e2e additionally needs ada-ha-tony on idc01 up (relay claim auth)
```

Committed on `dispatch/20261006-094149-ada-pi-tests-scenarios-live-ca`;
not pushed/deployed per dispatch rules.
