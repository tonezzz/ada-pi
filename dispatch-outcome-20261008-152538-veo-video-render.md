# Dispatch outcome — veo-video-render

**Card:** veo-video-render · **Task:** 20261008-152538-ada-render-video-prompt-ref-ur
**Result:** implemented, unit-tested, lint-clean; publish hop verified live; live Veo happy path pending a billed key (tony-omen keys 401).

## What changed

**New tool** — `backend/tools.d/ada_render_video.py` + manifest entry
(`read`, `secondary_allowed: false`, `timeout_s: 220`). Same 'render'
tool group as `ada_render_image` (sibling dispatch
`20261006-111525-new-tool-group-render-ada-rend`, unmerged — both
changes merge cleanly).

Flow: `POST /v1beta/models/{model}:predictLongRunning`
(`veo-3.1-fast-generate-preview` default) → operation name → bounded
poll `GET /v1beta/{name}` every 6s up to ~120s →
`generateVideoResponse.generatedSamples[0].video.uri` → download with
`x-goog-api-key` (redirect-following) → save `render-vid-*.mp4` under
`ADA_RENDER_DIR` → `traffic_camera.publish_relay` mints
`{VCAST_PUBLIC_API}/frame?screen=0&token=render:…` — the same
artifact→URL hop as `ada_render_image`, literally shared code.

- `secs`: 4|6|8 (default 8); `aspect`: landscape 16:9 (default) |
  portrait 9:16 — Veo has no square, honest error.
- `ref_url`: image-to-video first frame; same three shapes as the
  image tool (`doc/...` intake key via `document_check` engine,
  `/api/documents/...` path, http(s) image URL).
- Long-poll shape: bounded wait per the card. On timeout →
  `ok:false + pending + operation`; re-calling with `operation=`
  resumes the same render without re-billing.
- Result exposes `video_url` (not `image_url`/`cast_url`/`url` — the
  tg/line relays auto-attach those fields and would upload an mp4 as a
  broken photo; relay video delivery isn't implemented).

**`backend/traffic_camera.py`** — `publish_relay(jpeg, slug, mime,
prefix)`: backwards-compatible generalization, identical signature to
the sibling render branch (clean merge).

**`pwa_server.py`** — `/api/tools` unions tools.d drop-ins (same fix
the sibling needed) so `needs_tools` gating sees them.

**Guidance/SSOT** — `tool_guide.yml` `ada_render_video` entry
(including the pending→operation resume contract); `ssot.tool-surface.yml`
`count_cap` 108 → 109; `.env.example` documents
`ADA_RENDER_VIDEO_MODEL`/`WAIT_S`/`POLL_S`, `ADA_RENDER_DIR`,
`ADA_SELF_URL`. Job trail: `docs/ssot/jobs/ada/2026-10-08-veo-video-render.yml`.

**Tests/scenario** — `tests/test_render_video.py` (22 tests, stubbed
submit/poll/download/publish). `tests/scenarios-live/video_render_gen.yaml`
— `tier: manual` (billed quota + 1–3 min renders; not hourly smoke),
`needs_tools`-gated, `calls_any` + `result_contains` on the
`input-bridge/frame?` URL shape.

## Verify

- `python3 -m pytest tests/test_render_video.py -x -q` → **22 passed**
- `python3 -m pytest tests/test_tools_loader.py tests/test_traffic_camera.py` → 36 passed (incl. render suite)
- `python3 scripts/tool-lint.py` → clean (43 declared ≤ cap 109, scenario-covered)
- **Live publish hop** (tony-omen, real input-bridge): `run()` with
  stubbed Veo calls minted a real
  `…/api/input-bridge/frame?screen=0&token=render:…` URL; GET returns
  200 with the mp4 bytes (`ftyp` intact).
- **Live Veo call NOT exercised**: both `GEMINI_API_KEY`s on tony-omen
  return HTTP 401 (expired keys — the image card hit 429s on the same
  box). The billed idc03 `ada-ha-*` key exercises the happy path:
  `python3 scripts/scenario-live.py tests/scenarios-live/video_render_gen.yaml --url ws://<host>:8003/ws --api-key $ADA_API_KEY -v`

## Notes / limitations

- **content-type caveat**: input-bridge `GET /frame` sniffs only
  PNG/WAV magic and serves the mp4 labelled `image/jpeg` — `<video>`
  elements sniff the container and play it; direct browser nav shows
  broken-image. Proper fix is chaba-side (`stacks/web/input-bridge`:
  store the declared data-URI mime or sniff `ftyp`) — out of this
  worktree's scope, flagged as a follow-up.
- **Relays can't deliver video yet** (tg `sendVideo` / LINE video
  message) — `video_url` is share/castable today; a relay card would
  make clips auto-push like images do.
- The sibling `ada_render_image` also adds
  `/api/documents/{key}/image` + `document_check.to_dict.image_url` —
  those stay its job; this branch needs none of it (doc-key resolution
  is in-process).
