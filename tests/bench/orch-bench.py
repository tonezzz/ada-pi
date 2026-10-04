#!/usr/bin/env python3
"""Orchestration benchmark — compound-AI structures scored on the same
case set (CMS plan 'multi-model-orchestration', 2026-09-30).

Structures (--structure, default all):

  student   — baseline: distilled student only (current live path)
  cascade   — student first; escalate to 4b when p sits inside the gray
              band (--gray-lo/--gray-hi)
  parallel  — both score; weighted vote (4b weight rises while the
              student is inside the gray band)
  router    — feature router picks the expert per turn: code-switched
              TH/EN, embedded-affirmation, or imperative-shaped turns go
              straight to the 4b; short clean turns stay on the student;
              tool-routing cases always go to the 4b (the student has no
              choice head). Stand-in for a trained router head — it costs
              zero extra model calls; replace with a learned router once
              a router corpus exists.
  verifier  — student decides; YES decisions (and tool choices) are
              audited by the 4b, which can veto

Case sets (--sets, default all three):

  golden  — confirm-gate golden cases, AST-loaded from jev-bench.py's
            CONFIRM_CASES so the two benches cannot drift apart
  tool    — tool-routing golden cases (jev-bench.py TOOL_CASES); the
            student returns a stub choice, which is itself measured
  hard    — tests/bench/orch-hard-cases.jsonl (~30 rows): imperative-
            lookalikes, code-switched TH/EN, 'ok' non-confirmations

Metrics per structure: accuracy (overall + per set), p50/p95 latency,
cpu-seconds proxy (sum of server-side usage.elapsed_s — the $/call
proxy), heavy-call fraction, and ECE (10-bin calibration of the
probability behind each decision).

Usage:
  orch-bench.py [--student URL] [--heavy URL]
                [--structure student,cascade,...] [--sets golden,tool,hard]
                [--cases PATH] [--limit N] [--thr 0.75]
                [--corpus-out PATH] [--json-out PATH]
                [--mddb URL] [--report-cms]

  --corpus-out  append confirm-kind misses as jev-corpus rows (feeds the
                retrain loop's merge-corpus.py)
  --mddb        write a kind:benchmark doc to ada-ha-scenario-reports
  --report-cms  also refresh the 'bench-orch' CMS page
"""
import argparse, ast, json, re, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--student", default="http://100.74.146.0:8778")
parser.add_argument("--heavy",   default="http://100.123.163.11:8777")
parser.add_argument("--mddb", default="")
parser.add_argument("--report-cms", action="store_true")
parser.add_argument("--gray-lo", type=float, default=0.35)
parser.add_argument("--gray-hi", type=float, default=0.75)
parser.add_argument("--thr", type=float, default=0.75,
                    help="affirm decision threshold (matches jev-bench/production)")
parser.add_argument("--structure", default="all",
                    help="comma list: student,cascade,parallel,router,verifier or 'all'")
parser.add_argument("--sets", default="golden,tool,hard",
                    help="comma list of case sets to run")
parser.add_argument("--cases", default="",
                    help="override path to the hard-case JSONL")
parser.add_argument("--limit", type=int, default=0)
parser.add_argument("--corpus-out", default="",
                    help="append confirm-kind misses as jev-corpus rows")
parser.add_argument("--json-out", default="",
                    help="write the full result rows to this path")
parser.add_argument("--timeout", type=int, default=90)
ARGS = parser.parse_args()

REPO = Path(__file__).resolve().parent.parent.parent
JEV_BENCH = Path(__file__).resolve().parent / "jev-bench.py"
HARD_CASES = Path(ARGS.cases) if ARGS.cases else \
    Path(__file__).resolve().parent / "orch-hard-cases.jsonl"

# --- questions / states (same wording as jev-bench.py + the live probe) ----
CONFIRM_STATE = (
    'Ada, a voice assistant, asked the user to confirm a memory write. '
    'The user turn was: "{turn}"'
)
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
TOOL_STATE = (
    'Voice assistant Ada received user speech. Pick the single best action. '
    'User said: "{turn}"'
)
TOOL_Q = {
    "type": "choice",
    "instructions": "Choose the action Ada should take first.",
    "criteria": {
        "remember": "store the information in long-term memory",
        "recall": "search memory / past sessions to answer",
        "control": "act on a smart-home device (switch, scene, media)",
        "answer": "no tool needed — just speak the answer",
        "search": "search the web for current information",
    },
}
TOOL_AUDIT_Q = {
    "type": "noul",
    "instructions": (
        "You are auditing a tool-routing decision. Another model chose the "
        "action '{choice}' for this user turn. Is that the right first "
        "action, given the options remember/recall/control/answer/search?"),
    "criteria": {
        "true": "the chosen action is the best first step",
        "false": "a different action fits better",
    },
}

# --- router features (same token set as merge-corpus/eval-noul) -----------
THAI_RE = re.compile(r"[ก-๙]")
AFFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|yup|confirm(ed)?|go ahead|do it|sure|okay?|approved?|"
    r"proceed|absolutely|mhm|uh huh|sounds good)\b|"
    r"ใช่|ยืนยัน|ตกลง|เอาเลย|ทำเลย|ได้เลย|ทำได้|โอเค|ออเค|เออ|อือ|"
    r"ต่อไป|จัดไป|เอาสิ|ไปเลย|ทำไป|เผยแพร่เลย|ส่งเลย", re.IGNORECASE)
IMPERATIVE_RE = re.compile(
    r"\b(turn|set|delete|save|remember|show|play|send|publish|archive|"
    r"remind|cancel|confirm|tell|say|compute|open|close)\b|"
    r"ปิด|เปิด|ลบ|ส่ง|บันทึก|ยกเลิก|ตั้ง|จอง", re.IGNORECASE)


# --- case loading ----------------------------------------------------------
def _golden_lists() -> dict:
    """Pull CONFIRM_CASES/TOOL_CASES out of jev-bench.py via AST (same
    trick as jev-corpus-export.py — the module eats argv on import)."""
    tree = ast.parse(JEV_BENCH.read_text())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in ("CONFIRM_CASES", "TOOL_CASES"):
                    out[t.id] = ast.literal_eval(node.value)
    return out


def load_cases() -> list[dict]:
    wanted = {s.strip() for s in ARGS.sets.split(",") if s.strip()}
    cases = []
    if wanted & {"golden", "tool"}:
        g = _golden_lists()
        if "golden" in wanted:
            cases += [{"text": t, "kind": "confirm", "label": bool(l),
                       "set": "golden", "slice": "golden"}
                      for t, l in g.get("CONFIRM_CASES", [])]
        if "tool" in wanted:
            cases += [{"text": t, "kind": "tool", "label": l,
                       "set": "tool", "slice": "tool"}
                      for t, l in g.get("TOOL_CASES", [])]
    if "hard" in wanted and HARD_CASES.exists():
        for line in HARD_CASES.read_text().splitlines():
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    if ARGS.limit:
        cases = cases[: ARGS.limit]
    return cases


# --- endpoint --------------------------------------------------------------
def ask(url: str, state: str, question: dict) -> tuple[dict, float, float]:
    """POST /v1/systemone → (answer, wall_s, cpu_s). cpu_s is the
    server-reported usage.elapsed_s (the $/call proxy); falls back to
    wall time when the server doesn't report usage."""
    t0 = time.time()
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/systemone",
        data=json.dumps({"state": state,
                         "questions": {"q": question}}).encode(),
        headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=ARGS.timeout).read())
    wall = time.time() - t0
    cpu = float((resp.get("usage") or {}).get("elapsed_s") or wall)
    return resp["answers"]["q"], wall, cpu


class Ctx:
    """Per-structure counters — model calls and cpu-seconds spent."""

    def __init__(self):
        self.student_calls = 0
        self.heavy_calls = 0
        self.cpu_s = 0.0

    def student(self, state, question):
        self.student_calls += 1
        a, wall, cpu = ask(ARGS.student, state, question)
        self.cpu_s += cpu
        return a

    def heavy(self, state, question):
        self.heavy_calls += 1
        a, wall, cpu = ask(ARGS.heavy, state, question)
        self.cpu_s += cpu
        return a


# --- structures -------------------------------------------------------------
# Each decide(case, ctx) -> (decision, prob, detail)
#   decision : bool for confirm cases, str action for tool cases
#   prob     : confidence assigned to that decision (for ECE)
#   detail   : human trace for the miss report

def student_decide(case, ctx):
    state = CONFIRM_STATE.format(turn=case["text"]) if case["kind"] == "confirm" \
        else TOOL_STATE.format(turn=case["text"])
    q = CONFIRM_Q if case["kind"] == "confirm" else TOOL_Q
    a = ctx.student(state, q)
    if case["kind"] == "confirm":
        p = float(a["noul"])
        return p >= ARGS.thr, (p if p >= ARGS.thr else 1 - p), f"student p={p:.3f}"
    return a["choice"], float(a.get("confidence") or 0), \
        f"student choice={a['choice']}"


def cascade_decide(case, ctx):
    state = CONFIRM_STATE.format(turn=case["text"]) if case["kind"] == "confirm" \
        else TOOL_STATE.format(turn=case["text"])
    q = CONFIRM_Q if case["kind"] == "confirm" else TOOL_Q
    a = ctx.student(state, q)
    if case["kind"] == "confirm":
        p = float(a["noul"])
        if ARGS.gray_lo <= p <= ARGS.gray_hi:
            p2 = float(ctx.heavy(state, CONFIRM_Q)["noul"])
            return p2 >= ARGS.thr, (p2 if p2 >= ARGS.thr else 1 - p2), \
                f"cascade s={p:.3f}→4b={p2:.3f}"
        return p >= ARGS.thr, (p if p >= ARGS.thr else 1 - p), \
            f"student p={p:.3f} (no-escalate)"
    conf = float(a.get("confidence") or 0)
    if conf < 0.5:
        a2 = ctx.heavy(state, TOOL_Q)
        return a2["choice"], float(a2.get("confidence") or 0), \
            f"cascade stub→4b choice={a2['choice']}"
    return a["choice"], conf, f"student choice={a['choice']}"


def parallel_decide(case, ctx):
    state = CONFIRM_STATE.format(turn=case["text"]) if case["kind"] == "confirm" \
        else TOOL_STATE.format(turn=case["text"])
    if case["kind"] == "confirm":
        p = float(ctx.student(state, CONFIRM_Q)["noul"])
        p2 = float(ctx.heavy(state, CONFIRM_Q)["noul"])
        # weight: student alone when confident; 4b pulls when student is gray
        w4 = 0.7 if ARGS.gray_lo <= p <= ARGS.gray_hi else 0.3
        fused = (1 - w4) * p + w4 * p2
        return fused >= ARGS.thr, (fused if fused >= ARGS.thr else 1 - fused), \
            f"fuse s={p:.3f} 4b={p2:.3f} w4={w4} → {fused:.3f}"
    # only the 4b has a choice head; student confidence acts as a discount
    a = ctx.student(state, TOOL_Q)
    a2 = ctx.heavy(state, TOOL_Q)
    conf = 0.3 * float(a.get("confidence") or 0) + \
        0.7 * float(a2.get("confidence") or 0)
    return a2["choice"], conf, f"4b choice={a2['choice']} (student stub)"


def _route_to_heavy(turn: str) -> bool:
    """Feature router: escalate the shapes a distilled student misses.
    Code-switched TH/EN, long turns with an embedded affirmation token,
    and imperative-looking turns carrying an approval word go to the 4b.
    Placeholder for a trained router head (needs a router corpus)."""
    code_switch = bool(THAI_RE.search(turn)) and bool(re.search(r"[A-Za-z]", turn))
    embedded = len(turn) > 40 and bool(AFFIRM_RE.search(turn))
    imperative = bool(IMPERATIVE_RE.search(turn)) and bool(AFFIRM_RE.search(turn))
    return code_switch or embedded or imperative


def router_decide(case, ctx):
    if case["kind"] == "tool":
        a = ctx.heavy(TOOL_STATE.format(turn=case["text"]), TOOL_Q)
        return a["choice"], float(a.get("confidence") or 0), \
            f"router→4b choice={a['choice']}"
    state = CONFIRM_STATE.format(turn=case["text"])
    if _route_to_heavy(case["text"]):
        p = float(ctx.heavy(state, CONFIRM_Q)["noul"])
        return p >= ARGS.thr, (p if p >= ARGS.thr else 1 - p), \
            f"router→4b p={p:.3f}"
    p = float(ctx.student(state, CONFIRM_Q)["noul"])
    return p >= ARGS.thr, (p if p >= ARGS.thr else 1 - p), \
        f"router→student p={p:.3f}"


def verifier_decide(case, ctx):
    if case["kind"] == "tool":
        state = TOOL_STATE.format(turn=case["text"])
        a = ctx.student(state, TOOL_Q)
        aq = dict(TOOL_AUDIT_Q)
        aq["instructions"] = TOOL_AUDIT_Q["instructions"].format(choice=a["choice"])
        pa = float(ctx.heavy(state, aq)["noul"])
        if pa < 0.5:
            a2 = ctx.heavy(state, TOOL_Q)
            return a2["choice"], float(a2.get("confidence") or 0), \
                f"verifier vetoed stub '{a['choice']}' audit={pa:.3f}→4b={a2['choice']}"
        return a["choice"], pa, f"student choice={a['choice']} audit-ok={pa:.3f}"
    state = CONFIRM_STATE.format(turn=case["text"])
    p = float(ctx.student(state, CONFIRM_Q)["noul"])
    if p >= ARGS.thr:
        pa = float(ctx.heavy(state, AUDIT_Q)["noul"])
        if pa < 0.5:
            return False, 1 - pa, f"verifier vetoed s={p:.3f} audit={pa:.3f}"
        return True, pa, f"student p={p:.3f} audit-ok={pa:.3f}"
    return False, 1 - p, f"student p={p:.3f} (no-audit)"


STRUCTURES = {
    "student": student_decide,
    "cascade": cascade_decide,
    "parallel": parallel_decide,
    "router": router_decide,
    "verifier": verifier_decide,
}


# --- metrics ----------------------------------------------------------------
def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def _ece(rows: list[dict], bins: int = 10) -> float:
    """Expected calibration error over (prob, correct) pairs."""
    n = len(rows)
    if not n:
        return 0.0
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        bucket = [r for r in rows if lo <= r["prob"] < hi or
                  (b == bins - 1 and r["prob"] >= hi)]
        if not bucket:
            continue
        acc = sum(r["ok"] for r in bucket) / len(bucket)
        conf = sum(r["prob"] for r in bucket) / len(bucket)
        ece += len(bucket) / n * abs(acc - conf)
    return ece


def run(name: str, decide, cases: list[dict]) -> dict:
    ctx = Ctx()
    rows, lats = [], []
    for c in cases:
        t = time.time()
        try:
            decision, prob, detail = decide(c, ctx)
        except Exception as e:
            decision, prob, detail = None, 0.0, f"ERR {e}"
        lats.append(time.time() - t)
        ok = decision == c["label"]
        rows.append({"text": c["text"], "kind": c["kind"], "set": c["set"],
                     "slice": c.get("slice", ""), "label": c["label"],
                     "got": decision, "prob": round(float(prob), 4),
                     "ok": ok, "detail": detail})
    n_ok = sum(1 for r in rows if r["ok"])
    per_set = {}
    for s in {r["set"] for r in rows}:
        sub = [r for r in rows if r["set"] == s]
        per_set[s] = f"{sum(1 for r in sub if r['ok'])}/{len(sub)}"
    return {"name": name, "n": len(rows), "n_ok": n_ok,
            "score": f"{n_ok}/{len(rows)}",
            "acc": n_ok / len(rows) if rows else 0.0,
            "per_set": per_set,
            "p50_s": round(_percentile(lats, 0.50), 2),
            "p95_s": round(_percentile(lats, 0.95), 2),
            "cpu_s": round(ctx.cpu_s, 2),
            "cpu_per_call": round(ctx.cpu_s / len(rows), 3) if rows else 0.0,
            "student_calls": ctx.student_calls,
            "heavy_calls": ctx.heavy_calls,
            "heavy_frac": round(ctx.heavy_calls / len(rows), 2) if rows else 0.0,
            "ece": round(_ece(rows), 3),
            "rows": rows}


def main() -> int:
    cases = load_cases()
    if not cases:
        print("no cases loaded — check --sets/--cases", file=sys.stderr)
        return 2
    names = list(STRUCTURES) if ARGS.structure == "all" else \
        [s.strip() for s in ARGS.structure.split(",")]
    unknown = [n for n in names if n not in STRUCTURES]
    if unknown:
        print(f"unknown structure(s): {unknown} — pick from {list(STRUCTURES)}",
              file=sys.stderr)
        return 2

    results = [run(n, STRUCTURES[n], cases) for n in names]
    set_names = sorted({c["set"] for c in cases})

    set_counts = ", ".join(
        f"{s} {sum(1 for c in cases if c['set'] == s)}" for s in set_names)
    print(f"\n== orchestration bench — {len(cases)} cases ({set_counts}) ==\n")
    hdr = f"{'structure':<10} {'acc':>8} " + " ".join(f"{s:>8}" for s in set_names) + \
        f" {'p50':>6} {'p95':>6} {'cpu/c':>6} {'4b%':>5} {'ECE':>5}"
    print(hdr)
    for r in results:
        line = f"{r['name']:<10} {r['score']:>8} " + \
            " ".join(f"{r['per_set'].get(s, '-'):>8}" for s in set_names) + \
            f" {r['p50_s']:>5}s {r['p95_s']:>5}s {r['cpu_per_call']:>6} " \
            f"{r['heavy_frac']:>4.0%} {r['ece']:>5}"
        print(line)
    for r in results:
        misses = [x for x in r["rows"] if not x["ok"]]
        if misses:
            print(f"\n{r['name']} misses:")
            for x in misses:
                print(f"    exp={x['label']} got={x['got']} "
                      f"[{x['set']}/{x['slice']}] '{x['text'][:55]}' — {x['detail']}")

    if ARGS.corpus_out:
        _append_corpus(results)
    if ARGS.json_out:
        Path(ARGS.json_out).write_text(json.dumps(
            {"cases": len(cases), "results": results},
            ensure_ascii=False, indent=2))
        print(f"\n  json: {ARGS.json_out}")
    if ARGS.mddb:
        _report(results)
    return 0


def _append_corpus(results: list[dict]) -> None:
    """Feed the retrain loop: confirm-kind misses become jev-corpus rows
    (same shape realtime_provider writes; merge-corpus.py consumes it)."""
    seen, n = set(), 0
    with open(ARGS.corpus_out, "a") as fh:
        for r in results:
            for x in r["rows"]:
                if x["kind"] != "confirm" or x["ok"]:
                    continue
                key = (r["name"], " ".join(x["text"].lower().split()))
                if key in seen:
                    continue
                seen.add(key)
                fh.write(json.dumps({
                    "ts": time.time(), "src": f"orch-bench:{r['name']}",
                    "text": x["text"], "regex": bool(x["label"]),
                    "jev": x["prob"], "diverged": True,
                }, ensure_ascii=False) + "\n")
                n += 1
    print(f"\n  corpus: +{n} miss rows → {ARGS.corpus_out}")


def _report(results: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    lines = [f"# Orchestration bench — {now:%Y-%m-%d %H:%M}Z", "",
             "| structure | acc | p50 | p95 | cpu/call | 4b% | ECE |",
             "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(
            f"| {r['name']} | {r['score']} | {r['p50_s']}s | {r['p95_s']}s | "
            f"{r['cpu_per_call']} | {r['heavy_frac']:.0%} | {r['ece']} |")
    lines.append("")
    for r in results:
        misses = [x for x in r["rows"] if not x["ok"]]
        if misses:
            lines.append(f"**{r['name']} misses:** " +
                         "; ".join(f"'{x['text'][:60]}'" for x in misses))
    md = "\n".join(lines)
    meta = {"kind": ["benchmark"], "slug": ["bench-orch"],
            "title": [f"Orchestration bench — {now:%Y-%m-%d}"],
            "domain": ["bench"], "format": ["markdown"], "lang": ["en"],
            "summary": [" | ".join(f"{r['name']} {r['score']}" for r in results)],
            "updated": [now.isoformat(timespec="seconds")],
            "instance": ["idc01"], "written_by": ["orch-bench"],
            "ece": [str(min(r["ece"] for r in results))],
            "p95_s": [str(max(r["p95_s"] for r in results))]}
    key = f"bench/orch-{now:%Y%m%d-%H%M%S}"

    def _post(coll, k, m):
        req = urllib.request.Request(
            ARGS.mddb.rstrip("/") + "/add",
            data=json.dumps({"collection": coll, "key": k, "lang": "en",
                             "contentMd": md, "meta": m}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=60)

    _post("ada-ha-scenario-reports", key, meta)
    print(f"  reported: {key}")
    if ARGS.report_cms:
        meta["slug"] = ["bench-orch"]
        _post("ada-cms-pages", "bench-orch", meta)
        print("  cms page: bench-orch")


if __name__ == "__main__":
    sys.exit(main())
