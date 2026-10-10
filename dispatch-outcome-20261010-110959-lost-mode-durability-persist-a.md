# Dispatch outcome — lost-mode durability (persist armed watches across restarts)

## Result

Armed lost-mode watchers now persist to MDDB `device-telemetry` as
`_watch/<device>` docs and survive service restarts. The doc carries
`device`, `armed_at`, `interval_s`, `channel`, `until`, `last_pushed`,
`last_sighting`, `last_fp` (the delta fingerprint), and a resume claim
(`resumed_at`, `resumed_by` = host:pid:time_ns).

- **Arm** (`action='lost'`): persists the doc before spawning the task.
- **Poll loop**: reads its doc each iteration (leader read — follower
  lag could fake a delete), pushes only on fingerprint deltas as before,
  re-stamps `resumed_at` + last-pushed fields every poll (claim
  heartbeat), skips the persist on a doc-miss so a remote delete can't
  be resurrected, and exits after `_DOC_MISS_LIMIT` (3) consecutive
  misses — that's how `found` issued in another process stops it. The
  `finally` always deletes the doc (expiry, disarm, cancel).
- **Resume**: `resume_watches()` scans `_watch/*` docs — expired ones
  are deleted (stale cleanup), docs with a fresh claim
  (`< max(3*interval, 300s)`) belong to a live process and are skipped
  (no duplicate pushes across the ada services sharing the store),
  stale/unclaimed docs are claimed by writing `resumed_by` and re-armed
  with the original `until` and the persisted `last_fp` seeded — a
  device that moved during downtime immediately pushes a `delta`, an
  unchanged one doesn't re-push. A post-claim re-read stands down a
  resumer that lost the race.
- **Resume hook**: new tools.d convention — a drop-in module may define
  `async on_service_start(runner)`; `pwa_server.warm_cache` calls it at
  boot for every loaded tool with the shared ToolRunner. `run()` also
  lazy-resumes once per process (`runner._track_resumed`) so services
  without a startup hook pick watches up on the first track call.
- **`found`**: pops the in-process watch AND deletes the doc — all
  `_watch/*` docs when no device is given, or the input's canon even
  when it's out of the fleet pool.
- **`watches`**: also lists remote (other-process) watchers with
  `remote: true` + `resumed_by`.
- **Fleet pool**: skips `_`-prefixed doc keys so `_watch/*` docs never
  surface as devices.

## Files changed

- `backend/tools.d/ada_track_device.py` — +~200 lines (persistence
  helpers, claim-aware resume, loop doc-check + heartbeat persist,
  `found`/`watches` updates, lazy resume, `_` exclusion)
- `pwa_server.py` — `warm_cache` runs `on_service_start(runner)` for
  every tools.d tool that defines it (~15 lines)
- `tests/test_track_device.py` — FakeMddb add/delete + prefer_leader,
  watch-loop tests seed the persisted doc, +6 tests
- `docs/ssot/jobs/ada/2026-10-10-lost-mode-durability.yml` — trail

## Verification

- `pytest tests/test_track_device.py` → **19 passed**
- `pytest tests/ -k 'scenario or track or device'` → **51 passed**
  (incl. `test_device_track_merge` scenario: arm → watches → found)
- Full suite → 260 passed; one **pre-existing flake**
  (`test_eye_tool::test_empty_detections_is_live_but_empty` —
  time-sensitive `FRESH` fixture goes stale mid-suite; passes in
  isolation with and without this change)
- `tool-lint.py` → 3 violations, all pre-existing on HEAD (voice_fx,
  yt_cached_list) — none from this change
- Live smoke vs real mddb (100.102.134.91:11023,
  `ADA_TRACK_PUSH_DRYRUN=1`): arm persists `_watch/` doc with claim →
  fresh runner resumes a stale-claimed doc (new `resumed_by` written,
  `until` + `last_sighting` preserved) → a second resumer skips the
  fresh claim → remote `found` delete exits the live loop within 3
  polls → expired doc deleted on resume scan → baseline `[lost-mode]`
  push logged on the dry-run path.

## Caveats / notes

- The claim is a heartbeat + post-write re-check, not a distributed
  lock — a losing resumer detects the race and stands down; a crashed
  owner's watch is adoptable after `max(3*interval, 300s)`.
- During an mddb outage the doc-check fails — after 3 misses the watcher
  exits; its delete also fails so the doc survives and the next resume
  scan re-adopts it.
- Cross-process `found` takes effect within ≤3 poll intervals (doc
  misses); same-process `found` is immediate.
- No commit/push/deploy — worktree only; prod picks it up on the next
  `deploy-ada.sh` on idc03.
