#!/usr/bin/env bash
# ada-pi scenario CI gate — run a live-scenario tier and fail only on
# genuine model regressions.
#
# scenario-live exit codes -> scenario-report statuses -> this gate:
#   0 pass/flaky        ok
#   1 expectation fail  FAIL (blocks)
#   3 preflight INFRA   recorded 'infra', does NOT block
#   4 needs_tools       recorded 'unimplemented', does NOT block
#   quota/timeout       recorded 'quota'/'skip-quota', does NOT block
#
#   ./ci-scenarios.sh [tier]     # default smoke
#
# Needs: live backend on $ADA_LIVE_URL (default ws://127.0.0.1:8002/ws),
# ADA_API_KEY or --keys-file env (ADA_KEYS_FILE). Writes per-scenario
# report docs to MDDB unless MDDB_BASE_URL unreachable (dry-run).
set -o pipefail
cd "$(dirname "$0")/.." || exit 2

PY=".venv/bin/python"; [ -x "$PY" ] || PY=python3
tier="${1:-smoke}"

if ! curl -sf -m 5 "${MDDB_BASE_URL:-http://127.0.0.1:11023/v1}/health" >/dev/null 2>&1; then
    echo "ci-scenarios: MDDB unreachable — dry-run (no report writes)"
    exec "$PY" scripts/scenario-report.py --tier "$tier" --dry-run
fi
exec "$PY" scripts/scenario-report.py --tier "$tier"
