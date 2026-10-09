# control_entity: verify state delta after actuation

## What changed

`backend/tool_runner/ha.py` — `control_entity` now routes adjustable
actuations through a new `_control_verified` wrapper that reads the entity
state before the service call and polls it afterward (4 polls, 0.7 s
interval, mirroring `tv_action`'s `cast_verify` pattern). A new
`_CONTROL_VERIFY` map declares the ground truth per verb:

- `media_player` `volume_up`/`volume_down` → `volume_level` delta
- `media_player` `volume_mute`/`mute` → `is_volume_muted` delta
- `media_player` `select_source` → `source` must reach the requested value
- `cover` `open`/`close` → goal state (`opening`/`open` /
  `closing`/`closed`) or `current_position` moving the right way; opposite
  and dead states (`off`/`standby`/`unavailable`) fail fast
- `cover` `stop` → state must leave `opening`/`closing` — still-moving
  after stop fails loudly

When the watched value doesn't move and the state is observable, the
result is `ok:false` with a `verify` block (`before`/`after`/`delta`/
`state`) and an error that explicitly says HA accepted the call but the
device ignored it and the model must not claim it changed. When the state
read fails or the attribute is absent, verification is inconclusive
(`verify.ok=None`, no `verify` block attached) and never flips a call to
`ok:false`. Non-adjustable verbs pass through untouched.

Supporting changes:

- `backend/tool_guide.yml` — `control_entity` entry now explains the
  `verify` block and the `ok:false`-on-swallowed-call contract.
- `tests/test_tool_runner.py` — new `ControlEntityVerifyTests` (12 cases):
  moved/no-op volume, mute toggle, source match/mismatch, cover open via
  state and via position delta, swallowed cover open, still-moving cover
  stop, unverified passthrough, HA read failure and missing-attribute
  inconclusive cases.
- `tests/scenario_engine.py` — new `ha_entities:` stub key so
  `control_entity` can pass the confidence gate offline, and
  `runner.memory.mddb` is repointed to the in-memory fake for hermeticity.
- `tests/scenarios/control_entity_verify_state.yaml` — offline replay of
  the 2026-10-08 incident plus mute-inconclusive and cover fail cases.

## Result

The reported bug is fixed: a `volume_down` that HA accepts but the paused/
off cast player swallows now returns `ok:false` with the unchanged
`volume_level` instead of `ok:true`, so Ada can no longer say "done" when
nothing happened. `select_source` returning a different source also fails.
Cover `open`/`close`/`stop` get the same treatment since covers are the
only position-control surface — the codebase has no `brightness` control
action in `control_entity` (no light actuation path exists there), so the
card's brightness/position wording is covered where reachable.

## How to verify

```
cd <worktree>
.venv/bin/python -m pytest tests/test_tool_runner.py -q   # 229 passed
.venv/bin/python -m pytest tests/test_scenarios.py -q     # 13 passed
```

(`.venv` created in the worktree with `--system-site-packages`; system
python lacked `google-genai`/`starlette` needed for test collection.)

## Not done / notes

- No commit, push, or deploy — work stays on
  `dispatch/20261009-175933-control-entity-verify-state-de` in the
  worktree.
- Pre-existing tool-lint failures (voice_fx, yt_cached_list) are
  unrelated and left alone; lint is advisory in CI.
- Polling adds ~0–2.1 s to verified calls (exits early on success/dead
  state; the full 4-poll window only when the entity is alive but the
  attribute hasn't moved yet — that path returns `ok:false` anyway).
