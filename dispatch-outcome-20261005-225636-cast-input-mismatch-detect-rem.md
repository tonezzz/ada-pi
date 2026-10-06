# Dispatch outcome — 20261005-225636-cast-input-mismatch-detect-rem

Card: `cast-input-mismatch` (vcast program — cast lands but TV stays on
STB input; detect + remediate path)
Branch: `dispatch/20261005-225636-cast-input-mismatch-detect-rem` —
commit `34d3cf1` (6 files, +556/-6). Not pushed, not merged, not deployed.

## What changed

**`backend/tool_runner.py`** — new TV input-visibility lane:

- `_tv_cfg()` — reads a `tv` block from the cast-screens.json registry
  (`{"entity": "media_player.tony_tv", "screens": [..], "ok_apps":
  ["browser"]}`); env overrides `ADA_TV_ENTITY`, `ADA_TV_SCREENS`,
  `ADA_TV_OK_APPS`. `ok_apps` match the media_player's
  app_id/app_name/source as substrings.
- `_tv_input_state()` — reads the TV entity via HA and returns
  `{entity, state, app, on_cast_app}` where `on_cast_app` is True
  (browser foreground), False (foreign app/input or TV off), or None
  (HA unreachable/entity missing/no app attr — never blocks the cast).
- `_tv_input_verify(url)` — on a confirmed mismatch (False), runs the
  proven 2026-10-04 remediation: push the URL at the TV browser via
  `tv_action nav` (foregrounds the browser app → input switches), then
  re-reads the app. Result block: `tv_input_mismatch`, `tv_input`,
  `tv_remediation` (action/url/ok/after_app), `tv_input_after`, and a
  `tv_note` the caller merges into its `note` — "now showing" on
  recovery, a blunt "NOT visible — do not claim it is showing" when the
  TV stays on the wrong input.
- Wired in: `cctv_wall` start, `cast_to_screen` nav/play/image/audio,
  and `ada_camera_snapshot`'s direct screen pub — all only for screens
  in the tv.screens mapping. `tv_action cmd=nav` gets its own post-check
  appending `tv_input` + `input_warn` (the nav IS the remediation path —
  it reports instead of re-navving). `vcast_list` surfaces `tv_input`
  and flags TV-hosted screens `on_tv: true`.
- Root-relative vcast paths (`/apps/camwall/…`) are resolved against the
  public relay origin before the TV-browser push.
- Out of scope, noted in the job record: `yt_cast` targets
  `media_player.tony_tv_cast` (Chromecast) — a different input-switch
  lane.

**`tests/scenario_engine.py`** — new scenario fixtures: `env:` (temp
os.environ), `cast_screens:` (temp registry via ADA_CAST_SCREENS),
`ha_states:`/`ha_states_seq:` (stub `ha_client.get_state`, seq consumed
in call order), `ha_tv_action:` (cast-browser return body), `vcast:`
(stub `ToolRunner._vcast_api` per path; `{error}` raises).

**`tests/scenarios/cast_input_mismatch.yaml`** — replays the incident:
STB→browser shows `tv_input_mismatch` + remediation + recovery; STB→STB
reports `NOT visible` instead of claiming success; `vcast_list`
surfaces the app; a non-TV screen never touches HA.

**`tests/test_tool_runner.py`** — `TvInputMismatchTests`, 9 tests.

**`backend/tool_guide.yml`** — tv_input/input_warn documented on
tv_action, cast_to_screen, cctv_wall.

## Result

- `pytest tests/test_tool_runner.py::TvInputMismatchTests` — 9/9 pass.
- `pytest test_scenarios::test_cast_input_mismatch` — pass.
- Full touched-area suite (`test_tool_runner`, `test_scenarios`,
  `test_tool_audit`, `test_home_assistant`, `test_frontend_contract`):
  220 pass. `scripts/tool-lint.py`: clean.

## Operator step needed

The per-screen check engages via the `tv` block in
`~/.local/share/ada-pi/cast-screens.json` on idc01 — add e.g.
`"tv": {"entity": "media_player.tony_tv", "screens": [<n>], "ok_apps":
["browser"]}` once the TV's vcast screen number is confirmed (live
`/displays` shows no TV-labeled screen right now), or set
`ADA_TV_SCREENS` in the service env. `tv_action` post-nav checks and
`vcast_list` surfacing work immediately without it.

## How to verify

- `pytest tests/test_tool_runner.py::TvInputMismatchTests -q`
- `pytest tests/test_scenarios.py -k cast_input_mismatch -q`
- Manual (per card): cast a wall to the TV's screen with the TV on the
  True input — the tool result should carry `tv_input_mismatch`, the
  `tv_browser_nav` remediation, and the TV should switch to the browser.
