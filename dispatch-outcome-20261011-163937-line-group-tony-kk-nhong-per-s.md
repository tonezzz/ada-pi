# Dispatch outcome — line-multihome-group (attempt 2)

Task: LINE group (tony+kk+nhong) — mention-triggered per-sender routing
to each home assistant via the one chaba bot (@339sbrov).

## What changed

`backend/line_relay.py` — group-room routing implemented:

- `LINE_ALLOWED_GROUP_IDS` — room allowlist; unknown rooms ignored
  (logged). `join` events post a greeting carrying the `groupId` for
  allowlisting.
- `LINE_NAME_BACKENDS` — `name:ws-url` persona map. `ada` falls back to
  `ADA_WS_URL`; bare `ws://host:port` gets `/ws` appended; an empty URL
  (`chaba:`) is a known-but-offline persona → "isn't home yet" reply
  (covers the open `chaba-backend` card request's defer option).
- Mention parse — first configured persona name at a word boundary
  (case-insensitive, optional `@`); a leading `name,`/`@name:` is
  stripped as addressing noise; first match in the text wins.
- Unaddressed room chatter is ignored (noise rule) unless
  `LINE_DEFAULT_PERSONA` names a catch-all. DMs keep today's behavior —
  default `ada`, no tag, explicit mentions still re-route.
- Identity — room senders resolve via `LINE_USER_CALLERS` → issued key
  from `ADA_KEYS_FILE`; **no admin fallback in a room** (unmapped member
  gets a 1/h refusal echoing their userId). Sessions key on
  `(group, sender, persona)`.
- Replies tagged `[persona->sender]`; sender label = group profile
  displayName (cached) → caller minus `user-` → userId tail.
- Voice — audio messages save to `LINE_VOICE_CORPUS_DIR` (per-sender
  slug + `manifest.jsonl`, tg voice rule), ack in-channel; with
  `LINE_VOICE_TRANSCRIBE=1` the faster-whisper transcript routes like
  text. Group clips mirror the ack/transcript to the speaker's DM
  (`LINE_VOICE_DM_MIRROR`, default on).

Latent bugs fixed: `_mark_read` was passed the replyToken as a chat
userId (would 400) — now posts the event's `markAsReadToken` per current
LINE docs; `LINE_PUBLIC_IMG_BASE` repo default updated idc01 → idc03
edge (`157.85.102.125.sslip.io/relay-img`, matching the live unit env).

## Docs

- `docs/kb/msg-lane-multihome-routing.md` — the standard (rule, persona
  map, identity, voice, ops checklist).
- `docs/ssot/apps/ssot.apps.ada-persons.yml` — persona/backend registry.
- `docs/ssot/jobs/ada/2026-10-11-line-group-mention-routing.yml` — job
  trail.
- `.env.example` — LINE relay env block.

## Verification

- `tests/test_line_relay_group.py` — 18 new tests covering parse,
  routing, tags, ignore/refusal/offline paths, DM parity, voice
  save/ack/mirror. All pass.
- Full suite via repo venv: `pytest tests/ -x -q` → **1290 passed,
  1 skipped**. `scripts/tool-lint.py` → clean (60 pre-existing
  warnings).

## Remaining ops (outside worktree — deploy/host changes)

- `ada-line-relay` unit env on idc03: add `LINE_ALLOWED_GROUP_IDS` (the
  group id prints in the join greeting/log once the bot is added),
  `LINE_NAME_BACKENDS=ada:ws://127.0.0.1:8002,kaewta:ws://127.0.0.1:8007,chaba:`,
  `LINE_USER_CALLERS` entry for nhong, optional voice envs.
- `user-nhong` key must be minted on each reachable persona backend
  before nhong's mentions route — scoped per-backend tier (card answers
  mid-run: kk = guest of ada-ha / member of tony-ha; tony = member of
  kk-ha).
- `chaba` backend resolved mid-run via board answer: dedicated
  **ada-chaba on idc03 :8008** — keep `chaba:` empty in
  `LINE_NAME_BACKENDS` until provisioned (isn't-home reply beats a dead
  socket).
- Group `room`-type sources reuse the group profile endpoint for the
  sender label (falls back to caller name if LINE 404s).

lessons:
- LINE markAsRead wants the event's `markAsReadToken`, not a chat ref —
  passing replyToken-as-userId silently 400s.
- Persona ws URLs in env get written without `/ws` — normalize bare
  host:port in code or configs break silently.
- `pytest` needs the base checkout's `.venv` (system python3 lacks
  google-genai); run `/home/tony/CascadeProjects/ada-pi/.venv/bin/python -m pytest`.
