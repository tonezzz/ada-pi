# Ada secrets hygiene — standard

How secret material moves between Tony and the fleet, how leaks are
detected, and what happens when one is found. Card: `ada-secret-drop`
(2026-10-09). Implementation lives in `pwa/cards/ada-chat-card.js`
(secret-drop modal), `pwa_server.py` (`POST /api/secret-drop`),
`scripts/secret-canary.py`, and `scripts/secret-inventory.py`.

## 1. Transport rule

Secrets move **only** via:

- **secret-drop UI** — `<ada-chat-card>` 🔐 Drop secret modal →
  `POST /api/secret-drop`. Value rides the POST body once, lands at
  `~/.config/secrets/<name>` (0600) on the target host; ssh fan-out
  carries it on stdin, never argv. Receipt is metadata only
  (`{path, sha256[:8]}`).
- **direct file edit on the target host** — ssh in, write the file
  yourself, `chmod 600`.

**NEVER** via chat, LINE, kanban comms, git, email, or any durable text
surface. If a secret is needed mid-conversation, the answer is "drop it
via the card", never "paste it here".

## 2. Detection — grep-canary

`scripts/secret-canary.py` periodically scans durable text surfaces for
credential patterns (`sk-*`, `AIza`, `xoxb*`, `cfut_`, `ghp_`,
`github_pat_`, `glpat-`, `ada-*`, private-key blocks):

- transcripts `~/.local/share/ada/transcripts`
- ops/events `~/.local/share/ada/events.md`, `~/.local/share/ada-review`
- focus-inbox `~/focus-inbox`
- extra roots via argv or `SECRET_CANARY_ROOTS` (colon-separated)

Findings are reported as `file:line pattern sha8` — the matched text is
never printed. Any finding files an **incident card** on the kanban board
(`ADA_BOARD_API_URL`, default `http://127.0.0.1:8787` on the host running
it) and exits nonzero so a timer/cron wrapper alerts.

## 3. Incident rule

Any secret found in a durable text surface (chat log, card comms,
transcript, git object, journal) is treated as **exposed**:

1. **Rotate immediately** — mint a replacement and drop it via
   secret-drop (or direct file edit). Do not scrub first; a still-live
   secret in a log stays live.
2. **Then scrub** — remove/rewrite the durable copy (truncate the log
   line, delete the comms entry, force-push only when git history is
   provably unread — rotation is what actually protects you).

The incident card records where the fingerprint was found so the scrub
has a checklist.

## 4. Rotation inventory

`scripts/secret-inventory.py [--json] [host ...]` lists which secrets
live where — **metadata only**: name, mode, size, mtime, and the sha256
first8 fingerprint (same fingerprint the drop receipt returns, so an
inventory line can be reconciled against a drop receipt). It never reads
values out loud — but sha8 does let you confirm two hosts hold the same
key, and flags `!mode` for non-0600/0400 files and `!sha-mismatch` when
the same name differs across hosts.

Use it before rotating ("which hosts hold `openai-key`?") and after
("did every host pick up the new sha8?").

## Endpoint contract

`POST /api/secret-drop` — `{host, name, value}` →
`{ok, host, name, path, sha8, via}`.

- `host`: one of `idc03 | tony-dell | tony-omen | idc02`. The target's own
  hostname (or `ADA_HOST_ALIAS`, `localhost`) writes locally; anything
  else is delivered over `ssh` (BatchMode, value on stdin).
- `name`: `[a-z0-9._-]{1,64}`; `ada-ha-*-keys.json` is reserved (that's
  the auth key store — overwriting it locks everyone out).
- `value`: ≤64KB. Never logged, never in transcripts, never echoed —
  the log line and the ops event carry caller/host/name/sha8 only.
- Auth: any paired chat key (`key_can(name, "chat")`) — pending, revoked,
  and view-only keys are refused. ~5 drops/hour per key (429).
