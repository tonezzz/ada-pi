#!/usr/bin/env bash
# Bench readiness gate — run before any multi-scenario suite. Exits 0
# only when the bench result would be trustworthy. Each check maps to
# a known bad-data cause observed 2026-09-30 (casting 0.045 mid-
# refactor run, stale-context answers, 0-turn aborts).
set -u
FAIL=0

ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAIL=1; }

echo "preflight-bench $(date -u +%FT%TZ)"

# 1. Ada up AND stable — no restart in the last 3 min. A scenario
#    mid-restart yields stale-context answers.
if ! systemctl --user is-active -q ada-ha-tony; then
    bad "ada-ha-tony not active"
else
    since=$(systemctl --user show ada-ha-tony -p ActiveEnterTimestamp --value)
    age=$(( $(date +%s) - $(date -d "$since" +%s) ))
    [ "$age" -gt 180 ] && ok "ada up ${age}s" || bad "ada restarted ${age}s ago (<3min)"
fi

# 2. No scenario run in flight — the runner self-locks but a skipped
#    suite records nothing; worse, a racing run produces mixed data.
if journalctl --user --since "10 min ago" --no-pager 2>/dev/null \
     | grep -q "another scenario run is active"; then
    bad "another scenario run active"
else
    ok "no scenario lock"
fi

# 3. :8002 accepting — restart gap check (active service but listener
#    not yet bound happens ~10s during boot).
if curl -sf --max-time 3 -o /dev/null http://127.0.0.1:8002/healthz 2>/dev/null \
     || curl -sf --max-time 3 -o /dev/null http://127.0.0.1:8002/ 2>/dev/null; then
    ok "ada :8002 listening"
else
    bad "ada :8002 not accepting"
fi

# 4. Embed path — a Gemini 429 burst stalls recall scenarios. The
#    embed-probe timer (scripts/ada/embed-probe.py) already checks
#    this properly and writes ~/.local/share/ada/embed-probe.state —
#    reuse its verdict instead of probing again.
state=$(cat ~/.local/share/ada/embed-probe.state 2>/dev/null || echo "missing")
if [ "$state" = "up" ]; then
    ok "embed-probe state=up"
else
    bad "embed-probe state=$state"
fi

# 5. Headroom — load <2 on the 2-core box keeps tool calls inside the
#    live-session timeouts.
load=$(cut -d' ' -f1 /proc/loadavg)
if awk "BEGIN{exit !($load < 2.0)}"; then
    ok "load $load"
else
    bad "load $load >= 2"
fi

[ "$FAIL" -eq 0 ] && echo "READY" || { echo "NOT READY"; exit 1; }
