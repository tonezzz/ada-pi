# Dispatch outcome — render-flow-quality-pass (2026-10-14)

## Result: done

All six render-flow items landed inside `backend/tools.d/ada_render_image.py`
(no runner/provider changes; additive params + result fields only):

1. **Iterate-on-last** — `_LAST_RENDER` OrderedDict keyed by
   `runner.context.session_id` stores the last render's render-dir *path*
   (not bytes). A call with no ref args auto-feeds it as the edit base, so
   "make it warmer" preserves composition; `fresh=true` is the model's
   escape hatch for unrelated asks. TTL `ADA_RENDER_LAST_TTL_S` (default
   4h), 16-session cap. Result flags `iterated`/`iterated_from`.
2. **Vision self-check** — `_self_check` sends the render (shrunk to
   <=800px JPEG) to `ADA_RENDER_CHECK_MODEL` (default
   `gemini-3.5-flash-lite`) through `gemini_pool` with a JSON
   `{match, problem}` schema. An obvious miss re-renders once with the
   problem folded into the prompt; the retried image is delivered even if
   still flagged (`self_check` in the result tells the model to say it may
   be off). Advisory — any check failure just skips it. `ADA_RENDER_CHECK=0`
   disables.
3. **Multi-ref** — new `ref_urls[]` param (max 4, merged with `ref_url`),
   each entry resolved through the existing `_resolve_ref` shapes
   (doc key / /api/documents path / fetchable URL).
4. **Aspect intent** — explicit `aspect` always wins; else `WxH` pixel dims
   map to the nearest canvas; else page-ish wording (landing page, website,
   homepage, full page) defaults to 9:16 and attaches `page_hint` — "this
   draws a PICTURE of a page; for working HTML it's a code/CMS task."
5. **Delivery fidelity** — 2K/4K results add `full_res_url` (the published
   URL already serves original bytes; LINE re-encodes the bubble, not the
   source) + a note telling the model to paste the link. Depends on
   OPS-0651 (parked-queue delivery, aee7713) being sane, as the card noted.
6. **Transient retry** — `_render_retry` retries the image call once (1.5s)
   on transient failures: httpx/connection/timeout errors, 408/5xx,
   overload text. Quota-shaped and hard-4xx errors do NOT retry — the pool
   already rotated keys; a drained pool is spent, not transient.

Also touched: `manifest.yml` timeout_s 90→120 (self-check + possible
re-render headroom), `tool_guide.yml` ada_render_image entry documents the
new contract, `tests/test_render_image.py` +15 tests (25 total).

## Verification

- `.venv/bin/python -m pytest tests/test_render_image.py -q` — 25 passed
- Full suite `-x -q` (bench ignored; speaker_id/bench_speaker_id deselected —
  torch/speechbrain not installed on this host): **1397 passed, 7 skipped**
- `python3 scripts/tool-lint.py` — clean
- Worktree-local `.venv` (gitignored) created for the run: pytest, httpx,
  pyyaml, pillow, websockets, uvicorn, fastapi, numpy, qrcode; google-genai
  handled by tests/dep_stubs.

## Not verified live

No ada service/API keys on the dispatch host — the gemini_pool path, real
self-check calls, and relay delivery are mocked. On idc02 staging: "draw a
cat" then "make it warmer" in one session should show `iterated=true` and a
composition-preserving edit.

## Trail

`docs/ssot/jobs/ada/2026-10-14-render-flow-quality-pass.yml`
