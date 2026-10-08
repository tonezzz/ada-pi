#!/usr/bin/env python3
"""Build the bankq student training set (card nest-bank-router, Phase 2).

Inputs
  --corpus   tests/bench/bankq-corpus-*.jsonl — mined prod rows
             {query, utterance, bank_arg, hit_banks, top_bank, ...}
  --augment  tests/bench/bankq-augment.jsonl — hand-labeled
             no-memory utterances ({text, labels})
  --cases    optional extra labeled rows (NOT the golden/adv/hard eval
             sets — those stay eval-only per the suite contract)

Label rules
  - bank_arg naming real bank(s) (incl. comma lists + aliases) =>
    positives = resolved bank set. Ada already routed; strongest label.
  - bank_arg 'all' with a top_bank => positives = {top_bank} — the free
    mined label ("which bank produced the cited hit"), noisy by nature.
  - zero-hit 'all' rows are NOT auto-labeled skip — a miss isn't proof
    no lookup was warranted (phase-0 doc, open_items). Dropped.
  - person-scoped banks (personal-*) collapse to 'personal'.
  - Every row yields up to two texts — the user utterance AND the search
    query — the router sees whichever the caller passes at runtime.

Output: {text, labels:[...], src} JSONL rows, deduped on
(norm_text, frozenset(labels)), labels restricted to the domains.yml
bankq criteria keys (bank names + 'sessions' + 'skip').
"""
import argparse, json, re, sys
from collections import Counter
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent.parent / "tests" / "bench"

# Free-text bank_arg aliases seen in prod -> canonical bank label.
# Ambiguous tokens ('kb', 'banks', 'apps') resolve to nothing — the row
# falls back to top_bank or drops.
ALIAS = {
    "infra": "infrastructure-ssot",
    "kb-infrastructure-ssot": "infrastructure-ssot",
    "ops": "ops-scenarios",
    "persona-kk": "personal",
    "personal-kk": "personal",
    "chaba-archive": "chaba-docs",
}
RESOLVE_DROP = {"all", "*", "", "kb", "banks", "apps"}


def labels_from_domains(domains_path: Path) -> list[str]:
    dom = yaml.safe_load(domains_path.read_text())
    crit = dom["domains"]["bankq"]["question"]["criteria"]
    return list(crit.keys())


def norm_bank(name: str, valid: set[str]) -> str | None:
    n = str(name or "").strip().lower()
    if not n or n in RESOLVE_DROP:
        return None
    n = ALIAS.get(n, n)
    if n in valid:
        return n
    # person-scoped banks: personal-<id> -> personal
    if n.startswith("personal-") or n.startswith("persona-"):
        return "personal" if "personal" in valid else None
    return None


def norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", str(t or "").strip())


# Mined top_bank is "which bank produced the cited hit" — NOT intent.
# Measured against the hand-corrected hard set (2026-10-08): 2/22 agree.
# The transcript/report banks (sessions=session-summary docs,
# devin=session dumps, cms=published pages) match nearly everything —
# they win top hit on non-intent turns (junk-hit banks), so their mined
# labels are dropped and covered by hand-labeled augment rows instead.
MINED_LABEL_DROP = {"sessions", "devin", "cms"}


def row_labels(r: dict, valid: set[str],
               drop_mined: set[str]) -> list[str]:
    arg = str(r.get("bank_arg") or "").strip().lower()
    if arg and arg not in ("all", "*"):
        out = {b for a in arg.split(",")
               if (b := norm_bank(a, valid))}
        if out:
            return sorted(out)
        # Unresolvable arg ('kb', 'apps', ...) — fall through to top_bank.
    tb = norm_bank(r.get("top_bank"), valid)
    if tb and tb not in drop_mined:
        return [tb]
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(BENCH / "bankq-corpus-20261008.jsonl"))
    ap.add_argument("--augment", default=str(BENCH / "bankq-augment.jsonl"))
    ap.add_argument("--cases", default="", help="extra {text,label} rows")
    ap.add_argument("--domains", default=str(BENCH / "domains.yml"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    valid = set(labels_from_domains(Path(args.domains)))
    seen: dict[str, dict] = {}
    stats = Counter()

    def put(text: str, labels: list[str], src: str) -> None:
        t = norm_text(text)
        labs = sorted({l for l in labels if l in valid})
        if not t or not labs:
            stats["dropped_empty"] += 1
            return
        key = t.lower()
        if key in seen:
            # Same text, different label set — the labels are
            # multi-hot positives, not competing classes: union them
            # (that's exactly what multi-label is for) instead of
            # keeping contradictory rows.
            merged = sorted(set(seen[key]["labels"]) | set(labs))
            if merged == seen[key]["labels"]:
                stats["dedup"] += 1
            else:
                seen[key]["labels"] = merged
                stats["label_merge"] += 1
            return
        seen[key] = {"text": t, "labels": labs, "src": src}
        stats[f"src:{src}"] += 1

    drop_mined = MINED_LABEL_DROP & valid
    for line in Path(args.corpus).read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        labs = row_labels(r, valid, drop_mined)
        if not labs:
            stats["unlabeled"] += 1
            continue
        if r.get("utterance"):
            put(r["utterance"], labs, "corpus-utterance")
        if r.get("query"):
            put(r["query"], labs, "corpus-query")

    if args.augment and Path(args.augment).exists():
        for line in Path(args.augment).read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                put(r.get("text"), r.get("labels") or [], "augment")

    if args.cases:
        for line in Path(args.cases).read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                lab = r.get("labels") or ([r["label"]] if r.get("label") else [])
                put(r.get("text"), lab, "cases")

    rows = list(seen.values())
    Path(args.out).write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))

    lab_count = Counter(l for r in rows for l in r["labels"])
    print(f"wrote {len(rows)} rows -> {args.out}")
    print("stats:", dict(stats))
    print("labels:", dict(lab_count.most_common()))
    n_skip = lab_count.get("skip", 0)
    print(f"skip fraction: {n_skip}/{len(rows)} = {n_skip/max(1,len(rows)):.2f}")


if __name__ == "__main__":
    main()
