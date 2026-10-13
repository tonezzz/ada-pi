# Multi-home message-lane routing — standard

One shared chat surface (a LINE group) where several household members
talk to several persona backends. Card: `line-multihome-group`
(2026-10-11). Implementation: `backend/line_relay.py`; the Telegram
sibling follows the same member-identity rule in `backend/tg_relay.py`.

## The rule

A message lane carries a message only when it is **addressed**.

- **Mention-routed.** A member addresses a persona by name anywhere in
  the message — `ada, ...`, `@kaewta ...`, `tell chaba hi` (word-bounded,
  case-insensitive, optional `@`). The first configured persona name in
  the text wins; a leading `name,` / `@name:` is stripped as addressing
  noise before the turn ships.
- **Unaddressed room chatter never reaches a persona.** This is the
  noise rule: without it every member message would hit the default
  backend. Escape hatch: `LINE_DEFAULT_PERSONA` names one catch-all
  persona for unaddressed messages — off by default, and it must name
  a persona in `LINE_NAME_BACKENDS` (an unknown name is logged and
  treated as unset, never a crash).
- **Room pin (interview-room rule).** A mapped member's mention pins
  the room to that persona — unaddressed follow-ups keep routing there
  until another name is mentioned (switch). Only mapped members
  consult or set the pin: an unmapped member's mention must never
  reroute everyone's follow-ups, and a stranger's chatter must not
  ride someone else's pin into a refusal.
- **DMs are unchanged.** A 1:1 chat has no addressing problem — it
  defaults to `ada` (ADA_WS_URL), and an explicit mention still
  re-routes.

## Persona backends — `LINE_NAME_BACKENDS`

```
LINE_NAME_BACKENDS="ada:ws://127.0.0.1:8002,kaewta:ws://127.0.0.1:8007,chaba:"
```

- `name:ws-base` — same convention as `ADA_WS_URL` (include the `/ws`
  path, e.g. `ws://127.0.0.1:8002/ws`); the relay connects with
  `?api_key=...&channel=line` appended.
- An entry with an **empty URL** (`chaba:`) is a known-but-offline
  persona: mentioning it answers `[chaba->you] chaba isn't home yet`
  instead of silently ignoring the name.
- `ada` falls back to `ADA_WS_URL` when unlisted, so a minimal config is
  `LINE_NAME_BACKENDS="kaewta:ws://127.0.0.1:8007"`.

Current persona map (2026-10-11): `ada` → ada-ha-tony :8002,
`kaewta` → ada-kk :8007, `chaba` → ada-chaba :8008 (decided via card
request `chaba-backend`; until ada-chaba is provisioned keep `chaba:`
empty so mentions get the isn't-home reply instead of a dead socket).

## Replies — `[persona->sender]` tag

Room replies are tagged `[persona->sender]` so members can tell who a
line answers and which persona spoke. `sender` is the member's LINE
group displayName when the API will share it (`/v2/bot/group/…` for
`group` sources, `/v2/bot/room/…` for `room` sources), else their
caller name minus `user-`, else the userId tail.

## Identity — `LINE_USER_CALLERS`

```
LINE_USER_CALLERS="U<tony>:user-tony,U<kk>:user-kk,U<nhong>:user-nhong"
```

- In a room the sender's userId resolves a caller key name; the issued
  key comes from `ADA_KEYS_FILE` (`~/.config/secrets/ada-ha-*-keys.json`).
- **No admin fallback in a room** — an unmapped member must never
  inherit the owner key. A routed message from an unmapped member gets a
  rate-limited (1/h) refusal that echoes their userId for whitelisting.
- Sessions are keyed `(group, sender, persona)` — each member×persona
  pair is its own WS session with its own key, so owner_identity stays
  clean per person on each backend.
- **One key value, every backend.** The relay reads ONE keys file and
  presents the same key string to whichever persona backend the mention
  routes to — so a member's key must authenticate the *same value* on
  each reachable backend. `create_key` can't do this alone (it mints a
  fresh random value per call); enrol the value on the other backends
  with `auth.import_key`, which carries that backend's own metadata:

  ```
  # mint once, under the relay's env (ADA_KEYS_FILE = ada-ha-tony's
  # keys file on idc03)
  python3 -c "from backend import auth; \
      print(auth.create_key('user-nhong', ha_person='person.nhong'))"
  # -> ada-XXXX...  then on EACH other reachable backend (ada-kk, ...),
  # under THAT backend's env so ADA_KEYS_FILE resolves to its own file:
  python3 -c "from backend import auth; \
      assert auth.import_key('user-nhong', 'ada-XXXX...', \
          ha_person='person.nhong', apps=['chat'])"
  ```

  `import_key` refuses a taken name or a value already registered under
  another name; the per-backend `ha_person`/`apps` are what scope the
  member's tier there (2026-10-11 card answers: kk is a guest of ada-ha
  but a member of tony-ha; tony is a member of kk-ha). Then add the
  `U…:user-<name>` pair to the relay unit env (`LINE_USER_CALLERS`).

## Room gating — `LINE_ALLOWED_GROUP_IDS`

Comma list of groupIds/roomIds the relay serves. Unknown rooms are
ignored (logged). When the bot is added to a room it posts its own
`groupId=` line once — that line is how the owner discovers the id to
allowlist.

## Voice notes

The voice rule from tg_relay, applied in-channel:

1. Clips from enrolled senders (mapped room members / allowed DM users)
   save to `LINE_VOICE_CORPUS_DIR/<caller>/<stamp>.<ext>` plus a
   `manifest.jsonl` line — local disk only, never forwarded.
2. Ack posts in-channel. With `LINE_VOICE_TRANSCRIBE=1` a faster-whisper
   transcript (`LINE_WHISPER_MODEL`, default `base`) echoes as
   `🎙 "…"` and then routes **like a text message** — mention, default
   persona, or ignore.
3. Group clips also mirror the ack/transcript to the speaker's home
   channel (their DM lane with the bot) so the speaker has a private
   copy — `LINE_VOICE_DM_MIRROR=0` disables.

## Ops checklist (idc03 `ada-line-relay` unit)

- `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET` — chaba bot
  (@339sbrov) channel.
- `LINE_ALLOWED_USER_IDS`, `LINE_USER_CALLERS` — DMs + member identity.
- `LINE_ALLOWED_GROUP_IDS` — add the tony+kk+nhong group id (the bot's
  join greeting prints it).
- `LINE_NAME_BACKENDS` — persona → ws map (see above).
- `LINE_PUBLIC_IMG_BASE` — public edge for relay-hosted images; repo
  default tracks the idc03 edge (`157.85.102.125.sslip.io/relay-img`).
- `LINE_DEFAULT_PERSONA` — leave unset (spec: ignore unaddressed).
- Webhook: LINE console → Messaging API → webhook URL
  `https://157.85.102.125.sslip.io/webhook/line` (repointed idc01→idc03
  2026-10-11); Caddy proxies `/webhook/line` to `LINE_WEBHOOK_LISTEN`.
