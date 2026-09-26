#!/usr/bin/env python3
"""Report rollup — L2 daily over session reports + run records, L3 weekly
over daily rollups. Links every level to raw data by stable refs.

Reads `reports/<date>-*.json` (session reports) and `runs/*.json`
(benchmark/scenario run records), writes `runs/daily/<date>.json` — or for
--level weekly, reads `runs/daily/<day>.json` for the ISO week and writes
`runs/weekly/<iso-week>.json`. Optional --mddb publishes to the reports
bank so rollups are reachable by ada_memory_search / the mddb MCP server.

Escalation rules (what reaches the top):
  - session events in ESCALATE_KINDS (denials, guest speakers, noise, …)
  - run records' own computed escalations, propagated verbatim with refs
  - baseline checks from --baselines (tests/baselines.yaml): day-level
    latency p95, speaker-ID accuracy/refusal, scenario failures, recall
    hit-rates.

Usage:
  python3 scripts/report-rollup.py --date 2026-09-26
  python3 scripts/report-rollup.py --date 2026-09-26 --level weekly
  python3 scripts/report-rollup.py --date 2026-09-26 --mddb --instance tony
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

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


def _baseline_escalations(day_metrics: dict, runs: list[dict],
                          baselines: dict) -> list[dict]:
    """Compare day aggregates + run metrics against audited baselines."""
    esc: list[dict] = []
    lat = baselines.get("latency") or {}
    cap = lat.get("ttft_p95_ms")
    mult = float(lat.get("ttft_p95_regress_x") or 1.0)
    if cap and isinstance(day_metrics.get("ttft_p95_ms"), int):
        if day_metrics["ttft_p95_ms"] > cap * mult:
            esc.append({"rule": "ttft_p95_regression",
                        "detail": f"day ttft p95 {day_metrics['ttft_p95_ms']}ms "
                                  f"> baseline {cap}ms × {mult}",
                        "refs": day_metrics.get("session_refs", [])})
    tcap = lat.get("tool_dur_p95_ms")
    if tcap and isinstance(day_metrics.get("tool_dur_p95_ms"), int):
        if day_metrics["tool_dur_p95_ms"] > tcap:
            esc.append({"rule": "tool_dur_p95_regression",
                        "detail": f"day tool p95 {day_metrics['tool_dur_p95_ms']}ms "
                                  f"> baseline {tcap}ms",
                        "refs": day_metrics.get("session_refs", [])})
    spk = baselines.get("speaker_id") or {}
    scn = baselines.get("scenario") or {}
    rec = baselines.get("recall") or {}
    for r in runs:
        m = r.get("metrics") or {}
        ref = r["ref"]
        if r.get("tool") == "identify_eval":
            if m.get("accuracy") is not None and \
                    m["accuracy"] < (spk.get("accuracy_min") or 0):
                esc.append({"rule": "speaker_id_accuracy", "ref": ref,
                            "detail": f"accuracy {m['accuracy']} < "
                                      f"{spk['accuracy_min']}"})
            if m.get("refusal_rate") is not None and \
                    m["refusal_rate"] > (spk.get("refusal_rate_max") or 1):
                esc.append({"rule": "speaker_id_refusal", "ref": ref,
                            "detail": f"refusal {m['refusal_rate']} > "
                                      f"{spk['refusal_rate_max']}"})
        if r.get("kind") == "scenario" and \
                (m.get("failures") or 0) > (scn.get("failures_max") or 0):
            esc.append({"rule": "scenario_failures", "ref": ref,
                        "detail": f"{m['failures']} scenario failures"})
        if r.get("tool") == "recall-bench":
            hr = m.get("mddb_hit_rate")
            if hr is not None and rec.get("mddb_hit_rate_min") is not None \
                    and hr < rec["mddb_hit_rate_min"]:
                esc.append({"rule": "recall_hit_rate", "ref": ref,
                            "detail": f"mddb hit_rate {hr} < "
                                      f"{rec['mddb_hit_rate_min']}"})
            p95 = m.get("summaries_p95_ms")
            if p95 is not None and rec.get("summaries_p95_ms") \
                    and p95 > rec["summaries_p95_ms"]:
                esc.append({"rule": "recall_slow", "ref": ref,
                            "detail": f"summaries p95 {p95}ms > "
                                      f"{rec['summaries_p95_ms']}ms"})
    return esc


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
    tool_durs = [ev["dur_ms"] for ev in events
                 if ev.get("kind") == "tool_call"
                 and isinstance(ev.get("dur_ms"), int)]
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
        "_ttft_all": ttft,          # pooled into day aggregates, stripped
        "_tool_dur_all": tool_durs,  # before write
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
        level = rollup.get("level", "daily")
        key = f"rollup/{level}/{date}"
        body = (
            f"# {level.title()} rollup {date}\n\n"
            f"{rollup['summary']}\n\n"
            f"## Sessions\n" + "\n".join(
                f"- `{s['ref']}` owner={s.get('owner') or '-'} — {s['summary']}"
                for s in rollup["sessions"]) +
            "\n\n## Escalations\n" + (
                "\n".join(f"- {e}" for e in rollup["escalations"])
                if rollup["escalations"] else "- none"))
        res = await client.add_document(
            collection=collection, key=key, lang="en", content_md=body,
            meta={"kind": ["rollup"], "level": [level], "date": [date],
                  "ref": [rollup["ref"]], "source": ["report-rollup"]})
        return f"mddb://{collection}/{key}" if res is not None else None
    except Exception as exc:
        print(f"mddb write failed: {exc}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat())
    ap.add_argument("--level", choices=["daily", "weekly"], default="daily")
    ap.add_argument("--baselines", type=Path,
                    default=Path(__file__).parent.parent / "tests" / "baselines.yaml",
                    help="audited thresholds; escalate_when deltas")
    ap.add_argument("--reports-dir", type=Path, default=ADA_DATA_DIR / "reports")
    ap.add_argument("--runs-dir", type=Path, default=ADA_DATA_DIR / "runs")
    ap.add_argument("--out", type=Path, default=None,
                    help="default: <runs-dir>/<level>/<date>.json")
    ap.add_argument("--mddb", action="store_true", help="also write to MDDB")
    ap.add_argument("--instance", default=os.environ.get("ADA_INSTANCE_ID", "ada"))
    args = ap.parse_args()

    if args.level == "weekly":
        return _weekly(args)

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

    # Day aggregates for baseline checks; strip the raw pools from output.
    all_ttft = [v for s in sessions for v in s.pop("_ttft_all", [])]
    all_tool = [v for s in sessions for v in s.pop("_tool_dur_all", [])]
    day_metrics = {"ttft_p95_ms": _percentile(all_ttft, 95),
                   "tool_dur_p95_ms": _percentile(all_tool, 95),
                   "session_refs": [s["ref"] for s in sessions]}
    if args.baselines.is_file():
        baselines = yaml.safe_load(args.baselines.read_text()) or {}
        escalations += _baseline_escalations(day_metrics, runs, baselines)

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


def _weekly(args) -> int:
    """L3 rollup over the ISO week containing --date, from daily L2 files."""
    d = datetime.fromisoformat(args.date).date()
    monday = d - timedelta(days=d.weekday())
    days = [monday + timedelta(days=i) for i in range(7)]
    iso_week = f"{monday.isocalendar().year}-W{monday.isocalendar().week:02d}"

    dailies = []
    for day in days:
        p = args.runs_dir / "daily" / f"{day.isoformat()}.json"
        if p.is_file():
            dailies.append(json.loads(p.read_text(encoding="utf-8")))

    sessions = [s for dly in dailies for s in dly.get("sessions", [])]
    runs = [r for dly in dailies for r in dly.get("runs", [])]
    escalations = [
        dict(e, via=f"run:rollup/{dly['date']}")
        for dly in dailies for e in dly.get("escalations", [])]

    rollup = {
        "ref": f"run:rollup/{iso_week}", "kind": "rollup",
        "level": "weekly", "week": iso_week,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "daily_refs": [dly["ref"] for dly in dailies],
        "sessions": sessions, "runs": runs,
        "totals": {"days": len(dailies), "sessions": len(sessions),
                   "runs": len(runs), "escalations": len(escalations)},
        "escalations": escalations,
        "summary": (f"{iso_week}: {len(dailies)} days, {len(sessions)} "
                    f"sessions, {len(runs)} runs, {len(escalations)} "
                    f"escalations"),
    }
    out = args.out or args.runs_dir / "weekly" / f"{iso_week}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rollup, ensure_ascii=False, indent=2) + "\n")
    print(f"{rollup['summary']} -> {out}")

    if args.mddb:
        uri = asyncio.run(_write_mddb(rollup, iso_week, args.instance))
        print(f"mddb: {uri or 'FAILED'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
