# Dispatch outcome — lazy tool schemas (attempt 3)

Card: `ada-lazy-tool-schemas` — declare name+blurb+empty params for cold
tools, validate args server-side, repair via `tool_guide.yml` usage hints.
Attempt 3: attempt 2's implementation (`932550d` on
`dispatch/20261013-200555`) was merged into this worktree and its merge-gate
failure fixed plus two real regressions it shipped.

## Root cause of the held merge

The gate env is a bare interpreter — no `google-genai`, no `fastapi`, no
`pytest-asyncio`. `tests/test_calendar_providers.py` is the first test file
(alphabetically) whose import chain hits `from google import genai`, so
collection died there and the merge was held. Attempt 2 had made tests pass
locally via an **uncommitted, gitignored `.teststubs/` stub** — the fix
never traveled with the branch. The same ImportError reproduces on clean
`main` in this env (pre-existing latent gap, not caused by attempt 2's
diff).

## What changed on top of attempt 2

- **`tests/dep_stubs.py`** (new, committed) — duck-typed `google.genai`
  and `fastapi`/`starlette` modules installed into `sys.modules` only when
  the real package is absent. Generic kwargs→attrs model classes
  (fabricated per name, unset fields read `None`, `model_copy` supported),
  fabricated enum members, `Part.from_text/from_bytes`, a register-only
  `FastAPI` (route decorators, `mount`, `on_event`, `middleware`,
  `exception_handler`), `HTTPException`/`WebSocketDisconnect`/
  `BackgroundTasks`/`StaticFiles`. No-ops wherever the real deps exist.
- **`tests/conftest.py`** — calls `dep_stubs.install()` before collection,
  plus a `pytest_pyfunc_call` fallback that `asyncio.run()`s module-level
  `async def test_` functions when `pytest-asyncio` isn't installed.
- **`backend/tool_runner/__init__.py`** — `_ALIASES` back-compat calls
  skip `_validate_call_args` (new `from_alias` flag on `_execute_gated`).
  Absorbed-name implied defaults and `_alias_call_args` remap output are
  server-internal contract — `ada_daily_summary` injects
  `period='digest'` (deliberately outside `plan_day`'s declared enum),
  `ada_set_voice` remaps to `*_voice` actions, doc-card actions inject
  `photo=`/`doc=` seats. Canonical wire names still validate strictly.
- **`backend/realtime_provider.py`** — two declaration gaps the schema
  gate exposed, now declared: `control_entity` action `'mute'` (the
  method's verify map always accepted it), `cast_to_screen` `shortcut=`
  arg (absorbed `vcast_shortcut` spelling).
- **`tests/scenarios/session-owner-policy.yaml`** — quoted the `"on"`
  arg key (YAML 1.1 parses the bare key as boolean `True`; the schema
  gate rightly rejected the non-string arg name before the owner check).
- **`tests/test_detection.py`, `test_pose.py`, `test_hailo_vision.py`** —
  `skipUnless(find_spec("ai_edge_litert"))` on real-inference tests
  (models bundled, runtime dep absent on bare interpreters).
- **`tests/test_tool_runner.py`, `test_tool_lazy_schemas.py`** — enum /
  required / unknown-arg misses on canonical tools now assert
  `error_type: SchemaError` (the schema gate answers before the confirm
  gate and before method-level `ValueError` paths); new regression test
  for the alias exemption.
- **`docs/ssot/jobs/ada/2026-10-13-lazy-tool-schemas.yml`** — attempt-3
  notes appended.

## Result / measured

- `python3 scripts/tool-lint.py` — clean (51 declared, 0 errors).
- `python3 -m pytest tests/ -x -q` (bare system python, no venv) —
  **1349 passed, 4 skipped** (litert-bound inference tests), 0 failures.
  Attempt 2's own best was 1307 pass + 44 stub-gap failures.
- Wire decl JSON re-measured: builtin decls 52,403 → 22,540 bytes
  (−57.0%), consistent with attempt 2's all-surface 63,862 → 27,668
  (−56.7%) measurement.
- `trip_calendar` contract (unit): `{trip_slug, dates}` → SchemaError +
  `expected_args` + tool_guide hint; `{slug:"smoke-nonexistent-trip",
  start:"2030-01-01", end:"2030-01-03"}` passes validation
  (`tests/test_tool_lazy_schemas.py::test_real_schema_trip_calendar`).

## Verify

```
python3 scripts/tool-lint.py
python3 -m pytest tests/ -x -q          # bare interpreter — 1349 pass, 4 skip
python3 -m pytest tests/test_tool_lazy_schemas.py -q
```

## Not done (operator steps, same as attempt 2)

- Merge this branch, then `deploy-ada.sh` on idc03 + restart
  `ada-ha-tony` (dispatch rails: no push/deploy from session).
- Post-deploy benchmarks per card: fresh-session first-turn `in=` vs
  ~27.8k baseline; live `trip_calendar` smoke; `context_growth.yaml`
  (live tier — needs the running backend); 24h audit-fail / arg-error
  delta vs today.
- Rollback: `ADA_LAZY_SCHEMAS=0` on the unit, or widen
  `ADA_FULL_SCHEMA_TOOLS`.

lessons:
- Bare gate interpreters have no `google-genai`/`fastapi`/`pytest-asyncio`
  — any test-env stub MUST be committed (`.teststubs/` is gitignored;
  attempt 2's green was local-only). `tests/dep_stubs.py` + conftest now
  cover all three; no-op when real packages exist.
- `_ALIASES` implied defaults and `_alias_call_args` output are
  server-internal arg values that deliberately sit OUTSIDE declared
  schemas (period='digest', *_voice actions) — validate canonical wire
  calls only, never the merged alias args.
- The schema gate fires before confirm gates AND method arg validation —
  enum/required misses relabel ValueError→SchemaError; tests asserting
  the method's error_type need updating, and test args must be
  schema-valid to reach deeper gates (read-only, owner, pending-confirm).
- YAML 1.1 bools as arg KEYS (`on: true`) silently become `True` —
  schemas with additionalProperties:false reject them; quote keys in
  scenario files.
- Declared enums/args can drift behind what methods actually accept —
  the schema gate turned two latent lies into test failures
  (control_entity 'mute', cast_to_screen 'shortcut='); fix the decl,
  not the test, when the method contract is real.
