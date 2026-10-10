# Dispatch outcome — tools.d runner facade (ada-toolsd-runner-facade)

## What changed

`runner.context` is now the formal one-seam facade for tools.d modules.

- **`backend/tool_runner/common.py`** — `ToolContext` gains a `runner`
  back-reference and exposes `mddb` (property delegating to
  `runner.mddb` — the ROUTED client; ops-store split + read-replica
  failover live inside it), `session_id` (per-call `_CALLER_SESSION`
  ContextVar first, `runner.session_id` fallback), and
  `emit_ops_event(ev_type, detail, tool=...)`.
- **`backend/tool_runner/__init__.py`** — `ToolRunner` passes
  `runner=self` into `ToolContext`.
- **`backend/gemini_pool.py`** — `emit_ops_event`'s `exc` arg is now
  optional so non-quota events (dependency outages) can emit with just
  `ev_type` + `detail`. Throttling/shape unchanged.
- **`backend/tools.d/ada_track_device.py`** — all six hand-rolled
  `getattr(runner,"mddb")`-or-raw-httpx sites (`_mddb_get`, `_mddb_docs`,
  `_mac_registry`, `_mddb_write`, `_watch_doc`, the `resume_watches`
  claim re-read) now go through a `_mddb()` helper reading
  `runner.context.mddb`. `ADA_TRACK_MDBB` and `_mddb_base()` deleted —
  the env fallback bypassed client routing and only existed as a
  fixture seam. In chaba mode (`mddb=None`) the beacon source degrades
  silently. `ADA_TRACK_{TAILSCALE,LANSCAN}_JSON`, `_LANSCAN_URL`,
  `_PUSH_DRYRUN` stay — they inject data, not endpoint failover.
- **`backend/tools.d/ada_market_quote.py`** — `_get()` now returns
  `api_down` (every attempt failed at transport/parse level vs
  HTTP>=400 app-level); transport outages emit `market_quote_api_down`
  via `runner.context.emit_ops_event`.
- **`backend/tools.d/kanban.py`** (absorbs ada_board_write) — `ctx`
  plumbed through the `_request()` funnel and all action helpers;
  `board_client` status==0 (unreachable) emits
  `kanban_board_unreachable` via the facade.
- **`docs/ada-tool-dev.md`** — authoring contract rewritten around
  `runner.context` as the ONE seam (fixture injection; no env-var
  endpoint overrides).
- **`docs/ssot/jobs/ada/2026-10-10-toolsd-runner-facade.yml`** — job
  trail with decisions + verification.

## Tests

- New `tests/test_tool_context.py` (9 tests: delegation, chaba-mode
  None, ContextVar precedence, emit forwarding, real-runner wiring).
- `tests/test_track_device.py` `_runner()` injects `context.mddb`.
- `test_market_quote`/`test_kanban` unreachable tests now assert the
  ops-event emit; the market 500 test asserts NO emit (app-level).

## Result / verification

- 122 targeted tests pass; scenario suite 18 pass incl.
  `device_track_merge.yaml` (engine injects `runner.mddb=FakeMddb` —
  `context.mddb` delegates to it).
- Full suite: **1217 passed, 2 failed** — both are the 3 PRE-EXISTING
  tool-lint violations (`voice_fx` descsize + coverage,
  `yt_cached_list` impl), verified identical on clean HEAD via stash;
  already documented in `2026-10-10-alias-hit-rollup.yml`. Deploy gate
  will hit these on next deploy — they belong to a separate card.
- Env note: worktree had no `.venv`; created one
  (`venv --without-pip` + get-pip) with httpx/pytest/pyyaml/
  google-genai/fastapi/numpy/pillow/ai-edge-litert/qrcode/uvicorn —
  `.venv/` is gitignored.

## Not done (follow-ups)

- `tools.d/eye.py` still uses `getattr(runner, "mddb")` — works, but
  should adopt `context.mddb` on its next touch (wasn't named in the
  card).
- The voice_fx/yt_cached_list lint debt blocks the deploy gate until
  its owning card lands.
- Not committed, pushed, or deployed (dispatch rails).
