# dispatch-outcome — tools-merge-meta-voice

**Card:** `tools-merge-meta-voice` (chaba kanban)
**Worktree:** `dispatch-wt-20261005-182958-merge-the-meta-voice-group-8-3`
**Branch:** `dispatch/20261005-182958-merge-the-meta-voice-group-8-3` (uncommitted — no push/commit per card)
**Result:** done — meta/voice group merged 8 declared surfaces → 3 canonical tools.

## What changed

Canonical seats:
- `ada_persona` absorbed `ada_set_voice` → `action=set_voice|show_voice|list_voices` (+`voice` param).
- New `ada_ops` absorbed five meta tools → `action=outcome|usage|health|check|research`.
- `ada_enroll_speaker` absorbed `guest_register` → `who=speaker|guest`.

All seven absorbed names stay registered as hidden aliases in
`tool_runner._ALIASES` + `_ALIAS_ARG_DEFAULTS` (30 aliases total now) and are
removed from the declared model-facing surface (5 builtin declarations + the
chaba `guest_register` declaration removed; `ada_ops` declared; persona/enroll
declarations extended). `ada_set_voice`'s `action=set|show|list` collides with
persona's own actions, so `_alias_call_args` remaps them onto the `*_voice`
forms.

Fidelity decisions:
- `check`/`research` stay provider-side background ops (verdicts/findings arrive
  as injected turns); `ada_ops` runner method returns an honest error for them
  on non-live paths — same as pre-merge.
- `ada_mddb_health` was a tools.d drop-in: module deleted, manifest entry
  removed, `run()` ported verbatim to `runner._ops_mddb_health`.
- Voice switching stays provider-side (idle-gated reconnect); runner path
  handles persist/show/list for REST/alias calls.
- Gates key off resolved names: `ada_ops` holds `ada_outcome`'s
  MEMORY_WRITE_TOOLS + confirm seat but only `action='outcome'` triggers them;
  `usage|health|check|research` keep their old ungated semantics. Bank
  `allowed_tools` are alias-normalized so legacy `ada_outcome` entries still
  authorize. `who='guest'` is carved out of the enrollment confirm probe
  (`guest_register` was never gated). `DEVIN_CONFIRMED_TOOLS` / `confirm_strip`
  unchanged.
- Secondary parity: provider blocks `ada_ops action=check` + persona voice
  actions (old `ada_decision_check`/`ada_set_voice` seats); runner keeps
  outcome/health blocked (health replaces the drop-in's
  `secondary_allowed: false`) and usage/research free.

## Files changed

`backend/tool_runner.py`, `backend/realtime_provider.py`,
`backend/tools.d/manifest.yml`, `backend/tools.d/ada_mddb_health.py` (deleted),
`docs/ssot/ssot.tool-surface.yml`, `docs/ada-tool-dev.md`,
`docs/assessments/tool-consolidation-spec-2026-10-04.md` (status row),
`scripts/scenario-live.py` (LEGACY_TOOL_ALIASES), `tests/benchmark.yml`
(write_tools + scenario allowlist), `tests/scenario_engine.py`
(FakeMddb.is_ops_routed shim — upstream drift fix),
`tests/scenarios-live/change_voice.yaml` (canonical names),
`tests/scenarios-live/tool_merge_meta_voice.yaml` (new),
`tests/test_tool_runner.py` (MetaVoiceMergeAliasTests, 11 tests),
`tests/test_decision_check.py` (source assertions),
`docs/ssot/jobs/ada/2026-10-05-tools-merge-meta-voice.yml` (trail).

`.teststubs/` google.genai stub was recreated (gitignored) — needed by the
suite in this fresh worktree.

## How to verify

```bash
python3 scripts/tool-lint.py   # 82 declared (80 builtin + 2 tools.d), 30 aliases — clean
PYTHONPATH=.teststubs python3 -m unittest tests.test_tool_runner.MetaVoiceMergeAliasTests   # 11/11
PYTHONPATH=.teststubs python3 -m unittest tests.test_decision_check                          # 19/19
PYTHONPATH=.teststubs python3 -m unittest discover -s tests -p 'test_*.py'                   # 521 tests
```

Alias routing (all verified through `execute()`):
`ada_set_voice`→`ada_persona` `*_voice`; `ada_outcome`→`ada_ops outcome`
(confirm-gated); `ada_usage_summary`→`ada_ops usage`;
`ada_mddb_health`→`ada_ops health`; `ada_decision_check`→`ada_ops check`;
`ada_deep_research`→`ada_ops research`; `guest_register`→`ada_enroll_speaker
who=guest` (ungated).

## Caveats

- Full suite: 521 tests, 0 failures; 3 errors are `ModuleNotFoundError:
  ai_edge_litert` (TFLite runtime missing — vision/pose tests, pre-existing
  env dependency, unrelated).
- Scenario tests need `ADA_INSTANCE_ID` set (conftest sets `test` under
  discover; `tests/scenario_engine.py` runs standalone need `tony`).
- Uncommitted by design — dispatcher owns the merge/push decision.
