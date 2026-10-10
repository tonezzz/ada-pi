# Dispatch outcome — ada-tool-retry-storm (2026-10-10)

## Result: done

Per-session circuit breaker added to ada-pi's tool runner. When the
same tool fails twice with the same error_class inside the window, the
runner stops executing it and returns a synthesized "do not retry —
tell the user it is broken" result. Journal evidence 16:31:03–16:32:18
ICT (dead calendar token → ~75s of identical calendar_read retries).

### How it works

- `backend/tool_runner/common.py`
  - `TOOL_BREAKER_TRIP` (default 2), `TOOL_BREAKER_WINDOW_S` (120s),
    `TOOL_BREAKER_OPEN_S` (300s) — env-tunable via
    `ADA_TOOL_BREAKER_*`.
  - `_CALLER_SESSION` contextvar — per-call session key, same
    shared-runner race-free pattern as `_CALLER_IDENTITY` etc.
  - `_storm_error_class()` — classifies outcomes: exception type name
    verbatim (CalendarAuthError, ReadTimeout, PermissionError/ACL
    denials); bare `{error}` dicts fall back to auth|timeout|acl|error
    text buckets. Skipped: needs_confirm dicts, PermissionErrors whose
    message instructs a retry (confirmed=true / confirm_token / cfm-
    token — the confirm flow working), and arg typos
    (ValueError/KeyError/TypeError/AttributeError).
- `backend/tool_runner/__init__.py`
  - `execute(..., session=)` kwarg → `_CALLER_SESSION`; `_storm_scope()`
    keys breaker state by provider session_id → SpeakerSession →
    owner/caller identity (runner is a shared singleton).
  - `_storm_check()` early in `_execute_gated` — an open circuit returns
    `{ok: False, error_type: "CircuitOpen", error_class, circuit_open:
    true, already_announced, retry_after_s}`; gates and tool never run.
  - `_storm_record()` fed by every outcome: normalized results, gate
    PermissionErrors, and the secondary-speaker denial. First
    synthesized result tells the model to narrate the outage ONCE;
    repeats while open say "already told the user". After OPEN_S the
    circuit half-opens — one probe; success resets, failure re-trips.
- `backend/realtime_provider.py` — all three `tool_runner.execute()`
  call sites now pass `session=self.session_id`.
- Tests updated for the new contract:
  - `tests/test_calendar_providers.py` — 3rd identical-class
    calendar_read failure is now CircuitOpen (still loud ok:false).
  - `tests/test_doc_archive_tool.py` — the four ada_doc_* aliases share
    the `docs` seat; after 2 ACL denials later calls get circuit_open
    dicts instead of raising.
- New coverage:
  - `tests/test_tool_runner.py` `StormBreakerTests` — 15 tests: trip at
    2 same-class failures, per-session isolation, announce-once,
    ReadTimeout/CalendarAuthError classes, ACL-denial trip,
    proposals/typos/needs_confirm never trip, success reset, half-open.
  - `tests/scenarios/tool_storm_breaker.yaml` — forced-failing
    calendar_read: 2 real failures, then synthesized circuit_open with
    narrate-once semantics; other tools unaffected.
- `docs/ssot/jobs/ada/2026-10-10-tool-storm-breaker.yml` — job trail.

## Verification

- `.venv/bin/python -m pytest tests/test_tool_runner.py -q` — 255 pass
- `.venv/bin/python -m pytest tests/test_scenarios.py -q` — 18 pass
- tools_loader + calendar_providers + ops_events + provider_events +
  web_search — 116 pass; test_doc_archive_tool — 24 pass
- Full `tests/` run: 1101 pass, 9 fail — all pre-existing/environment:
  `ai_edge_litert` not installed (detection/pose/hailo), 2 tool-lint
  tests flagging baseline debt (voice_fx descsize+coverage,
  yt_cached_list undeclared — untouched files), 3 eye_tool staleness
  flakes (`FRESH` fixture timestamps captured at import go stale during
  a 2.5-min suite run — pass in isolation)
- Worktree-local `.venv` (gitignored) created for google-genai/fastapi;
  torch/speechbrain not installed (speaker-ID tests not run).

## Not verified live

No ada service/MDDB on the dispatch host. On idc03 after deploy: the
breaker logs `storm breaker OPEN tool=<name> scope=<sess-*> ...` when it
trips; during a real outage expect 2 real failures then CircuitOpen
results — and Ada narrating the outage exactly once.
