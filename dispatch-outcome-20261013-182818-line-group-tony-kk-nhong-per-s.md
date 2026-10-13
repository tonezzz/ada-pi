# Dispatch outcome — line-multihome-group (attempt 4)

Task: LINE group (tony+kk+nhong) — mention-triggered per-sender routing
to each home assistant via the one chaba bot (@339sbrov).

## State on entry

Attempts 2+3 were already merged into this tree (2aa8ed4 + merge
cc7431e): mention parse, `LINE_NAME_BACKENDS`, `LINE_ALLOWED_GROUP_IDS`,
per-member `LINE_USER_CALLERS` identity, `[persona->sender]` tags, voice
corpus + DM mirror, the standard doc, and 21 tests. This session
reviewed the merged code against the card's remaining items and found
two real gaps — one auth bypass, one missing primitive for the
"user-nhong key mint on each backend" follow-up.

## What changed

`backend/line_relay.py` — **room persona pin is now member-only.**
`_route` wrote `pinned[room]` on any group mention, but `handle_event`
ran it *before* the caller check — an unmapped member's mention pinned
the room, rerouting every member's subsequent unaddressed follow-ups to
their pick (a bypass of the noise rule the card exists to enforce).
Unmapped senders now get `pinned=None`: their mentions still earn the
rate-limited refusal, but can't pin; their chatter can't ride someone
else's pin either. Mapped members pin/switch unchanged. The pin feature
(commit 24d6e3d) previously had zero tests.

`backend/auth.py` — **`import_key(name, key, apps, ha_person, person)`.**
The card's "mint the member key on each persona backend" was impossible
as documented: `create_key` mints a fresh random value per call while
the relay resolves ONE string from its `ADA_KEYS_FILE` and presents it
to every `LINE_NAME_BACKENDS` backend — per-backend mints would each be
a different string the relay can't all satisfy. `import_key` enrols an
existing value live (under `_KEYS_LOCK`, event-logged) with the target
backend's own `ha_person`/`apps` — per-backend tier. Refuses a taken
name or a value already registered under another name (identity must
stay unambiguous in `_parse_keys` key→name).

`docs/kb/msg-lane-multihome-routing.md` — documented the room pin +
its member-only gate under "The rule"; rewrote the identity section to
the correct one-key-value model with a `create_key` → `import_key`
runbook (the old text implied per-backend mints, which cannot work).

`docs/ssot/jobs/ada/2026-10-11-line-group-mention-routing.yml` —
hardening-2 section + corrected key-mint follow-up. Quoted the new
followup line (unquoted `name:` colons inside `- ` items are the
invalid-YAML trap attempt 3 warned about — re-verified with yaml.safe_load).

## Tests

- `tests/test_line_relay_group.py` 21 → **24**: member mention pins the
  room + follow-up reuses the pinned session, a new mention switches the
  pin, unmapped member cannot pin.
- `tests/test_auth.py` +2: `import_key` enrols the same value with
  per-backend `ha_person`/`apps` and resolves via `websocket_caller`;
  refuses taken names, already-registered values, and bad shapes.
- `python3 -m unittest tests.test_line_relay_group tests.test_auth` —
  **54/54 pass**. Full `unittest discover -s tests`: 880 run, 48 errors
  all pre-existing env-dep gaps (google-genai version skew /
  `LiveServerMessage`, missing `ai_edge_litert`/hailo/mddb) — none in
  touched files, same class attempt 3 recorded.
- `scripts/tool-lint.py` clean (61 pre-existing coverage warnings, none
  on changed files).

## Remaining ops (outside worktree — idc03, not deployable from here)

- `ada-line-relay` unit env: `LINE_ALLOWED_GROUP_IDS` (groupId prints in
  the join greeting when the bot is added), `LINE_NAME_BACKENDS=
  ada:ws://127.0.0.1:8002,kaewta:ws://127.0.0.1:8007,chaba:`,
  `LINE_USER_CALLERS` entry for nhong, optional voice envs.
- `user-nhong` key: mint once under the relay's env (`create_key`), then
  `auth.import_key` the SAME value into each reachable persona backend
  with per-backend `ha_person`/`apps` — exact commands are in the
  routing doc. Tiers per card answers: kk guest of ada-ha / member of
  tony-ha; tony member of kk-ha.
- `chaba:` stays empty (isn't-home reply) until ada-chaba :8008 is
  provisioned — `chaba-backend` card.

lessons:
- Auth checks that run AFTER routing-state writes are leaks: the room
  pin was written by senders who then got refused. Order side-effects
  behind the gate, or scope the shared state out of the unauthed call.
- "Mint the same key on N backends" needs an import primitive — random
  mints never converge. When a relay holds the canonical registry,
  enrolment copies values outward, not names.
- Follow-up sessions in a pinned room reuse the FIRST mention's session
  (skey = group×sender×persona) — assert on send_turn of the existing
  session, not on new session creation.
