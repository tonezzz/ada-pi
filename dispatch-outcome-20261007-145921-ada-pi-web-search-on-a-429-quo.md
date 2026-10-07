# Dispatch outcome — web-search-quota-fallback (2026-10-07)

## Result: done

web_search now degrades loudly on a 429/quota-shaped gemini error
instead of surfacing a bare failure or an unmarked fallback:

- `backend/tool_runner/web.py`
  - `_is_quota_error()` — detects quota-shaped failures via structured
    exception fields (`code`/`status_code`/`status` == 429 or
    RESOURCE_EXHAUSTED) plus a text signature (429, resource_exhausted,
    rate limit, too many requests, quota).
  - `web_search()` — on such an error in `auto` (default) mode it retries
    once against `_web_search_ddg` and annotates the result:
    `provider_used`, `quota_exhausted=True`, `degraded` ("briefly tell the
    user live search is degraded"), `fallback_error`. Non-quota failures
    keep the existing any-error fallback but report
    `quota_exhausted=False`; every result now carries `provider_used` +
    `quota_exhausted`. Explicit `provider='gemini'` still raises (the
    tool description already instructs picking duckduckgo when quota is
    gone) — but its 429 still posts the ops event.
  - `_emit_quota_ops_event()` — fire-and-forget doc to
    `ada-ha-events-<instance>` with `meta.kind=ops-event`,
    `type=web_search_quota`, `tool=web_search`, `session_id`, `ts` —
    same feed shape as provider `_emit_ops_event` / write_outbox
    dead-letters, so quota burn lands in the hourly digest before it
    reaches zero. Runner-side so REST + deep-research callers count too;
    throttled to one event per `ADA_QUOTA_EVENT_MIN_S` (default 300s) so
    a 429 storm can't flood the feed. No-op when mddb is absent (chaba
    mode) or no event loop.
- `backend/tool_runner/__init__.py` — `_web_quota_event_at` throttle stamp.
- `backend/realtime_provider.py` + `backend/tool_guide.yml` — web_search
  description now states the `quota_exhausted`/`degraded` contract so the
  model says "live search is degraded" instead of passing fallback hits
  off as grounded.
- `tests/test_web_search.py` — new, 14 tests.
- `docs/ssot/jobs/ada/2026-10-07-web-search-quota-fallback.yml` — job trail.

## Verification

- `.venv/bin/python -m pytest tests/test_web_search.py -q` — 14 passed
- `.venv/bin/python -m pytest tests/test_tool_runner.py -q` — 201 passed
- `tests/test_ops_events.py test_provider_events.py test_tools_loader.py`
  — 51 passed
- `scripts/tool-lint.py` — 3 PRE-EXISTING violations on tools.d
  `ada_track_device` / `ada_device_acl` (baseline debt on this HEAD,
  unrelated files; my diff adds no tools/aliases and lint doesn't flag
  web_search)
- Note: worktree-local `.venv` (gitignored) was created to run tests —
  the dispatch host lacks google-genai/PIL/numpy; torch/speechbrain not
  installed, so speaker-ID tests were not run here.

## Not verified live (no ada service/MDDB on the dispatch host)

On idc03 after deploy: trigger a real grounded-search 429 (or set
`ADA_QUOTA_EVENT_MIN_S=1` in a test shell) and confirm a
`web_search_quota` doc appears in `ada-ha-events-tony` and the next
hourly digest.
