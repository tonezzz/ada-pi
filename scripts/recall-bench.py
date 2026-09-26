#!/usr/bin/env python3
"""Devin-side recall benchmark: how fast/accurately can a new session
answer "what did we decide about X?"

Times each recall path per probe:
  - summaries : scan ~/.local/share/devin/cli/summaries/*.md locally
  - mddb      : vector_search over the reports/memory collection

Probe file (YAML):
  - q: "why can't ada view kk's persona"
    expect: "policy_identity"
  - q: "session security"
    expect: "secondary"

Usage:
  python3 scripts/recall-bench.py --probes tests/recall-probes.yaml \
      --collection ada-ha-reports-tony --json-out runs/recall-<date>.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SUMMARIES_DIR = os.path.expanduser("~/.local/share/devin/cli/summaries")


def _grep_summaries(query: str, expect: str, summaries_dir: Path) -> dict:
    """Local-only path: substring/term scan of continuation summaries."""
    terms = [t.lower() for t in query.split() if len(t) > 3]
    hits = 0
    expect_hit = False
    for p in sorted(summaries_dir.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if all(t in text for t in terms):
            hits += 1
        if expect.lower() in text:
            expect_hit = True
    return {"files_hit": hits, "expect_found": expect_hit}


async def _mddb_probe(collection: str, query: str, expect: str) -> dict:
    from backend.mddb_client import MddbClient
    res = await MddbClient().vector_search(collection, query, limit=5)
    if res is None:
        return {"hits": None, "expect_found": False, "error": "search failed"}
    found = any(expect.lower() in json.dumps(r).lower() for r in res)
    return {"hits": len(res), "expect_found": found}


async def _run(probes: list[dict], collection: str | None,
               summaries_dir: Path) -> dict:
    rows = []
    stats: dict[str, list[float]] = {}
    hit_rates: dict[str, list[bool]] = {}
    for probe in probes:
        q, expect = probe["q"], probe.get("expect", "")
        row: dict = {"q": q, "expect": expect, "paths": {}}
        t0 = time.monotonic()
        row["paths"]["summaries"] = _grep_summaries(q, expect, summaries_dir)
        row["paths"]["summaries"]["ms"] = int((time.monotonic() - t0) * 1000)
        if collection:
            t0 = time.monotonic()
            row["paths"]["mddb"] = await _mddb_probe(collection, q, expect)
            row["paths"]["mddb"]["ms"] = int((time.monotonic() - t0) * 1000)
        for path, r in row["paths"].items():
            stats.setdefault(path, []).append(r["ms"])
            hit_rates.setdefault(path, []).append(bool(r.get("expect_found")))
        rows.append(row)
        print(f"  {q[:50]!r}: " + "  ".join(
            f"{p}={r['ms']}ms hit={r.get('expect_found')}"
            for p, r in row["paths"].items()))
    metrics = {
        "probes": len(rows),
        **{f"{p}_p50_ms": sorted(v)[len(v) // 2] if v else None
           for p, v in stats.items()},
        **{f"{p}_hit_rate": round(sum(h) / len(h), 3) if h else None
           for p, h in hit_rates.items()},
    }
    return {
        "ref": f"run:bench/{datetime.now(timezone.utc).date().isoformat()}-recall",
        "kind": "bench", "tool": "recall-bench",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metrics": metrics, "events": rows,
        "escalations": [
            {"rule": "mddb_worse_than_grep",
             "detail": "MDDB hit_rate below local summaries scan"}
        ] if (metrics.get("mddb_hit_rate") is not None
              and metrics.get("summaries_hit_rate") is not None
              and metrics["mddb_hit_rate"] < metrics["summaries_hit_rate"])
        else [],
        "summary": "recall bench: " + "; ".join(
            f"{p} p50={metrics.get(f'{p}_p50_ms')}ms "
            f"hit={metrics.get(f'{p}_hit_rate')}"
            for p in stats),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probes", type=Path, required=True)
    ap.add_argument("--collection", default=None,
                    help="MDDB collection for the mddb path (omit to skip)")
    ap.add_argument("--summaries-dir", type=Path, default=Path(SUMMARIES_DIR))
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    probes = yaml.safe_load(args.probes.read_text())
    run = asyncio.run(_run(probes, args.collection, args.summaries_dir))
    print(run["summary"])
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(run, indent=2) + "\n")
        print(f"run record: {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
