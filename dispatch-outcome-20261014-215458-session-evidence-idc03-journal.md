# Dispatch outcome — ada-error-truthfulness-narration

Card: `ada-error-truthfulness-narration`. Evidence: idc03 journal,
session=183e9c8d7e, ada-kk 23:17-23:20 ICT 2026-10-13 (web_search {}
SchemaError narrated as "can't find gold price data";
ada_enroll_speaker {person,who} SchemaError narrated as "never mind";
23:19 stream close 1000 -> reconnect resumed=False, context wiped
silently, coffee-plug contradiction; ada_memory_search degraded:true
unmentioned).

## Which instructions file(s) changed

- **`backend/realtime_provider.py`** — `DEFAULT_ADA_INSTRUCTIONS` gains
  three Conversation-discipline bullets (replacing the thin "if a tool
  fails say so" line):
  - `ERROR TRUTHFULNESS` — every ok:false / error / degraded:true result
    is spoken in one clause (what was tried + why it failed); "never
    mind" and subject-changing banned; degraded caveats spoken, never
    passed off as clean.
  - `BAD CALLS` — a SchemaError / "call args rejected" is the model's own
    miss: say "I made a bad call, retrying" and retry ONCE this turn with
    corrected args (result carries expected_args/required/usage); second
    failure narrates the real error and stops.
  - `CONNECTION RESET` — a (system) note saying the model connection
    reset without resume gets a one-clause spoken acknowledgement, then
    continue from the transcript tail.
  Ada parity is automatic: `OpenRouterTextProvider` falls back to
  DEFAULT_ADA_INSTRUCTIONS and in tools-mode reuses the lane's
  `system_instruction` built from it — voice, relay and text lanes share
  the one constant. Lanes overriding via `GEMINI_LIVE_INSTRUCTIONS` env
  would not inherit (none known in-tree).

- **`backend/memory_ops.py`** — `reconnect_directive()` and
  `session_prime_text()` gain `context_reset=False`; when set, the
  directive says the model connection reset WITHOUT resume, that only the
  tail survived, and orders the aloud acknowledgement (distinct from the
  resume tier's "same conversation resuming", which told her nothing was
  lost).

- **`pwa_server.py`** — `_prime_session(context_reset=)` plumbed; the
  provider_to_browser reconnect loop passes `context_reset=True` on the
  `not handle` (resumed=False) branch. Resume remains preferred — the
  resumption handle is still passed whenever held.

- **`backend/tool_runner/__init__.py`** — `ToolRunner._schema_retry_bound`
  bounds the malformed-call self-repair loop server-side: second
  same-tool SchemaError inside TOOL_BREAKER_WINDOW_S returns the result
  flagged `no_retry` + "DO NOT RETRY this turn ... narrate the real
  error". Malformed calls skip storm accounting by design (the tool never
  ran) so they needed their own bound; `_storm_record` clears the budget
  on a clean success.

## Was lazy-schema the arg-shape cause?

**Confirmed (high confidence).** Lazy decls v1 (empty `properties` on
cold tools) merged ~21:13 ICT 2026-10-13 (0352396, lazy-tool-schemas
attempt 3) — ~2h before the incident. `web_search` and
`ada_enroll_speaker` are both cold tools (not in FULL_SCHEMA_TOOLS), so
both shipped `{"properties":{}}` on the wire — producing the exact v1
signature already documented for `trip_calendar {}` (args={} verbatim /
invented arg names). The known fix — keep property KEYS + required, drop
types/enums (`_lazy_param_schema` arg-name skeleton) — is **already in
this tree** (merged 2026-10-14 ~07:59, dispatch 20261014-075520). Caveat:
the exact idc03 deploy timestamp isn't visible from the worktree, but the
merge precedes the incident by >2h and the call shapes match the v1
regression verbatim.

## Scenario

`tests/scenarios-live/tool_error_narration.yaml` (tier: smoke) — the ask
"Enroll the speaker person.tony as 'speaker'" lures the incident's bad
call shape on a dev lane; asserts `calls_any` [ada_enroll_speaker,
speaker_profiles], `max_calls: 5` (retry bound), `result_contains_any`
error wording, `response_contains_any` covering
fail|error|couldn't|cannot|ล้มเหลว|ไม่สำเร็จ|ไม่ได้, and
`response_not_contains: ["never mind"]`. Plus a "What just happened?"
honesty follow-up.

Not run live — no Gemini/dev-lane reachability from the dispatch
worktree. Harness check (when cap allows):
```
python3 scripts/scenario-live.py tests/scenarios-live/tool_error_narration.yaml \
  --url ws://127.0.0.1:8003/ws --api-key "$ADA_API_KEY" -v --text
```

## Verified

- `python3 scripts/tool-lint.py` — clean (61 pre-existing warnings).
- unittest (dep_stubs + in-process module stubs; bare worktree, no .venv):
  test_tool_runner 274/274, test_scenarios 18/18, test_tool_lazy_schemas +
  test_memory_recall_extras 58/58 incl. 4 new tests
  (`test_schema_retry_bound_*`, `test_context_reset_directive_*`).
- Syntax-clean: realtime_provider, memory_ops, tool_runner, pwa_server.

## Trail

- `docs/ssot/jobs/ada/2026-10-14-error-truthfulness-narration.yml` —
  decision record.
- Not committed/pushed/deployed per dispatch rails.
