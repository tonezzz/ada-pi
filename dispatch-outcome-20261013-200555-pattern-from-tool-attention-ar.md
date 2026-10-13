# Dispatch outcome — lazy tool schemas

Card: `ada-lazy-tool-schemas` — declare name+blurb+empty params for cold
tools, validate args server-side, repair via existing `tool_guide.yml`
usage hints. Attempt 2 (attempt 1 never merged/verified).

## What changed (commit `932550d` on `dispatch/20261013-200555-pattern-from-tool-attention-ar`)

- **`backend/realtime_provider.py`** — the inline
  `FUNCTION_DECLARATIONS` literal moved into `_builtin_declarations()`
  (fresh copies per call — connect() filters and
  `_fill_bank_placeholders` mutate the dicts per session). New:
  - `FULL_SCHEMA_TOOLS` — the 10 hot tools that keep full wire schemas:
    `set_facial_expression`, `ada_memory_search`, `cms_read`,
    `control_entity`, `calendar_read`, `cast_to_screen`, `cctv_wall`,
    `docs`, `kanban`, `chat_send`.
  - `_apply_lazy_schemas(decls)` — replaces `parameters_json_schema` /
    `parameters` with `{"type":"object","properties":{}}` for every decl
    not in the hot set. Applied last in `connect()`, after exclusions,
    chaba allowlist, bank placeholders, camera + tools.d appends — so all
    51 wire decls obey one rule.
  - Kill switch `ADA_LAZY_SCHEMAS=0`; `ADA_FULL_SCHEMA_TOOLS=csv`
    extends the hot set without a redeploy.
  - `declared_tool_schemas()` — the REAL schema map (builtins +
    `CAMERA_DECLARATION` + `CHABA_DECLARATIONS`; builtin wins chaba
    re-decl collisions so guest-scoped subsets can't reject
    owner-surface args like `challenge_id`).
- **`backend/tool_runner/common.py`** — `_declared_schemas()` merges the
  provider map with live tools.d decls (manifest-mtime reloads stay
  fresh); `_validate_call_args()` does shallow schema checks: missing
  required, unknown keys (hard-reject when `additionalProperties:false`
  or every supplied key is unknown), structural type misses, enum
  membership. Scalar spellings ('5' for number) still pass to method
  coercion.
- **`backend/tool_runner/__init__.py`** — `execute()` validates in
  `_execute_gated` after alias remap + confirm-envelope pops, before any
  gate mints a confirm token and before `_normalize_args` can silently
  strip unknowns. A miss returns `ok:false` + `error_type:SchemaError` +
  `expected_args`/`required` + the `tool_guide.yml` `usage` hint through
  the same `_usage_hint`/`_capture_reminder` wrapper every failure uses.
  No new retry machinery.
- **`docs/ssot/ssot.tool-surface.yml`** — `lazy_schemas` block
  (env names + hot set) as the contract of record.
- **`scripts/tool-lint.py`** — `provider_const_set()` AST-reads
  `FULL_SCHEMA_TOOLS`; lint errors on ssot↔provider drift and on hot
  names that aren't declared.
- **`tests/test_tool_lazy_schemas.py`** (new, 14 tests) + 3 new
  lint-fixture tests in `tests/test_tool_audit.py` (drift both ways).
- **`docs/ssot/jobs/ada/2026-10-13-lazy-tool-schemas.yml`** — job trail.

## Result / measured

- Wire decl JSON: **63,862 → 27,668 bytes (−56.7%)**, ~ −9k input
  tokens of the ~27.8k first-turn floor (chars/4 est; measured over all
  50 decls incl. tools.d). Card hoped for a ~10-15k floor — honest est
  is ~18-19k unless instruction/context blocks shrink too.
- `tool-lint`: clean — 51 declared (37 builtin + 14 tools.d), 0 errors.
- Focused tests: 49/49 pass (lazy decls + schema validation + lint
  fixtures).
- Full suite (`ada-pi/.venv` + `.teststubs`): **1307 passed, 44 failed**
  — failure set is byte-identical to clean HEAD (1285 passed, same 44
  stub-env gaps: `types.Behavior`/`MediaResolution`/`LiveServerMessage`
  etc. absent from the teststubs google.genai). Zero regressions; +22
  net new passing tests.
- trip_calendar e2e (unit level): `{trip_slug, dates}` → SchemaError +
  `expected_args` + `tool_guide` hint; `{slug:"smoke-nonexistent-trip",
  start:"2030-01-01", end:"2030-01-03"}` passes validation.

## Verify

```
python3 scripts/tool-lint.py
.venv/bin/python -m pytest tests/test_tool_lazy_schemas.py tests/test_tool_audit.py -q
.venv/bin/python -m pytest tests/ -q   # 44 fails are pre-existing stub gaps — diff vs HEAD
```

## Not done (operator ops, post-merge)

- Push/merge the dispatch branch, then `deploy-ada.sh` on idc03 +
  restart `ada-ha-tony` (dispatch rails: no push/deploy from session).
- Benchmark: fresh-session first-turn `in=` after deploy vs the ~27.8k
  baseline; live `trip_calendar` smoke (`smoke-nonexistent-trip`,
  2030-01-01→03); `context_growth.yaml` ceiling+floor; 24h audit-fail /
  arg-error delta. None of these are measurable without the deploy.
- Rollback if arg-error rate spikes: `ADA_LAZY_SCHEMAS=0` env on the
  unit, or widen `ADA_FULL_SCHEMA_TOOLS`.

lessons:
- `.teststubs/google/genai.py` is a bare namespace — no `types.Behavior`
  etc., so any test touching `_builtin_declarations()` must tolerate it;
  the 44 suite failures it causes are pre-existing on clean HEAD.
- CHABA_DECLARATIONS re-declares memory tools guest-scoped (property
  subset) — the real-schema map must let builtin decls win name
  collisions or guest-mode surfaces reject owner args.
- `execute()` pops `confirmed`/`confirm_token`/`_verified_affirm` before
  validation and re-injects only into named method params — validate
  after the pops, and note dyn tools (`**kwargs` bind) never get
  `confirmed` re-injected (pre-existing quirk).
- `_normalize_args` silently strips unknown args — schema validation
  has to run BEFORE it or a miss becomes a silent partial call.
- Decl dicts must be built fresh per connect — bank placeholders and
  filters mutate them per session.
