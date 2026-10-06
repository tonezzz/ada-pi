# Dispatch outcome — tv-cast-gev-path (20261006-111008)

## Task 1 — which path did tv_action take at 11:51–53 2026-10-05

CONFIRMED from the cast-browser journal on tony-omen
(`journalctl --user -u cast-browser`, times +07; the card's "11:51–53"
is UTC = 18:51–53 +07). Six `runCommand` entries, all
`speaker:person.tony`, all served by `POST http://100.75.102.88:8799/cmd`
— the **direct CAST_BROWSER_URL path**, not the HA rest_command fallback:

- 5× `{cmd:nav, text:https://tony-dell.taila0626a.ts.net/apps/gev/}`
- 1× `{cmd:scroll, text:down}`

Each nav → `gotoInternal` → headless Chromium loads GEV → `shotAndCast`
→ PNG screenshot → `media_player.play_media` image/png on the cast
target → `{ok:true, cast:"cast:200", url}`. The scroll re-shot →
`{cast:"cast:200"}`. So the sessions' `ok=True` was literal truth — a
**static PNG** cast succeeded; the TV never got live GEV (the PNG was
whatever Cesium had painted ~4s in, plus the off/idle-Chromecast
200-no-op class could also mean nothing appeared at all).

The card's hypothesis (a) — Ada calling `nav gev` and getting ok despite
'unknown key' — is NOT what happened in that window. A real `nav gev`
shows at 20:40 +07 (no speaker, manual probe); it returns
`{ok:false, err:"unknown key gev"}` and already raises loudly via the
err-body check in `ha_client.tv_action` (present since 7c440ba, Oct 2).

## Task 2 — GEV support decision

**screenlive lane, not a KEYS entry.** A 'gev' key in cast-browser KEYS
funnels into `gotoInternal + shotAndCast` → PNG, always. Live video
exists only via the desktop-stream lane: `nav tony-omen:workspace:N`
(omen's physical ws → cast-desktop@0 x11grab→HLS index0.m3u8 → served at
:8082 → `camera.tony_omen_desktop_1` → `camera.play_stream` HLS to the
cast target). The `screenlive:*` spelling targets the tony-dell seat
(display :1 doesn't exist on omen; `camera.desktop_1` reads tony-dell's
dir) — the `tony-omen:` spelling is the only working lane here.

Implemented in `backend/tool_runner.py`: `_gev_tv_target()` rewrites GEV
nav intents (`gev`, god's-eye-view spellings, **and /apps/gev/ URLs** —
the exact call Ada made) to `tony-omen:workspace:4` before the ACL check
(so it lands on the owner-locked 'omen' source). Override:
`ADA_GEV_TV_TARGET` or `cast-screens.json` `"gev".target`. Result carries
`gev_lane` + `gev_note`. Tool description + `tool_guide.yml` updated.

## Task 3 — fail loudly

`_tv_cast_verify()`: any tv_action result carrying `cast`/`stream` now
resolves the real cast target (`input_select.tv_cast_target` →
`media_player.tony_tv_cast`; `ADA_TV_CAST_ENTITY` override) and polls
~7.5s for it to leave off/idle/standby/unavailable/unknown. Still dead →
result flips to `ok:false` + error "nothing reached the TV". HA-down =
inconclusive, never fails. Off-Chromecast 200 no-ops can no longer
report ok.

## Task 4 — coverage

- `tests/scenarios/tv_cast_gev_live.yaml` — offline regression (rewrite
  for key/URL, plain URL untouched, verify pass + fail). Passes.
- `tests/scenarios-live/gev_to_tv_live.yaml` — tier:full live scenario
  (nav gev → camera.play_stream markers → HA playing check → stop).
- `TvGevLaneTests` ×10 in `test_tool_runner.py`. 210 tests pass total.
- `tests/benchmark.yml`: `gev_to_tv_live` in write_allowed_in, casting,
  and gev suites.

## Omen provisioning (live, done)

- `gev-workspace.service` + `~/.local/bin/gev-workspace.sh`: dedicated-
  profile `google-chrome --app=…/apps/gev/` parked maximized on XFCE ws4
  (re-park loop every 15s). Verified: window on desktop 3, GEV answering
  `get_current_view_state` through the gev-gemini bridge.
- `cast-stream-http.service` + `~/.local/bin/cast-stream-http.sh`:
  `python3 -m http.server 8082` on `~/.local/share/cast-stream` (with
  `tony-omen/desktop` symlink → cast-desktop/desktop) — restores the URL
  `camera.tony_omen_desktop_1` expects (the old caddy-tony-omen :8082
  block is disabled). Verified `curl :8082/tony-omen/desktop/index0.m3u8` → 200.

## Caveats / follow-ups

- The lane switches tony-omen's physical workspace to 4 and leaves it —
  inherent to the `tony-omen:workspace` target (the stream IS the
  physical display). GEV lives on ws4 permanently via the service.
- `screenlive:*` from omen remains broken (starts @N on :N; only :0
  exists) — out of scope; documented.
- A real cast-browser-side `nav gev` handler (provision+stream+play in
  one) would be cleaner but that file is hand-managed on omen, not in
  this repo.
- First live run needs Tony present to eyeball the TV; not fired
  unattended (the TV is on True STB — a cast would hijack it).

## Verify

```
~/CascadeProjects/ada-pi/.venv/bin/python -m pytest \
  tests/test_tool_runner.py -k TvGevLane -q          # 10 pass
~/CascadeProjects/ada-pi/.venv/bin/python -m pytest \
  tests/test_scenarios.py -k tv_cast_gev_live -q     # 1 pass
ssh tony-omen 'systemctl --user status gev-workspace cast-stream-http'
```

SSOT trail: `docs/ssot/jobs/ada/2026-10-06-tv-cast-gev-live.yml`.
