# Dispatch outcome — ada-pi CI (attempt 2)

## What I found

Attempt 1 had already landed `.github/workflows/ci.yml` (pytest + advisory
tool-lint + PR template) on main — but **every CI run failed**. Two
`test_gemini_pool` ops-event tests failed deterministically on the GitHub
runner while passing on tony-dell, plus one intermittent
`test_recall_gate` cluster.

Root cause — a sentinel bug family, not test debt: `now - stamp < window`
where `stamp` defaults to `0.0` reads as "stamped at host boot".
`time.monotonic()` counts from boot, so on a fresh ephemeral runner
(uptime < the throttle window) every first-use event was silently
throttled. Four instances fixed at the source:

- `backend/gemini_pool.py` — `_event_at.get(tool, 0.0)` → `None` check
  (suppresses first ops event when uptime < 300 s → the 2 red tests)
- `backend/realtime_provider.py` — `_strong_hit_at` 0.0 → `-inf`
  (recall wrongly gated when uptime < 60 s → the intermittent flake)
- `backend/mddb_client.py` — `_follower_ok_at` 0.0 → `-inf` (skipped
  the first follower probe within 30 s of loop start)
- `backend/hailo_vision.py` — `_temperature_read_at` 0.0 → `-inf`
  (skipped first temperature read within 2.5 s of boot)

Plus `tests/test_eye_tool.py`: module-level `FRESH` stamped
`time.time()` at import — the doc exceeds its own 3×pub_s (15 s) stale
window during any full suite; `LiveTest.setUp` now re-stamps it.

## What I changed

- `scripts/scenario-schema-check.py` (new) — yaml.safe_load +
  required-keys check (`name`/`tier`/`turns`) over
  `tests/scenarios-live/*.yaml`; turns must be a non-empty list of
  non-empty mappings (open step vocabulary — audio/speech/reconnect
  steps are legal).
- 51 scenario files backfilled `tier: full` — the engine's documented
  default, so zero behavior change; the check now enforces all three
  keys as the card specifies. 183 files, 0 violations.
- `.github/workflows/ci.yml` — added `scenario-schema` step before
  pytest; refreshed stale debt comments (named tools cleared, new
  voice_fx debt in flight — tool-lint stays advisory on purpose).
- `.github/PULL_REQUEST_TEMPLATE.md` — schema check in the test plan.
- `docs/ssot/jobs/ada/2026-10-10-ci-push-gate.yml` — decision trail.

## Result

- `pytest tests/ -q` + the documented deselects: **1105 passed, 0
  failed** in a CI-equivalent venv (filtered requirements).
- `scripts/scenario-schema-check.py`: 183 files, 0 violations.
- `scripts/tool-lint.py`: 3 violations, all in-flight voice_fx /
  yt_cached_list debt from another session — correctly advisory.
- Commit `423fd5b` on the dispatch branch (60 files).

## Not done / needs operator

- **gh-runs-watch wiring**: the watcher lives in chaba (outside this
  worktree). Board request raised — if it doesn't auto-discover repos
  with workflows, `tonezzz/ada-pi` needs adding to its watch config.
- Remote verification: next push to main after merge should show a
  green run — `gh run list --repo tonezzz/ada-pi`.

## How to verify

```
python3 scripts/scenario-schema-check.py   # 183 files, 0 violations
python3 -m pytest tests/ -q                # green (5 deselects documented)
python3 scripts/tool-lint.py               # advisory: 3 known debt rows
gh run list --repo tonezzz/ada-pi          # green after merge
```

lessons:
- `time.monotonic()` counts from host boot — `stamp` fields defaulting to 0.0 mean "at boot", not "never"; on fresh CI runners (uptime < throttle window) every first-use event silently throttles. Sentinel for "never" is `-inf` or an explicit None check.
- Module-level `X = {"ts": time.time()}` test fixtures go stale inside a full suite — stamp freshness per-test (setUp), not at import.
- pytest `--deselect` lists rot: name the *class* of exclusion in comments (live-service tests, repo-clean assertions) not the incident of the week.
- ada-pi scenario corpus: `tier` missing means `full` by engine default — 51 files relied on the implicit default until backfilled.
- gh-runs-watch (chaba) is the alert path for GitHub Actions failures; its watched-repo config is NOT in the ada-pi worktree — wiring it needs a chaba-side change.
