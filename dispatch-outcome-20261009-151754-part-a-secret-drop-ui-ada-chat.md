# dispatch outcome — ada-secret-drop (attempt 2)

## What changed

**Part A — secret-drop UI + endpoint**

- `pwa/cards/ada-chat-card.js` (new) — `<ada-chat-card>` web component per
  the ada-*-card standard: text chat over `/ws` (guest.js protocol) plus a
  `.acc-secret` toolbar button opening a modal. Modal fields: target host
  dropdown (idc03/tony-dell/tony-omen/idc02), secret name (client-validated
  `[a-z0-9._-]{1,64}`), value (`type=password`, `autocomplete=off`,
  `data-lpignore`/`data-1p-ignore`). On submit the value leaves the page
  exactly once in the POST body; it's cleared from the DOM on success and
  on modal close, and is never rendered or logged. Receipt shows only
  `path + sha8`.
- `pwa/chat.html` (new) — mount page, mirrors `pwa/keys.html`.
- `pwa_server.py` — `POST /api/secret-drop` accepting `{host,name,value}`:
  - auth: `caller_name()` + `key_can(name, "chat")` — any paired chat key
    (card answer: any-paired); pending/revoked/view-only refused; 401/403.
  - rate limit: sliding window, 5/hour per caller (429;
    `ADA_SECRET_DROP_MAX_PER_HOUR` tunable).
  - name: `^[a-z0-9._-]{1,64}$`; `ada-ha-*-keys.json` reserved (writing
    over the auth key store in the same dir would lock everyone out).
  - delivery: local write `0600` when `host` == this hostname /
    `ADA_HOST_ALIAS` / localhost (mode normalized on re-drop too);
    otherwise **ssh fan-out** — value on stdin, never argv (BatchMode,
    8s connect, 20s total timeout). Fan-out honors Tony's explicit
    drop-scope answer even though the dispatch brief parenthetical said
    out-of-scope — flagged for review.
  - receipt `{ok, host, name, path, sha8, via}` — value never logged;
    `logger.info` + `event_log.log_event` carry metadata only.
- `tests/test_secret_drop.py` — 15 cases: auth gates, validation,
  0600 write + mode normalization, rate limit, ssh argv-isolation
  (asserts the value is on stdin, not argv), and the compliance check:
  sentinel value absent from captured log records (journal proxy), the
  ops/events file, live session transcripts, and the response.
- `.env.example` — documents `ADA_SECRETS_DIR`, `ADA_HOST_ALIAS`,
  `ADA_SECRET_DROP_MAX_PER_HOUR`.

**Part B — secrets hygiene standard**

- `docs/kb/ada-secrets-hygiene.md` — transport rule (secret-drop UI or
  direct file edit only; never chat/LINE/kanban/git), canary detection,
  rotate-then-scrub incident rule, rotation inventory.
- `scripts/secret-canary.py` — grep-canary over transcripts, ops/events,
  `~/.local/share/ada-review`, `~/focus-inbox`, dispatch outcomes (+ argv /
  `SECRET_CANARY_ROOTS`). Reports `file:line pattern sha8` fingerprints —
  never match text — and files an incident card via board-api on findings
  (exit 2; clean exit 0).
- `scripts/secret-inventory.py` — per-host `~/.config/secrets` listing,
  metadata only (name/mode/bytes/mtime/sha8), local or over ssh; flags
  `!mode` and `!sha-mismatch` across hosts.
- `docs/ssot/jobs/ada/2026-10-09-secret-drop.yml` — job record.

## Notes for review

- `ada-chat-card.js` did not exist anywhere (attempt 1 left no branch or
  card detail) — created new as a real chat card so the `.acc-secret`
  modal lives on an actual chat surface, reachable at `/chat.html`.
- ssh fan-out requires the service account on idc03 to already have
  BatchMode ssh to the fleet — same pattern as the cms
  host-services-cms generator. A failed/unreachable host returns 502
  with the ssh error (no value echoed).
- Not committed/pushed/deployed per dispatch rails. Deploy on idc03 via
  `~/.local/bin/deploy-ada.sh` after merge.

## Verification

- `.venv/bin/python -m pytest tests/ -q` (CI dep set): **965 passed**,
  1 skipped, 5 deselected (same deselects as `.github/workflows/ci.yml`).
- `node --check pwa/cards/ada-chat-card.js` — clean.
- `secret-inventory.py --json` ran live (metadata only, no values).
- `secret-canary.py` smoke test on a synthetic root detected `sk-`/`xoxb`
  patterns, printed fingerprints only, degraded cleanly on unreachable
  board.
- Manual spot: `curl -X POST /api/secret-drop -H 'X-Api-Key: …'` on a live
  instance → writes `~/.config/secrets/<name>` 0600, returns sha8 receipt.
