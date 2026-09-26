#!/usr/bin/env python3
"""Daily rollup (L2) over L1 session reports and run records — the drill-
down layer that links a day's activity to its raw data.

Reads `reports/<date>-*.json` (session reports written at session end) and
`runs/*.json` (benchmark/scenario run records from identify_eval.py,
scenario-live.py --json-out, etc.), then writes
`runs/daily/<date>.json` and optionally an MDDB doc in the reports bank so
the rollup is reachable by ada_memory_search and the mddb MCP server.

Escalation rules (what reaches the top): every session event kind in
ESCALATE_KINDS, plus any escalations a lower run already computed — they
propagate verbatim with their refs intact.

Usage:
  python3 scripts/report-rollup.py --date 2026-09-26
  python3 scripts/report-rollup.py --date 2026-09-26 --mddb --instance tony
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ADA_DATA_DIR = Path(os.environ.get(
    "ADA_TRANSCRIPT_DIR", os.path.expanduser("~/.local/share/ada/transcripts"))
).parent

# Event kinds that carry security/ops significance — presence alone is
# reportable upward (the ref survives into the rollup for drill-down).
ESCALATE_KINDS = {"tool_denied", "secondary_speaker", "speaker_unrecognized",
                  "barge_noise", "reconnect"}


def _percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, round((pct / 100) * (len(s) - 1))))
    return s[k]


def _session_row(path: Path) -> dict:
    rpt = json.loads(path.read_text(encoding="utf-8"))
    sid = path.stem.split("-", 3)[-1] if len(path.stem.split("-")) > 3 else path.stem
    events = rpt.get("session_events") or []
    kinds: dict[str, int] = {}
    for ev in events:
        kinds[ev.get("kind", "?")] = kinds.get(ev.get("kind", "?"), 0) + 1
    owner = next(
        (ev.get("owner") or ev.get("identity") for ev in events
         if ev.get("kind") == "connect"), None)
    ttft = [ev["ttft_ms"] for ev in events
            if ev.get("kind") == "turn_latency"
            and isinstance(ev.get("ttft_ms"), int)]
    escalations = [
        {"kind": ev.get("kind"), "turn": ev.get("turn"),
         "ref": f"report:{path.stem}#ev{events.index(ev)}"}
        for ev in events if ev.get("kind") in ESCALATE_KINDS]
    return {
        "ref": f"report:{path.stem}",
        "transcript_ref": f"transcript:{path.stem}",
        "owner": owner,
        "summary": rpt.get("summary") or rpt.get("focus") or "",
        "turns": rpt.get("turns"),
        "event_kinds": kinds,
        "ttft_p50_ms": _percentile(ttft, 50),
        "ttft_p95_ms": _percentile(ttft, 95),
        "escalations": escalations,
    }


def _run_row(path: Path) -> dict:
    run = json.loads(path.read_text(encoding="utf-8"))
    return {
        "ref": run.get("ref") or f"run:{path.stem}",
        "kind": run.get("kind"), "tool": run.get("tool"),
        "ts": run.get("ts") or "",
        "summary": run.get("summary") or "",
        "metrics": run.get("metrics") or {},
        "escalations": run.get("escalations") or [],
    }


async def _write_mddb(rollup: dict, date: str, instance: str) -> str | None:
    try:
        from backend.mddb_client import MddbClient
        client = MddbClient()
        collection = f"ada-ha-reports-{instance}"
        key = f"rollup/daily/{date}"
        body = (
            f"# Daily rollup {date}\n\n"
            f"{rollup['summary']}\n\n"
            f"## Sessions\n" + "\n".join(
                f"- `{s['ref']}` owner={s.get('owner') or '-'} — {s['summary']}"
                for s in rollup["sessions"]) +
            "\n\n## Escalations\n" + (
                "\n".join(f"- {e}" for e in rollup["escalations"])
                if rollup["escalations"] else "- none"))
        res = await client.add_document(
            collection=collection, key=key, lang="en", content_md=body,
            meta={"kind": ["rollup"], "level": ["daily"], "date": [date],
                  "ref": [rollup["ref"]], "source": ["report-rollup"]})
        return f"mddb://{collection}/{key}" if res is not None else None
    except Exception as exc:
        print(f"mddb write failed: {exc}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat())
    ap.add_argument("--reports-dir", type=Path, default=ADA_DATA_DIR / "reports")
    ap.add_argument("--runs-dir", type=Path, default=ADA_DATA_DIR / "runs")
    ap.add_argument("--out", type=Path, default=None,
                    help="default: <runs-dir>/daily/<date>.json")
    ap.add_argument("--mddb", action="store_true", help="also write to MDDB")
    ap.add_argument("--instance", default=os.environ.get("ADA_INSTANCE_ID", "ada"))
    args = ap.parse_args()

    sessions, runs = [], []
    if args.reports_dir.is_dir():
        for p in sorted(args.reports_dir.glob(f"{args.date}-*.json")):
            try:
                sessions.append(_session_row(p))
            except Exception as exc:
                print(f"skip {p.name}: {exc}", file=sys.stderr)
    if args.runs_dir.is_dir():
        for p in sorted(args.runs_dir.glob("*.json")):
            try:
                row = _run_row(p)
                if args.date in row["ref"] or row["ts"].startswith(args.date):
                    runs.append(row)
            except Exception as exc:
                print(f"skip {p.name}: {exc}", file=sys.stderr)

    escalations = [dict(e, session=s["ref"]) for s in sessions
                   for e in s["escalations"]]
    escalations += [dict(e, run=r["ref"]) for r in runs
                    for e in r["escalations"]]

    rollup = {
        "ref": f"run:rollup/{args.date}",
        "kind": "rollup", "level": "daily", "date": args.date,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sessions": sessions, "runs": runs,
        "totals": {"sessions": len(sessions), "runs": len(runs),
                   "escalations": len(escalations)},
        "escalations": escalations,
        "summary": (f"{args.date}: {len(sessions)} sessions, {len(runs)} runs, "
                    f"{len(escalations)} escalations"),
    }
    out = args.out or args.runs_dir / "daily" / f"{args.date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rollup, ensure_ascii=False, indent=2) + "\n")
    print(f"{rollup['summary']} -> {out}")
    for e in escalations:
        print(f"  escalate: {e}")

    if args.mddb:
        uri = asyncio.run(_write_mddb(rollup, args.date, args.instance))
        print(f"mddb: {uri or 'FAILED'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
