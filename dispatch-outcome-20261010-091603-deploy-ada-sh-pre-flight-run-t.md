# Dispatch outcome — deploy-ada.sh pre-flight gates

Card: `ada-deploy-gate-tests` — run tests before restarting services.
Attempt 2 (attempt 1 died silent — likely tried to touch
`~/.local/bin/deploy-ada.sh`, outside the worktree sandbox).

## What changed (commit e23027d on dispatch branch)

- **`scripts/deploy-ada.sh`** (new, executable) — canonical vendored copy
  of the deploy script, which was previously untracked and existed only
  at `~/.local/bin/` on the hosts. Implements the AGENTS.md-documented
  flow: snapshot dirty tree to `wip/deploy-snapshot-*` → fetch +
  `merge --ff-only origin/main` → **new gates** → `systemctl --user
  restart` the six idc03 units → verify `is-active`.
- **Gates between merge and restart:**
  1. Mass-delete warn — embedded python numstat pass over
     `backend/tools.d/` + `backend/tool_runner/`. Flags a file that
     *shrinks past half* its old line count while the same merge touched
     no scenario (`tests/scenarios{,-live}` file) referencing the module
     stem or its manifest tool name. Pause persists via
     `.git/deploy-mass-delete-warn` so a rerun can't slip past;
     `DEPLOY_FORCE=1` clears it.
  2. `python3 scripts/tool-lint.py` — hard stop.
  3. `.venv/bin/python -m pytest tests/ -x -q` — hard stop (missing venv
     also aborts).
- **`AGENTS.md`** — deploy bullet now points at the repo copy +
  `install -m 0755 scripts/deploy-ada.sh ~/.local/bin/deploy-ada.sh`,
  plus a gates summary line.
- **`docs/ssot/jobs/ada/2026-10-10-deploy-gate-tests.yml`** — job trail.

## Design decisions worth noting

- "Drops >50% of lines" is implemented as **net shrink** (`new < old/2`),
  not raw deletions — a full rewrite deletes every old line, and
  wholesale rewrites are normal for tools.d (the 958-line module shape).
  Raw-deletion semantics false-flagged on the first test run.
- Coverage check is deliberately loose (stem + manifest tool names in
  changed scenario filename/content) — it's a pause heuristic.
- `DEPLOY_FORCE` only clears the mass-delete pause; lint/test failures
  have no override.

## Verification

Scratch-repo harness (`/tmp/deploy-test`: bare origin, stub systemctl,
rc-driven lint/pytest stubs): **19/19 checks pass** — clean merge
deploys; >50% gut without scenario pauses pre-restart (systemctl never
invoked); pause survives reruns until DEPLOY_FORCE; gut + covering
scenario deploys; full-file deletion pauses; tool-lint/pytest failures
abort pre-restart; dirty tree snapshots to `wip/`; `is-active` failure
exits non-zero. `bash -n` clean; manifest regex parses all 9 tool
entries including `ada_look → eye`.

## Still needed (host side, outside worktree)

1. `install -m 0755 scripts/deploy-ada.sh ~/.local/bin/deploy-ada.sh` on
   idc03 (and idc02 if it uses the script) — replaces the old untracked
   copy. Until then the old gateless script still runs deploys.
2. **tool-lint is red on current main** (3 pre-existing violations:
   `voice_fx` descsize + coverage, `yt_cached_list` impl seat). Once
   installed, the gate will hold deploys until those clear — first
   install should coincide with a lint-fix pass.

Card verify ("break a tool → refuses; clean tree deploys") is exercised
by the harness T2/T5/T6 vs T1/T3.

lessons:
- "deploy-ada.sh is NOT in the repo — it lives at ~/.local/bin on the hosts; dispatch worktrees can't reach it, so the fix is vendoring a canonical copy into scripts/ + AGENTS.md install step"
- "mass-delete gate must measure NET shrink (new < old/2): raw deletion counts false-flag every wholesale tools.d rewrite"
- "a post-merge pause needs a persisted marker (.git/deploy-mass-delete-warn) — otherwise a rerun sees pre==post and proceeds without DEPLOY_FORCE"
- "tool-lint is already red on main (voice_fx descsize+coverage, yt_cached_list impl) — a hard lint gate blocks ALL deploys until debt clears; flag it before installing"
- "scratch-repo deploy tests need `git init -b main` on the bare origin (clone gets no HEAD otherwise) and local user.name/user.email per repo"
