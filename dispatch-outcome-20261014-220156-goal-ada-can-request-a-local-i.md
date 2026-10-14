# Dispatch outcome — local image render (sd-server on tony-omen)

Status: in progress — see end for final state.

## What changed (worktree)

- `backend/tools.d/ada_render_image.py` — new `provider` param
  (`gemini` default | `local`) + `job` resume param. Local path submits
  to sd-server's native async sdcpp API (`POST /sdcpp/v1/img_gen` →
  job id + queue_position), foreground-polls ≤45s, then returns
  `pending` + arms a background task that publishes the PNG via
  `traffic_camera.publish_relay` and pushes it to the originating chat
  through `chat_send` — the "rendering, delivered when done" contract.
- `backend/tool_guide.yml` — provider=local contract documented.
- `.env.example` — `ADA_SD_SERVER_URL` (default
  http://100.75.102.88:8399, omen tailnet bind), `ADA_RENDER_LOCAL_*`
  knobs.
- `scripts/sd-server.service` — systemd user unit for omen.
- `tests/test_render_image.py` — 11 new tests (22/22 pass);
  `tests/scenarios-live/img_render_local.yaml` (manual tier).
- `docs/ssot/jobs/ada/2026-10-14-local-image-render.yml` — runbook +
  LiteLLM `local_image` stanza for the idc03 gateway (pending — lives
  in the chaba repo, outside this worktree).

## Verified on tony-omen

- sd-server loads `~/models/sd/sdxl-lightning-4step.q4_0.gguf`
  standalone via `-m` (CLIP+UNet+VAE embedded).
- Async job API live: 512px/4-step render completed in 130s;
  `result.images[].b64_json` shape confirmed.
