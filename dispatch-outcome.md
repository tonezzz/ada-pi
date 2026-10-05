# dispatch outcome — tools-merge-yt (yt group 4 -> 1)

Task: merge the yt group per the consolidation spec —
`yt_cast` + `yt_cast_status` + `yt_cast_stop` + `yt_transcript`
collapse into one declared tool `yt(action=cast|status|stop|transcript)`.
Absorbed names stay registered-but-hidden via `tool_runner._ALIASES`;
gates key off the resolved tool name, not the alias.

## What changed

- `backend/realtime_provider.py`
  - Four yt declarations replaced by a single `yt` declaration —
    explicit `action` enum `[cast, status, stop, transcript]`, params
    `query` (cast target / search phrase), `url` (transcript target),
    `language`; `required: ["action"]`. Description names the absorbed
    tools so legacy phrasing still routes.
  - `ACTUATING_TOOLS` dropped `yt_cast`/`yt_cast_stop`; added a
    per-action probe so `yt(action=cast|stop)` counts against the
    actuation budget while `status`/`transcript` reads stay free —
    exact parity with the old seats (same pattern as
    `ada_camera_snapshot`'s display-push probe).
  - Instruction text repointed: NEWS-vs-MEDIA bullet and the
    `cast_to_screen` description now say `yt(action='cast')`.
- `backend/tool_runner.py`
  - `_ALIASES` +4 rows: `yt_cast`, `yt_cast_status`, `yt_cast_stop`,
    `yt_transcript` → `yt`.
  - `_ALIAS_ARG_DEFAULTS` +4 rows carrying the implied `action=`.
    No `_alias_call_args` shim needed — params map 1:1 after the
    implied action merges.
  - New `yt(action, query, url, language)` method dispatches per-action
    onto the unchanged absorbed methods (calendar_read pattern);
    `query`/`url` cross-fill so either arg name works; unknown action
    raises `ValueError`. `yt` enters no gate set — the old names were
    ungated.
- `docs/ssot/ssot.tool-surface.yml`
  - New `yt` family (merge_card tools-merge-yt, canonical `yt`,
    scenario `tool_merge_yt`, four absorbed names).
  - `display` family absorbed list now names `yt` instead of the three
    `yt_*` names (they're already aliases — absorbing them directly
    would chain). `yt_cast_status` removed from `coverage_debt`
    (stale once off-surface).
- `docs/assessments/tool-consolidation-spec-2026-10-04.md` — yt row
  added to the family table; display row repointed at `yt`.
- `scripts/scenario-live.py` — `LEGACY_TOOL_ALIASES` +4 rows.
- `tests/scenarios-live/tool_merge_yt.yaml` — family regression
  scenario (read-side turns: cast status + transcript; the real-cast
  path stays covered by tv_cast_control / cast_dub_to_tv).
- `tests/test_tool_runner.py` — `YtMergeAliasTests` (5 tests): each
  absorbed name routes to `yt` with the right action, canonical
  dispatch + query/url fallback, unknown action rejected.
- `docs/ssot/jobs/ada/2026-10-05-tools-merge-yt.yml` — SSOT job record.

## Files changed

- `python3 scripts/tool-lint.py` — clean; 85 declared (was 88),
  27 aliases, 41 warnings (all pre-existing; HEAD had 43).
- `YtMergeAliasTests`: 5/5 pass — every absorbed alias routes to `yt`.
- Full suite: 546 passed, 2 failed — identical to HEAD baseline
  (`test_memory_lifecycle`, `test_michael_technician` need live mddb on
  this host; verified same failures existed before this change).
- `DEVIN_CONFIRMED_TOOLS` / confirm_strip untouched — gates resolve on
  the canonical name; yt family was never confirm-gated.

## Notes / follow-ups

- tools-merge-display should now absorb `yt` (not the `yt_*` names) and
  re-point the four yt aliases at `ada_display` to avoid alias chains.
- Not committed/pushed per dispatch rules — worktree only.
