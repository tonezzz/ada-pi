#!/usr/bin/env bash
# deploy-ada.sh — deploy gate for the ada-voice production/staging checkouts.
#
# Canonical copy lives in the repo (this file). Install on the host:
#     install -m 0755 scripts/deploy-ada.sh ~/.local/bin/deploy-ada.sh
#
# Flow (AGENTS.md "Deploy flow"):
#   1. snapshot a dirty tree to wip/deploy-snapshot-<ts>
#   2. fetch + ff-merge origin/main
#   3. PRE-FLIGHT GATES — deploy refuses before any restart (card
#      ada-deploy-gate-tests, 2026-10-10):
#      a. mass-delete warn — if the merged diff drops >50% of the lines of
#         a backend/tools.d/ or backend/tool_runner/ file without touching
#         a scenario that covers it, pause; rerun with DEPLOY_FORCE=1 to
#         override. Catches inverted rebase resolutions (the 2026-10-08
#         ada_track_device incident).
#      b. python3 scripts/tool-lint.py          — tool contract check
#      c. .venv/bin/python -m pytest tests/ -x -q — unit tests
#   4. drain grace — wait for live /ws sessions to close (ADA_DRAIN_S)
#   5. restart the ada services and verify each is active
#
# Env:
#   ADA_REPO       checkout to deploy (default ~/CascadeProjects/ada-voice)
#   ADA_SERVICES   systemd --user units to restart (default: idc03 set)
#   ADA_DRAIN_S    seconds to wait for live ws sessions to end before
#                  restarting anyway (default 45; 0 skips the drain)
#   DEPLOY_FORCE=1 bypass the mass-delete pause only — lint/test failures
#                  are hard stops, fix the tree.

set -euo pipefail

REPO="${ADA_REPO:-$HOME/CascadeProjects/ada-voice}"
SERVICES="${ADA_SERVICES:-ada-ha-tony ada-ha-michael ada-pi-pwa ada-dev ada-kk-dev ada-kk ada-line-relay ada-tg-relay}"

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] FAIL: %s\n' "$*" >&2; exit 1; }

cd "$REPO" 2>/dev/null || die "repo $REPO not found"
git rev-parse --git-dir >/dev/null 2>&1 || die "$REPO is not a git checkout"

# --- 0. self-drift check ---------------------------------------------------
# The installed copy at ~/.local/bin/deploy-ada.sh predating gates was the
# 2026-10-10 outage enabler (.venv symlink shipped to prod undetected). If
# this copy differs from the canonical repo copy, refuse — reinstall with:
#   install -m 0755 scripts/deploy-ada.sh ~/.local/bin/deploy-ada.sh
canon="$REPO/scripts/deploy-ada.sh"
self="$(readlink -f "$0" 2>/dev/null || echo "$0")"
if [[ "$self" != "$canon" && -f "$canon" ]] && ! cmp -s "$self" "$canon"; then
    die "deploy script drifted from canonical $canon — reinstall it"
fi

# --- 1. snapshot a dirty tree --------------------------------------------
if [[ -n "$(git status --porcelain)" ]]; then
    snap="wip/deploy-snapshot-$(date +%Y%m%d-%H%M%S)"
    log "dirty tree — snapshotting to $snap"
    git switch -c "$snap" --quiet
    git add -A
    git commit -qm "deploy snapshot $(date -Is)"
    git switch - --quiet
    [[ -z "$(git status --porcelain)" ]] || die "tree still dirty after snapshot"
fi

# --- 2. fetch + ff-merge --------------------------------------------------
log "fetching origin"
git fetch origin --quiet
pre=$(git rev-parse HEAD)
git merge --ff-only origin/main --quiet || die "ff-merge origin/main failed"
post=$(git rev-parse HEAD)
if [[ "$pre" == "$post" ]]; then
    log "already at origin/main ($post)"
else
    log "merged $pre -> $post"
fi

# --- 3a. mass-delete gate on the merged diff ------------------------------
# The pause lands AFTER the merge, so a rerun would see pre==post and slip
# past — persist the warn in .git/ and hold it until DEPLOY_FORCE=1 clears.
WARNF=.git/deploy-mass-delete-warn
[[ -f "$WARNF" && "$(cat "$WARNF" 2>/dev/null || true)" != "$post" ]] && rm -f "$WARNF"
if [[ -f "$WARNF" ]]; then
    [[ "${DEPLOY_FORCE:-0}" == "1" ]] || die "deploy is paused on a mass-delete warn at ${post:0:12} — rerun with DEPLOY_FORCE=1 to override"
    rm -f "$WARNF"
    log "DEPLOY_FORCE=1 — continuing past mass-delete warn"
elif [[ "$pre" != "$post" ]]; then
    if ! python3 - "$pre" "$post" <<'PYEOF'
import re, subprocess, sys

pre, post = sys.argv[1], sys.argv[2]

def git(*args):
    return subprocess.run(["git", *args], capture_output=True,
                          text=True, check=True).stdout

# tool name -> module map (tools.d stems don't always equal tool names,
# e.g. ada_look -> eye.py)
mod_tools = {}
try:
    txt = open("backend/tools.d/manifest.yml").read()
    for m in re.finditer(
            r"^  ([A-Za-z_0-9]+):\s*\n(?:(?:    |  #).*\n)*?    module: ([A-Za-z_0-9]+)",
            txt, re.M):
        mod_tools.setdefault(m.group(2), []).append(m.group(1))
except OSError:
    pass

changed = git("diff", "--name-only", f"{pre}..{post}").split()
scenarios = [f for f in changed
             if f.startswith(("tests/scenarios/", "tests/scenarios-live/"))]

def post_path(p):
    # numstat renames print "dir/{old => new}.py" or "old => new"
    p = re.sub(r"\{[^{}]*? => ([^{}]*?)\}", r"\1", p)
    return p.split(" => ")[-1] if " => " in p else p

GUARDED = ("backend/tools.d/", "backend/tool_runner/")
flagged = []
for line in git("diff", "--numstat", "-M", f"{pre}..{post}",
                "--", *GUARDED).splitlines():
    parts = line.split("\t")
    if len(parts) < 3 or parts[0] == "-":
        continue  # binary or unparseable
    added, deleted, path = int(parts[0]), int(parts[1]), post_path(parts[2])
    try:
        old = len(git("show", f"{pre}:{path}").splitlines())
    except subprocess.CalledProcessError:
        continue  # new file or rename target — nothing to drop
    # flag only when the file SHRANK past half — a rewrite that replaces
    # every line is normal for tools.d; a gutted file is not
    new = old - deleted + added
    if old == 0 or new * 2 >= old:
        continue
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    names = {stem} | set(mod_tools.get(stem, []))
    covered = False
    for sc in scenarios:
        if any(n in sc for n in names):
            covered = True
            break
        try:
            if any(n in open(sc).read() for n in names):
                covered = True
                break
        except OSError:
            pass
    if not covered:
        flagged.append((path, old, new))

for path, old, new in flagged:
    print(f"[deploy] mass-delete: {path} shrank {old} -> {new} lines "
          f"(-{(old - new) * 100 // old}%) and the merge touched no "
          f"scenario covering it", file=sys.stderr)
sys.exit(1 if flagged else 0)
PYEOF
    then
        if [[ "${DEPLOY_FORCE:-0}" == "1" ]]; then
            log "DEPLOY_FORCE=1 — continuing past mass-delete warn"
        else
            echo "$post" > "$WARNF"
            die "mass-delete gate paused the deploy (possible inverted rebase resolution); rerun with DEPLOY_FORCE=1 to override"
        fi
    fi
fi

# --- 3b. tool contract lint ----------------------------------------------
log "gate: tool-lint"
python3 scripts/tool-lint.py || die "tool-lint failed — deploy aborted before restart"

# --- 3c. unit tests -------------------------------------------------------
log "gate: pytest tests/ -x -q"
[[ -x .venv/bin/python ]] || die ".venv/bin/python missing — cannot run tests, deploy aborted"
# interpreter sanity — a symlink-loop or gutted venv passes -x but dies at
# runtime with 203/EXEC (2026-10-10 outage). One import is the cheapest proof.
.venv/bin/python -c "import fastapi" 2>/dev/null \
    || die ".venv interpreter broken (imports fail) — deploy aborted"
.venv/bin/python -m pytest tests/ -x -q || die "pytest failed — deploy aborted before restart"

# --- 4. drain grace -------------------------------------------------------
# A fleet restart mid-conversation kills live turns (2026-10-14: session
# 4585f3b060 died mid-conversation). Each backend reports its live /ws
# count on the unauthenticated /api/health endpoint; wait for sessions to
# drain before restarting. Transcript append is incremental per turn, so
# an expiry restart loses nothing already spoken — the wait only spares a
# reply being cut mid-flight.
DRAIN_S="${ADA_DRAIN_S:-45}"

service_port() {
    # ADA_PORT in the unit environment first, then --port in ExecStart.
    # Empty when unresolvable — that service is skipped (e.g. relays with
    # no HTTP listener).
    local p
    p=$(systemctl --user show "$1" -p Environment --value 2>/dev/null \
        | tr ' ' '\n' | sed -n 's/^ADA_PORT=\([0-9][0-9]*\)$/\1/p' | head -1)
    if [[ -z "$p" ]]; then
        p=$(systemctl --user cat "$1" 2>/dev/null \
            | sed -n 's/.*--port[= ]\{1,\}\([0-9]\{1,5\}\).*/\1/p' | head -1)
    fi
    printf '%s' "$p"
}

live_sessions() {
    curl -fsS -m 3 "http://127.0.0.1:$1/api/health" 2>/dev/null | python3 -c '
import json, sys
try:
    print(int(json.load(sys.stdin).get("active_sessions") or 0))
except Exception:
    print(0)'
}

if [[ "$DRAIN_S" != "0" ]]; then
    deadline=$((SECONDS + DRAIN_S))
    busy=""
    while :; do
        busy=""
        for s in $SERVICES; do
            port=$(service_port "$s")
            [[ -n "$port" ]] || continue
            n=$(live_sessions "$port")
            (( n > 0 )) && busy="$busy ${s}:${n}"
        done
        [[ -z "$busy" ]] && break
        (( SECONDS < deadline )) || break
        log "drain: live ws sessions$busy — waiting"
        sleep 3
    done
    if [[ -n "$busy" ]]; then
        log "drain grace expired — restarting anyway:$busy"
    else
        log "drain: no live ws sessions"
    fi
fi

# --- 5. restart + verify --------------------------------------------------
log "restarting: $SERVICES"
# shellcheck disable=SC2086
systemctl --user restart $SERVICES
sleep 2
fail=0
for s in $SERVICES; do
    if systemctl --user is-active --quiet "$s"; then
        log "active: $s"
    else
        printf '[deploy] FAIL: not active: %s\n' "$s" >&2
        fail=1
    fi
done
[[ $fail -eq 0 ]] || die "one or more services failed to come up"
log "deploy complete: $post"
