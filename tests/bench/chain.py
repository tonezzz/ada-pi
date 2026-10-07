"""Chain executor — runs `impl: chain` topologies from topologies.yml.

A chain is a small DAG of nodes walked per case:

    node = {id, level, role, edges: [{when: <pred>, to: <id|accept>}]}

Roles:
    router          L0 — edges only, zero model calls
    learned_router  L0 — routes by accumulated per-signature stats
                    (router-stats JSONL from --stats-out); falls back to
                    edge predicates when the signature is cold
    decider         asks the domain question (noul or choice)
    auditor         asks the domain audit question; sets the veto state
    arbiter/fallback decider aliases — terminal unless edges say otherwise

Edge predicates (first match wins; missing match => accept):
    else | always   unconditional
    kind:<k>        case kind matches (confirm/tool/bank/triage/...)
    feat:<f>        feature active on the case text (see FEATURES)
    yes             current decision is affirmative — noul True or any
                    choice pick (a choice IS an affirmative commit)
    no              current decision is the noul negative
    in_band         decider landed in the gray band / low-confidence
    veto            auditor noul < veto_below
    uphold          auditor passed

`to: accept` returns the current decision. An auditor veto with no veto
edge flips a noul decision to False (verifier-builtin semantics); a veto
on a choice decision with no edge returns the pick marked 'vetoed'.

Feature selection bias note: per-level stats are accumulated only on
cases that reached that level — fine for routing, not for headline
accuracy claims.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

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

MAX_STEPS = 10  # cycle guard


def features(text: str) -> dict[str, bool]:
    """L0 feature predicates — the same signals the builtin feature
    router uses. Cheap enough to be 'free' level-0 routing."""
    return {
        "code_switch": bool(THAI_RE.search(text)) and
                       bool(re.search(r"[A-Za-z]", text)),
        "embedded": len(text) > 40 and bool(AFFIRM_RE.search(text)),
        "imperative": bool(IMPERATIVE_RE.search(text)) and
                      bool(AFFIRM_RE.search(text)),
        "thai": bool(THAI_RE.search(text)),
        "short": len(text) <= 40,
    }


def featsig(kind: str, text: str) -> str:
    """Stable (kind, active-features) signature — the learned router's
    lookup key and the stats file's grouping dimension. Includes the
    kind, so it is usable as a flat dict key."""
    active = sorted(k for k, v in features(text).items() if v)
    return f"{kind}:{'+'.join(active) if active else 'clean'}"


def route_hard(text: str) -> bool:
    """The builtin feature router's rule, as a reusable L0 helper."""
    f = features(text)
    return f["code_switch"] or f["embedded"] or f["imperative"]


@dataclass
class Node:
    id: str
    level: str = "L1"
    role: str = "decider"
    edges: list = field(default_factory=list)
    params: dict = field(default_factory=dict)


class Chain:
    """One executable topology."""

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.nodes = {n["id"]: Node(
            id=n["id"], level=n.get("level", "L1"),
            role=n.get("role", "decider"),
            edges=[{**e, "when": _norm_when(e.get("when"))}
                   for e in n.get("edges") or []],
            params={k: v for k, v in n.items()
                    if k not in ("id", "level", "role", "edges")})
            for n in spec.get("nodes") or []}
        self.entry = spec.get("entry") or next(iter(self.nodes), None)
        self.veto_below = float(spec.get("veto_below", 0.5))

    def _pick(self, node: Node, st: dict, case: dict) -> str | None:
        for e in node.edges:
            if _match(e.get("when") or "else", st, case):
                return e.get("to")
        return None

    def _to_level_node(self, level: str) -> str | None:
        for nid, n in self.nodes.items():
            if n.level == level and n.role in (
                    "decider", "arbiter", "fallback"):
                return nid
        return None

    def decide(self, case, call, doms, thr, gray, stats=None):
        """Walk the DAG.

        call(level, state, question) -> answer dict — does the HTTP and
        the cost accounting. Returns (decision, prob, detail, trace).
        """
        st = {"decision": None, "prob": 0.0, "band": False, "veto": None}
        trace, hops, nid = [], [], self.entry
        for _ in range(MAX_STEPS):
            node = self.nodes.get(nid)
            if node is None:
                hops.append(f"?{nid}")
                break
            if node.role == "learned_router":
                tgt = _learned_route(case, stats or {},
                                     int(node.params.get("min_n", 15)))
                if tgt and (nid2 := self._to_level_node(tgt)):
                    hops.append(f"{nid}~{tgt}")
                    nid = nid2
                    continue
                # cold signature -> behave like a plain router
            if node.role == "router" or (
                    node.role == "learned_router" and node.edges):
                nxt = self._pick(node, st, case)
                hops.append(f"{nid}")
            elif node.role == "auditor":
                a = call(node.level, _state(doms, case),
                         _audit_q(doms, case, st["decision"]))
                pa = float(a["noul"])
                st["veto"] = pa < float(
                    node.params.get("veto_below", self.veto_below))
                trace.append({"node": nid, "level": node.level,
                              "role": "auditor",
                              "decision": not st["veto"], "prob": pa})
                hops.append(f"{nid}={pa:.2f}{'veto' if st['veto'] else 'ok'}")
                nxt = self._pick(node, st, case)
                if nxt is None:
                    if st["veto"]:
                        if isinstance(st["decision"], bool):
                            st["decision"], st["prob"] = False, 1 - pa
                        else:
                            st["prob"] = pa
                    break
            else:  # decider / arbiter / fallback
                a = call(node.level, _state(doms, case),
                         _decide_q(doms, case))
                st["decision"], st["prob"], st["band"] = _decision(
                    a, case, thr, gray, node)
                trace.append({"node": nid, "level": node.level,
                              "role": node.role,
                              "decision": st["decision"],
                              "prob": st["prob"]})
                hops.append(f"{nid}={_fmt(st['decision'])}:{st['prob']:.2f}")
                nxt = self._pick(node, st, case)
            if nxt in (None, "accept"):
                break
            nid = nxt
        return st["decision"], st["prob"], "→".join(hops), trace


def _norm_when(when):
    """YAML 1.1 parses bare yes/no/on/off as bools — map them back so
    unquoted `when: yes` in a registry still means the predicate."""
    if when is True:
        return "yes"
    if when is False:
        return "no"
    return when


def _fmt(d) -> str:
    return ("T" if d else "F") if isinstance(d, bool) else str(d)


def _match(when: str, st: dict, case: dict) -> bool:
    if when in ("else", "always"):
        return True
    if when.startswith("kind:"):
        return case["kind"] == when[5:]
    if when.startswith("feat:"):
        return features(case["text"]).get(when[5:], False)
    if when == "yes":
        return bool(st["decision"])
    if when == "no":
        return st["decision"] is False
    if when == "in_band":
        return st["band"]
    if when == "veto":
        return st["veto"] is True
    if when == "uphold":
        return st["veto"] is False
    return False


def _state(doms: dict, case: dict) -> str:
    return doms[case["kind"]]["state"].format(turn=case["text"])


def _decide_q(doms: dict, case: dict) -> dict:
    return doms[case["kind"]]["question"]


def _audit_q(doms: dict, case: dict, decision) -> dict:
    q = dict(doms[case["kind"]]["audit"])
    dec = str(decision).lower() if isinstance(decision, bool) else decision
    q["instructions"] = q["instructions"].format(decision=dec)
    return q


def _decision(answer: dict, case: dict, thr: float,
              gray: tuple, node: Node):
    """(decision, prob, in_band) from a /v1/systemone answer."""
    if answer.get("type") == "choice" or "choice" in answer:
        conf = float(answer.get("confidence") or 0)
        low = float(node.params.get("low_conf", 0.5))
        return answer["choice"], conf, conf < low
    p = float(answer["noul"])
    return p >= thr, (p if p >= thr else 1 - p), gray[0] <= p <= gray[1]


def _learned_route(case: dict, stats: dict, min_n: int) -> str | None:
    """Pick the level with the best recorded accuracy for this case's
    signature — cold signatures return None (caller falls back to the
    edge predicates). stats = {featsig: {level: [n, ok]}}."""
    per_level = stats.get(featsig(case["kind"], case["text"])) or {}
    best, best_key = None, (-1.0, -1)  # acc, -level_cost
    order = {"L1": 1, "L2": 2, "L3": 3}
    for level, (n, ok) in per_level.items():
        if n < min_n:
            continue
        key = (ok / n, -order.get(level, 9))  # tie -> cheaper level
        if key > best_key:
            best, best_key = level, key
    return best


def load_chains(path) -> dict[str, Chain]:
    """impl:chain entries from a topologies.yml registry."""
    data = yaml.safe_load(open(path).read()) or {}
    return {name: Chain(name, spec)
            for name, spec in (data.get("topologies") or {}).items()
            if spec.get("impl") == "chain"}


def load_domains(path) -> dict[str, dict]:
    return (yaml.safe_load(open(path).read()) or {}).get("domains") or {}
