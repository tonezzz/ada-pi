#!/usr/bin/env python3
"""Speaker-ID benchmark: score held-out clips against enrolled profiles.

File naming: <expected-name>-<n>.pcm (e.g. kk-1.pcm, kk-2.pcm) — the part
before the LAST '-' is the expected speaker. Same audio rules as
enroll_speakers.py (16 kHz mono s16).

Reports per clip: expected vs identified, best score, runner-up, margin
(>= 0.45 score / >= 0.05 margin = accepted). Prints a confusion matrix and
writes a JSON run record consumable by report-rollup.py.

Benchmark against the old method: enroll a profile with 1 sample vs 3+
samples (enroll_speakers.py merges) and compare margin + refusal rate.

Usage:
  python3 scripts/identify_eval.py eval-clips/*.pcm \
      --profiles /tmp/eval-speakers.json --json-out /tmp/eval.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAMPLE_RATE = 16000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", type=Path,
                    help="held-out clips named <speaker>-<n>.pcm|.wav")
    ap.add_argument("--profiles", default=None,
                    help="speaker_profiles.json (default: ADA_SPEAKER_PROFILES)")
    ap.add_argument("--json-out", type=Path, default=None,
                    help="write the run record here (runs/bench-*.json shape)")
    args = ap.parse_args()

    if args.profiles:
        os.environ["ADA_SPEAKER_PROFILES"] = args.profiles

    from scripts.enroll_speakers import _read_audio  # noqa: E402
    from backend.speaker_id import (  # noqa: E402 — heavy import (torch)
        DEFAULT_THRESHOLD, MIN_MARGIN, SpeakerIdentifier, _cosine_similarity)

    ident = SpeakerIdentifier()
    enrolled = list(ident._enrolled)  # name -> embedding map
    if not enrolled:
        print("no enrolled speakers in profiles file", file=sys.stderr)
        return 1

    rows: list[dict] = []
    correct = refused = wrong = 0
    for f in args.files:
        expected = f.stem.rsplit("-", 1)[0].lower()
        try:
            pcm = _read_audio(f)
            emb = ident._compute_embedding(pcm, SAMPLE_RATE)
            scores = sorted(
                ((float(_cosine_similarity(emb, ident._enrolled[n])), n)
                 for n in enrolled), reverse=True)
        except Exception as exc:
            print(f"  {f.name}: SKIP — {exc}", file=sys.stderr)
            continue
        best_score, best = scores[0]
        margin = best_score - (scores[1][0] if len(scores) > 1 else 0.0)
        accepted = best_score >= DEFAULT_THRESHOLD and margin >= MIN_MARGIN
        got = best if accepted else None
        ok = got == expected
        rows.append({"clip": f.name, "expected": expected, "got": got,
                     "score": round(best_score, 4),
                     "runner_up": scores[1][1] if len(scores) > 1 else None,
                     "margin": round(margin, 4), "accepted": accepted})
        if got is None:
            refused += 1
            status = "refused"
        elif ok:
            correct += 1
            status = "ok"
        else:
            wrong += 1
            status = f"WRONG->{got}"
        print(f"  {f.name}: expected={expected} {status} "
              f"score={best_score:.3f} margin={margin:.3f}")

    n = len(rows)
    metrics = {
        "clips": n, "correct": correct, "wrong": wrong, "refused": refused,
        "accuracy": round(correct / n, 3) if n else 0.0,
        "refusal_rate": round(refused / n, 3) if n else 0.0,
        "threshold": DEFAULT_THRESHOLD, "min_margin": MIN_MARGIN,
        "enrolled": enrolled,
    }
    print(f"\n{n} clips: {correct} ok, {wrong} wrong, {refused} refused "
          f"(accuracy {metrics['accuracy']}, refusal {metrics['refusal_rate']})")

    run = {
        "ref": f"run:bench/{datetime.now(timezone.utc).date().isoformat()}-speaker-id",
        "kind": "bench", "tool": "identify_eval",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metrics": metrics, "events": rows,
        "escalations": [
            {"rule": "accuracy_below_1", "detail": f"{wrong} misidentified",
             "refs": [f"clip:{r['clip']}" for r in rows
                      if r["got"] not in (None, r["expected"])]}
        ] if wrong else [],
        "summary": (f"speaker-id eval: {n} clips, {correct} ok, {wrong} wrong, "
                    f"{refused} refused"),
    }
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(run, indent=2) + "\n")
        print(f"run record: {args.json_out}")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
