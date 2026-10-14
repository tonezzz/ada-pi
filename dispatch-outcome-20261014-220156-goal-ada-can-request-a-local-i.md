# Dispatch outcome — local image render (sd-server on tony-omen)

Status: done (litellm stanza staged, needs applying on idc03 — see below).

## What changed (worktree, committed on the dispatch branch)

- `backend/tools.d/ada_render_image.py` — new `provider` param
  (`gemini` default | `local`) + `job` resume param. Local submits to
  sd-server's native async sdcpp API (`POST /sdcpp/v1/img_gen` → job id
  + queue_position), foreground-polls ≤ `ADA_RENDER_LOCAL_WAIT_S` (45s),
  then returns `pending` + arms a background task that publishes the
  PNG via `traffic_camera.publish_relay` and pushes it to the
  originating chat through `runner.chat_send` — the card's
  "rendering, delivered via chat_send when done" contract. `job=`
  re-attaches after a restart (mirrors ada_render_video `operation=`);
  a module-level set dedupes armed delivery tasks. ref_url edits are
  refused honestly (local is text-to-image only); size>1K renders at
  1K with a note.
- `backend/tool_guide.yml` — provider=local contract documented
  (pending → job= → status-check-only, never resubmit).
- `.env.example` — `ADA_SD_SERVER_URL` (default
  http://100.75.102.88:8399) + `ADA_RENDER_LOCAL_{WAIT,POLL,DEADLINE}_S`.
- `scripts/sd-server.service` — systemd user unit (tailnet bind
  100.75.102.88:8399, `-m` autoload, `--threads 8`, Nice=10,
  Restart=on-failure).
- `tests/test_render_image.py` — +11 tests → 22/22 pass.
- `tests/scenarios-live/img_render_local.yaml` — manual tier
  (10min CPU render, not for hourly smoke).
- `docs/ssot/jobs/ada/2026-10-14-local-image-render.yml` — runbook,
  decisions, LiteLLM stanza, followups.

## Done on tony-omen (this host)

- `sd-server.service` installed to `~/.config/systemd/user/`, enabled +
  active; linger already on for tony. `GET /v1/models` → `sd-cpp-local`,
  capabilities show `img_gen`. Verified live after unit start.
- Probe earlier: `sd-server` loads the GGUF standalone via `-m`;
  512px/4-step job completed in 130s; job result carries
  `result.images[].b64_json` (extractor handles it).
- Real tool-path smoke: `run(provider='local')` → submitted
  `job_6acf9b4a_00000000`, returned the pending contract in ~4s.

## Decisions worth knowing

- **Bind = tailnet IP, not the card's 127.0.0.1**: LiteLLM lives on
  idc03 — a loopback bind would be unreachable. `100.75.102.88:8399`
  keeps it tailnet-only (never LAN); on-omen callers hairpin it.
- **Tool → sd-server direct, not via LiteLLM**: the OpenAI
  `/v1/images/generations` surface is synchronous — a ~10min render
  would blow the 90s tool timeout and LiteLLM's budget. The sdcpp async
  API gives queue_position + job states for the pending/deliver
  contract. The LiteLLM `local_image` stanza (in the job yml) is for
  synchronous consumers, `timeout: 900`.
- SDXL-Lightning params used: steps=4, txt_cfg=1.0, euler_a, seed=-1.

## Still open

- Apply the `local_image` model_list stanza in chaba
  `stacks/idc03/litellm/config.yaml` + reload the gateway (posted on
  the card; repo is outside this worktree).
- Optional: GTX1650 partial-offload benchmark when Nest is idle.
- sd-server restarts drop the in-memory job queue (job= then 404s →
  honest resubmit message); armed delivery tasks don't survive an ada
  backend restart (followup: persist armed ids under ADA_RENDER_DIR).

## Verify

```
systemctl --user status sd-server.service            # on tony-omen
curl -s http://100.75.102.88:8399/v1/models          # sd-cpp-local
python3 -m pytest tests/test_render_image.py -x -q   # 22/22
python3 scripts/tool-lint.py                         # clean
```
