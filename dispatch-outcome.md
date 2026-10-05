# Dispatch outcome — tools-merge-docs-drive (8 -> 2)

**Result: done.** The docs+drive group is merged to two canonical
action= tools; the eight absorbed names are soft aliases off the
declared surface.

## What changed

- `backend/tool_runner.py`
  - `_ALIASES`: `ada_doc_search|ada_doc_get|ada_doc_print|ada_doc_archive`
    -> `docs`; `drive_search|drive_show|drive_get|drive_update` -> `drive`
    (31 retirees total). `_ALIAS_ARG_DEFAULTS` carries the implied
    `action=`; arg shapes are 1:1 so `_alias_call_args` needed nothing.
  - New canonical methods `docs()` and `drive()` dispatch per-action
    onto the unchanged absorbed methods.
  - Gates key off the resolved name: `docs` holds `DOC_TOOLS` +
    `DOC_CONFIRMED_TOOLS`; `drive` holds `DRIVE_TOOLS` +
    `DRIVE_CONFIRMED_TOOLS`. Per-action splits: `_check_doc_confirmed`
    frees search/get; new `_check_drive_confirmed` frees search/get/show
    and gates update (also gained the ADA_READ_ONLY denial every other
    write gate has — deliberate tightening, noted in the job file).
  - `_CHANGE_LOG_TOOLS`: `ada_doc_archive` -> `docs`, filtered to
    action=archive so reads don't log.
- `backend/realtime_provider.py`
  - Removed the 8 absorbed declarations; added `docs` and `drive` with
    explicit action enums and absorbed-name callouts in descriptions.
  - `DOC_TOOLS`/`DRIVE_TOOLS` and `DOC_INSTRUCTIONS`/`DRIVE_INSTRUCTIONS`
    rewritten for canonical names; `ACTUATING_TOOLS` now keys docs off
    action in {archive,print} at the call site.
  - `DEVIN_CONFIRMED_TOOLS`/`confirm_strip` untouched.
- `docs/ssot/ssot.tool-surface.yml` — `docs` + `drive` families
  (scenario `tool_merge_docs_drive`); retired names pruned from
  `coverage_debt`.
- `docs/assessments/tool-consolidation-spec-2026-10-04.md` — family
  table row added.
- `scripts/scenario-live.py` — `LEGACY_TOOL_ALIASES` synced: the 8 new
  rows plus the 8 calendar+plan rows the previous merge missed.
- `tests/benchmark.yml` — write_tools `ada_doc_*` -> `docs`;
  `doc_recall`, `chaba_memory_review`, `tool_merge_docs_drive` join
  `write_allowed_in` (name-level policy can't split actions).
- `tests/scenarios-live/doc_upload.yaml` — `events_not_contain` now
  checks canonical `docs` (absorbed names can never match resolved
  tool_call events).
- New `tests/scenarios-live/tool_merge_docs_drive.yaml` (family
  regression: docs search, drive search, drive get).
- New `DocsDriveMergeAliasTests` in `tests/test_tool_runner.py` — 8
  tests covering alias routing, confirm gates, and bank-policy denial.
- Job trail: `docs/ssot/jobs/ada/2026-10-05-tools-merge-docs-drive.yml`.

## Verification

- `python3 scripts/tool-lint.py` — clean: 82 declared (79 builtin + 3
  tools.d), 31 aliases, 0 errors.
- `_resolve_alias` smoke: all 8 absorbed names map to the right
  canonical + action.
- `DocsDriveMergeAliasTests` + `test_doc_archive_tool`: 20 tests pass.
- Full suite: 519 tests; 2 failures + 4 errors all environmental and
  identical at HEAD (memory backend absent, `ai_edge_litert` missing,
  one genai-stub quirk). No docs/drive regressions.

Note: the worktree lacked the gitignored `.teststubs/` harness — a
minimal `google.genai` stub was recreated in-worktree (untracked) to run
the suite.
