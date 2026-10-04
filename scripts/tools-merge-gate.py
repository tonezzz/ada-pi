#!/usr/bin/env python3
"""Merge-card close gate — a tools-merge-* kanban card may not sit in
done/closed until its family's regression scenario exists AND has a
passing report in MDDB ada-ha-scenario-reports.

The tool-consolidation contract (docs/assessments/
tool-consolidation-spec-2026-10-04.md + docs/ssot/ssot.tool-surface.yml)
binds each family to a scenario: `tool_merge_<family>`. This script is
the check; the enforcement call-sites live outside this repo:

  chaba board-api   — run `tools-merge-gate.py --card-id <id>` inside the
                      /action close path for tools-merge-* cards;
                      non-zero exit = refuse the move
  ssot-validate     — run `tools-merge-gate.py --cards-json cards.json`
                      in the audit stage (fetch via board-api GET /cards)

Usage:
  tools-merge-gate.py [--cards-json FILE | --cards-url URL] [--json]
  tools-merge-gate.py --card-id tools-merge-camera   # single-card check

Env: ADA_BOARD_API_URL (default http://127.0.0.1:8787), MDDB_BASE_URL
(default http://127.0.0.1:11023/v1), ADA_TOOL_SURFACE (SSOT path override).

Exit: 0 clean / 1 gate violation (a merge card closed without a passing
scenario) / 2 could not verify (board or MDDB unreachable — treat as
block, a gate that cannot see its evidence must not pass silently).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
REPORTS_COLLECTION = "ada-ha-scenario-reports"
PASSING = {"pass", "flaky"}       # flaky = passed on retry (scenario-report.py)
CLOSED_COLUMNS = {"done", "closed"}
WARN_COLUMNS = {"review"}


def _post(url: str, payload: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _get(url: str, timeout: int = 15) -> dict:
    return json.loads(urllib.request.urlopen(url, timeout=timeout).read())


def latest_report_status(mddb: str, scenario: str) -> tuple[str | None, str | None]:
    """(status, report_key) of the newest report doc for the scenario.
    status "unreachable" = MDDB call failed — a gate must not confuse
    'no evidence' with 'cannot see the evidence'."""
    try:
        docs = _post(f"{mddb.rstrip('/')}/search", {
            "collection": REPORTS_COLLECTION, "limit": 25,
            "filterMeta": {"subject": ["scenario-live"],
                           "scenario": [scenario]},
        })
    except Exception:
        return "unreachable", None
    if not isinstance(docs, list):
        docs = docs.get("documents") or docs.get("results") or []
    best = None
    for d in docs:
        key = str(d.get("key") or "")
        meta = d.get("meta") or {}
        status = (meta.get("status") or [None])[0]
        if status and (best is None or key > best[0]):
            best = (key, str(status))
    return (best[1], best[0]) if best else (None, None)


def scenario_name(repo: Path, stem: str, scen_dir: str) -> tuple[str, Path]:
    """The scenario's report identity = yaml `name:` field (hyphenated in
    this repo) or the file stem when absent."""
    path = repo / scen_dir / f"{stem}.yaml"
    if not path.is_file():
        return stem, path
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        spec = {}
    return str(spec.get("name") or stem), path


def load_cards(args) -> list[dict]:
    if args.cards_json:
        data = json.loads(Path(args.cards_json).read_text())
    else:
        data = _get(args.cards_url)
    if isinstance(data, dict):
        data = data.get("cards") or []
    return [c for c in data if isinstance(c, dict)]


def evaluate(cards: list[dict], repo: Path, mddb: str,
             only_card: str | None = None) -> dict:
    ssot_path = Path(os.environ.get(
        "ADA_TOOL_SURFACE", repo / "docs/ssot/ssot.tool-surface.yml"))
    ssot = yaml.safe_load(ssot_path.read_text(encoding="utf-8")) or {}
    scen_dir = str(ssot.get("scenario_dir") or "tests/scenarios-live")
    fam_by_card = {}
    for fam, cfg in (ssot.get("families") or {}).items():
        cfg = cfg or {}
        if cfg.get("merge_card"):
            fam_by_card[str(cfg["merge_card"])] = (fam, cfg)

    by_id = {str(c.get("id")): c for c in cards}
    merge_ids = sorted(set(fam_by_card) |
                       {i for i in by_id if i.startswith("tools-merge-")})
    if only_card:
        merge_ids = [only_card]
        fam_by_card.setdefault(only_card, (only_card.removeprefix(
            "tools-merge-"), {"scenario": f"tool_merge_{only_card.removeprefix('tools-merge-')}",
                              "canonical": None}))

    results, violations, warnings = [], [], []
    for cid in merge_ids:
        fam, cfg = fam_by_card.get(cid, (cid, {}))
        card = by_id.get(cid)
        column = str((card or {}).get("column") or "absent").lower()
        stem = str(cfg.get("scenario") or f"tool_merge_{fam}")
        rpt_name, scen_path = scenario_name(repo, stem, scen_dir)
        exists = scen_path.is_file()
        status, key = latest_report_status(mddb, rpt_name) if exists \
            else (None, None)
        ok = exists and status in PASSING
        entry = {"card": cid, "family": fam, "column": column,
                 "scenario": stem, "scenario_exists": exists,
                 "report_status": status, "report_key": key, "ok": ok}
        results.append(entry)
        if column in CLOSED_COLUMNS and not ok:
            why = ("scenario file missing" if not exists else
                   "report store unreachable — cannot verify"
                   if status == "unreachable" else
                   f"latest report is {status or 'absent'}, need pass/flaky")
            violations.append(
                f"{cid}: closed in '{column}' but family '{fam}' "
                f"regression scenario {stem} — {why}")
        elif column in WARN_COLUMNS and not ok:
            warnings.append(
                f"{cid}: in '{column}' — family '{fam}' scenario {stem} "
                "not yet passing; close will be blocked")
        if only_card and column not in CLOSED_COLUMNS:
            # A card-id check is the close-time call: evaluate as if the
            # card were moving to done regardless of its current column.
            if not ok:
                violations.append(
                    f"{cid}: cannot close — family '{fam}' scenario "
                    f"{stem}: " + ("file missing" if not exists else
                                   "report store unreachable"
                                   if status == "unreachable" else
                                   f"latest report {status or 'absent'}"))
    return {"results": results, "violations": violations,
            "warnings": warnings}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--cards-json", help="board-api /cards output file")
    src.add_argument("--cards-url",
                     default=os.environ.get("ADA_BOARD_API_URL",
                                            "http://127.0.0.1:8787")
                     + "/cards")
    ap.add_argument("--card-id", help="check one card's closability")
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--mddb", default=os.environ.get(
        "MDDB_BASE_URL", "http://127.0.0.1:11023/v1"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        cards = load_cards(args)
    except Exception as exc:
        print(f"cannot read board state: {exc}", file=sys.stderr)
        return 2

    rep = evaluate(cards, Path(args.repo), args.mddb, args.card_id)
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
    else:
        for r in rep["results"]:
            mark = "ok" if r["ok"] else (
                "FAIL" if r["column"] in CLOSED_COLUMNS else "--")
            print(f"{mark:4} {r['card']:24} {r['column']:8} "
                  f"{r['scenario']}: exists={r['scenario_exists']} "
                  f"report={r['report_status'] or 'none'}")
        for w in rep["warnings"]:
            print(f"warn {w}")
        for v in rep["violations"]:
            print(f"FAIL {v}")
        print(f"tools-merge-gate: {len(rep['violations'])} violation(s), "
              f"{len(rep['warnings'])} warning(s)")
    return 1 if rep["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
