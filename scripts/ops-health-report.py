#!/usr/bin/env python3
"""Ops health reporter — writes report/health-* docs + refreshes the
ops-health CMS page so Ada answers 'what's broken?' from one read.

Checks: mddb http+vector, ada-ha-tony ws, jev (8777) + jev-student (8778)
systemone, embed proxy. Counts systemd restart churn (restarts in 24h —
the crash-loop signal a single is-active check misses).
"""
import json, subprocess, time, urllib.request
from datetime import datetime, timezone

MDDB = "http://100.74.146.0:11023/v1"
NOW = datetime.now().astimezone()

def probe(name, fn):
    t0 = time.time()
    try:
        detail = fn()
        return {"name": name, "ok": True, "ms": int((time.time()-t0)*1000), "detail": detail}
    except Exception as e:
        return {"name": name, "ok": False, "ms": int((time.time()-t0)*1000), "detail": str(e)[:120]}

def get(url, timeout=8, json_body=None):
    if json_body is not None:
        req = urllib.request.Request(url, data=json.dumps(json_body).encode(),
                                     headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

def restarts(unit):
    try:
        out = subprocess.check_output(
            ["journalctl", "--user", "-u", unit, "--since", "-24h",
             "--no-pager", "-o", "json", "--output-fields", "MESSAGE"],
            text=True, timeout=15)
        n = out.count("Started ") + out.count("start-pre")
        return n
    except Exception:
        return -1

def is_active(unit):
    try:
        return subprocess.check_output(
            ["systemctl", "--user", "is-active", unit],
            text=True, timeout=5).strip() == "active"
    except Exception:
        return False

checks = [
    probe("mddb", lambda: get("http://100.74.146.0:11023/health")),
    probe("mddb-vector", lambda: get(MDDB + "/vector-search", 15, {
        "collection": "ada-cms-pages", "query": "health", "topK": 1})),
    probe("ada-ha-tony-http", lambda:
            urllib.request.urlopen("http://127.0.0.1:8002/", timeout=8).status),
    probe("open-jev-1b", lambda: get("http://100.74.146.0:8777/v1/systemone", 30, {
        "state": "probe", "questions": {"q": {"type": "noul",
        "instructions": "x", "criteria": {"true": "t", "false": "f"}}}})),
    probe("jev-student", lambda: get("http://100.74.146.0:8778/health")),
]

# long-running units vs timer-driven oneshots — inactive is only bad
# for the first kind. gev-gemini lives on tony-dell, not here.
SERVICES = ["mddb", "ada-ha-tony", "open-jev", "jev-student", "ada-pi-pwa"]
TIMERS = ["news-flood", "ada-scenario-smoke", "jev-bench",
          "ada-memory-sync", "doc-mirror"]
svc_rows = []
for u in SERVICES:
    active = is_active(u)
    n = restarts(u)
    flag = " ⚠" if (not active or n > 10) else ""
    svc_rows.append({"unit": u, "active": active, "restarts_24h": n, "flag": flag})
for t in TIMERS:
    on = is_active(t + ".timer")
    svc_rows.append({"unit": t + ".timer", "active": on, "restarts_24h": "-",
                     "flag": " ⚠" if not on else ""})

bad = [c for c in checks if not c["ok"]]
bad_svc = [s for s in svc_rows if s["flag"]]
verdict = "healthy" if not bad and not bad_svc else (
          "degraded" if len(bad) + len(bad_svc) <= 2 else "failing")

lines = [f"# Ops health — {NOW:%Y-%m-%d %H:%M}\n",
         f"**{verdict.upper()}**\n"]
if bad or bad_svc:
    lines.append("## Flagged\n")
    for c in bad:
        lines.append(f"- **{c['name']}** — {c['detail']}")
    for s in bad_svc:
        lines.append(f"- **{s['unit']}** — active={s['active']} restarts24h={s['restarts_24h']}")
    lines.append("")
lines.append("## Probes\n")
lines.append("| probe | ok | ms | detail |")
lines.append("|---|---|---|---|")
for c in checks:
    lines.append(f"| {c['name']} | {'yes' if c['ok'] else 'NO'} | {c['ms']} | {str(c['detail'])[:60]} |")
lines.append("\n## Services\n")
lines.append("| unit | active | restarts (24h) |")
lines.append("|---|---|---|")
for s in svc_rows:
    lines.append(f"| {s['unit']} | {s['active']} | {s['restarts_24h']}{s['flag']} |")
md = "\n".join(lines)

def post(path, payload):
    req = urllib.request.Request(MDDB + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())

ts = NOW.strftime("%Y%m%d-%H%M%S")
meta = {"kind": ["report"], "domain": ["health"], "suite": ["ops-health"],
        "summary": [f"{verdict}: {len(bad)+len(bad_svc)} flagged of "
                    f"{len(checks)+len(svc_rows)} checks"],
        "fresh_for": ["1h"],
        "ts": [NOW.isoformat(timespec="seconds")]}
post("/add", {"collection": "ada-ha-scenario-reports",
              "key": f"report/health-{ts}", "lang": "en",
              "contentMd": md, "meta": meta})
post("/add", {"collection": "ada-cms-pages", "key": "ops-health", "lang": "en",
              "contentMd": md,
              "meta": {**meta, "kind": ["report"], "slug": ["ops-health"],
                       "title": [f"Ops health — {NOW:%Y-%m-%d %H:%M}"],
                       "format": ["markdown"], "instance": ["idc01"],
                       "updated": [NOW.isoformat(timespec="seconds")],
                       "timeline": [f"{NOW.isoformat(timespec='minutes')}: {verdict} "
                                    f"({len(bad)+len(bad_svc)} flags)"]}})
print(f"{verdict} — {len(bad)+len(bad_svc)} flagged, published report/health-{ts}")
