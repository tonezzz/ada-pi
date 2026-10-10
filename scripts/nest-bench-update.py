#!/usr/bin/env python3
"""nest-bench-update.py — Nest topology trend -> CMS `nest-bench`.

Reads every kind:benchmark doc keyed bench/orch-* from
ada-ha-scenario-reports, renders a managed block:

  - latest run scorecard (structures × acc/cost/ECE)
  - accuracy trend: run-date x structure (the 'are cascades learning'
    question over time)
  - cost trend: cpu-sec/call + heavy-call fraction per structure
  - corpus growth + topology registry digest (topologies.yml)

Auto-update contract identical to the other ada cms updaters:
ada-cms-automation registry interval gate, managed block, meta.

Env:
    MDDB_BASE_URL   default http://100.102.134.91:11023/v1
    REPO            default = this checkout root

Usage:
    nest-bench-update.py            # gated by registry interval
    nest-bench-update.py --force    # run regardless
    nest-bench-update.py --dry-run  # render + print, write nothing
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(os.environ.get("REPO") or Path(__file__).resolve().parents[1])
TOPOLOGIES = REPO / "tests/bench/topologies.yml"
MDDB = os.environ.get(
    "MDDB_BASE_URL", "http://100.102.134.91:11023/v1").rstrip("/")
COLLECTION = "ada-cms-pages"
REGISTRY = "ada-cms-automation"
REPORTS = "ada-ha-scenario-reports"
PAGE = "nest-bench"
BLOCK_BEGIN = "<!-- nest-bench:auto -->"
BLOCK_END = "<!-- /nest-bench:auto -->"
BLOCK_RE = re.compile(re.escape(BLOCK_BEGIN) + r".*?" +
                      re.escape(BLOCK_END), re.S)
ICT = timezone(timedelta(hours=7))
SOURCES = ["ada-ha-scenario-reports bench/orch-*",
           "tests/bench/topologies.yml"]

ROW_RE = re.compile(
    r"^\|\s*([A-Za-z0-9_-]+)\s*\|\s*([^|]+?)\s*\|\s*([0-9.]+)s\s*\|"
    r"\s*([0-9.]+)s\s*\|\s*([0-9.]+)\s*\|\s*([0-9]+)%\s*\|\s*([0-9.]+)\s*\|")


# ---------- MDDB ----------

def _post(path, payload, timeout=60):
    req = urllib.request.Request(
        f"{MDDB}/{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _search_all(collection, page_size=500):
    docs, offset = [], 0
    while True:
        page = _post("search", {"collection": collection, "query": "",
                                "limit": page_size, "offset": offset})
        docs += page
        if len(page) < page_size:
            return docs
        offset += page_size


def get_page(lang):
    docs = _search_all(COLLECTION)
    return next((d for d in docs
                 if d.get("key") == PAGE and d.get("lang") == lang), None)


# ---------- parse runs ----------

def parse_runs(docs):
    """bench/orch-* docs -> [{ts, rows:{struct:{acc,p50,p95,cpu,heavy,ece}}}]"""
    runs = []
    for d in docs:
        key = d.get("key") or ""
        if not key.startswith("bench/orch-"):
            continue
        meta = d.get("meta") or {}
        ts = (meta.get("updated") or [d.get("updated_at") or ""])[0]
        rows = {}
        for ln in (d.get("contentMd") or "").splitlines():
            m = ROW_RE.match(ln)
            if m:
                name, acc, p50, p95, cpu, heavy, ece = m.groups()
                rows[name] = {"acc": acc.strip(), "p50": float(p50),
                              "p95": float(p95), "cpu": float(cpu),
                              "heavy": int(heavy), "ece": float(ece)}
        if rows:
            runs.append({"ts": ts, "key": key, "rows": rows})
    runs.sort(key=lambda r: r["ts"])
    return runs


def topo_digest():
    try:
        import yaml
        d = yaml.safe_load(TOPOLOGIES.read_text())
        return d.get("topologies") or {}
    except Exception:
        return {}


# ---------- render ----------

def render(runs, topos, now):
    lines = [BLOCK_BEGIN, "",
             f"_updated {now.astimezone(ICT):%Y-%m-%d %H:%M} ICT — "
             f"{len(runs)} bench runs on record_", ""]
    if not runs:
        lines += ["_no bench/orch-* docs yet_", "", BLOCK_END]
        return "\n".join(lines)

    latest = runs[-1]
    lines += [f"### Latest — `{latest['key']}`", "",
              "| structure | acc | p50 | p95 | cpu/call | 4b% | ECE |",
              "|---|---|---|---|---|---|---|"]
    for name, r in latest["rows"].items():
        lines.append(f"| {name} | {r['acc']} | {r['p50']}s | {r['p95']}s | "
                     f"{r['cpu']} | {r['heavy']}% | {r['ece']} |")

    structs = sorted({s for r in runs for s in r["rows"]})
    lines += ["", "### Accuracy trend", "",
              "| run |" + "|".join(structs) + "|",
              "|---|" + "|".join(["---"] * len(structs)) + "|"]
    for r in runs[-12:]:
        cells = [r["rows"].get(s, {}).get("acc", "—") for s in structs]
        lines.append(f"| {r['ts'][:10]} |" + "|".join(cells) + "|")

    lines += ["", "### Cost trend (cpu-s/call · heavy%)", "",
              "| run |" + "|".join(structs) + "|",
              "|---|" + "|".join(["---"] * len(structs)) + "|"]
    for r in runs[-12:]:
        cells = [
            (f"{x['cpu']}·{x['heavy']}%"
             if (x := r["rows"].get(s)) else "—")
            for s in structs]
        lines.append(f"| {r['ts'][:10]} |" + "|".join(cells) + "|")

    lines += ["", "### Topology registry", "",
              "| name | way | nodes | status |", "|---|---|---|---|"]
    for name, t in topos.items():
        nodes = t.get("nodes") or []
        chain = " → ".join(f"{n.get('level','?')}/{n.get('role','?')}"
                           for n in nodes)
        status = ("benchable" if t.get("impl") in ("builtin", "chain")
                  else "design")
        lines.append(f"| {name} | {t.get('way','-')} | {chain} | {status} |")
    lines += ["", BLOCK_END]
    return "\n".join(lines)


def upsert_block(body, block):
    if BLOCK_RE.search(body or ""):
        return BLOCK_RE.sub(lambda m: block, body, count=1)
    m = re.search(r"^## ", body or "", re.M)
    if m:
        return body[:m.start()] + block + "\n\n" + body[m.start():]
    return (body or "").rstrip() + "\n\n" + block + "\n"


def page_body(lang, block):
    doc = get_page(lang)
    if doc and doc.get("contentMd"):
        return upsert_block(doc["contentMd"], block)
    return ("# Nest bench — collective-intelligence scoreboard\n\n"
            "Auto-updated by `nest-bench-update.py`. Compound-AI topologies "
            "compared on the same confirm-gate case sets — the iteration "
            "loop is corpus-in/corpus-out: misses feed the next run. "
            "Trend rows show whether structure changes earn their cost.\n\n"
            + block + "\n")


def publish(lang, body, now):
    doc = get_page(lang) or {}
    meta = {k: (v if isinstance(v, list) else [str(v)])
            for k, v in (doc.get("meta") or {}).items()}
    meta.update({
        "updated": [now.isoformat(timespec="seconds")],
        "last_verified": [now.date().isoformat()],
        "lang": [lang], "kind": ["report"], "attribute": ["report"],
        "report_role": ["rollup"], "domain": ["bench"],
        "generated_by": ["nest-bench-update.py"], "sources": SOURCES,
        "title": ["Nest bench — topology scoreboard"],
        "summary": ["Compound-AI topology trends — accuracy, cost, ECE "
                    "across orch-bench runs."],
        "fresh_for": ["7d"], "confidence": ["high"],
        "timeline": [f"{now.isoformat(timespec='minutes')}: trend refresh"],
        "written_by": ["nest-bench-update"],
    })
    _post("add", {"collection": COLLECTION, "key": PAGE, "lang": lang,
                  "contentMd": body, "meta": meta}, timeout=120)


def save_registry(cfg, now):
    meta = {"kind": ["automation-config"], "bank": ["cms"],
            "scope": ["tony"], "status": ["active"], "source": ["api"],
            "written_by": ["nest-bench-update"], "subject": [PAGE],
            "attribute": ["automation"], "slug": [PAGE],
            "title": [f"CMS automation: {PAGE}"], "format": ["json"],
            "lang": ["en"], "updated": [now.isoformat(timespec="seconds")],
            "last_verified": [now.date().isoformat()]}
    _post("add", {"collection": REGISTRY, "key": PAGE, "lang": "en",
                  "contentMd": json.dumps(cfg, ensure_ascii=False,
                                          indent=2), "meta": meta},
          timeout=120)


def load_registry():
    try:
        docs = _search_all(REGISTRY)
    except Exception as e:
        print(f"warn: registry unreachable ({e}) — running anyway",
              file=sys.stderr)
        return {}, False
    for d in docs:
        if d.get("key") == PAGE:
            try:
                return json.loads(d.get("contentMd") or "{}"), True
            except json.JSONDecodeError:
                return {}, True
    return {}, True


def gated(cfg, now, force):
    if not cfg.get("enabled", True):
        return "disabled"
    if force or cfg.get("run_now"):
        return None
    interval = int(cfg.get("interval_min") or 0)
    last = cfg.get("last_run")
    if interval and last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if now < last_dt + timedelta(minutes=interval, seconds=-30):
                return f"interval (last run {last})"
        except ValueError:
            pass
    return None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    if not args.dry_run:
        cfg, online = load_registry()
        why = gated(cfg, now, args.force)
        if why:
            print(f"nest-bench: skipped ({why})")
            return 0
    docs = _search_all(REPORTS)
    runs = parse_runs(docs)
    block = render(runs, topo_digest(), now)
    if args.dry_run:
        print(block)
        return 0
    publish("en", page_body("en", block), now)
    cfg = dict(cfg)
    cfg.update({"last_run": now.isoformat(timespec="seconds"),
                "run_now": False, "last_runs": len(runs)})
    save_registry(cfg, now)
    print(f"nest-bench: published ({len(runs)} runs tracked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
