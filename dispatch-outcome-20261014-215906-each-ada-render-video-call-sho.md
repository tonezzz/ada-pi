# ada-render-job-wp-log — dispatch outcome

## What changed

**`backend/tools.d/ada_render_video.py`** — every call that reaches the
Veo attempt stage now lands one `render_job` post on the WP boards:

- Extracted the submit-or-resume flow into `_attempt()`; `run()` collects
  a `job` ctx (prompt/ratio/secs/res/ref_url/operation/t0/runner) and
  calls `_log_render_job(job, out)` after the attempt returns.
- `_log_render_job` builds the meta payload and schedules
  `_post_render_job` via `asyncio.create_task` — fire-and-forget, own
  15s httpx client, tracked in module `_WP_TASKS` for GC/tests. It
  never raises and never blocks `_finish`; a WP outage can't touch the
  render result.
- Meta per card spec: `kind='video'`, `prompt`, `model`, `secs`,
  `aspect`, `res`, `ref_url`, `operation` (Veo op name — resume dedup
  key), `duration_s` (wall-clock), `host`, `backend='veo'`,
  `status` ok|error (+`pending` for poll-timeout calls), `output_url`,
  `source='ada_render_video'`, `requested_by` (lane/user best-effort:
  `ADA_INSTANCE_ID`/`runner._memory_identity()`), `error` on failure,
  `est_cost_usd`, `parent_job=''`, `chain_index=0`.
- Title per title_standard: `Render — {prompt[:60]} {YYYY-MM-DD}`.
- `est_cost_usd` is a documented estimate: model contains `fast` →
  {720p: $0.10, 1080p: $0.12}/s, else {720p: $0.40, 1080p: $0.40}/s,
  × secs.
- Skips silently when `wp-boards.env` is absent/unconfigured.

**`backend/wp_client.py`** — `create_post` gained a backwards-compatible
`meta: dict | None` kwarg → `payload["meta"]` (the render_job CPT's
plugin-registered show_in_rest post meta carries the video fields).

**`tests/test_render_video.py`** — new `RenderJobWpLogTest` (6 tests):
success meta shape/title, error meta, pending status, resume dedup key,
silent skip without env, WP failure never breaks the render.
`RenderVideoTest.setUp` stubs `wp_client.env → {}` so no suite can hit a
real board on a configured host.

**`docs/ssot/jobs/ada/2026-10-14-render-job-wp-log.yml`** — decision
record.

## Judgment calls (documented in the SSOT yml)

- `status='pending'` for poll-timeout returns — outside the card's
  ok|error pair but the submit already billed a render; the resume call
  shares `operation`, so the plugin's dedup reconciles onto the same job.
- `parent_job`/`chain_index` always `""`/`0` — a resume shares the
  submit's operation (same job after dedup); the fields are reserved for
  future extend chains.
- Gate-closed / lane-not-open / bad-arg refusals don't post — no render
  was attempted. Logging starts at ref-resolution/submit onward.

## Verification

- `.venv/bin/python -m pytest tests/test_render_video.py -x -q` → 31/31
- `python3 scripts/tool-lint.py` → clean (53 declared, cap 112)
- `.venv/bin/python -m pytest tests/ -x -q` → 1420 passed, 7 skipped
  (fresh uv-managed `.venv` in the worktree: httpx, numpy, pillow,
  pyyaml, websockets, aiohttp, uvicorn, fastapi, pytest)
- Live WP write not exercised — no `wp-boards.env` on this host (the
  silent-skip path is covered by test). First real render_job post
  lands on the next billed render post-deploy; watch that the plugin
  accepts `meta.status='pending'`.
