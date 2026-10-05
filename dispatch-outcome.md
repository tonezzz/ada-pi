# Dispatch outcome — tools-merge-memory (20261004-210440)

Merged the memory tool group 8 → 4 per the card's self-contained spec.
Six names absorbed into the three surviving canonical tools;
`ada_forget` unchanged, `ada_resolve_action` untouched (workflow, not
memory). All absorbed names stay callable via `tool_runner._ALIASES` and
are off the declared surface — the repo's established hidden-alias
mechanism (there is no literal `x-legacy` field in the codebase; this IS
the x-legacy semantics: registered in the runner, absent from
declarations).

## Alias map (all verified by tests)

| absorbed name             | resolves to                              |
|---------------------------|------------------------------------------|
| `guest_recall`            | `ada_memory_search` scope=`guest`        |
| `vocab_note`              | `ada_remember` kind=`vocab`              |
| `report_habit_observation`| `ada_remember` kind=`habit`              |
| `guest_remember`          | `ada_remember` kind=`guest`              |
| `guest_remember_private`  | `ada_remember` kind=`guest`, private=true|
| `ada_ha_recall`           | `ada_session_recall` scope=`history`     |

## What changed

- `backend/tool_runner.py`
  - `_ALIASES` + `_ALIAS_ARG_DEFAULTS`: the six rows above; implied
    args merge under caller args; `_alias_call_args()` maps
    `vocab_note(term, correct, note)` → `text="term → correction"` and
    `ada_ha_recall(query)` → `question`. `_ARG_ALIASES` gained `q→query`
    (the card's `search(q, …)` spelling).
  - `ada_memory_search` gained `scope=all|banks|sessions|guest`: banks =
    existing `memory_ops.memory_search`; sessions = new `_session_hits()`
    over the recall-summary collection; guest = chaba `recall`.
    An explicit `bank=` name without `scope` implies `banks`, preserving
    the pre-merge result shape for bank-scoped callers.
  - `ada_remember` gained `kind` + `private` + `note`: `vocab` appends to
    the speaker's own `vocab/log` (new `_vocab_append`, shared with the
    kept `vocab_note()` method); `guest` writes the chaba store
    (`private=True` → `remember_private`); `habit` returns a
    descriptive error store-side (the real path is provider-dispatched);
    curated kinds still go through `memory_ops.remember` unchanged.
  - `ada_session_recall(question, scope=sessions|history)`: `history`
    delegates to the unchanged `ada_ha_recall` logic; `sessions` is
    provider-dispatched (a store-side call returns a clear error).
  - Gate carve-outs (`_REMEMBER_NONBANK_KINDS`): `kind∈{vocab,guest,habit}`
    skips the `MEMORY_WRITE_TOOLS` bank gate and the secondary-speaker
    block; `scope=guest` search skips the secondary block — matching the
    absorbed tools' pre-merge access exactly. Alias resolution runs
    before all gates, so every gate keys off the resolved canonical name.
    `DEVIN_CONFIRMED_TOOLS` / confirm_strip logic untouched.
- `backend/realtime_provider.py`
  - Deleted declarations: `vocab_note`, `report_habit_observation`,
    `ada_ha_recall`; CHABA declarations for `guest_remember` /
    `guest_remember_private` / `guest_recall` re-declared as
    guest-scoped `ada_remember` (kind='guest', private flag) and
    `ada_memory_search` (scope='guest') — the allowlist swap drops the
    bank-facing versions so no name is declared twice.
  - Canonical schemas extended: `ada_memory_search` +`scope`,
    `ada_remember` +`kind`/`private`/`note`/habit fields (bank no longer
    required — runner validates per kind), `ada_session_recall`
    +`scope`/`limit`.
  - Dispatch: the habit-observation `elif` accepts both the legacy name
    and `ada_remember` kind='habit' (emits the same `habit_observation`
    ProviderEvent); `ada_session_recall` scope='history' falls through
    to the runner (bypassing the redundant-recall gate and the provider
    secondary block, exactly as `ada_ha_recall` did); the budget
    `remember_block` and the `_CONFIRM_GATED_TOOLS` Jev probe skip the
    non-bank kinds; `HABIT_TOOLS` now tracks `ada_remember`.
  - Instructions updated (word-coaching → `ada_remember` kind='vocab',
    HA recall → `ada_session_recall` scope='history', chaba guest text).
- `backend/visual_habits.py` — challenge prompt now asks for
  `ada_remember` kind='habit' with the same structured fields.
- `docs/ssot/ssot.tool-surface.yml` — memory family split into
  `memory_search` / `memory_remember` / `memory_recall` sub-families
  (each absorbed list points at its true canonical, so lint warns on
  none); `ada_ha_recall` moved out of the `ha` family's absorbed list;
  the five retired names dropped from `coverage_debt` (`guest_register`
  stays — still declared).
- `docs/assessments/tool-consolidation-spec-2026-10-04.md` — memory row
  updated to the executed 8→4 mapping (three canonicals + ada_forget),
  `ha` absorbed count corrected (ada_ha_recall moved).
- `tests/scenarios-live/tool_merge_memory.yaml` — **new** family
  regression scenario (search → ada_memory_search, remember →
  ada_remember, home-memory recall → ada_session_recall/ha search).
- `tests/test_tool_runner.py` — **new** `MemoryMergeAliasTests` (7 tests):
  every alias routes to its parent with the right implied args.
- `tests/test_provider_events.py` — kept the legacy-name habit test and
  added the canonical `ada_remember` kind='habit' event test.
- `tests/test_visual_habits.py` — prompt assertion updated.
- `docs/ssot/jobs/ada/2026-10-04-tools-merge-memory.yml` — **new** job
  doc recording the decisions (three canonicals, gate carve-outs).

## Verification

- `python3 scripts/tool-lint.py` → clean: 100 declared
  (97 builtin + 3 tools.d), 6 aliases, 0 errors.
- `python3 -m unittest tests.test_tool_runner.MemoryMergeAliasTests`
  → 7/7 pass (with a local `google.genai` stub — the SDK isn't
  installed in this env; stub removed after verification).
- `python3 -m unittest tests.test_provider_events tests.test_visual_habits
  tests.test_tool_audit` → all pass (46 tests).
- Full suite `python3 -m unittest discover tests` → 469 tests; the only
  failures are pre-existing/environmental and identical on pristine HEAD:
  3 × `ai_edge_litert` missing (Pi-only dep) and 3 × CMS publish
  mock-drift tests — untouched by this change.
- Card has no `pipeline: ci` field → card-pipeline not run.
- Not committed, not pushed, not deployed (uncommitted diff in worktree).

## Notes / limitations

- `x-legacy: true` as a literal declaration flag doesn't exist — the
  established mechanism (alias table + off-surface) is what lint
  enforces and what was implemented.
- `tool_merge_memory` still needs a live scenario run on idc02 for the
  merge-gate's pass/flaky report before the card can close to done.
- `scope=all` now includes session-summary hits; on chaba instances it
  also includes guest notes — intentional per `scope=all|…|guest`.
