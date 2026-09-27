# Devin job report — dedicated "Failed jobs" section

Date: 2026-09-27 · Task: dispatch-wt-20260927-215500-separate-failed-devin-jobs-int
Ref: handoff spec `handoff-extract-2026-09-27-fd98e6e49c-3` (bank doc
`extract-2026-09-27-fd98e6e49c-3` in ada-ha-bank-devin-handoff)

## Problem

The `devin-job-report` CMS page (ada-cms-pages) is composed by an agent from
the `job/<id>` ledger docs in `ada-ha-bank-devin-handoff`. Failed jobs —
especially spawn failures that never wrote a transcript (e.g.
`job/20260927-040246-audit-tests-scenarios-live-yam`, a `--permission-mode
smart` rejection on mn01) — kept `status=running` in the ledger and cluttered
the report's active list. Root cause upstream: `devin-dispatch-watch` skips
any task without `transcript.json` (`[ -f transcript.json ] || continue`),
so spawn failures were never re-stamped failed.

## Change

- `backend/devin_dispatch.py`: new `tasks()` — parses `devin-dispatch status`
  into structured rows (task_id/state/result/repo/transcript).
- `backend/tool_runner.py`: new `devin_job_report` tool. Reads `job/<id>`
  docs (kind=job), verifies ledger-`running` entries against dispatch status
  on this host — dead unit + `transcript=never` ⇒ failed (30 min grace for
  `gone` units so fresh dispatches aren't false-flagged) — and composes
  markdown with sections **Active jobs / Failed jobs / Done / Stale ·
  superseded**. `publish=true` writes the page via `cms_publish_page` and is
  gated by `_check_cms_write_allowed` (confirmed=true).
- `backend/realtime_provider.py`: `devin_job_report` declaration +
  DEVIN_INSTRUCTIONS tell Ada to use it for the job report and to never list
  failed jobs among active ones.
- `tests/test_tool_runner.py`: `DevinJobReportTests` — spawn-failure
  classification, ledger-failed routing, dispatch-outage resilience, and the
  publish gate.

## Ledger status → section

| ledger status            | section  |
|--------------------------|----------|
| running/awaiting-user/…  | Active   |
| failed, or running + dead unit / no transcript (this host) | Failed |
| done/success/completed   | Done     |
| superseded/cancel/stale  | Stale    |

Remote-host jobs the local `devin-dispatch` can't see keep ledger truth.

## Follow-ups (out of scope, noted for chaba repo)

- `devin-dispatch-watch` should mark spawn failures (`exit_code` present, no
  transcript) as failed so the ledger converges without manual verification.
- `devin-dispatch status` doesn't expose `exit_code`; a mid-run failure on a
  collected unit is indistinguishable from success until the ledger updates.

## Verify

- `python -m unittest tests.test_tool_runner.DevinJobReportTests` (venv:
  ada-pi `.venv`; one unrelated pre-existing failure in
  `ControlGateTests.test_dangerous_cover_allowed_with_confirmed` — missing
  banks registry in test env).
- Live smoke: `runner.devin_job_report(limit=80)` against
  MDDB_BASE_URL=http://100.74.146.0:11023/v1 — produced Failed=2 (the mn01
  spawn failure + `resume-20260926-183018-...`), active/done/stale correct.
- Page 'devin-job-report' was NOT republished by this task (existing content
  is hand-curated Thai/EN with retry notes; regeneration is on-demand via
  the tool with publish=true).
