#!/usr/bin/env python3
"""Package the Jev advisory corpus for Colab fine-tuning.

Sources
-------
1. ``~/.local/share/ada/jev-corpus.jsonl`` — rows appended by the live
   probe in realtime_provider (transcript text, regex label, Jev score,
   divergence flag). These are REAL production turns.
2. ``tests/bench/jev-bench.py`` confirm-gate cases — hand-labeled golden
   set, always included as eval data.

Output
------
``jev-corpus-out/`` (or --out dir) containing:

  train.jsonl   — {text, label} for student training. Label = regex
                  verdict, EXCEPT diverged rows which are excluded from
                  training (regex may be wrong there — that's exactly
                  the ambiguity the student shouldn't inherit).
  diverged.jsonl — divergence rows for hand review → promote to
                  golden set after labeling.
  eval.jsonl    — golden bench cases (never trained on).
  summary.json  — counts + label balance.

The bench cases carry ``expected`` labels from jev-bench.py; corpus
labels come from the production regex gate. Diverged rows are the
highest-value data — review them before training.

Usage:
  python3 scripts/jev-corpus-export.py [--corpus PATH] [--out DIR]
"""
from __future__ import annotations

import argparse

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = Path.home() / ".local/share/ada/jev-corpus.jsonl"
BENCH = REPO / "tests/bench/jev-bench.py"


def _load_bench_cases() -> list[dict]:
    """Pull CONFIRM_CASES out of jev-bench.py via AST — the module runs
    argparse at top level so exec/import would eat our argv."""
    import ast
    tree = ast.parse(BENCH.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "CONFIRM_CASES":
                    return [
                        {"text": str(c[0]), "label": bool(c[1])}
                        for c in ast.literal_eval(node.value)
                    ]
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    ap.add_argument("--out", default="jev-corpus-out")
    args = ap.parse_args()

    corpus_path = Path(args.corpus)
    rows = []
    if corpus_path.exists():
        for line in corpus_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    else:
        print(f"no corpus at {corpus_path} — bench cases only")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    train, diverged = [], []
    for r in rows:
        row = {"text": r["text"], "label": bool(r["regex"])}
        if r.get("diverged"):
            row["jev"] = r.get("jev")
            diverged.append(row)
        else:
            train.append(row)

    eval_rows = _load_bench_cases()

    (out_dir / "train.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in train))
    (out_dir / "diverged.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in diverged))
    (out_dir / "eval.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in eval_rows))

    pos = sum(1 for r in train if r["label"])
    summary = {
        "corpus_rows": len(rows), "train": len(train),
        "train_pos": pos, "train_neg": len(train) - pos,
        "diverged_pending_review": len(diverged),
        "eval_golden": len(eval_rows),
        "note": "diverged rows excluded from train until hand-labeled",
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
