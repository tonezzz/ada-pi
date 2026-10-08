#!/usr/bin/env python3
"""lane-probe.py — latency probe for a /v1/systemone lane endpoint.

N sequential confirm-shaped calls against a served specialist; reports
wall-clock p50/p95 (what a caller feels) alongside server-side
usage.elapsed_s (the cpu-seconds proxy) so lanes with/without honest
usage reporting stay comparable. Pure stdlib.

Usage:
  lane-probe.py --url http://127.0.0.1:8878 [--n 200] [--warmup 10]
                [--timeout 30] [--json-out probe.json]

Exit non-zero when >5% of calls error.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request

# Same state/question wording as orch-bench.py's confirm domain — keep
# probe results comparable to suite numbers.
CONFIRM_STATE = (
    'Ada, a voice assistant, asked the user to confirm a memory write. '
    'The user turn was: "{turn}"'
)
CONFIRM_Q = {
    "type": "noul",
    "instructions": (
        "Did the user explicitly affirm or confirm? The turn counts as "
        "affirmation only when it is a standalone short affirmation "
        "(like yes, ok, confirm, go ahead, ยืนยัน) or begins with an "
        "affirmation. An approval word embedded inside a longer request "
        "does NOT count."),
    "criteria": {
        "true": "the whole turn is a short affirmation, or it leads "
                "with one",
        "false": "no affirmation present, or an affirmative word is "
                 "buried inside a longer request",
    },
}
# representative mix: short EN/TH affirms, negations, one long imperative
TURNS = [
    "yes",
    "ยืนยัน",
    "no don't save that",
    "ok go ahead",
    "turn off the TV ok and then save the page",
    "confirm",
]


def pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return s[i]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()
    url = a.url.rstrip("/") + "/v1/systemone"

    def call(turn: str) -> tuple[float, float]:
        body = json.dumps({
            "state": CONFIRM_STATE.format(turn=turn),
            "questions": {"q": CONFIRM_Q}}).encode()
        t0 = time.time()
        req = urllib.request.Request(url, data=body, headers={
                                     "Content-Type": "application/json"})
        resp = json.loads(
            urllib.request.urlopen(req, timeout=a.timeout).read())
        wall = time.time() - t0
        cpu = float((resp.get("usage") or {}).get("elapsed_s") or wall)
        return wall, cpu

    for i in range(a.warmup):
        call(TURNS[i % len(TURNS)])

    walls, cpus, errs = [], [], 0
    t0 = time.time()
    for i in range(a.n):
        try:
            w, c = call(TURNS[i % len(TURNS)])
            walls.append(w * 1000)
            cpus.append(c * 1000)
        except Exception as exc:
            errs += 1
            print(f"call {i}: {exc}", file=sys.stderr)
    total = time.time() - t0

    res = {
        "url": a.url, "n": a.n, "errors": errs,
        "wall_s": round(total, 2),
        "p50_ms": round(pct(walls, 0.50), 1),
        "p95_ms": round(pct(walls, 0.95), 1),
        "mean_ms": round(statistics.fmean(walls), 1) if walls else 0,
        "min_ms": round(min(walls), 1) if walls else 0,
        "max_ms": round(max(walls), 1) if walls else 0,
        "cpu_p50_ms": round(pct(cpus, 0.50), 1),
        "cpu_p95_ms": round(pct(cpus, 0.95), 1),
        "throughput_rps": round(a.n / total, 1) if total else 0,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    print(json.dumps(res, indent=2))
    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump(res, f, indent=2)
    return 1 if errs > max(1, a.n * 0.05) else 0


if __name__ == "__main__":
    sys.exit(main())
