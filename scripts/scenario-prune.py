#!/usr/bin/env python3
"""Prune ada-ha-scenario-reports per the retention policy in
tests/benchmark.yml 'standard.retention'.

New docs carry a real mddb TTL set at write time (scenario-report.py,
scenario-benchmark.py) — this script is the backstop for docs written
before TTLs existed, plus dedupe and validity stamping that TTL can't do:

  report/<scenario>-<ts>     delete past per-status age cap; within a
                             consecutive run of identical statuses keep
                             only first + last (the rest are duplicates
                             of the same observation)
  benchmark/<suite>-<ts>     never deleted (trend data); stamps
                             meta.valid=false on legacy runs where >50%
                             of scenarios were infra/quota — those runs
                             measured the environment, not Ada
  auto-report/<suite>-<ts>   delete older than 30d

Usage:
  scenario-prune.py [--mddb URL] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import urllib.request

COLLECTION = "ada-ha-scenario-reports"
UNSCORED = {"infra", "unimplemented", "quota", "skip-quota"}

STATUS_AGE_CAP_DAYS = {
    "pass": 3, "flaky": 7, "fail": 30,
    "infra": 3, "quota": 3, "skip-quota": 3,
    "skip": 3, "unimplemented": 3,
}
AUTO_REPORT_AGE_CAP_DAYS = 30

KEY_TS_RE = re.compile(r"-(\d{8}-\d{6})$")


def _req(mddb: str, path: str, payload: dict, method: str = "POST") -> dict | None:
    req = urllib.request.Request(
        f"{mddb.rstrip('/')}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method=method)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception as exc:
        print(f"  {path} failed: {exc}")
        return None


def _key_ts(key: str) -> datetime.datetime | None:
    m = KEY_TS_RE.search(key)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def _meta_status(doc: dict) -> str:
    v = (doc.get("meta") or {}).get("status") or []
    return str(v[0]) if v else ""


def _scenario_of(key: str) -> str:
    return KEY_TS_RE.sub("", key.split("/", 1)[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mddb", default=os.environ.get("MDDB_BASE_URL")
                    or "http://127.0.0.1:11023/v1")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    docs = _req(args.mddb, "/search",
                {"collection": COLLECTION, "query": "", "limit": 10000})
    if not isinstance(docs, list):
        print("search returned no docs")
        return 1

    now = datetime.datetime.now()
    to_delete: list[tuple[str, str]] = []   # (key, reason)
    to_stamp: list[tuple[str, dict]] = []   # (key, meta patch)

    # --- report/* : age caps + consecutive-duplicate dedupe ---
    reports = [d for d in docs if d.get("key", "").startswith("report/")]
    by_scenario: dict[str, list[dict]] = {}
    for d in reports:
        by_scenario.setdefault(_scenario_of(d["key"]), []).append(d)

    for scenario, group in by_scenario.items():
        group.sort(key=lambda d: d["key"])          # ts-suffixed keys sort
        # age cap
        for d in group:
            ts = _key_ts(d["key"])
            cap = STATUS_AGE_CAP_DAYS.get(_meta_status(d), 3)
            if ts and (now - ts).days > cap:
                to_delete.append(
                    (d["key"], f"older than {cap}d cap for status "
                               f"{_meta_status(d) or '?'}"))
        # dedupe consecutive identical statuses — keep first + last of a run
        run_start = 0
        for i in range(1, len(group) + 1):
            if (i == len(group)
                    or _meta_status(group[i]) != _meta_status(group[run_start])):
                run = group[run_start:i]
                for dup in run[1:-1]:
                    to_delete.append(
                        (dup["key"], f"consecutive duplicate "
                                     f"{_meta_status(dup) or '?'} "
                                     f"of {scenario}"))
                run_start = i

    # --- auto-report/* : 30d cap ---
    for d in docs:
        if d.get("key", "").startswith("auto-report/"):
            ts = _key_ts(d["key"])
            if ts and (now - ts).days > AUTO_REPORT_AGE_CAP_DAYS:
                to_delete.append((d["key"], "auto-report older than 30d"))

    # --- benchmark/* : stamp validity on docs that predate the flag ---
    for d in docs:
        if not d.get("key", "").startswith("benchmark/"):
            continue
        meta = d.get("meta") or {}
        if meta.get("valid"):
            continue                          # already stamped
        statuses = [str(s).rpartition(":")[-1]
                    for s in (meta.get("scenarios") or [])]
        if not statuses:
            continue
        n_unscored = sum(1 for s in statuses if s in UNSCORED)
        valid = n_unscored / len(statuses) <= 0.5
        patch = {"valid": [str(valid).lower()]}
        if not valid:
            patch["invalid_reason"] = [
                f"{n_unscored}/{len(statuses)} scenarios infra/quota "
                "(backfilled by scenario-prune.py)"]
        to_stamp.append((d["key"], patch))

    for key, reason in to_delete:
        print(f"delete  {key}  ({reason})")
        if not args.dry_run:
            _req(args.mddb, "/delete",
                 {"collection": COLLECTION, "key": key, "lang": "en"})
    for key, patch in to_stamp:
        print(f"stamp   {key}  {patch}")
        if not args.dry_run:
            _req(args.mddb, "/update",
                 {"collection": COLLECTION, "key": key, "lang": "en",
                  "meta": patch}, method="PATCH")

    print(f"\n{len(docs)} docs scanned: {len(to_delete)} to delete, "
          f"{len(to_stamp)} benchmarks stamped"
          + (" (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
