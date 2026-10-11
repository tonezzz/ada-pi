# Dispatch outcome — line-multihome-group (attempt 3)

Task: LINE group (tony+kk+nhong) — mention-triggered per-sender routing
to each home assistant via the one chaba bot (@339sbrov).

## State on entry

Attempt 2's work was already checkpoint-committed into this tree
(2aa8ed4): mention parse, `LINE_NAME_BACKENDS`, `LINE_ALLOWED_GROUP_IDS`,
per-member `LINE_USER_CALLERS` identity with no room admin fallback,
`[persona->sender]` reply tags, voice corpus + DM mirror, the standard
doc `docs/kb/msg-lane-multihome-routing.md`, and 18 tests. This session
reviewed that diff, found four edge cases, and hardened them.

## What changed this session

`backend/line_relay.py`:

- **`LINE_DEFAULT_PERSONA` typo crash** — `_route` resolved a default
  persona that wasn't in `LINE_NAME_BACKENDS`, then
  `_name_backends()[persona]` KeyError'd and killed the `handle_event`
  task (affects text, image, and voice-transcript paths). `_route` now
  validates the resolved name against the map: an unknown default logs
  and resolves to ignore. Group images route through `_route` too, so
  the same guard applies; both call sites use `.get()`.
- **Room-type sources hit the wrong LINE profile endpoint** —
  `_sender_label` always called `/v2/bot/group/{id}/member/{uid}`;
  `room` sources 404 there (their endpoint is `/room/`). It now picks
  the path segment from the event's source type.
- **Join greeting spent a push message** — `join` events carry a
  replyToken; the greeting now uses the free reply path.
- `asyncio.get_event_loop()` → `get_running_loop()` in the transcribe
  executor path.

`docs/kb/msg-lane-multihome-routing.md` — notes that
`LINE_DEFAULT_PERSONA` must name a configured persona and that `room`
sources use the `/room/` member endpoint.

`docs/ssot/jobs/ada/2026-10-11-line-group-mention-routing.yml` —
hardening section added; also **fixed the file's invalid YAML**
(`tests: backend…`-style colons inside sequence items — broken since
attempt 2; three other job ymls in the dir have the same pre-existing
issue, untouched).

`tests/test_line_relay_group.py` — 18 → **21 tests**: unknown-default
route ignore + no-crash event test, room-source `/room/` endpoint +
displayName label, join greeting replyToken assertion.

## Verification

- `unittest tests.test_line_relay_group` — **21/21 pass** (no pytest in
  the local venv; the suite is unittest-based so discovery runs fine).
- Full discovery `unittest discover tests` — 1276 run, 5 errors all
  pre-existing env-dep gaps (`pytest`, `ai_edge_litert`, hailo missing
  from this venv) — **identical on the clean tree** (verified via
  stash). `scripts/tool-lint.py` clean (60 pre-existing warnings).

## Remaining ops (outside worktree — idc03, not deployable from here)

- `ada-line-relay` unit env: add `LINE_ALLOWED_GROUP_IDS` (group id
  prints in the join greeting/log when the bot is added),
  `LINE_NAME_BACKENDS=ada:ws://127.0.0.1:8002,kaewta:ws://127.0.0.1:8007,chaba:`,
  `LINE_USER_CALLERS` entry for nhong, optional voice envs
  (`LINE_VOICE_CORPUS_DIR`, `LINE_VOICE_TRANSCRIBE`).
- `user-nhong` key must be minted on each reachable persona backend
  (`auth.create_key` / `ada_member_keys` tool / keys card) — scoped to
  her tier per backend (card answers: kk is guest of ada-ha / member of
  tony-ha; tony is member of kk-ha — apply the same model to nhong).
- `chaba:` stays empty (isn't-home reply) until ada-chaba :8008 is
  provisioned — see the `chaba-backend` card.

lessons:
- ssot job ymls are human-written and several ship invalid YAML —
  `key: value` colons inside `- ` sequence items. If a check ever
  loads them, quote the scalars or use `|` blocks.
- `python -m unittest discover -s tests` is a workable fallback when
  the local venv lacks pytest; expect only env-dep import errors.
