# dispatch 20261005-220046 — tool-decl-impl-lint

## Task
Extend `scripts/tool-lint.py`: for every realtime_provider tool
declaration assert a working ToolRunner method exists, and vice versa
for public tool-dispatch methods — flag decl-without-impl and
impl-without-decl. Runs in CI via the tools-ci-audit family
(`tests/test_tool_audit.py`, which invokes the lint against the real
repo).

## What changed
- `scripts/tool-lint.py` — new `runner_methods()` AST pass over
  `class ToolRunner` in `backend/tool_runner.py` (public methods →
  {line, async}), collected via `collect_surface()`. New `impl` check
  in `lint()`:
  - **decl→impl**: a builtin declaration must have a same-named public
    *async* ToolRunner method (`execute()` does `getattr(self, name)`
    then `await method(**call_args)` — a sync def isn't a working
    seat), unless listed in `ssot.provider_dispatched`. A sync method
    for a declared name is flagged separately.
  - **impl→decl**: every public async ToolRunner method is a dispatch
    seat — it must be declared, be an `_ALIASES` key (absorbed impls
    kept for internal delegation — 75 today), or be listed in
    `ssot.runner_internal`. Anything else is flagged.
  - stale/wrong-side exemption entries warn; absorbed-impl count is
    reported as info; `surface` gains `runner_methods` +
    `provider_dispatched` counts.
- `docs/ssot/ssot.tool-surface.yml` — new `provider_dispatched:
  [set_facial_expression]` (provider emits the expression event inline;
  the only decl without a runner method) and `runner_internal:
  [execute]` (the dispatch entry point; the only non-tool async seat).
- `tests/test_tool_audit.py` — `_mini_repo` now writes a `ToolRunner`
  class (`runner_tools` defaults to the declared tools; `runner_sync`,
  `provider_dispatched`, `runner_internal` fixture knobs added). 6 new
  tests: decl-without-impl, impl-without-decl, sync-method-not-impl,
  provider_dispatched exempt, runner_internal exempt, alias-key impl
  accepted.
- `docs/ssot/jobs/ada/2026-10-05-tool-decl-impl-lint.yml` — job trail.

## Result
- `python3 -m unittest tests.test_tool_audit` — 25 tests, all pass.
- Live verify on the real tree:
  - appended `{"name": "zz_orphan_probe", ...}` decl to
    realtime_provider.py → `FAIL impl: zz_orphan_probe ... has no
    ToolRunner method`, exit 1 (reverted)
  - inserted `async def zz_rogue_impl` into ToolRunner →
    `FAIL impl: ToolRunner.zz_rogue_impl ... no declaration or alias`,
    exit 1 (reverted)
  - clean tree → `tool-lint: clean`, exit 0
- Surface math: 112 public async seats = 36 declared methods + 75
  alias-key absorbed impls + `execute`.

## Notes / decisions
- The card's "public tools_ methods" maps to same-named public async
  methods on `ToolRunner` — there is no `tools_` prefix in this
  codebase; dispatch is `getattr(self, name)`.
- Alias-key methods are deliberately exempt — merge cards kept them as
  the absorbed implementations the canonical action= tools delegate to.
- Out of scope (documented in the job file): param-schema vs signature
  sanity, tools.d manifest↔module drift.

## Verify
```
python3 scripts/tool-lint.py            # exit 0, 'tool-lint: clean'
python3 -m unittest tests.test_tool_audit -v   # 25 tests
# negative probe: add a decl dict to backend/realtime_provider.py
#   → 'impl: <name> ... has no ToolRunner method', exit 1
```
