# dispatch outcome — ada_render_video LiteLLM bypass / spend ledger

Card: `ada-render-video-bypasses-litellm-no-per-ede157`
Dispatch: `20261014-220118-ada-render-video-calls-generat`

## What was done

Implemented the card's recommended option **(b) — meter in-tool** —
and made option **(a) — LiteLLM routing** a two-env flip after
verifying upstream support.

- `backend/usage_tracker.py` — `record()` accepts `est_cost_usd`
  (flat USD → `cost` in the jsonl row) and `detail` (audit-only dict,
  never aggregated). Reports gain `flat_cost_usd` and fold flat costs
  into `est_cost_usd`, so `ada_usage_summary` / `ada_ops action=usage`
  now answer "what did we spend" including non-token calls.
- `backend/tools.d/ada_render_video.py` — on every *accepted*
  `predictLongRunning`, `_meter_submit()` appends
  `source="render_video"`, `est_cost_usd = secs * rate`, and a detail
  row (model/secs/aspect/res/operation/api_base) to `ADA_USAGE_LOG`
  (`data/usage.jsonl`). Metering happens at submit time — the render
  is billed on accept; `operation=` resumes meter nothing; a ledger
  failure warns but never breaks a paid render. Rate table covers the
  veo-3.1 family per second per resolution (fast $0.10/$0.12, lite
  $0.05/$0.08, standard $0.40 — ai.google.dev, Oct 2026; the card's
  ~$0.15/s was stale); `ADA_RENDER_VIDEO_RATE_USD` overrides for
  unlisted models. `est_cost_usd` rides the ok + pending results.
- Option (a) verified upstream: LiteLLM's `/gemini` passthrough
  officially supports Veo `predictLongRunning` + operation polling +
  file download (PR BerriAI/litellm#14228). Made the tool
  passthrough-ready: `_download()` rewrites the Google-hosted clip URI
  onto `ADA_GEMINI_API_BASE` when it's non-default (the substitution
  LiteLLM docs require — a virtual key 401s against Google direct),
  and `ADA_RENDER_VIDEO_API_KEY` overrides the auth key.
- Docs: `.env.example`, module docstring, `tool_guide.yml` (quote the
  estimate when asked), `docs/ssot/jobs/ada/2026-10-14-render-video-
  spend-ledger.yml`.

## Result

Veo spend now lands in the same local ledger as everything else —
`est_cost_usd` includes it per-source and per-day, and "reset the
counters" clears it. Routing video through the gateway later needs
only env (`ADA_GEMINI_API_BASE=<proxy>/gemini` +
`ADA_RENDER_VIDEO_API_KEY=<virtual-key>`); open question is whether
the deployed proxy's passthrough attributes duration cost to LRO
calls — verify on idc03 before flipping.

## Verify

- `python3 -m pytest tests/test_render_video.py -x -q` → 31/31
- `python3 -m pytest tests/test_scenarios.py tests/test_tool_runner.py
  tests/test_gemini_pool.py tests/test_tool_context.py
  tests/test_ops_events.py tests/test_tool_usage_report.py` → all pass
- `python3 scripts/tool-lint.py` → clean
- Live: submit a render on a lane, then
  `grep render_video data/usage.jsonl` shows a `cost` row and
  `ada_usage_summary(source=render_video)` reports it.
