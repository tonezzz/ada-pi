# ada-pi — agent operations

## Deploy flow (idc01/idc02)

`origin` = `github.com/tonezzz/ada-pi`, branch `main` — single source of truth.

- **tony-dell** `~/CascadeProjects/ada-pi` — the edit point. Commit + push here.
- **idc02** `~/CascadeProjects/ada-pi` — staging checkout; keep clean, `git pull --ff-only`. Verify scenarios here before prod.
- **idc01** `~/CascadeProjects/ada-pi` — production checkout serving `ada-ha-tony`, `ada-ha-michael`, `ada-pi-pwa`, `ada-dev`. **Deploy-only**: do not hand-edit. Deploy via `~/.local/bin/deploy-ada.sh` — it snapshots any dirty tree to `wip/deploy-snapshot-*`, ff-merges `origin/main`, restarts the four services, verifies active.

Never `git switch -f` / `checkout -f` / `reset --hard` on a checkout another session may be using — `-f` discards uncommitted work silently (incident 2026-10-01). Snapshot first; the deploy script does this for you.
