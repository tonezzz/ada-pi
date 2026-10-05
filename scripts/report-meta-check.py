#!/usr/bin/env python3
"""Report meta contract audit — validate every ada-cms-pages doc against
meta_contract (ssot.apps.ada-cms-reports.yml): summary (~240 chars),
domain, fresh_for, confidence, timeline, updated.

Prints one line per non-conformant page plus a summary; missing required
fields exit 1 (warnings alone don't fail). Read-only — safe against any
MDDB follower.

Usage: report-meta-check.py [--mddb URL] [--all-kinds]

Env: MDDB_BASE_URL (default http://127.0.0.1:11023/v1),
     ADA_CMS_COLLECTION (default ada-cms-pages).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.report_meta import validate_report_meta  # noqa: E402


def _post(url: str, payload: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _first(meta: dict, name: str) -> str:
    v = (meta or {}).get(name)
    if isinstance(v, list) and v:
        return str(v[0])
    return str(v) if isinstance(v, str) else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mddb", default=os.environ.get(
        "MDDB_BASE_URL", "http://127.0.0.1:11023/v1"))
    ap.add_argument("--collection", default=os.environ.get(
        "ADA_CMS_COLLECTION", "ada-cms-pages"))
    ap.add_argument("--all-kinds", action="store_true",
                    help="audit every doc, not just kind page|report")
    args = ap.parse_args()

    try:
        res = _post(f"{args.mddb.rstrip('/')}/search",
                    {"collection": args.collection, "limit": 400})
    except Exception as exc:
        print(f"mddb unreachable at {args.mddb}: {exc}", file=sys.stderr)
        return 2
    docs = res if isinstance(res, list) else (res.get("documents")
                                              or res.get("results") or [])
    n_ok = n_warn = n_bad = 0
    for d in docs:
        meta = d.get("meta") or {}
        slug = _first(meta, "slug") or d.get("key") or "?"
        if not args.all_kinds and _first(meta, "kind") not in ("page", "report"):
            continue
        check = validate_report_meta(meta)
        if check["ok"] and not check["warnings"]:
            n_ok += 1
            continue
        if not check["ok"]:
            n_bad += 1
            print(f"FAIL {slug}: missing {', '.join(check['missing'])}")
        else:
            n_warn += 1
        for w in check["warnings"]:
            print(f"  warn {slug}: {w}")
    print(f"{len(docs)} docs scanned — {n_ok} conformant, "
          f"{n_warn} warnings-only, {n_bad} missing required fields")
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
