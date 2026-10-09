# dispatch-outcome: ada-scenario-casting-hang — tony-dell follow-up (attempt 2)

Card: `ada-scenario-casting-hang`. This session is the approved "tony-dell
session" for the raised request `tony-dell-side-needed-no-ssh-from-idc03-eb011b`
(Tony answered "dispatch-it" at 19:35). The ada-pi-side fix from attempt 1
already merged as `3a1b089f` (gev_relay/vcast_online preflights, bounded
connect(), flushed progress, live-progress doc, --max-scenarios/--budget-s).

## Root cause (confirmed from attempt 1): not a hang

`ada-scenario-casting.service` was killed at TimeoutStartSec every night while
still actively churning scenarios — suite needs ~3.5-4h vs the 2h (now 2.5h)
cap; doc only written at suite end → zero artifacts; block-buffered stdout
(pre-PYTHONUNBUFFERED) made it look silent. Amplifier: the gev-cmd edge route
404'd all night since chaba `7cb549b1` (generated Caddyfile dropped
`uri strip_prefix /apps/gev-cmd`) — ~16 gev_* scenarios each burned ~8min
failing twice ≈ 2h wasted inside the lock, starving 02:00/03:00 smoke.

## This session — tony-dell-side items

### 1. gev-cmd relay: FIXED + verified end-to-end
- `strip: /apps/gev-cmd` added to the gev-cmd route in
  chaba-tony-dell `docs/ssot/infrastructure/ssot.routes.yml` (uncommitted —
  chaba-tony-dell's kanban-commit.timer auto-committer will sweep it; renderer
  emits `handle_path /apps/gev-cmd/*` for it). Live
  `stacks/web/Caddyfile` (bind-mounted into the `web` container) updated
  and caddy reloaded — found already applied at ~19:40; this session verified.
- Verified: `POST /apps/gev-cmd/command` (`get_current_view_state`, wait=1)
  → **200 with real view-state JSON, delivered=2**, both via
  `http://127.0.0.1:8080` on tony-dell and from idc03 via
  `https://tony-dell.taila0626a.ts.net`. Bridge itself serves only
  `POST /command` + `GET /command/health` on 127.0.0.1:8790 (inside the
  gev-gemini container — same bridge.py owns both 8789 ws and 8790 cmd).
- `render-routes.py --check`: 51 routes, 18 warnings — all pre-existing
  shadow-order notes, none for gev-cmd.
- Bridge journal now shows `POST /command 200` (19:46+).

### 2. gev-gemini Gemini key: CHECKED — dead, needs Tony
- The identical 53-char `AQ.…` credential sits in all three stores:
  `~/.config/secrets/gemini-api-key.env`, `gemini-mic-test.env`, and the
  podman `gemini-api-key` secret (sha8 c42bd447).
- 401 on `/v1beta/models` both as `?key=` and as Bearer — dead either way.
- Real-path proof: ran `gev_ws_tool_roundtrip` on idc03 — preflight reached
  the bridge ws but got a clean 1000 close (bridge can't establish the Live
  session) → classified INFRA correctly; gev-gemini journal shows
  `client_handler setup error: 1008 … invalid authentication credentials`
  (18:13–18:19 today).
- Fix path for Tony: drop a fresh AI Studio key into
  `~/.config/secrets/gemini-api-key.env`, then
  `podman secret rm gemini-api-key && podman secret create gemini-api-key <file>`
  + `systemctl --user restart gev-gemini.service`. gev-cmd command lane is
  unaffected (it doesn't use the key).

### 3. idc03 deploy gap — request raised on the card
- Deployed checkout is `8b3896a` (pre-fix; origin/main has it).
- Unit already has `PYTHONUNBUFFERED=1` + `TimeoutStartSec=9000`, but no
  `BENCH_BUDGET_S`; a healthy suite is ~3h > 9000s → Sat 02:00 run on old
  code will still be SIGTERM'd with no docs (== lines will stream now).
- Board request `gev-cmd-relay-restored-verified-200-e2e-be01aa` asks:
  OK to `deploy-ada.sh` + add `Environment=BENCH_BUDGET_S=8400` to the unit
  (suggested: deploy-and-budget). Unanswered at session end (~20:05).

## DONE-WHEN status

"stall root cause identified and fixed/acked" — met: timeout exhaustion
(not a stall), relay-404 amplifier removed and verified live, ada-pi fixes
merged. "casting run completes + writes doc" — demonstrated by attempt 1's
bounded run (casting-20261009-185732); a full overnight completion still
needs the deploy+budget pin (awaiting answer).

## Files touched this session

- `docs/ssot/jobs/ada/2026-10-09-casting-suite-timeout.yml` — added
  `verify-2:` block (relay restored + verified, key dead, deploy gap) and
  rewrote `still-open` (key replacement steps, suite-length options).
- chaba-tony-dell `ssot.routes.yml` / `stacks/web/Caddyfile` — the strip fix
  was already in place on arrival (applied ~19:40, uncommitted); verified
  rather than authored here.

lessons:
- chaba edge: the *live* Caddyfile is bind-mounted from chaba-tony-dell
  (the single-writer checkout), not ~/CascadeProjects/chaba — check mounts
  (`podman inspect web`) before editing; drifts between Caddyfile and
  Caddyfile.generated are normal (hand-tuned blocks on top).
- gev-cmd bridge shares the gev-gemini container/process: :8789 = ws,
  :8790 = cmd (POST /command, GET /command/health only) — restart via
  gev-gemini.service.
- Podman file-driver secrets decode base64 from
  ~/.local/share/containers/storage/secrets/filedriver/secretsdata.json —
  compare by sha256, never print.
- The `AQ.…`-prefixed "Gemini key" deployed everywhere 401s as both
  ?key= and Bearer — it is not a valid API key; replaced needed.
- A `gev_bridge` scenario turn on a dead-key bridge ends INFRA via clean
  1000 close — preflights correctly classify environment vs model failure.
