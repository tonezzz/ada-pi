# Dispatch outcome — ada-dead-surface-metric (attempt 2)

## What changed

`scripts/tool-usage-report.py` gained a **transcript pass** alongside its
existing journal census — same data family as ada-alias-telemetry, but
bucketing canonical calls:

- `scan_reports()` reads `$ADA_DATA_DIR/reports/<day>-<session>.json`
  session_events: `kind=tool_call` counts per canonical tool into
  trailing 7d + 30d windows (event `ts`, file-date fallback; files fully
  outside 30d skipped pre-parse); `kind=tool_denied` tallied separately
  as gated demand.
- `canonical_name()` resolves the as-called name exactly like dispatch
  does: `_ALIASES` first, then bare-underscore normalization — the alias
  table comes from tool-lint's `collect_surface()` (no backend import).
- `report/tool-usage` CMS page gains two sections: **Canonical calls —
  transcript pass** (tool | 7d | 30d | denied | last called | note, with
  via-alias attribution and not-declared flags) and **Zero-call tools —
  30d** — the merge/exclusion candidate list, explicitly review-only
  (rare-but-critical caveat per the card). Meta summary carries the
  zero-call-30d count.
- CLI: `--reports-dir` (default `<ADA_TRANSCRIPT_DIR>/../reports`, the
  report-rollup convention), `--no-reports`. The existing
  ada-tool-usage.timer on idc03 picks it up on its next daily tick — no
  new infra.

## Result

- `tests/test_tool_usage_report.py` (new): 13/13 pass — includes the
  card's verify contract (fixture 7d count matches a hand-counted set of
  tool_call events: ada_memory_search 3 = 2 direct + 1 guest_recall
  alias, kanban 1; 30d adds the Sep-20 file; Aug-15 file contributes
  nothing).
- Dry-run on tony-dell (no reports dir): journal census unchanged,
  transcript pass warns to stderr and skips.
- Dry-run against a fixture reports dir renders both new sections
  correctly (alias folding, denied column, zero-call list + caveat).
- Pre-existing: tool-lint reports 3 violations on this worktree's base
  (`yt_cached_list` impl seat, `voice_fx` coverage, +1). Not introduced
  here — lint audits `backend/` + `docs/ssot/ssot.tool-surface.yml`,
  neither touched by this diff. Prior attempt's merge failure left no
  detail in card comms; this branch is a clean single-file diff + one
  new test file.

## How to verify

- `python3 -m pytest tests/test_tool_usage_report.py -q`
- `python3 scripts/tool-usage-report.py --journal-file /dev/null --dry-run`
  (journal-only path; warns about missing reports dir)
- On idc03 after deploy: `scripts/tool-usage-report.py --dry-run` shows
  real 7d/30d counts, or check the `report/tool-usage` CMS page after
  the next timer tick — zero-call-30d rows are the merge-review list.
- Job doc: `docs/ssot/jobs/ada/2026-10-10-dead-surface-metric.yml`

lessons:
- tool_call session events carry the AS-CALLED name (pre-alias); canonicalize via tool-lint's collect_surface()["aliases"], not backend imports (tool_runner pulls google.genai)
- session reports live at <ADA_TRANSCRIPT_DIR>/../reports — same convention as report-rollup.py; transcripts (.md) carry no tool events, the JSON session_events do
- tool-lint on this base had 3 pre-existing violations (yt_cached_list impl, voice_fx coverage) — check `git diff --stat` scope before assuming a red gate is yours
- card comms carried no prior-failure detail for attempt 2; when primer says "inspect comms" and comms are empty, treat as a fresh implementation and keep the diff minimal
