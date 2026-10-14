# ada-get-home-state-plug-omission — dispatch outcome

## Filter cause (the reported question)

`get_home_state`'s `all_plugs`/`plugs_on` are built from
`HomeAssistantClient.snapshot()` (`backend/home_assistant.py`), which
populated `plug_states`/`plug_names` **only** from
`self.home_plug_entities` — the `HOME_ASSISTANT_HOME_PLUGS` env whitelist
(falling back to `DEFAULT_HOME_PLUGS`, the five office lights). On ada-kk
that env contains just `switch.plug_tv`, so `switch.plakkaaaef_local`
never entered the dict.

It was **configured-entity whitelisting**, not a name pattern, not
availability (the entity was available+off), and not domain scoping in
the tool. `home_search` found it because `entities()`/`search_entities()`
scan all of `/api/states` under `DISCOVERABLE_DOMAINS` — a different
selection path, which is why the two tools disagreed in the same session.

## What changed

- `backend/home_assistant.py`: new module constant `HOME_PLUG_ENTITY_RE`
  = `switch\.(plug|plak|usb_test_hub)` — the same plug family the safety
  classifier already used inline. `snapshot()` now builds the plug set as
  configured list **∪** discovered matches in the live states, so
  `plug_states`, `plug_names`, and `plugs_on` all cover the union (a
  discovered plug that is on also lands in `plugs_on` — no
  self-contradictory summary).
- `backend/tool_runner/common.py`: `_classify_and_sync` imports
  `HOME_PLUG_ENTITY_RE` instead of carrying its own regex copy — one
  source of truth for "what counts as a mains plug".
- `backend/office_lights.py`: `evaluate()` narrows `snapshot.plugs_on`
  back to the configured `home_plug_entities` — a discovered TV plug
  left on must not read as the "office lights left on" habit.
- `tests/test_home_assistant.py`: new
  `test_snapshot_discovers_plug_family_beyond_configured_list` — env
  `HOME_ASSISTANT_HOME_PLUGS="switch.plug_tv"`, states carry
  `switch.plakkaaaef_local` (off), `switch.plug_fan` (on), and a
  non-plug switch; asserts the plak* entity survives `plug_states`/
  `plug_names`, non-plugs stay out, and an on-state discovered plug
  lands in `plugs_on`.
- `docs/ssot/jobs/ada/2026-10-14-get-home-state-plug-discovery.yml`:
  job trail (root cause, decisions, verification).

Both `get_home_state` paths consume the same snapshot
(`tool_runner/ha.py` and the realtime_provider fast path ~L5297), so the
fix covers both.

## Result / verification

- New unit test passes; `tests/test_home_assistant.py` +
  `tests/test_office_lights.py`: 22 passed.
- Full suite: `python3 -m pytest tests/ -x -q` → 1415 passed, 7 skipped.
- `python3 scripts/tool-lint.py` → clean.
- Post-deploy live check on ada-kk: `get_home_state` `all_plugs` should
  list both `switch.plug_tv` and `switch.plakkaaaef_local`.

Not committed, not pushed, not deployed (dispatch scope).
