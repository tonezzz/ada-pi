# dispatch outcome — 20261010-080626-alias-hit-rollup-deprecation-p

Card: `ada-alias-telemetry` — alias-hit rollup, deprecation path for the
92-name `_ALIASES` compat layer. Attempt 2 (attempt 1 left no comms or
artifacts; nothing to reuse).

## What changed

- **`scripts/ada/alias_hit_report.py`** (new, stdlib-only) — emitted-name
  census over the session transcript stores:
  - `call-logs/<day>-<session>.jsonl` `function_call` events — `name` is
    the AS-EMITTED name (written before alias resolution at
    realtime_provider.py:4617), the journald-immune source.
  - `reports/<day>-<session>.json` `tool_call` events — only for
    sessions with no call-log file at all (dedupe by session id);
    `emitted` field preferred, `tool` fallback under-counts aliases on
    pre-2026-10-10 data.
  - service journal `tool alias <old> -> <new>` / `tool <name> args=`
    lines — the ws/HTTP `execute()` path writes no call-log, so journal
    hits are a separate column that still gates eligibility.
  - Per emitted name: hits, kind (declared|alias|unknown), last day.
    Per alias row: transcript + journal hits, last hit, quiet days,
    removal_eligible (quiet >= 14d of *observed* data — a partial window
    can never declare an alias dead). Plus `dead_surface` (declared
    tools unused on every path — the ada-dead-surface-metric feed) and
    `unknown` (emitted names with no seat — drift signal).
  - Publishes CMS page `report/alias-hits` in ada-cms-pages
    (kind=report → reports-index; meta contract validated pre-publish)
    and writes `<data>/runs/alias-hits/<date>.json`. Refuses to publish
    when there is no data at all.
- **`backend/realtime_provider.py`** — `log_event("tool_call")` gains
  `emitted=_call_name`: `tool=` stays the resolved canonical, `emitted`
  preserves the as-called name. This makes the card's premise
  ("log_event already records the emitted name") actually true.
- **`scripts/ada-alias-report.service` / `.timer`** (new) — weekly Mon
  06:40, mirrors the ada-tool-usage install pattern for idc03.
- **`tests/test_alias_hit_report.py`** (new) — 10 tests: windowing,
  reports-fallback dedupe, kind split, eligibility incl. the
  partial-coverage guard, journal cross-check, dead-surface union,
  publish meta contract.
- **`docs/assessments/tool-consolidation-spec-2026-10-04.md`** — added
  the rollup as item 4 of the self-auditing machinery list, with the
  removal contract (delete `_ALIASES` row + family `absorbed` entry in
  one commit — lint fails on either half alone).
- **`docs/ssot/jobs/ada/2026-10-10-alias-hit-rollup.yml`** — job ledger.

## Result / verify

- `pytest tests/test_alias_hit_report.py` — 10/10; provider-touching
  subset (dead_turn, provider_events, ops_events, event_recorder) —
  61/61 on `~/CascadeProjects/ada-pi/.venv` python.
- Dry-run on tony-dell call-logs: 7 emitted names / 257 calls / 92 alias
  rows / 0 removal-eligible (only 5d of coverage — the conservative
  guard working) / 40 dead-surface / sources: call-logs.
- `tool-lint.py`: 3 violations **pre-existing on clean HEAD**
  (voice_fx descsize + coverage, yt_cached_list impl) — verified via
  stash; unrelated to this change.
- Card verify criteria: "digest lands in ada-ha-ops / reports-index"
  needs the timer (or a manual run) on **idc03** after deploy — page
  slug `report/alias-hits`, kind=report so reports-index picks it up.
  "One real alias retires cleanly" needs ≥14d of production call-log
  coverage; call-logs began 2026-10-05, so first eligible rows surface
  ~2026-10-19.

lessons:
- Session-report `tool_call.tool` is the RESOLVED canonical name — the
  provider rewrites call.name before dispatch (model_copy at
  realtime_provider.py:~4629). Emitted names live only in call-logs
  `function_call` events (and journal `tool alias` lines).
- Worktrees lack `.venv`; provider tests need the main checkout's
  `~/CascadeProjects/ada-pi/.venv/bin/python` (google.genai).
- tool-lint.py currently fails on 3 pre-existing violations on HEAD
  (voice_fx descsize+coverage, yt_cached_list impl) — not a dispatch
  regression; check clean HEAD before assuming your change broke it.
- kanban card paths in the task brief may not exist in the worktree —
  card spec lives behind the board API (127.0.0.1:8787/cards).
