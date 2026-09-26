# Finalize: Ada CMS multi-page miniapp / idc01 migration (achieved-butter tail)

Date: 2026-09-26 · Session: dispatch `20260926-190458-finalize-the-ada-cms-multi-pag`

Source: tail of session `achieved-butter` in `~/.local/share/devin/cli/sessions.db`.
Migration M0–M6 was already functionally complete; this session closed the
remaining follow-ups.

## Follow-up dispositions

| # | Item | Result |
|---|------|--------|
| 1 | Push 4 unpushed chaba commits (ede1105, 21e6fb0, cee2af1, 2f7fc06) | Already done — later sessions rebased master and pushed; equivalents on origin/master: 4c4582c, e53fe81, a0d8e5b (M6 content incl. memory-distill.py), 8e739f0. master == origin/master. |
| 2 | M7 soak then archive tony-dell mddb.db | Passive — still in the ~1 week soak window; nothing to do |
| 3 | ssot.apps.ada-testing.yml YAML error (parallel session's file) | Resolved — file no longer exists; `ssot-validate-all` 484/484 clean |
| 4 | Distill event-emit `ssh tony-dell` fails from idc01 | **Fixed** — see below |
| 5 | Stale refs / mn01 verify.sh | Done — see below |

## Emit fix (was the only real gap)

Root cause: `ssh tony-dell` from idc01 hits Tailscale SSH check-mode
(recurring browser auth) — not a missing key. All five ada job scripts
emitted via that ssh path, so idc01 events never reached the HA feed.

Fix (canonical): chaba branch `dispatch/20260926-190458-ada-cms-emit-fix`
in worktree `~/CascadeProjects/dispatch-wt-20260926-190458-ada-cms-chaba`:

- `f1fa7c7` — new `scripts/ada/chaba_event.py`: ssh-first, falls back to
  `POST <ha>/api/services/shell_command/chaba_event` (existing HA hook that
  base64-pipes into chaba-event-log.py). All 5 emit-capable ada scripts
  refactored onto it.
- `bda2313` — ssot.health.mobile.yml stale tony-dell MDDB recovery refs →
  idc01 quadlet; `stacks/mn01/verify.sh` rewritten for standby role.
- `9d95dae` — job SSOT `docs/ssot/jobs/infrastructure/2026-09-26-ada-event-emit-http-fallback.yml`
  + closed the M6 known-gap note in `public-host-plan.md`.

Deployed: scp'd to `idc01:~/CascadeProjects/chaba-vault/scripts/ada/` +
`home-assistant-token.env` provisioned to idc01 secrets (600). Verified:
`python3 chaba_event.py` on idc01 → `ok via http` → event visible in
`/local/chaba-events.json`.

**Needs operator**: merge `dispatch/20260926-190458-ada-cms-emit-fix` to
chaba master and push (dispatch cannot push), then `git pull` in idc01's
chaba-vault to clean its now-dirty checkout.

## Notes / observations

- idc01 rebooted ~19:20 during this session (unrelated — probably the
  concurrent link-aggregation dispatch task); all units came back, MDDB
  cold-start ~6 min then healthy (11023/9000/3002 listening).
- `ada-ha-tony.env` on idc01 has a stale `HOME_ASSISTANT_TOKEN` (401 on
  tony-ha REST). Ada itself works, but worth checking which tools use it.
- ada-pi main is ahead 1 (607035c feat(notify)) — another session's
  unpushed commit, left alone.
- CMS miniapp live check: `/apps/ha/ada-tony/cms/` → 200 via mn01 edge.
