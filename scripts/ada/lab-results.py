#!/usr/bin/env python3
"""Render ada-ha-scenario-reports → CMS 'lab-results' page.

Groups report/<scenario>-<ts> docs by scenario, keeps the newest,
renders a status matrix (pass/flaky/fail + failed turns + time), and
upserts the ada-cms-pages/lab-results document so the CMS page always
shows the latest suite state.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from backend.report_meta import validate_report_meta  # noqa: E402

MDDB = os.environ.get("MDDB_BASE_URL", "http://127.0.0.1:11023/v1")


def post(ep, payload):
    req = urllib.request.Request(f"{MDDB}/{ep}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    # MDDB under corpus load can take >30s on a cold search — a bare
    # timeout here marked the whole smoke run failed even after every
    # scenario had already reported. Give it headroom + one retry.
    last = None
    for _ in range(2):
        try:
            return json.load(urllib.request.urlopen(req, timeout=120))
        except Exception as exc:  # noqa: BLE001 — retried once, then raise
            last = exc
    raise last


def main() -> int:
    docs = post("search", {"collection": "ada-ha-scenario-reports",
                           "query": "scenario report status",
                           "top_k": 400})
    if not isinstance(docs, list):
        docs = docs.get("results", [])
    latest = {}
    for d in docs:
        m = re.match(r"report/(.+)-(\d{8}-\d{6})", d.get("key") or "")
        if not m:
            continue
        name, ts = m.group(1), m.group(2)
        if name not in latest or ts > latest[name][0]:
            latest[name] = (ts, d)
    rows = []
    counts = {"pass": 0, "flaky": 0, "fail": 0}
    for name in sorted(latest):
        ts, d = latest[name]
        meta = d.get("meta") or {}
        status = "pass"
        body = d.get("contentMd") or ""
        sm = re.search(r"# .+ — (\w+)", body)
        if sm:
            status = sm.group(1)
        counts[status] = counts.get(status, 0) + 1
        turns = (meta.get("turns") or ["?"])[0]
        failed = ",".join(meta.get("failed_turns") or [])
        when = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}"
        rows.append(f"| {name} | {status} | {turns} | {failed or '-'} | {when} |")
    body = f"""# Lab results — scenario suite

Auto-rendered {datetime.now(timezone.utc).isoformat(timespec='seconds')}.
Latest per scenario · pass {counts.get('pass',0)} · flaky {counts.get('flaky',0)} · fail {counts.get('fail',0)}

| scenario | status | turns | failed turns | last run (UTC) |
|---|---|---|---|---|
{chr(10).join(rows)}

*Source collection: `ada-ha-scenario-reports` (14-day TTL).*
"""
    now = datetime.now(timezone.utc)
    # Report meta contract (ssot.apps.ada-cms-reports.yml) — summary/domain/
    # fresh_for/confidence/timeline/updated are required on every report page.
    meta = {"kind": ["page"], "slug": ["lab-results"],
            "title": ["Lab results — scenario suite"],
            "format": ["markdown"],
            "domain": ["bench"],
            "summary": [f"Scenario suite: pass {counts.get('pass', 0)} · "
                        f"flaky {counts.get('flaky', 0)} · "
                        f"fail {counts.get('fail', 0)} of {len(latest)}"],
            "fresh_for": ["25h"],
            "confidence": ["high"],
            "updated": [now.isoformat()],
            "timeline": [f"{now.isoformat(timespec='minutes')}: rendered "
                         f"{len(latest)} scenarios"],
            "instance": ["ada"]}
    check = validate_report_meta(meta)
    if not check["ok"]:
        print(f"meta contract violation, not publishing: {check['missing']}",
              file=sys.stderr)
        return 1
    for w in check["warnings"]:
        print(f"meta warning: {w}", file=sys.stderr)
    payload = {"collection": "ada-cms-pages", "key": "lab-results", "lang": "en",
               "contentMd": body, "meta": meta}
    print("cms:", post("add", payload).get("key"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
