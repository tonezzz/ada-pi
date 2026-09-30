#!/usr/bin/env python3
"""Orchestration benchmark — the same confirm-gate cases run through
different compound structures:

  student   — baseline: distilbert student only (current live path)
  cascade   — student first; escalate to 4b when student p is inside
              the gray band (default 0.35–0.75)
  parallel  — both score; combine as weighted vote (4b weight higher
              when student is uncertain, else student dominates)
  verifier  — student decides; when it says YES, the 4b audits just
              that decision and can veto (veto = mark False)

Each structure reports accuracy on the same case set plus latency.
The point: prove which composite beats the student-only baseline on
the cases the student misses (imperative-lookalike affirmations)
without paying the 4b's ~20s on every call.

Usage:
  orch-bench.py [--student URL] [--heavy URL]
                [--mddb URL] [--report-cms]
"""
import argparse, json, sys, time, urllib.request
from datetime import datetime, timezone

parser = argparse.ArgumentParser()
parser.add_argument("--student", default="http://100.74.146.0:8778")
parser.add_argument("--heavy",   default="http://100.123.163.11:8777")
parser.add_argument("--mddb", default="")
parser.add_argument("--report-cms", action="store_true")
parser.add_argument("--gray-lo", type=float, default=0.35)
parser.add_argument("--gray-hi", type=float, default=0.75)
ARGS = parser.parse_args()

CONFIRM_Q = {
    "type": "noul",
    "instructions": (
        "Did the user explicitly affirm or confirm? The turn counts as "
        "affirmation only when it is a standalone short affirmation (like "
        "yes, ok, confirm, go ahead, ยืนยัน) or begins with an affirmation. "
        "An approval word embedded inside a longer request does NOT count."),
    "criteria": {
        "true": "the whole turn is a short affirmation, or it leads with one",
        "false": "no affirmation present, or an affirmative word is buried inside a longer request",
    },
}
AUDIT_Q = {
    "type": "noul",
    "instructions": (
        "You are auditing a voice-confirmation decision. Another model said "
        "the user affirmed. Vetoes happen when the turn is actually an "
        "instruction that happens to contain 'ok', 'confirm', 'yes' as part "
        "of a command — e.g. 'turn off the TV ok', 'save this to memory "
        "please', 'confirm delete the old page' are instructions, not "
        "confirmations. Is this a real confirmation?"),
    "criteria": {
        "true": "genuinely an affirmative response to a pending question",
        "false": "an imperative/instruction containing an approval word",
    },
}
CASES = [
    ("yes, publish it", True), ("Yes", True), ("go ahead", True),
    ("ยืนยันครับ", True), ("ใช่ ทำเลย", True),
    ("remember that the lab stack design is approved", False),
    ("turn off the TV ok", False), ("what is the weather today", False),
    ("save this to memory please", False),
    ("confirm delete the old page", False),
]


def score(url: str, state: str, q: dict, timeout: int = 90) -> tuple[float, float]:
    """→ (noul p, seconds)."""
    t = time.time()
    req = urllib.request.Request(
        url + "/v1/systemone",
        data=json.dumps({"state": state, "questions": {"q": q}}).encode(),
        headers={"Content-Type": "application/json"})
    p = json.loads(urllib.request.urlopen(req, timeout=timeout).read()
                   )["answers"]["q"]["noul"]
    return p, time.time() - t


def run(name: str, decide) -> dict:
    rows, lats = [], []
    for turn, exp in CASES:
        t = time.time()
        decision, detail = decide(turn)
        dt = time.time() - t
        lats.append(dt)
        ok = decision == exp
        rows.append((ok, turn, detail, exp))
    n_ok = sum(1 for r in rows if r[0])
    lats.sort()
    med = lats[len(lats) // 2]
    return {"name": name, "score": f"{n_ok}/{len(CASES)}",
            "acc": n_ok / len(CASES), "median_s": round(med, 2),
            "rows": rows}


def student_decide(turn):
    p, _ = score(ARGS.student, turn, CONFIRM_Q)
    return p >= ARGS.gray_lo, f"student p={p:.3f}"


def cascade_decide(turn):
    p, dt = score(ARGS.student, turn, CONFIRM_Q)
    if ARGS.gray_lo <= p <= ARGS.gray_hi:
        p2, _ = score(ARGS.heavy, turn, CONFIRM_Q)
        return p2 >= 0.75, f"cascade s={p:.3f}→4b={p2:.3f}"
    return p >= ARGS.gray_lo, f"student p={p:.3f} (no-escalate)"


def parallel_decide(turn):
    p, _ = score(ARGS.student, turn, CONFIRM_Q)
    p2, _ = score(ARGS.heavy, turn, CONFIRM_Q)
    # weight: student alone when confident; 4b pulls when student is gray
    w4 = 0.7 if ARGS.gray_lo <= p <= ARGS.gray_hi else 0.3
    fused = (1 - w4) * p + w4 * p2
    return fused >= ARGS.gray_lo, f"fuse s={p:.3f} 4b={p2:.3f} w4={w4} → {fused:.3f}"


def verifier_decide(turn):
    p, _ = score(ARGS.student, turn, CONFIRM_Q)
    if p >= ARGS.gray_lo:
        pa, _ = score(ARGS.heavy, turn, AUDIT_Q)
        if pa < 0.5:
            return False, f"verifier vetoed s={p:.3f} audit={pa:.3f}"
        return True, f"student p={p:.3f} audit-ok={pa:.3f}"
    return False, f"student p={p:.3f} (no-audit)"


results = [
    run("student", student_decide),
    run("cascade", cascade_decide),
    run("parallel", parallel_decide),
    run("verifier", verifier_decide),
]

print(f"\n== orchestration bench — {len(CASES)} confirm cases ==\n")
for r in results:
    print(f"{r['name']:<10} {r['score']:>6}  median {r['median_s']:>5}s")
    for ok, turn, detail, exp in r["rows"]:
        if not ok:
            print(f"    MISS exp={exp} '{turn[:50]}' — {detail}")

if ARGS.mddb:
    now = datetime.now(timezone.utc)
    lines = [f"# Orchestration bench — {now:%Y-%m-%d %H:%M}Z", "",
             "| structure | golden | median latency |", "|---|---|---|"]
    for r in results:
        lines.append(f"| {r['name']} | {r['score']} | {r['median_s']}s |")
    lines.append("")
    for r in results:
        misses = [x for x in r["rows"] if not x[0]]
        if misses:
            lines.append(f"**{r['name']} misses:** " +
                         "; ".join(f"'{x[1][:60]}'" for x in misses))
    meta = {"kind": ["benchmark"], "slug": ["bench-orch"],
            "title": [f"Orchestration bench — {now:%Y-%m-%d}"],
            "domain": ["bench"], "format": ["markdown"], "lang": ["en"],
            "summary": [" | ".join(f"{r['name']} {r['score']}" for r in results)],
            "updated": [now.isoformat(timespec="seconds")],
            "instance": ["idc01"], "written_by": ["orch-bench"]}
    key = f"bench/orch-{now:%Y%m%d-%H%M%S}"
    _post_req = urllib.request.Request(
        ARGS.mddb + "/add",
        data=json.dumps({"collection": "ada-ha-scenario-reports",
                         "key": key, "lang": "en",
                         "contentMd": "\n".join(lines), "meta": meta}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(_post_req, timeout=60)
    print(f"  reported: {key}")
    if ARGS.report_cms:
        meta["slug"] = ["bench-orch"]
        _post_req = urllib.request.Request(
            ARGS.mddb + "/add",
            data=json.dumps({"collection": "ada-cms-pages",
                             "key": "bench-orch", "lang": "en",
                             "contentMd": "\n".join(lines), "meta": meta}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(_post_req, timeout=60)
        print("  cms page: bench-orch")
