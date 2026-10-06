# Dispatch outcome — gev_command arg whitelist

Card: `gev-command-whitelist` — "add a per-command known-args map …
reject unknown or missing keys locally with the expected schema in the
error, before the ws relay round-trip."

## What changed

- `backend/tool_runner.py`
  - `_GEV_ARG_SCHEMAS` — class-level map covering **all 28** commands
    from the GEV tool schema (chaba
    `stacks/tony-dell/gev-gemini/tools.json`, extracted from
    `GEV_REALTIME_TOOLS` in gods-eye-view `vite.config.js`). Each entry:
    `args` kind-specs (type + enum for the error text), `required`,
    plus `requires_one` for `fly_to_location`
    (`locationId | query | latitude+longitude`).
  - `_gev_args_error(name, args)` — validator returning
    `{ok: False, error, expected}` with a compact schema
    (`{arg: kind, * = required}`) or `None`.
  - `gev_command()` calls the validator **after** the existing 0bb8e9b
    `{name,args}`-inside-args unwrap and **before** the relay payload is
    built. Commands absent from the map pass through untouched.
  - `args=None` → treated as `{}` (missing-required errors); non-dict
    non-None → "args must be an object"; non-string `name` → passthrough.
- `backend/realtime_provider.py` — `gev_command` `args=` description now
  notes the local schema check ("a rejected call returns the expected
  args in the error; fix and retry").
- `tests/test_tool_runner.py` — new `GevArgWhitelistTests` (9 tests):
  unknown arg, missing required, no-args, non-dict args, requires_one,
  `annotate_map` required, unwrap-then-validate, unmapped passthrough,
  `execute()` boundary normalization.
- `docs/ssot/jobs/ada/2026-10-06-gev-command-whitelist.yml` — job trail
  (decisions + verification).

## Design notes

- The card listed `fly_to_location: {locationId, query, latitude,
  longitude}` as an example; the map uses the full schema property set
  (`+viewMode, rangeM, waitForArrival`) so schema-valid calls can never
  be locally rejected — the whitelist only short-circuits calls the GEV
  client is guaranteed to refuse.
- Key-level checks only (unknown/missing) per the card; value/enum
  validation remains the client's job.
- Cross-checked 1:1 against `gods-eye-view/src/voice/gevActions.js`
  dispatch — every schema name is client-handled, zero drift; and
  `annotateMap` does require `args.annotations` to be a non-empty array.

## Verification

- `pytest tests/test_tool_runner.py -k Gev`: 17/17 pass.
- Full file: 191/191. **Full suite: 702/702 pass** (via the ada-pi venv
  at `~/CascadeProjects/ada-pi/.venv`).
- `python3 scripts/tool-lint.py`: clean (38 declared, 90 aliases).
- Synthetic bad-args probe (direct call, real default `GEV_CMD_URL`) —
  all returned in **0ms** with schema errors, no relay round-trip:
  - `fly_to_location {query:'Bangkok', altitude:5000}` →
    `unknown args ['altitude']` + expected schema
  - `annotate_map {flyTo:true}` →
    `missing required args ['annotations']` + schema
  - wrapped envelope `{name,args:{bogus}}` → `unknown args ['bogus']` +
    `needs one of locationId | query | latitude+longitude`
- Live `gev_*` scenarios were NOT run in-worktree (no Ada backend on
  this host — services on idc03; live scenario runs belong to the idc02
  staging checkout per AGENTS.md). The whitelist only rejects shapes the
  client itself refuses, so schema-valid scenario calls are unaffected.

## Files

- `backend/tool_runner.py` (+171)
- `backend/realtime_provider.py` (1-line description)
- `tests/test_tool_runner.py` (+91)
- `docs/ssot/jobs/ada/2026-10-06-gev-command-whitelist.yml` (new)
