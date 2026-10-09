# dispatch-outcome: ada-scenario-casting-hang — reproduce/diagnose

## Verdict: NOT a stall — timeout exhaustion + zero observability

`ada-scenario-casting.service` ran fine both nights and was SIGTERM'd at
TimeoutStartSec mid-suite. Evidence:

- ada-ha-tony journal (Oct 8): fresh scenario sessions every ~4–5 min
  continuously 20:47 → 22:47 kill. Last session closed at exactly
  22:47:02 = the systemd SIGTERM.
- `/tmp/bench-*.json` mtimes (Oct 9): suite walked in order —
  camera_snapshot 02:05 … gev_layers_track in progress at 03:58, killed
  04:00 at scenario ~27/33. Zero `== name:` journal lines because
  (a) scenario children run under `subprocess.run(capture_output=True)`
  and (b) the pre-16:57 unit lacked PYTHONUNBUFFERED — buffered prints
  were lost at SIGTERM.
- Suite needs ~3.5–4h vs TimeoutStartSec=7200: ~33–43 scenarios × ~4–5min
  per attempt, and every failing scenario retries once (×2).
- Night amplifier: the gev-cmd relay on tony-dell returned HTTP 404 all
  night → ~16 gev_* scenarios each burned ~8min failing twice ≈ 2h of
  guaranteed-fail runtime. Several vcast/camera scenarios correctly
  exited fast (skip/infra/unimplemented — no events files).
- Smoke starvation confirmed: 02:00/03:00/04:00 smoke runs logged
  "another scenario run is active — skipping" while casting held :8199.
- Latent bug found: scenario-live.py `connect()` ready-wait loop had no
  total deadline (per-recv 30s, unbounded loop) — now capped at 120s.

## Changes (branch dispatch/20261009-175902-…, 3 commits)

- `scripts/scenario-live.py`: new `gev_relay` preflight (gev-cmd endpoint
  answers? — don't require clients, tours create their own by casting);
  `connect()` bounded (120s deadline, open_timeout=15);
  `_VcastDisplay._open` open_timeout=15.
- `tests/scenarios-live/gev_*.yaml` (16 files): `preflight: gev_relay` +
  `vcast_online` on the target screen ({real_screen} / 1 / 9);
  `gev_showcase`'s dict-form preflight (crashed run_preflight) fixed to
  list form.
- `scripts/scenario-benchmark.py`: flush=True progress lines; live
  progress doc `benchmark/{suite}/live` written at start + per scenario;
  SIGTERM handler writes the doc then exits; `--max-scenarios` /
  `--budget-s` (env `BENCH_BUDGET_S`); truncated runs mark doc
  partial+invalid.
- `docs/ssot/jobs/ada/2026-10-09-casting-suite-timeout.yml` — runbook.

## Verified live on idc03 (shadow tree /tmp/bench-fix; deploy checkout
untouched, cleaned up after)

- `gev_za_tour` → INFRA rc=3 in ~2s on the 404'd relay (was ~8min ×2).
- `--suite casting --max-scenarios 3` completed in ~8.5min:
  camera_snapshot pass (208s), camera_snapshot_offline fail,
  cctv_snapshot_to_screen pass — `benchmark/casting-20261009-185732`,
  `benchmark/casting/live`, `auto-report/casting-…` all written
  (to MDDB_OPS_URL = idc02:11025 ops store, by design).
- SIGTERM mid-run → live doc reads "KILLED by signal 15", process exited.

## Still open / recommendations

- gev-cmd relay 404 on tony-dell is a real outage — gev_* now INFRA-skip
  until it returns; worth a board card of its own.
- Even healthy, the suite is ~3h > TimeoutStartSec=9000. Options (units
  live in ~/.config/systemd/user on idc03, not in git): set
  `Environment=BENCH_BUDGET_S=8400` under the 9000s timeout; split
  casting into casting-io/casting-gev on alternating nights; or make the
  daily ada-bench-casting rerun opt-in. Tonight's 02:00 run will at least
  stream `==` lines + leave `benchmark/casting/live` regardless.
