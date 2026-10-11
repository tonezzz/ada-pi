# dispatch-outcome — home-search-unknown-state-entities

## What the audit found

**No `unknown`/`unavailable` state filter existed in the device-search
pipeline.** `HomeAssistantClient.entities()`
(`backend/home_assistant.py`) has always included every
`DISCOVERABLE_DOMAINS` entity and annotates them with
`available: false` plus the raw `state`. The whole chain passes that
shape through untouched:

- `home_search kind=device` (`backend/tool_runner/ha.py`) →
  `search_entities()` / `entities()`
- the realtime fast-path in `backend/realtime_provider.py` (~L5062)
- `GET /api/home-assistant/entities` in `pwa_server.py` (returns
  `ha_client.entities()` verbatim; the frontend disables the power
  button on `available:false` but still renders the card)
- `AdaMemoryStore.search_devices` (searches the same `entities()`
  snapshot)

**The real hiding mechanism found instead:** `entities()` dedups by HA
`object_id` across *all* discoverable domains, keeping the highest
`DOMAIN_PRIORITY` entry. A `switch.gate_motor` / `light.gate_motor` /
`input_boolean.gate_motor` sibling (Tuya curtain motors commonly ship
one) silently erased `cover.gate_motor` — the cover vanished from
search and `/api/home-assistant/entities`, making the physical gate
unfindable and uncontrollable by voice regardless of its state. The
dedup was built for the "same load as light.foo + switch.foo" case and
was over-applied to actuator domains.

## Changes

- `backend/home_assistant.py` — `entities()`: the object_id collapse
  now applies only inside `CONTROLLABLE_DOMAINS`
  (light/fan/switch/input_boolean). `cover`/`button`/`media_player`
  entities key on their full entity_id — a different actuator is never
  a duplicate load. `search_entities()`: equal-score results now sort
  available-first (unavailable entities stay in results, flagged
  `available:false` — annotated + ranked down, never dropped).
- `backend/tool_runner/ha.py` — `home_search`/`search_home_devices`
  now pass `limit` through to `search_entities` (the declared
  `limit=` default 10 was silently ignored; `search_entities`
  defaulted to 5). Same fix in the `realtime_provider.py` fast-path.
- `tests/test_home_assistant.py` — +3 tests: `cover.gate_motor`
  survives beside a `switch.gate_motor` sibling; `search_entities`
  returns `state=unknown` and `state=unavailable` devices flagged
  `available:false`; unavailable ranks below available at equal score.
- `docs/ssot/jobs/ada/2026-10-11-home-search-unknown-state-entities.yml`
  — trail.

## Verification

- `python3 scripts/tool-lint.py` — clean (exit 0): 46 declared, 92
  aliases.
- `ADA_INSTANCE_ID=test ../ada-pi/.venv/bin/python -m unittest
  tests.test_tool_runner tests.test_home_assistant` — 287 tests, all
  pass (13/13 in test_home_assistant incl. the 3 new ones). The five
  `plan_day` errors seen *without* `ADA_INSTANCE_ID` are a pre-existing
  env requirement, unrelated to this change.
- Live check pending deploy: on ada-ha-michael, `home_search
  query="gate" kind="device"` returns `cover.gate_motor` with
  `available:false` even while its state is `unknown`, and any
  `switch.gate_motor` sibling now appears alongside instead of hiding
  the cover.

Not committed / not pushed / not deployed — changes are on branch
`dispatch/20261011-082400-self-contained-spec-this-card-` in this
worktree for review + the normal idc03 deploy flow.

Note: `AdaMemoryStore._classify_and_sync` still labels
unknown/unavailable devices `trusted_broken` in confidence groups —
that's a label, not a filter, but a permanently-`unknown` Tuya cover
inflates the "N broken" count in `ha_confidence`. Left as-is (out of
card scope).
