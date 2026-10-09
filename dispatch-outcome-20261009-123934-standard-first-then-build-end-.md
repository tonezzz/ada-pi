# Dispatch outcome — ada-member-pwa-invite (standard first, then build end-to-end)

## What changed

**Standard** — `docs/kb/ada-member-invite.md` (new): invite token format
(`token_urlsafe(24)`, persistent, bearer + single-device, rotatable),
manifest naming `ADA-{INSTANCE}({Person})` (INSTANCE = `ADA_PWA_INSTANCE`
or `HA-<ADA_INSTANCE_ID upper>` → `ADA-HA-TONY(KK)`), key states
(pending/issued/revoked), approval surfaces, revocation, member flow,
component standard.

**Key states** — `backend/auth.py`: entries gain `approved` (absent = true,
legacy-compat), `revoked` tombstone + `revoked_at`, `person` label,
persistent `invite` token, `invite_claimed` first-touch marker. Pending
and revoked keys are excluded from `_parse_keys` — no sessions, no API,
no `/ws`. `revoke_key` now tombstones instead of deleting (name stays
taken, audit survives). New: `approve_key`, `key_status`,
`invite_for_token`, `invite_token_for`, `rotate_invite`, `claim_invite`,
`invite_details`, `reinvite_key`. **Fail-closed fix:** `configured()` and
`websocket_caller` no longer treat a pending-only keys file as "auth
unconfigured" (that would have failed open).

**API** — `pwa_server.py`:
- `POST /api/auth/invites` mints a *pending* key + invite link; accepts
  `person`, `ha_person`, `apps`, `approved:true` escape hatch; a revoked
  name resurrects as a fresh pending invite (old links die).
- `POST /api/auth/keys/{name}/approve` + `.../reject` — the gate.
- `GET /api/auth/keys` now returns `issued|pending|revoked` groups +
  `details` (status, person, claimed).
- Public surface (no auth — ada-pi-pwa has no CF Access):
  `GET /i/{tok}` landing page (server-templated app name for iOS A2HS),
  `GET /i/{tok}/manifest.json` (per-member install name),
  `GET /api/invite/{tok}` (status poll), `POST /api/invite/{tok}/redeem`
  (pending → claim marker only; issued → TOFU-bind + key + session
  cookie; revoked → 410).
- `reissue_invite` is state-aware; `update_invite` refuses revoked names.

**Member PWA** — `pwa/invite/index.html`: landing page with iOS A2HS
steps, auto-claim, pending poll (5 s), redeem **inside the installed PWA
only** (Safari and installed-PWA localStorage are separate — claiming
never pre-binds the device so the hop can't strand the member).

**Keys admin** — `pwa/cards/ada-keys-card.js` (vanilla web component,
embeddable via `api-base`) + `pwa/keys.html`: Issued | Pending | Revoked
sections, Approve/Reject on pending, Revoke/Re-pair on issued,
mint-invite form with clipboard copy.

**Ada voice gate** — `backend/tools.d/ada_member_keys.py` (`owner_only`):
`list | invite | approve | reject | revoke | reissue` with fuzzy
name/person resolution ("approve kk" → `user-kk`). Registered in
`manifest.yml`, documented in `tool_guide.yml`, scenario
`tests/scenarios-live/member_keys.yaml`, `ssot.tool-surface.yml`
count_cap 109 → 110.

**Docs/env** — `.env.example` documents `ADA_PWA_INSTANCE` /
`ADA_PUBLIC_URL`; `session-security-policy.md` onboarding updated to the
pending-invite flow; job trail at
`docs/ssot/jobs/ada/2026-10-09-member-invite-pwa.yml`.

## Verify

- `python3 -m unittest tests.test_auth` — 26/26 pass (9 new
  MemberInviteTests cover the gate, tombstones, tokens, claims,
  re-invite).
- Tool module smoke-tested standalone: full lifecycle invite → pending →
  approve → reissue → revoke → reinvite → reject all correct.
- `node --check` clean on `pwa/invite/index.html` JS +
  `ada-keys-card.js`; all YAML parses.
- `scripts/tool-lint.py` — the only violation is pre-existing on HEAD
  (`ToolRunner.yt_cached_list`, screens.py — untouched by this work).
- Not deployed: deploy to idc03 via `~/.local/bin/deploy-ada.sh` per
  AGENTS.md; fastapi isn't installed in this worktree env so the runtime
  surface check happens there.
