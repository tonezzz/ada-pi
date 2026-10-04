#!/usr/bin/env python3
"""Merge the Jev confirm-gate corpora into one training set.

Sources (fetched from idc01:~/.local/share/ada/):
  jev-corpus.jsonl          live probe rows  {text, regex, jev, diverged, ts}
  jev-corpus-mined.jsonl    transcript-mined {text, regex, src, ask}
  jev-corpus-reviewed.jsonl hand/auto-reviewed diverged rows
                            {..., review_verdict: true|false|"ambiguous"}

Label policy (extends jev-corpus-export.py):
  - reviewed row with verdict true/false  -> eval set only (held out of
    training so it stays an honest failure-case benchmark)
  - reviewed row with verdict "ambiguous" -> dropped entirely
  - unreviewed diverged live row          -> dropped (regex may be wrong)
  - regex-positive row whose text carries a clear negation
    ("ไม่ใช่", "ไม่เอา", leading "เดี๋ยว/อย่า/no/nope") and NO affirmation
    inside the first 20 chars  -> relabeled False (regex false-positive
    pollution: "ไม่ ใช่ ผม ไม่ ใช่ KK" was mined as an affirmation)
  - everything else                       -> label = regex

Outputs:
  corpus-merged.jsonl   {text, label, src}   deduped by normalized text
  eval-reviewed.jsonl   {text, label}        reviewed ground truth (held out)
  corpus-stats.json     counts + per-slice breakdown (thai / embedded)
"""
import json, re, sys
from pathlib import Path

THAI_RE = re.compile(r"[ก-๙]")
# negation markers that make a regex-"positive" actually negative
NEGATE_RE = re.compile(r"ไม่\s*ใช่|ไม่\s*เอา|ไม่ต้อง|อย่า|อย่าเพิ่ง|nope|not now", re.IGNORECASE)
NEG_LEAD_RE = re.compile(r"^\s*(เดี๋ยว|อย่า|ไม่|no\b|nope|wait\b|hold on)", re.IGNORECASE)
# affirmation-ish tokens used by the production gate — for slice stats only
AFFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|yup|confirm(ed)?|go ahead|do it|sure|okay?|approved?|"
    r"proceed|absolutely|mhm|uh huh|sounds good)\b|"
    r"ใช่|ยืนยัน|ตกลง|เอาเลย|ทำเลย|ได้เลย|ทำได้|โอเค|ออเค|เออ|อือ|"
    r"ต่อไป|จัดไป|เอาสิ|ไปเลย|ทำไป|เผยแพร่เลย|ส่งเลย", re.IGNORECASE)


def norm_key(t: str) -> str:
    return " ".join(t.strip().lower().split())


def main(workdir: str) -> None:
    d = Path(workdir)
    live = [json.loads(l) for l in (d / "jev-corpus.jsonl").read_text().splitlines() if l.strip()]
    mined = [json.loads(l) for l in (d / "jev-corpus-mined.jsonl").read_text().splitlines() if l.strip()]
    reviewed = [json.loads(l) for l in (d / "jev-corpus-reviewed.jsonl").read_text().splitlines() if l.strip()]

    verdict_by_key = {}
    ambiguous_keys = set()
    for r in reviewed:
        k = norm_key(r["text"])
        v = r.get("review_verdict")
        if v == "ambiguous":
            ambiguous_keys.add(k)
        elif isinstance(v, bool):
            verdict_by_key[k] = v

    rows, dropped_div, dropped_amb, relabeled = [], 0, 0, 0
    eval_rows, eval_seen = [], set()
    for r in reviewed:
        v = r.get("review_verdict")
        k = norm_key(r["text"])
        if isinstance(v, bool) and k not in eval_seen:
            eval_seen.add(k)
            eval_rows.append({"text": r["text"].strip(), "label": v})

    for src_name, src_rows in (("live", live), ("mined", mined)):
        for r in src_rows:
            text = r.get("text", "").strip()
            if not text:
                continue
            k = norm_key(text)
            if k in ambiguous_keys:
                dropped_amb += 1
                continue
            if k in verdict_by_key:
                continue  # held out for eval
            if r.get("diverged"):
                dropped_div += 1
                continue
            label = bool(r.get("regex"))
            if label and (NEGATE_RE.search(text) or NEG_LEAD_RE.search(text)) \
                    and not AFFIRM_RE.search(text[:20]):
                label = False
                relabeled += 1
            rows.append({"text": text, "label": label, "src": src_name})

    seen, ded = set(), []
    for r in rows:
        k = norm_key(r["text"])
        if k in seen:
            continue
        seen.add(k)
        ded.append(r)

    pos = [r for r in ded if r["label"]]
    thai = [r for r in ded if THAI_RE.search(r["text"])]
    embedded = [r for r in ded if len(r["text"]) > 60 and AFFIRM_RE.search(r["text"])]
    stats = {
        "total": len(ded), "pos": len(pos), "neg": len(ded) - len(pos),
        "thai": len(thai), "thai_pos": sum(1 for r in thai if r["label"]),
        "embedded_affirm_tokens": len(embedded),
        "embedded_pos": sum(1 for r in embedded if r["label"]),
        "dropped_unreviewed_diverged": dropped_div,
        "dropped_ambiguous": dropped_amb,
        "negation_relabels": relabeled,
        "eval_reviewed_heldout": len(eval_rows),
        "src_counts": {s: sum(1 for r in ded if r["src"] == s) for s in ("live", "mined")},
    }
    (d / "corpus-merged.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ded))
    (d / "eval-reviewed.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in eval_rows))
    (d / "corpus-stats.json").write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
