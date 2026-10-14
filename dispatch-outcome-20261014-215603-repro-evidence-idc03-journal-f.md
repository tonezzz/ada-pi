# Dispatch outcome — ada-control-entity-phantom-ok

## Resolver path found (the repro)

`control_entity` (backend/tool_runner/ha.py) dispatches `switch.*` to the
on/off path → `HomeAssistantClient.set_power` (backend/home_assistant.py)
POSTs `/api/services/switch/turn_off` → **HA returns 200 with an empty
changed-states list even for entity_ids that do not exist** → `set_power`
ignored the response body and echoed the *requested* state
(`{entity_id, state:"off"}`) → `normalize_tool_result` wrapped the
formatted string as `{ok: true, output: "Turned off switch.coffee_plug…"}`.

No fuzzy resolution and no stub/mock path on the kk lane — the ok was
fabricated from the request args, not read back from HA. The real plug is
`switch.plakkaaaef_local` ('Coffee plug', mains-plug → dangerous class).

## Fix

- `control_entity` now runs `_control_precheck(entity_id)` before domain
  dispatch: one `GET /api/states/<id>` serves both the ghost check and
  the before-state read (no extra HA roundtrip vs the verified paths).
  - **404** → `{ok: false, error: "'<id>' does not exist… Did you mean
    switch.plakkaaaef_local ('Coffee plug')?…", close_matches: [...]}`
    — nothing was actuated, matches come from `search_entities` on the
    object_id words (`coffee_plug`→`coffee plug`), same-domain list as
    fallback. Covers ghost cover/button/media_player ids too.
  - non-404 get_state failure (transport/auth) → proceeds; the actuate
    call reports its own error.
- on/off calls now verify via new `_control_verify` kind `state_target`:
  poll up to 4×0.7s until `state == "on"|"off"`; a state that never
  lands returns ok:false "stayed '<x>' … do NOT claim it changed".
  Success keeps the narrated `output: "Turned off <eid>: …"` string,
  plus a `verify` block.

## Files changed

- `backend/tool_runner/ha.py` — `_control_precheck`, `_control_close_matches`,
  `state_target` kind, `_control_verified(before=)` param, on/off verify.
- `backend/home_assistant.py` — comment: set_power echoes requested state,
  not ground truth.
- `backend/tool_guide.yml` — control_entity entry documents ghost-id and
  on/off verify contracts.
- `tests/scenario_engine.py` — `ha_404:` fixture (get_state raises httpx
  404), `search_entities` stubbed with the real scorer over `ha_entities`,
  `set_power` echo stub.
- `tests/scenarios/control_entity_ghost.yaml` — new: ghost→ok:false+match,
  real turn_off verified, swallowed turn_off→ok:false.
- `tests/test_tool_runner.py` — 6 new ControlEntityVerifyTests; stateful
  set_power/get_state fixture in HaMergeAliasTests.
- `docs/ssot/jobs/ada/2026-10-14-control-entity-ghost-ok.yml` — job trail.

## Verify

- `unittest tests.test_tool_runner tests.test_command_lane
  tests.test_memory_banks tests.test_ops_events` — 398 OK
- `unittest tests.test_scenarios` — 19/19 OK (needs ADA_INSTANCE_ID=test
  when run without pytest/conftest)
- `unittest tests.test_home_assistant` — 13 OK
- `scripts/tool-lint.py` — clean

Not committed/pushed/deployed (dispatch worktree rules). Note: pwa_server
`/api/set_power` and `main.py` still call `ha_client.set_power` raw —
direct REST path, out of card scope.
