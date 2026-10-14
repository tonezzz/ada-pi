# dispatch-outcome — webchat lane on boards WP (ADA-0655)

## What changed

The boards chat screen ships as a bespoke widget on the existing lane
backend — no new service, no plugin.

- **`pwa/cards/ada-webchat-card.js`** (new) — LINE-style chat panel
  (`<ada-webchat-card lane member>`), Shadow DOM, no build. Setup gate
  redeems a pasted `/i/<token>` invite link against the lane (or takes a
  raw key / `?key=`), stores key+device id in boards-origin localStorage,
  then opens `wss://lane/ws?api_key=…&channel=web&chat_id=web-<member>
  &mode=text&device_id=…`. Streams `assistant_transcript_delta` into a
  growing bubble, renders tool-result media (`image_url`/`video_url`)
  inline, dims `channel_activity` lines, shows typing/tool-call
  indicators, auto-reconnects (4401/4403 → back to setup).
- **`pwa_server.py`** — `channel=web` ws sessions now require the
  `chat` key capability (view-only iframe keys get 4403);
  `webchat_deliver()` pushes `chat_push` frames to every live session
  bound to the chat and parks offline pushes in `_WEBCHAT_PARKED`
  (deque, 20/chat, flushed at connect — OPS-0651 pattern, in-memory);
  `POST /api/webchat/send` is the key-gated relay-style loopback;
  `ADA_WEBCHAT_ORIGINS` env gates a scoped CORSMiddleware for the one
  cross-origin fetch (invite redeem — ws needs no CORS).
- **`backend/tool_runner/chat.py`** — `chat_send channel=web` routes to
  `_chat_send_web`: in-process `webchat_deliver` first, loopback
  `ADA_WEBCHAT_SEND_URL` POST when off-process; `to=` defaults to the
  bound session chat like the relays.
- **`backend/realtime_provider.py`** — chat_send `channel`/`to` docs now
  cover `web` / `web-<member>`.
- **`backend/wp_client.py`** — `find_page`/`ensure_page` (slug-keyed
  create-or-update, parent + private status).
- **`backend/tools.d/wp_chat_page.py`** + manifest — mints/updates the
  private page `/chat/web-<member>` embedding the card (parent `chat`
  auto-ensured). Returns the URL for chat.
- **Docs** — `ssot.channels.yml` gained the `web:` channel entry;
  `docs/ssot/jobs/ada/2026-10-14-webchat-lane-boards.yml` is the trail;
  `.env.example` documents `ADA_WEBCHAT_ORIGINS`/`ADA_WEBCHAT_SEND_URL`.

## Verification

- `tests/test_webchat.py` — 13 tests (deliver/park/flush, endpoint
  validation, chat_send routing + in-context default + explicit to=,
  in-process + loopback paths) — all pass.
- `python3 scripts/tool-lint.py` — clean; `wp_chat_page` covered by
  `tests/scenarios-live/wp_chat_page.yaml`.
- `python3 -m pytest tests/ -x -q` — 1427 passed, 7 skipped.
- `node --check` on the widget — clean.
- Commit `b6acbed` on `dispatch/20261014-220023-…`.

## To go live (deploy steps, not done here)

1. Set `ADA_WEBCHAT_ORIGINS=https://boards.surf-thailand.com` on the
   member lane(s) and deploy.
2. Run `wp_chat_page member=<m> lane=https://ada-ha.surf-thailand.com`
   (or ask Ada) → private `/chat/web-<m>` page.
3. Send each member their invite link; they paste it into the page once.
4. Push test: `curl -X POST <lane>/api/webchat/send -H 'x-api-key: …'
   -d '{"chat_id":"web-<m>","text":"hi"}'` (parks if page closed).

## lessons:

- tool-lint descsize gate: tools.d DECLARATION descriptions must be
  ≤4 lines AND ≤300 chars; long prose belongs in tool_guide.yml.
- New tools can't join coverage_debt — a tests/scenarios-live/*.yaml
  referencing the tool (calls_any) is required or lint fails.
- scenario engine has no mock facility; validation-only steps (arg
  guards before env/cred checks) are the safe coverage for
  credentialed tools.
- `patch.dict(sys.modules, {"pwa_server": None})` is the clean way to
  force the ImportError fallback path in _chat_send_web tests.
