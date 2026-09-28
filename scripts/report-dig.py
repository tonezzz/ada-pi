#!/usr/bin/env python3
"""Resolve report-hierarchy refs — the drill-down read path.

Refs (v1):
  run:rollup/<date|iso-week>            runs/{daily,weekly}/<id>.json
  report:<date>-<session-id>[#ev<n>]    reports/<date>-<sid>.json (+event n)
  run:<kind>/<date>-<id>                runs/*.json matching ref
  transcript:<date>-<sid>[#t<n>]        transcripts/<date>-<sid>.md (+turn n)
  mddb://<collection>/<key>             MDDB doc (writes need the primary;
                                        reads work on a live follower too)
  file:<abs-path>                       plain file

Each resolved level prints the artifact's own refs — so `report-dig` on a
weekly line tells you which refs to dig next.

Usage:
  python3 scripts/report-dig.py run:rollup/2026-W39
  python3 scripts/report-dig.py report:2026-09-26-abc123 --raw
  python3 scripts/report-dig.py transcript:2026-09-26-abc123#t3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ADA_DATA_DIR = Path(os.environ.get(
    "ADA_TRANSCRIPT_DIR", os.path.expanduser("~/.local/share/ada/transcripts"))
).parent


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ref_summary(obj: dict) -> list[str]:
    """Collect child refs present at this level."""
    refs = []
    for key in ("daily_refs",):
        refs += obj.get(key) or []
    for s in obj.get("sessions") or []:
        if s.get("ref"):
            refs.append(s["ref"])
        if s.get("transcript_ref"):
            refs.append(s["transcript_ref"])
    for r in obj.get("runs") or []:
        if r.get("ref"):
            refs.append(r["ref"])
    for e in obj.get("escalations") or []:
        if isinstance(e, dict) and e.get("ref"):
            refs.append(e["ref"])
        if isinstance(e, dict):
            refs += e.get("refs") or []
    return refs


def _print_rollup(obj: dict) -> None:
    print(f"{obj.get('ref')}  ({obj.get('level', '?')} rollup)")
    print(f"  {obj.get('summary', '')}")
    for s in obj.get("sessions") or []:
        kinds = ", ".join(f"{k}×{v}" for k, v in
                          (s.get("event_kinds") or {}).items())
        p95 = s.get("ttft_p95_ms")
        lat = f" ttft_p95={p95}ms" if p95 is not None else ""
        print(f"  session {s['ref']} owner={s.get('owner') or '-'}"
              f"{lat} [{kinds}]")
    for r in obj.get("runs") or []:
        print(f"  run {r['ref']} ({r.get('tool')}) — {r.get('summary', '')}")
    for e in obj.get("escalations") or []:
        print(f"  ! {e}")


def _print_report(obj: dict, ev_index: int | None, raw: bool) -> None:
    events = obj.get("session_events") or []
    if ev_index is not None:
        lo, hi = max(0, ev_index - 2), min(len(events), ev_index + 3)
        print(f"events {lo}..{hi - 1} (of {len(events)}), #{ev_index} marked:")
        for i in range(lo, hi):
            mark = ">" if i == ev_index else " "
            print(f"{mark} ev{i}: {json.dumps(events[i], ensure_ascii=False)}")
        return
    if raw:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
        return
    print(obj.get("memory_block") or obj.get("summary") or json.dumps(obj)[:500])
    print(f"\nsession_events ({len(events)}):")
    for i, ev in enumerate(events):
        print(f"  ev{i} t={ev.get('turn')}: "
              f"{ev.get('kind')} {json.dumps({k: v for k, v in ev.items() if k not in ('ts', 'kind', 'turn')}, ensure_ascii=False)[:120]}")


def _print_transcript(path: Path, turn: int | None) -> None:
    text = path.read_text(encoding="utf-8")
    if turn is None:
        print(text[:8000])
        if len(text) > 8000:
            print(f"\n[... truncated — {len(text)} chars total, use #t<n>]")
        return
    sections = re.split(r"(?m)^(?=## )", text)
    lo, hi = max(0, turn - 1), min(len(sections), turn + 2)
    for i in range(lo, hi):
        mark = ">" if i == turn else " "
        body = sections[i].strip()
        print(f"{mark} turn {i}: {body[:400]}")


async def _mddb_get(collection: str, key: str) -> None:
    from backend.mddb_client import MddbClient
    doc = await MddbClient().get_document(collection, key)
    if not doc:
        print(f"not found: mddb://{collection}/{key}", file=sys.stderr)
        return
    print(doc.get("contentMd") or doc.get("content_md") or json.dumps(doc)[:2000])
    meta = doc.get("meta") or {}
    if meta.get("ref"):
        print(f"\nmeta.ref: {meta['ref']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ref")
    ap.add_argument("--raw", action="store_true", help="print full JSON/text")
    ap.add_argument("--reports-dir", type=Path, default=ADA_DATA_DIR / "reports")
    ap.add_argument("--runs-dir", type=Path, default=ADA_DATA_DIR / "runs")
    ap.add_argument("--transcripts-dir", type=Path,
                    default=Path(os.environ.get(
                        "ADA_TRANSCRIPT_DIR",
                        os.path.expanduser("~/.local/share/ada/transcripts"))))
    args = ap.parse_args()

    ref = args.ref
    base, _, frag = ref.partition("#")

    if base.startswith("mddb://"):
        rest = base[7:]
        collection, _, key = rest.partition("/")
        asyncio.run(_mddb_get(collection, key))
        return 0

    if base.startswith("file:"):
        path = Path(base[5:])
        print(path.read_text(encoding="utf-8", errors="replace")[:8000])
        return 0

    if base.startswith("run:rollup/"):
        rid = base.split("/", 1)[1]
        for level in ("daily", "weekly"):
            p = args.runs_dir / level / f"{rid}.json"
            if p.is_file():
                obj = _load_json(p)
                _print_rollup(obj)
                print("\ndig next:")
                for r in _ref_summary(obj):
                    print(f"  {r}")
                return 0
        print(f"no rollup file for {rid}", file=sys.stderr)
        return 1

    if base.startswith("report:") or base.startswith("transcript:"):
        stem = base.split(":", 1)[1]
        if base.startswith("report:"):
            p = args.reports_dir / f"{stem}.json"
            if not p.is_file():
                print(f"no report {p}", file=sys.stderr)
                return 1
            _print_report(_load_json(p),
                          int(frag[2:]) if frag.startswith("ev") else None,
                          args.raw)
        else:
            p = args.transcripts_dir / f"{stem}.md"
            if not p.is_file():
                print(f"no transcript {p}", file=sys.stderr)
                return 1
            _print_transcript(p,
                              int(frag[1:]) if frag.startswith("t") else None)
        return 0

    if base.startswith("run:"):
        for p in sorted(args.runs_dir.glob("*.json")):
            try:
                if _load_json(p).get("ref") == base:
                    _print_rollup(_load_json(p))
                    return 0
            except Exception:
                continue
        print(f"no run record for {base}", file=sys.stderr)
        return 1

    print(f"unrecognized ref: {ref}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
