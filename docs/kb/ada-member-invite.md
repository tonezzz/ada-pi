# Ada Member Invite — standard

How a new household member gets Ada on their phone: an invite link that
installs a per-member PWA, mints a **pending** key, and only activates
after an owner clears the approval gate. Card:
`ada-member-pwa-invite` (2026-10-09).

This doc is the contract. Implementation lives in `backend/auth.py`,
`pwa_server.py`, `pwa/invite/`, `pwa/cards/ada-keys-card.js`, and
`backend/tools.d/ada_member_keys.py`.

## Surfaces

| Surface | Path | Auth |
|---|---|---|
| Invite landing (member) | `GET /i/{invite_token}` | public — no login, no CF Access |
| Per-invite manifest | `GET /i/{invite_token}/manifest.json` | public |
| Invite status (member) | `GET /api/invite/{invite_token}` | public |
| Invite redeem (member) | `POST /api/invite/{invite_token}/redeem` | public, device-bound |
| Invite mint (operator) | `POST /api/auth/invites` | owner/admin key |
| Approve / reject | `POST /api/auth/keys/{name}/approve`, `.../reject` | owner/admin key |
| Keys admin card | `<ada-keys-card>` (`pwa/keys.html`) | owner/admin key |
| Ada voice gate | `ada_member_keys` tool | owner_only |

The member-facing surface is deliberately **unauthenticated**: ada-pi-pwa
on idc03 is public (no Cloudflare Access) so a member never hits a login
wall. The invite URL is the only secret a member ever sees — the raw API
key is handed to the device exactly once, at redeem time, after approval.

## Invite token format

- Minted at invite creation: `secrets.token_urlsafe(24)` — ~32 URL-safe
  chars, persisted on the key entry as `invite`.
- Invite URL: `/i/{token}` (absolute form built from `origin` or
  `ADA_SELF_URL`).
- **Persistent, not TTL-bound.** Unlike burn-once `/redeem/{token}`
  links (10 min), an invite link must survive LINE delivery and a
  member installing days later.
- Bearer + device-bound: the first device to touch
  `/api/invite/{token}/redeem` claims the invite and TOFU-binds the key's
  `device` field. After that, only that device can redeem — a forwarded
  link opens the landing page but redeems to 403.
- Rotatable: `ada_member_keys action=invite` on an existing name (or
  `POST /api/auth/invites` on a revoked name) mints a fresh token and
  invalidates old links.

## PWA manifest naming — `ADA-{INSTANCE}({Person})`

Member installs must show the member's name on the home screen — that is
what iOS Add-to-Home-Screen reads and what distinguishes "Tony's Ada"
from "KK's Ada".

- `INSTANCE` = `ADA_PWA_INSTANCE` env when set, else
  `HA-{ADA_INSTANCE_ID.upper()}` — on idc03 `ada-ha-tony` yields
  `HA-TONY`, so member `kk` installs as **`ADA-HA-TONY(KK)`**.
- `Person` = the invite's `person` label (display name), title-cased.
- The instance owner's own install keeps the existing
  `ADA-HA({Instance})` form from `/manifest.json` (`ADA_PWA_NAME` still
  wins there).
- iOS honours `apple-mobile-web-app-title` at A2HS time — the landing
  page renders it server-side (`{{APP_NAME}}` template slot), so the
  correct name is in the DOM before any JS runs. Android/desktop reads
  `manifest.json`, served per-token at `/i/{token}/manifest.json` with
  `start_url=/i/{token}`.

## Key states

Each file-issued key entry (`ADA_KEYS_FILE`) carries:

```json
"user-kk": {
  "key": "ada-…",
  "device": "…",            // TOFU device binding (null until claimed)
  "issued": "2026-10-09",
  "ha_person": "person.kk", // optional person binding
  "person": "KK",           // display label (manifest + keys card)
  "invite": "<token>",      // persistent invite token, if minted
  "invite_claimed": {"device": "…", "at": "…"},
  "approved": false,        // absent === true (legacy compat)
  "revoked": true,          // tombstone — replaces hard delete
  "revoked_at": "…"
}
```

Derived `status`:

| status | rule | authenticates? |
|---|---|---|
| `pending` | `approved == false`, not revoked | **no** — excluded from `_parse_keys`, so no session, no `/ws`, no API |
| `issued` | approved (or legacy, no `approved` field) | yes |
| `revoked` | `revoked == true` | no — tombstone keeps the name taken and the audit trail |

`approved=false` is the invite default: `POST /api/auth/invites` mints a
pending key bound to the person label; nothing about the member's device
can authenticate until the gate clears. (Direct keys via
`POST /api/auth/keys` still mint approved — operator path.)

## Approval gate

Two equivalent surfaces, both owner-tier:

1. **Ada voice** — `ada_member_keys` tool (`owner_only`):
   - "approve KK" → `action=approve name=kk` (fuzzy: `kk` resolves the
     pending `user-kk`).
   - "reject KK" → `action=reject` — tombstones the pending key.
   - "who's pending?" → `action=list` reads pending/issued/revoked.
   - "invite KK" → `action=invite name=kk person=KK` — returns the
     invite URL to relay over LINE/TG.
2. **Keys card** — `<ada-keys-card>` lists pending keys with
   **Approve / Reject** buttons (`POST /api/auth/keys/{name}/approve` /
   `reject`).

Approval flips `approved` to `true`. The member's already-installed PWA
finishes redeem on next open or button tap — no new link needed.

## Member flow

1. Operator: `POST /api/auth/invites {name:"user-kk", person:"KK",
   ha_person:"person.kk"}` → `{invite_url: "/i/<tok>", …}` — send it.
2. Member opens `/i/<tok>` on iPhone Safari: landing page shows
   `ADA-HA-TONY(KK)` branding, an Add-to-Home-Screen hint, and claims
   the invite (TOFU-binds the device).
3. Page shows **waiting for approval** and polls
   `/api/invite/<tok>` until the gate clears.
4. Owner approves (voice or keys card).
5. Member taps **Open Ada**: redeem returns the key once, the page stores
   it in `localStorage` and lands on `/` — normal PWA from there.

## Revocation

`DELETE /api/auth/keys/{name}` (or `ada_member_keys action=revoke`, or
Reject on a pending key) writes a **tombstone**: `revoked:true` +
`revoked_at`, name stays taken, sessions die on next request (the key is
gone from `_parse_keys`). Tombstones list under **Revoked** on the keys
card — the audit trail survives.

## Component standard

Member-facing and admin UI is vanilla web components (`ada-*-card`
pattern, e.g. `<ada-keys-card>` in `pwa/cards/`): no build step, no
framework, Shadow DOM encapsulated. The same file drops unchanged into
the ada-pi-pwa static root, an HA sidebar iframe/panel, or a CMS embed.
Target matrix: iPhone 15 Safari + Add-to-Home-Screen.
