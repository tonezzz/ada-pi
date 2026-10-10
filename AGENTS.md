# ada-pi — agent operations

## Deploy flow (idc03/idc02)

`origin` = `github.com/tonezzz/ada-pi`, branch `main` — single source of truth.

- **tony-dell** `~/CascadeProjects/ada-pi` — the edit point. Commit + push here.
- **idc02** `~/CascadeProjects/ada-pi` — staging checkout; keep clean, `git pull --ff-only`. Verify scenarios here before prod.
- **idc03** `~/CascadeProjects/ada-pi` — production checkout serving `ada-ha-tony`, `ada-ha-michael`, `ada-pi-pwa`, `ada-dev`, `ada-line-relay`, `ada-tg-relay` (migrated from idc01 2026-10-05; idc01 is retired — its mddb is masked, never unmask). **Deploy-only**: do not hand-edit. Deploy via `~/.local/bin/deploy-ada.sh` — canonical copy is `scripts/deploy-ada.sh` in this repo; install with `install -m 0755 scripts/deploy-ada.sh ~/.local/bin/deploy-ada.sh`. It snapshots any dirty tree to `wip/deploy-snapshot-*`, ff-merges `origin/main`, runs the pre-flight gates, restarts the services, verifies active.
- **Deploy gates** (post-merge, pre-restart — a failure aborts before any restart): mass-delete warn (a merged diff that shrinks a `backend/tools.d/` or `backend/tool_runner/` file past half without touching a covering scenario pauses until `DEPLOY_FORCE=1`), `python3 scripts/tool-lint.py`, `.venv/bin/python -m pytest tests/ -x -q`. Lint/test failures are hard stops — fix the tree; `DEPLOY_FORCE` only clears the mass-delete pause.

Never `git switch -f` / `checkout -f` / `reset --hard` on a checkout another session may be using — `-f` discards uncommitted work silently (incident 2026-10-01). Snapshot first; the deploy script does this for you.
