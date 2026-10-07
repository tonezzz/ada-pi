#!/usr/bin/env python3
"""Seed miner for the report-opinion bench domain (chaba
ssot.nest-bench.yml, card report-loop-bench, design
docs/design/report-session-loop.md §4b).

Scans report-linked kanban cards (cards carrying a `report:` field) for
from=ada comms tagged [opinion] and appends them as corpus rows to
tests/bench/orch-report-opinion-cases.jsonl — the orch-bench case-file
convention. Rows carry kind: report-opinion; labels (proven_right,
category) stay null — they get filled when a scorer lands, per the
domain rule that a scorer with no corpus is dead code. Mining now means
the corpus exists before the scorer does.

  report-opinion-mine.py [--board URL] [--mddb URL] [--out PATH]
                         [--snapshot] [--dry-run]

--board  defaults to $ADA_BOARD_API_URL or the production board-api
         (same default ada_board_write.py uses).
--mddb   defaults to $MDDB_BASE_URL or http://127.0.0.1:11023/v1 —
         used only with --snapshot.
--snapshot  fetch the current ada-cms-pages doc for each card's report
         slug into report_snapshot. NOTE: the snapshot is the doc as it
         is NOW, not as it was at opinion time — good enough for a seed
         corpus; the domain's real snapshots accrue live.
Dedupe: a (card, at) pair already present in the out file is skipped.
Stdlib-only; safe to run from cron or by hand.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

DEFAULT_BOARD = "https://tony-dell.taila0626a.ts.net/apps/board-api"
DEFAULT_MDDB = "http://127.0.0.1:11023/v1"
DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "orch-report-opinion-cases.jsonl")

_OPINION_RE = re.compile(r"^\s*\[opinion\]", re.I)


def _post(url: str, payload: dict, timeout: float = 15.0):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get(url: str, timeout: float = 15.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def _snapshot(mddb: str, slug: str) -> dict | None:
    """Current ada-cms-pages doc for the slug — best effort."""
    try:
        doc = _post(f"{mddb.rstrip('/')}/get",
                    {"collection": "ada-cms-pages", "key": slug,
                     "lang": "en"})
    except Exception:
        return None
    if not isinstance(doc, dict) or not doc.get("contentMd"):
        return None
    return {"key": slug,
            "title": doc.get("title") or "",
            "updated": doc.get("updated") or "",
            "body": doc.get("contentMd") or ""}


def _seen_keys(path: str) -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                seen.add((str(row.get("card")), str(row.get("at"))))
    except OSError:
        pass
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board",
                    default=os.environ.get("ADA_BOARD_API_URL")
                    or DEFAULT_BOARD)
    ap.add_argument("--mddb",
                    default=os.environ.get("MDDB_BASE_URL")
                    or DEFAULT_MDDB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        data = _get(f"{args.board.rstrip('/')}/cards")
    except Exception as exc:
        print(f"board unreachable: {exc}", file=sys.stderr)
        return 2
    cards = data.get("cards") or []

    seen = _seen_keys(args.out)
    rows: list[dict] = []
    for card in cards:
        slug = str(card.get("report") or "").strip()
        if not slug:
            continue
        snap = None
        for comm in card.get("comms") or []:
            if str(comm.get("from") or "") != "ada":
                continue
            text = str(comm.get("text") or "")
            if not _OPINION_RE.match(text):
                continue
            key = (str(card.get("id")), str(comm.get("at")))
            if key in seen:
                continue
            if args.snapshot and snap is None:
                snap = _snapshot(args.mddb, slug)
            rows.append({
                "kind": "report-opinion",
                "card": card.get("id"),
                "report": slug,
                "at": comm.get("at"),
                "opinion": _OPINION_RE.sub("", text).strip(),
                "report_snapshot": snap,
                # labels — filled when the scorer lands (dead-code rule)
                "proven_right": None,   # acted-on | ignored | contradicted
                "category": None,       # spot-on | partially-useful |
                                        # off-base | evidence-missing
            })

    if not rows:
        print("no new [opinion] comms on report-linked cards")
        return 0
    for row in rows:
        print(f"  {row['at']} {row['card']} (report={row['report']}): "
              f"{row['opinion'][:80]}")
    if args.dry_run:
        print(f"dry-run: would append {len(rows)} row(s) to {args.out}")
        return 0
    with open(args.out, "a") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"appended {len(rows)} row(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
