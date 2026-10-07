#!/usr/bin/env python3
"""Orchestration benchmark — compound-AI structures scored on the same
case set (CMS plan 'multi-model-orchestration', 2026-09-30).

Structures (--structure, default all = builtins + registry chains):

  builtin   student | cascade | parallel | router | verifier — the five
            hand-coded ways, now domain-driven (any case kind works:
            noul kinds threshold at --thr, choice kinds use confidence)
  chain     every `impl: chain` entry in --topologies (default
            tests/bench/topologies.yml) — graph-executor structures
            defined declaratively (see chain.py for the node/edge DSL)

Case sets (--sets, default all):

  golden  — confirm-gate golden cases, AST-loaded from jev-bench.py's
            CONFIRM_CASES so the two benches cannot drift apart
  tool    — tool-routing golden cases (jev-bench.py TOOL_CASES); the
            student returns a stub choice, which is itself measured
  <name>  — any tests/bench/orch-<name>-cases.jsonl file (hard, bank,
            triage, toolx, ...). Rows carry their own kind, which picks
            the domain question from --domains (tests/bench/domains.yml)

Metrics per structure: accuracy (overall + per set), p50/p95 latency,
cpu-seconds proxy (sum of server-side usage.elapsed_s — the $/call
proxy), heavy-call fraction, and ECE (10-bin calibration of the
probability behind each decision).

Usage:
  orch-bench.py [--student URL] [--heavy URL] [--arbiter URL]
                [--structure student,cascade,nest-3l-verify,...]
                [--sets golden,tool,hard,bank,triage,toolx]
                [--cases PATH] [--limit N] [--thr 0.75]
                [--corpus-out PATH] [--stats-out PATH] [--json-out PATH]
                [--mddb URL] [--report-cms]

  --suite NAME  specialist key in suites.yml — expands to that suite's
                non-spec tier sets (golden/hard/adversarial); unions
                with --sets if both are given
  --corpus-out  append confirm-kind misses as jev-corpus rows (feeds the
                retrain loop's merge-corpus.py)
  --stats-out   append per-case AND per-node outcome rows (JSONL). The
                learned_router chain node reads this same file on the
                next run — the corpus-in/corpus-out loop.
  --arbiter     L3 endpoint; defaults to --heavy until a real L3 lane
                (Gemini-backed /v1/systemone) exists
  --mddb        write a kind:benchmark doc to ada-ha-scenario-reports
  --report-cms  also refresh the 'bench-orch' CMS page
"""
import argparse, ast, glob, json, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

import chain as ch

parser = argparse.ArgumentParser()
parser.add_argument("--student", default="http://idc03.taila0626a.ts.net:8778")
parser.add_argument("--heavy",   default="http://100.75.102.88:8777")
parser.add_argument("--arbiter", default="",
                    help="L3 endpoint — defaults to --heavy (stand-in)")
parser.add_argument("--mddb", default="")
parser.add_argument("--report-cms", action="store_true")
parser.add_argument("--gray-lo", type=float, default=0.35)
parser.add_argument("--gray-hi", type=float, default=0.75)
parser.add_argument("--thr", type=float, default=0.75,
                    help="affirm decision threshold (matches jev-bench/production)")
parser.add_argument("--structure", default="all",
                    help="comma list of builtin + chain names, or 'all'")
parser.add_argument("--sets", default="all",
                    help="comma list of case sets, or 'all'")
parser.add_argument("--suite", default="",
                    help="specialist key in --suites (suites.yml) — "
                         "adds its non-spec tier sets to --sets")
parser.add_argument("--suites", default="",
                    help="suite registry path (default suites.yml)")
parser.add_argument("--cases", default="",
                    help="override path to the hard-case JSONL")
parser.add_argument("--domains", default="",
                    help="domain question registry (default domains.yml)")
parser.add_argument("--topologies", default="",
                    help="topology registry (default topologies.yml)")
parser.add_argument("--limit", type=int, default=0)
parser.add_argument("--corpus-out", default="",
                    help="append confirm-kind misses as jev-corpus rows")
parser.add_argument("--stats-out", default="",
                    help="append per-case/per-node outcome rows (JSONL); "
                         "read back by learned_router chains")
parser.add_argument("--json-out", default="",
                    help="write the full result rows to this path")
parser.add_argument("--timeout", type=int, default=90)
ARGS = parser.parse_args()

_HERE = Path(__file__).resolve().parent
REPO = _HERE.parent.parent
JEV_BENCH = _HERE / "jev-bench.py"
HARD_CASES = Path(ARGS.cases) if ARGS.cases else \
    _HERE / "orch-hard-cases.jsonl"
DOMAINS_PATH = Path(ARGS.domains) if ARGS.domains else _HERE / "domains.yml"
TOPO_PATH = Path(ARGS.topologies) if ARGS.topologies else \
    _HERE / "topologies.yml"

DOMS = ch.load_domains(DOMAINS_PATH)
GRAY = (ARGS.gray_lo, ARGS.gray_hi)
SUITES_PATH = Path(ARGS.suites) if ARGS.suites else _HERE / "suites.yml"


def _suite_sets() -> list[str]:
    """Expand --suite <specialist> to its non-spec tier set names."""
    if not ARGS.suite:
        return []
    data = yaml.safe_load(SUITES_PATH.read_text()) or {}
    suites = data.get("suites") or {}
    spec = suites.get(ARGS.suite)
    if spec is None:
        print(f"unknown suite '{ARGS.suite}' — pick from {sorted(suites)}",
              file=sys.stderr)
        sys.exit(2)
    return [cfg["set"] for cfg in (spec.get("tiers") or {}).values()
            if isinstance(cfg, dict) and cfg.get("set")]


def state_for(case: dict) -> str:
    return DOMS[case["kind"]]["state"].format(turn=case["text"])


def question_for(case: dict) -> dict:
    return DOMS[case["kind"]]["question"]


def audit_for(case: dict, decision) -> dict:
    q = dict(DOMS[case["kind"]]["audit"])
    dec = str(decision).lower() if isinstance(decision, bool) else decision
    q["instructions"] = q["instructions"].format(decision=dec)
    return q


def is_noul(case: dict) -> bool:
    return DOMS[case["kind"]]["question"]["type"] == "noul"


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


def _jsonl_sets() -> list[str]:
    """Every orch-<set>-cases.jsonl beside this file is a case set."""
    return sorted(
        Path(p).name[len("orch-"):-len("-cases.jsonl")]
        for p in glob.glob(str(_HERE / "orch-*-cases.jsonl")))


def load_cases() -> list[dict]:
    wanted = {s.strip() for s in ARGS.sets.split(",") if s.strip()}
    if ARGS.suite and wanted == {"all"}:
        wanted = set()          # --suite narrows the default 'all'
    wanted |= set(_suite_sets())
    if "all" in wanted:
        wanted = {"golden", "tool", *_jsonl_sets()}
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
    for s in sorted(wanted - {"golden", "tool", "all"}):
        path = HARD_CASES if s == "hard" else _HERE / f"orch-{s}-cases.jsonl"
        if not path.exists():
            print(f"warn: no case file for set '{s}' ({path})",
                  file=sys.stderr)
            continue
        for line in path.read_text().splitlines():
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


LEVEL_URLS = {"L1": ARGS.student,
              "L2": ARGS.heavy,
              "L3": ARGS.arbiter or ARGS.heavy}


class Ctx:
    """Per-structure counters — model calls and cpu-seconds spent,
    tagged by level (L1 student / L2 heavy / L3 arbiter)."""

    def __init__(self):
        self.calls: dict[str, int] = {}
        self.cpu_s = 0.0

    def call(self, level: str, state, question):
        self.calls[level] = self.calls.get(level, 0) + 1
        a, wall, cpu = ask(LEVEL_URLS[level], state, question)
        self.cpu_s += cpu
        return a

    # builtin-structure shims
    def student(self, state, question):
        return self.call("L1", state, question)

    def heavy(self, state, question):
        return self.call("L2", state, question)

    @property
    def student_calls(self):
        return self.calls.get("L1", 0)

    @property
    def heavy_calls(self):
        # L3 arbiter counts as heavy spend for the cost column
        return self.calls.get("L2", 0) + self.calls.get("L3", 0)


# --- structures -------------------------------------------------------------
# Each decide(case, ctx) -> (decision, prob, detail, trace)
#   decision : bool for noul kinds, str action for choice kinds
#   prob     : confidence assigned to that decision (for ECE)
#   detail   : human trace for the miss report
#   trace    : [{node, level, role, decision, prob}] — per-node outcomes
#              feed the learned-router stats corpus

def _d(answer, case, low_conf=0.5):
    """chain._decision without a node — (decision, prob, in_band)."""
    if answer.get("type") == "choice" or "choice" in answer:
        conf = float(answer.get("confidence") or 0)
        return answer["choice"], conf, conf < low_conf
    p = float(answer["noul"])
    return p >= ARGS.thr, (p if p >= ARGS.thr else 1 - p), \
        ARGS.gray_lo <= p <= ARGS.gray_hi


def _t(level, role, decision, prob):
    return {"node": role, "level": level, "role": role,
            "decision": decision, "prob": prob}


def student_decide(case, ctx):
    a = ctx.student(state_for(case), question_for(case))
    dec, prob, _ = _d(a, case)
    return dec, prob, f"student {_short(dec, prob)}", \
        [_t("L1", "decider", dec, prob)]


def cascade_decide(case, ctx):
    state, q = state_for(case), question_for(case)
    a = ctx.student(state, q)
    dec, prob, band = _d(a, case)
    tr = [_t("L1", "decider", dec, prob)]
    if not band:
        return dec, prob, f"student {_short(dec, prob)} (no-escalate)", tr
    a2 = ctx.heavy(state, q)
    dec2, prob2, _ = _d(a2, case)
    tr.append(_t("L2", "decider", dec2, prob2))
    return dec2, prob2, \
        f"cascade {_short(dec, prob)}→4b={_short(dec2, prob2)}", tr


def parallel_decide(case, ctx):
    state, q = state_for(case), question_for(case)
    a = ctx.student(state, q)
    a2 = ctx.heavy(state, q)
    dec, prob, _ = _d(a, case)
    dec2, prob2, _ = _d(a2, case)
    tr = [_t("L1", "decider", dec, prob),
          _t("L2", "decider", dec2, prob2)]
    if is_noul(case):
        p = float(a["noul"])
        w4 = 0.7 if ARGS.gray_lo <= p <= ARGS.gray_hi else 0.3
        fused = (1 - w4) * p + w4 * float(a2["noul"])
        out = fused >= ARGS.thr
        return out, (fused if out else 1 - fused), \
            f"fuse s={p:.3f} 4b={a2['noul']:.3f} w4={w4} → {fused:.3f}", tr
    # only the 4b has a choice head; student confidence acts as a discount
    conf = 0.3 * float(a.get("confidence") or 0) + \
        0.7 * float(a2.get("confidence") or 0)
    return a2["choice"], conf, \
        f"4b choice={a2['choice']} (student stub)", tr


def router_decide(case, ctx):
    state, q = state_for(case), question_for(case)
    if not is_noul(case) or ch.route_hard(case["text"]):
        a = ctx.heavy(state, q)
        dec, prob, _ = _d(a, case)
        return dec, prob, f"router→4b {_short(dec, prob)}", \
            [_t("L2", "decider", dec, prob)]
    a = ctx.student(state, q)
    dec, prob, _ = _d(a, case)
    return dec, prob, f"router→student {_short(dec, prob)}", \
        [_t("L1", "decider", dec, prob)]


def verifier_decide(case, ctx):
    state, q = state_for(case), question_for(case)
    a = ctx.student(state, q)
    dec, prob, _ = _d(a, case)
    tr = [_t("L1", "decider", dec, prob)]
    if not dec:  # audit only affirmative/commit decisions
        return dec, prob, f"student {_short(dec, prob)} (no-audit)", tr
    pa = float(ctx.heavy(state, audit_for(case, dec))["noul"])
    if pa >= 0.5:
        tr.append(_t("L2", "auditor", True, pa))
        return dec, pa, f"student {_short(dec, prob)} audit-ok={pa:.3f}", tr
    tr.append(_t("L2", "auditor", False, pa))
    if is_noul(case):
        return False, 1 - pa, \
            f"verifier vetoed {_short(dec, prob)} audit={pa:.3f}", tr
    a2 = ctx.heavy(state, q)
    dec2, prob2, _ = _d(a2, case)
    tr.append(_t("L2", "decider", dec2, prob2))
    return dec2, prob2, \
        f"verifier vetoed stub '{dec}' audit={pa:.3f}→4b={dec2}", tr


def _short(dec, prob) -> str:
    if isinstance(dec, bool):
        return f"p={prob:.3f}→{'T' if dec else 'F'}"
    return f"choice={dec}@{prob:.2f}"


def _chain_decide(spec: ch.Chain):
    def fn(case, ctx):
        return spec.decide(case, ctx.call, DOMS, ARGS.thr, GRAY,
                           stats=ROUTER_STATS)
    return fn


def _load_router_stats():
    """Aggregate the stats-out JSONL into {featsig: {level: [n, ok]}} —
    decider/arbiter/fallback node rows only (auditor rows aren't
    decision-typed)."""
    stats: dict[str, dict[str, list]] = {}
    if not ARGS.stats_out or not Path(ARGS.stats_out).exists():
        return stats
    for line in Path(ARGS.stats_out).read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("row") != "node" or not r.get("scoreable"):
            continue
        lv = stats.setdefault(r["featsig"], {})
        n, ok = lv.get(r["level"], [0, 0])
        lv[r["level"]] = [n + 1, ok + int(bool(r["ok"]))]
    return stats


BUILTINS = {
    "student": student_decide,
    "cascade": cascade_decide,
    "parallel": parallel_decide,
    "router": router_decide,
    "verifier": verifier_decide,
}
CHAINS = ch.load_chains(TOPO_PATH)
ROUTER_STATS = _load_router_stats()
STRUCTURES = {**BUILTINS, **{n: _chain_decide(c) for n, c in CHAINS.items()}}


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
            decision, prob, detail, trace = decide(c, ctx)
        except Exception as e:
            decision, prob, detail, trace = None, 0.0, f"ERR {e}", []
        lats.append(time.time() - t)
        ok = decision == c["label"]
        rows.append({"text": c["text"], "kind": c["kind"], "set": c["set"],
                     "slice": c.get("slice", ""), "label": c["label"],
                     "got": decision, "prob": round(float(prob), 4),
                     "ok": ok, "detail": detail,
                     "featsig": ch.featsig(c["kind"], c["text"]),
                     "trace": trace})
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
    missing = sorted({c["kind"] for c in cases} - set(DOMS))
    if missing:
        print(f"warn: case kinds with no domain in {DOMAINS_PATH.name}: "
              f"{missing}", file=sys.stderr)
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
    w = max(10, max(len(r["name"]) for r in results) + 2)
    hdr = f"{'structure':<{w}} {'acc':>8} " + \
        " ".join(f"{s:>8}" for s in set_names) + \
        f" {'p50':>6} {'p95':>6} {'cpu/c':>6} {'4b%':>5} {'ECE':>5}"
    print(hdr)
    for r in results:
        line = f"{r['name']:<{w}} {r['score']:>8} " + \
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
    if ARGS.stats_out:
        _append_stats(results)
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
                    "kind": x["kind"], "slice": x["slice"],
                }, ensure_ascii=False) + "\n")
                n += 1
    print(f"\n  corpus: +{n} miss rows → {ARGS.corpus_out}")


def _append_stats(results: list[dict]) -> None:
    """Corpus-out for the learned router: one 'case' row per outcome,
    one 'node' row per model call (scoreable for decision roles)."""
    ts = time.time()
    n = 0
    with open(ARGS.stats_out, "a") as fh:
        for r in results:
            for x in r["rows"]:
                base = {"ts": ts, "structure": r["name"],
                        "kind": x["kind"], "featsig": x["featsig"],
                        "set": x["set"], "slice": x["slice"]}
                fh.write(json.dumps(
                    {**base, "row": "case", "text": x["text"],
                     "label": x["label"], "got": x["got"],
                     "prob": x["prob"], "ok": x["ok"],
                     "detail": x["detail"]},
                    ensure_ascii=False) + "\n")
                n += 1
                for t in x["trace"]:
                    scoreable = t["role"] in ("decider", "arbiter",
                                              "fallback")
                    fh.write(json.dumps(
                        {**base, "row": "node", "node": t["node"],
                         "level": t["level"], "role": t["role"],
                         "decision": t["decision"], "prob": t["prob"],
                         "ok": (t["decision"] == x["label"])
                         if scoreable else None,
                         "scoreable": scoreable},
                        ensure_ascii=False) + "\n")
                    n += 1
    print(f"  stats: +{n} rows → {ARGS.stats_out}")


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
            "instance": ["idc03"], "written_by": ["orch-bench"],
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
