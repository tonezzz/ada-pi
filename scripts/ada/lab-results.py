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

MDDB = os.environ.get("MDDB_BASE_URL", "http://127.0.0.1:11023/v1")


def post(ep, payload):
    req = urllib.request.Request(f"{MDDB}/{ep}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


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
    payload = {"collection": "ada-cms-pages", "key": "lab-results", "lang": "en",
               "contentMd": body,
               "meta": {"kind": ["page"], "slug": ["lab-results"],
                        "title": ["Lab results — scenario suite"],
                        "format": ["markdown"],
                        "updated": [datetime.now(timezone.utc).isoformat()],
                        "instance": ["ada"]}}
    print("cms:", post("add", payload).get("key"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
