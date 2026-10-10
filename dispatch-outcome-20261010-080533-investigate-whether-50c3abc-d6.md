# Dispatch outcome — ada-transcript-leak-storm

Task: investigate whether the 50c3abc/d614648 leak sanitizer writes
sanitized text back into the transcript stream; check dead-turn guard /
memory-hit-cap interplay after `live_reconnecting` (storm trigger); add a
test asserting tool-result text never appears in
`assistant_transcript_delta`.

## Found

- **Real bug (fixed).** `_strip_tool_leak` emits `text[:m.start()]`
  verbatim, but `m` was the first non-None match in *detector* order
  (declared `_tool_leak_re` → `_GENERIC_TOOL_LEAK_RE` → `_ROLE_LEAK_RE`),
  not the earliest *position*. A declared-name match later in the delta
  shadowed an earlier generic `name{key:` or role-tag leak, which then
  rode the emitted prefix into `assistant_transcript_delta`. E.g.
  `"พบแล้ว\nuser\nada_remember{x:1}"` used to emit `"พบแล้ว\nuser\n"` —
  fabricated role tag included. Fixed by taking `min(m.start())` across
  all three detectors.
- **Tool-result transport is clean.** Results reach the UI only on the
  dedicated `tool_result` ProviderEvent — never as transcript deltas. The
  observed echo wrappers (`Tool <name> returned:`, `<code_output>`) are
  already inside `_tool_leak_re`; once any detector fires, `_leak_active`
  suppresses the rest of the turn including raw hits JSON. The dead-turn
  fallback speaks only a curated `_first_speakable_line` (≤160 chars).
- **Residual gap (documented, not fixed):** a bare raw-JSON echo with no
  wrapper — model regurgitating `{"hits": […]}` — matches no detector
  (`{` has no preceding identifier). Deltas split inside the JSON would
  evade a regex anyway; fixing needs a hold-back like `_artifact_hold`.
- **Reconnect interplay is safe-but-degraded.** A `live_reconnecting`
  rebuild kills per-turn state (`turn_tool_results`, `_dead_retried`,
  `_leak_active`, `_artifact_hold`); `ConversationMemory` survives. A
  dead turn on the fresh provider has no results → honest retry-ask
  fallback (no leak). `_dead_retried` resets per provider, so a flapping
  session grants one retry nudge per reconnect — each re-injects the
  ≤600-char digest into server-side context, bounded by 1s→15s backoff.
  `MEMORY_HIT_CONTENT_MAX` is reconnect-agnostic (tool boundary);
  no-handle reconnects re-prime via `session_prime_text`, which already
  applies `is_transcript_shaped`. pwa's post-resume replay suppression
  compares sanitized-vs-sanitized text — consistent.

## Changed

- `backend/realtime_provider.py` — `_strip_tool_leak` strips at the
  earliest leak position across all three detectors.
- `tests/test_transcript_leak.py` — new, 6 tests: ordering cases plus a
  full `provider.events()` storm replay asserting joined
  `assistant_transcript_delta` never carries `"hits"`, `returned:`, `{`,
  `ada_remember{`, hit content, or hit key — while `tool_result` events
  still deliver the payload; and a fully-leaked text-channel turn goes
  dead-turn → retry → speaks the plate rather than silence.
- `docs/ssot/jobs/ada/2026-10-10-transcript-leak-storm.yml` — audit trail.

## Verify

```
python3 -m pytest tests/test_transcript_leak.py -x -q   # 6/6
python3 -m pytest tests/test_dead_turn.py tests/test_context_budget.py \
  tests/test_speech_sanitize.py tests/test_provider_events.py \
  tests/test_text_channel.py tests/test_ops_events.py \
  tests/test_event_recorder.py -x -q                    # 81 pass, no regressions
```

Note: ran in a throwaway `.venv-test` (removed after) — the worktree has
no `.venv` and system python3 lacks project deps. Not committed, not
pushed, not deployed.
