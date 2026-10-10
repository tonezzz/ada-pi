# Dispatch outcome — chat-send-photos-picker

**Card:** `chat-send-photos-picker` — "Either configure Google Photos auth
(run gphoto-auth.py, set GPHOTO_REFRESH_TOKEN) or make chat_send degrade
honestly — 'photo sending is not configured' instead of an opaque 501."

## Which path and why

**Honest degradation** — chosen. Configuring the auth for real requires
an interactive OAuth consent flow (gphoto-auth.py opens a browser for
the operator's Google sign-in) plus writing `GPHOTO_REFRESH_TOKEN` into
the doc-archive service's env on idc03 — outside this worktree and not
doable unattended. The code fix leaves a clean seam: once the operator
runs gphoto-auth.py and sets the token, the 501 simply stops and the
picker works with no further changes here.

## What changed

- `backend/doc_archive_client.py` — new `PhotosNotConfiguredError`
  (subclass of RuntimeError). `photos_picker_create()` and
  `photos_picker_poll()` now translate the service's documented HTTP 501
  (GPHOTO_REFRESH_TOKEN missing server-side) into that error with the
  message *"photo sending is not configured — Google Photos auth is
  missing on the doc-archive service (run gphoto-auth.py there and set
  GPHOTO_REFRESH_TOKEN)"*. Poll's 404 now says *"unknown or expired
  photos picker session … — start a new picker"*; its generic branch
  gained the response body like create's.
- `backend/tool_guide.yml` — `chat_send` entry: a photo= 'not
  configured' result means server-side auth is missing — tell the user
  photo picking isn't set up yet (needs the operator), don't retry.
  The hint auto-attaches to failed results via `_usage_hint`.
- `docs/ssot/jobs/ada/2026-10-10-photos-picker-not-configured.yml` —
  job trail per repo convention.

## Effect

`chat_send photo='pick'`, `photo='picked'`, and the `photos_pick` /
`photos_picked` forward aliases now return
`{ok: false, error: "photo sending is not configured — …",
error_type: "PhotosNotConfiguredError"}` instead of
`"photos picker failed: HTTP 501 …"`. The named class also lands
verbatim as the storm-breaker error class, so repeated photo= attempts
while unconfigured trip the per-session circuit (observed in test logs:
`storm breaker OPEN tool=chat_send … PhotosNotConfiguredError`) instead
of retry-storming like the journal showed.

## Tests

- `tests/test_doc_archive_tool.py` — new `PhotosPickerErrorTest`:
  501→PhotosNotConfiguredError (create + poll), 404→expired-session,
  other codes stay generic.
- `tests/test_tool_runner.py` — new
  `test_chat_send_photo_unconfigured_degrades_honestly`: both photo=
  actions surface `ok:false` + `PhotosNotConfiguredError` + "not
  configured", and no channel send is queued.

## Verify

- `python3 -m unittest tests.test_doc_archive_tool` — 15 pass.
- `ADA_INSTANCE_ID=test python3 -m unittest tests.test_tool_runner` —
  256 pass (env var needed under unittest; conftest.py supplies it
  under pytest — the real gate). Run in a venv with
  backend/requirements.txt deps (httpx, pyyaml, google-genai, numpy,
  pillow suffice for these files).
- `scripts/tool-lint.py` — 3 violations, all **pre-existing** on this
  base (voice_fx descsize + coverage, yt_cached_list impl); verified
  identical with the change stashed. Unrelated to this job.
- Live check after deploy: ask Ada to pick photos with the token still
  unset → she should say photo picking isn't set up, not error out.

## Notes

- Board comment POST to board-api timed out repeatedly (endpoint hangs
  on POST from here; GET 404s). Status posted to outcome instead.
- Not committed, not pushed, not deployed — per dispatch rules.
