# Dispatch outcome — tools-merge-calendar-plan (20261005-122438)

Merged the calendar+plan tool group 9 → 3 per the card's self-contained
spec. Eight names absorbed into canonical tools via
`tool_runner._ALIASES` — the repo's established hidden-alias mechanism
(registered in the runner, absent from declarations; there is no
literal `x-legacy` field — this IS the x-legacy semantics).

## Alias map (all verified by tests)

| absorbed name              | resolves to                              |
|----------------------------|------------------------------------------|
| `calendar_list_events`     | `calendar_read` action=`events`          |
| `calendar_list_calendars`  | `calendar_read` action=`calendars`       |
| `calendar_freebusy`        | `calendar_read` action=`freebusy`        |
| `calendar_create_event`    | `calendar_write` action=`create`         |
| `calendar_delete_event`    | `calendar_write` action=`delete`         |
| `calendar_shift_overdue`   | `calendar_write` action=`shift`          |
| `ada_daily_summary`        | `plan_day` period=`digest` (alias-only)  |
| `ada_weekly_comparison`    | `plan_day` period=`week`, `end`→`day`    |

## What changed

- `backend/tool_runner.py`
  - `_ALIASES` + `_ALIAS_ARG_DEFAULTS`: the eight rows above.
  - `_alias_call_args`: `ada_weekly_comparison(end=…)` maps `end`→`day`
    (the window end); `days`/`refresh` pass through.
  - `calendar_read(action=events|calendars|freebusy, day, days, query,
    calendar)` and `calendar_write(action=create|delete|shift, title,
    start, end, notes, location, calendar, event_id, to)` — per-action
    dispatch onto the unchanged absorbed methods; unknown action raises.
  - `plan_day(period=today|tomorrow|week, day, days, refresh)`:
    today/tomorrow return the merged events+tasks view with the day's
    session digest folded in under `digest`; `week` returns the weekly
    digest comparison; `day=` overrides the target. Works without a
    configured calendar (digest-only, tagged `calendar: not
    configured`). `period='digest'` is the alias-only seat that
    preserves ada_daily_summary's bare-digest contract.
  - `CALENDAR_WRITE_TOOLS` = {`calendar_write`, `tasks_add`,
    `tasks_complete`, `tasks_move`} — the canonical holds the seat so
    every create/delete/shift (direct or aliased) stays
    confirm-gated. `DEVIN_CONFIRMED_TOOLS` / `confirm_strip` untouched —
    gates key off the resolved name.

- `backend/realtime_provider.py`
  - Eight declarations removed; `calendar_read` + `calendar_write`
    declared with explicit `action` enums, `plan_day` with a `period`
    enum; descriptions name the absorbed tools for legacy phrasing.
  - `CALENDAR_TOOLS` / actuation sets updated to canonical names;
    `CALENDAR_INSTRUCTIONS` rewritten and `SUMMARY_INSTRUCTIONS`
    folded in; `SUMMARY_TOOLS` removed. `ADA_EXCLUDED_TOOLS` filtering
    unchanged.

- `docs/ssot/ssot.tool-surface.yml` — calendar/plan families rewritten
  as calendar_read / calendar_write / plan_day sub-families; absorbed
  names pruned from coverage_debt.
- `docs/assessments/tool-consolidation-spec-2026-10-04.md` — the group
  row updated to the landed design.
- `tests/benchmark.yml` — `write_tools` three absorbed writers →
  `calendar_write`; `tool_merge_calendar_plan` joins `write_allowed_in`.
- `tests/scenarios-live/tool_merge_calendar_plan.yaml` — family
  regression scenario (lint requires it once the merge starts);
  `shift_overdue.yaml` + `chaba_memory_review.yaml` repointed to
  canonical names.
- `tests/test_tool_runner.py` — `CalendarPlanMergeAliasTests` (11
  tests): every absorbed name routes to its parent, write aliases keep
  the confirmation gate, unknown action/period rejected, digest-only
  fallback when the calendar is unconfigured.
- `docs/ssot/jobs/ada/2026-10-05-tools-merge-calendar-plan.yml` — this
  job's SSOT trail.
- `.teststubs/` (gitignored, local only) — google.genai import stub for
  this SDK-less host so the suite can run.

## Verify

- `python3 scripts/tool-lint.py` → clean: 88 declared (94 → 88), 23
  aliases, 0 violations (12 pre-existing warnings).
- `PYTHONPATH=.teststubs ADA_INSTANCE_ID=test python3 -m unittest
  tests.test_tool_runner.CalendarPlanMergeAliasTests` → 11/11.
- Full suite: 489 tests, 2F/3E — identical failure set to HEAD
  (verified via stash): memory-lifecycle + michael-technician
  scenarios need a live mddb; detection/pose/hailo errors are
  `ai_edge_litert` absent on this host. No merge regressions.

## Notes

- `tools-merge-gate.py` needs a passing `tool_merge_calendar_plan` run
  in `ada-ha-scenario-reports` before the card can close — scenario
  file ships here; the live run is idc02's lane.
- Not committed to the default branch, not pushed, not deployed
  (dispatch rules).
