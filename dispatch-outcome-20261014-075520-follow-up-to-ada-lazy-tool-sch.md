# Dispatch outcome — lazy schemas v2: arg-name skeleton

Card: `ada-lazy-schemas-arg-names` — follow-up to `ada-lazy-tool-schemas`
post-deploy verify. Regression: all 39 lazy-stripped decls shipped zero
arg names; Gemini Live called cold tools with `{}` verbatim
(trip_calendar {}, 2/2 live sessions). The server repair path worked
(ok:false + SchemaError + usage hint) but Gemini Live did not retry and
told the user "missing trip name". Fix is option b from the verify
comment: keep property KEYS + required on the wire.

## What changed (worktree `dispatch/20261014-075520-follow-up-to-ada-lazy-tool-sch`)

- **`backend/realtime_provider.py`** — `_apply_lazy_schemas` now projects
  each cold decl through new `_lazy_param_schema()`: keeps
  `{"type":"object","properties":{<arg>:{}},"required":[...]}` and drops
  per-property type/description/enum, nested bulk, and
  `additionalProperties`. `required` omitted when absent/empty in the
  source. `FULL_SCHEMA_TOOLS` hot-10, `ADA_LAZY_SCHEMAS=0`,
  `ADA_FULL_SCHEMA_TOOLS` unchanged; `_LAZY_EMPTY_SCHEMA` removed.
- **`tests/test_tool_lazy_schemas.py`** — updated for the new shape:
  cold-decls-ship-skeleton sweep (keys match the real schema, every prop
  def is `{}`, required preserved, no extra top-level keys), exact-pin
  tests on `ada_forget` (builtin, keeps required) and `trip_calendar`
  (tools.d — the reported regression decl), both-schema-keys
  skeletonization incl. enum/nested/`additionalProperties` stripping,
  no-required omits the key, plus `test_empty_args_schema_error`
  asserting a `{}` call still dies on SchemaError + expected_args before
  the method runs. Hot-tools-keep-full-schema and kill-switch tests
  unchanged.
- **`docs/ssot/ssot.tool-surface.yml`** — `lazy_schemas` comment updated
  to the arg-name skeleton shape.
- **`docs/ssot/jobs/ada/2026-10-14-lazy-schema-arg-names.yml`** — job
  trail (regression, decision, measurement).

## Result / measured

Wire decl JSON over the tony-voice surface (51 decls: builtin + camera +
tools.d, non-chaba):

| surface        | bytes  |
|----------------|--------|
| full schemas   | 67,447 |
| v1 empty       | 29,155 |
| v2 arg-names   | 33,265 |

−50.7% vs pre-lazy; +4,110 bytes (~1k tokens) vs v1 for the arg-name
shape — comfortably under the <=~38k target.

- `python3 scripts/tool-lint.py`: clean (51 declared, 0 errors).
- `tests/test_tool_lazy_schemas.py` + `tests/test_tool_audit.py`:
  **54/54 pass** under `python3 -m unittest` with dep_stubs + in-process
  stubs for httpx/websockets/etc. (this bare worktree has no .venv and
  no pytest — the deploy gate's `.venv/bin/python -m pytest tests/ -x -q`
  runs on merge as usual).

## Verify

```
python3 scripts/tool-lint.py
.venv/bin/python -m pytest tests/test_tool_lazy_schemas.py tests/test_tool_audit.py -q
```

## Not done / notes

- Live e2e deferred — Gemini Live spend cap blocks voice sessions
  (ada-gemini-live-spend-cap). Once unblocked: fresh voice session,
  ask for a trip calendar; expect the model to fill slug/start/end from
  the wire skeleton instead of emitting `{}`.
- No push/deploy per dispatch rails; merge gate + `deploy-ada.sh` on
  idc03 as usual.
