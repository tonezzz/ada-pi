# Dispatch outcome — tool_result trace fidelity (card ada-trace-full-result)

## What changed

`backend/realtime_provider.py`
- New helpers: `_json_safe` (deep json-safe copy, depth-capped),
  `_canonical_result_json` (sorted keys, tight separators — stable hash
  bytes), `_result_hash` (sha256 of canonical JSON of the *normalized*
  result — the same object the model sees via FunctionResponse),
  `_full_result_payload` (full payload; head-truncated past a 200 KB
  serialized cap with `{_truncated, chars, head}` marker — the hash
  still pins the body).
- ws `tool_call` event gains `id`; ws `tool_result` event gains `id` +
  `result_hash` — `result` stump unchanged (`_safe_args`, ≤300-char
  strings).
- `session_events` tool_call entry now anchors `result_hash`
  (tier-1 ↔ tier-2 join).
- Call-log `function_result` line (audit tier-2 trace doc, keyed by
  session_id + call id) now writes `result_hash` + `result_full`
  alongside the legacy `result` stump.
- `ADA_TRACE_FULL=1` additionally emits a `tool_result_full` ws event
  `{id, name, result, result_hash}` with the complete payload — the
  card's named alternative, taken alongside the primary path.

`pwa_server.py` — forwards `tool_result_full` to the ws client
(debug flag only; off-flag sessions see no change).

`scripts/scenario-live.py` — `check_turn` merges `tool_result_full`
events into `results`, so `result_contains` / `result_contains_any` /
`result_not_contains` / `result_geo_near` / `no_failed_result` assert on
ground truth when the flag is on and stay stump-compatible when off.
Docstring + verbose print updated.

`tests/test_trace_fidelity.py` (new, 12 tests) — canonical-hash
invariants (key-order, sha256 identity, non-dict inputs), cap marker,
ws event id+hash+stump, call-log `result_full` ground truth keyed by
session+call id, `session_events` hash anchor, flag on/off,
failed-result hashing.

Docs — `docs/session-audit.md` tier-2 schema + audit flow updated;
`.env.example` documents `ADA_TRACE_FULL`;
`docs/ssot/jobs/ada/2026-10-10-trace-result-fidelity.yml` records the
decisions (notably: trace doc = call-log, not an mddb ops event, because
`_emit_ops_event` is capped at 5/session and feeds the hourly chaba
index — per-result docs would spam it).

## Result

Done — both the primary spec and the ADA_TRACE_FULL alternative are
implemented and complementary: always-on ground truth in the call-log
file for the audit flow; ws-carried ground truth for debug scenario runs.

## How to verify

- `python3 -m pytest tests/test_trace_fidelity.py` — 12/12 pass.
- Audit join: `jq 'select(.event=="function_result") | {id,name,result_hash,result_full}' ~/.local/share/ada/call-logs/<date>-<sid>.jsonl`
  — recompute `sha256` over the canonical JSON of `result_full` to check
  integrity; same hash appears on the ws `tool_result` event and in
  `session_events`.
- Live debug run: start ada with `ADA_TRACE_FULL=1`, run
  `scripts/scenario-live.py` — `tool_result_full` events flow over the
  ws and `result_*` checks assert on complete payloads.

## Caveats / pre-existing failures (not from this diff)

- `ai_edge_litert` missing in this venv → test_detection / test_pose /
  test_hailo_vision fail on this host only.
- `test_eye_tool` LiveTest fixtures go stale in a full-suite run
  (module-level `time.time()`); passes standalone.
- `scripts/tool-lint.py` is red on the base tree: voice_fx descsize +
  no-scenario-coverage, `ToolRunner.yt_cached_list` undeclared — all in
  files this diff never touched (main already violates).
