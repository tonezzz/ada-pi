# Dispatch outcome — linepush-quota-audit (2026-10-17)

## What was found (census)

`journalctl --user -u ada-line-relay` on idc03 retains ~12 days
(Oct 5 21:14 → Oct 17 15:58), not 30. Within that window, by counting
`POST /v2/bot/message/push|reply` HTTP lines paired with the preceding
`deliver to <chat>` log:

- push calls: **314 OK / 21 HTTP-400 / 81 HTTP-429**; reply calls: 505 OK
  (replies are free — never consume quota)
- push-200 by recipient: tony DM `U7ef…3d90` 161 · nhong DM `Uc0b…534c`
  80 · kk group `C822…55c5` 39 · kk DM `U749…0478` 34
- sources: **chaba-nag** (timer `*:0/30`, 120-min resend) is the standing
  spender — '⏰ CHABA N pending' pushes to tony, copies parked for
  nhong+kk too; **ada_track_device** armed watch (iphone-15) pushes
  `[delta …]` every ~5-min beacon (nhong ~20 pushes in 4h Oct 16, 15 sat
  parked); **gemini-wallet-watch** hourly :47; **chat_send** irregular
  user-requested; **line-digest** weekly per-member. 21 pushes went to
  unresolvable names (`person.tony`, `owner`, `both`, placeholder U-ids).
- burn ≈35 push calls/day healthy vs a 300/month cap (~10/day
  sustainable). Oct 12 alone spent 112 calls. Cap hit **Oct 14 12:24**
  ICT; first-class 429 since.
- plan facts (live `api.line.me/v2/bot/message/quota`): bot **ชบา
  @339sbrov**, `{type:limited, value:300}`, `totalUsage:300`. Quota is
  per-message (a 5-bubble push costs 5) and resets on the **1st of the
  month** — starved until Nov 1 ICT.

## What changed (worktree, branch `dispatch/20261017-162335-…`, not pushed)

- `backend/line_relay.py` — `/send` accepts `class=`/`priority=`
  `urgent`|`bulk` (`LINE_BULK_LANES` force-list; `_push_class` helper).
  Bulk payloads park durably, then a per-chat debounce task flushes them
  as ONE bundled push after `LINE_BULK_COALESCE_S` (300s default) —
  adjacent texts merge so N caller msgs can cost 1 quota message — or
  ride the next inbound replyToken free (existing OPS-0651 ride). A
  confirmed 429 'monthly limit' sets `_quota_blocked` for
  `LINE_QUOTA_RETRY_S` (6h): bulk skips the API entirely; urgent always
  attempts and a 200 clears the block (`quota_recovered` event).
  Rate-limit 429s don't set the block. `_quota_probe()` reads LINE's
  free quota endpoints at boot (logs used/limit/remaining, seeds the
  block when spent) and on depletion (`quota_depleted` ops event — the
  non-push alert path). `X-Line-Request-Id` logged on failures. New
  `GET /quota` loopback route → `{quota, quota_blocked,
  quota_blocked_until, parked}`. Source-attribution log
  `send: inst=.. to=.. class=.. msgs=..` per /send.
- `backend/tools.d/ada_track_device.py` — watch deltas send
  `class: bulk`.
- `.env.example` — new LINE_* knobs documented.
- `tests/test_line_relay_quota.py` — 14 tests.
- `docs/ssot/jobs/ada/2026-10-17-line-push-quota-policy.yml` — plan-tier
  facts + census + caller-adoption checklist.

## Verify

- `pytest tests/test_line_relay_quota.py test_line_relay_parked.py
  test_line_relay_group.py test_line_reply_context.py` — 56 passed
- `pytest tests/ -x -q` — 1576 passed, 7 skipped (sibling-worktree venv)
- `python3 scripts/tool-lint.py` — clean
- No reply-path regression: replyToken flow untouched; bulk feeds the
  same parked queue OPS-0651 already flushes on reply.
- Post-deploy check on idc03: `curl -s 127.0.0.1:8912/quota` shows
  used/limit/parked; `journalctl -u ada-line-relay | grep 'send:'`
  gives per-source counts for the next census.

## Still open (Tony's call / other repos)

- chaba-nag.py, gemini-wallet-watch.py, line-digest.py live in
  chaba-tony-dell/chaba checkouts — they should add `"class":"bulk"` to
  their /send payloads (or set `LINE_BULK_LANES` on idc03) to actually
  demote; until then everything is urgent (unchanged behavior).
- Plan upgrade is a billing decision: even with bulk classing, ~10
  msgs/day headroom is thin for per-member digests on a 300/mo cap.
