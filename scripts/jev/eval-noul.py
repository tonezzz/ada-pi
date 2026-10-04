#!/usr/bin/env python3
"""Score a /v1/systemone noul endpoint against labeled confirm turns.

Reads JSONL rows {text, label} (or corpus rows with "regex"), wraps each
in the production CONFIRM_STATE prompt, asks the CONFIRM_Q noul question
(same wording as tests/bench/jev-bench.py), and reports accuracy at a
threshold plus per-slice stats (thai / embedded / positive / negative).

  eval-noul.py BASE_URL cases.jsonl [--thr 0.75] [--limit N] [--out results.json]
"""
import argparse, json, re, sys, time, urllib.request

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
THAI_RE = re.compile(r"[ก-๙]")
AFFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|yup|confirm(ed)?|go ahead|do it|sure|okay?|approved?|"
    r"proceed|absolutely|mhm|uh huh|sounds good)\b|"
    r"ใช่|ยืนยัน|ตกลง|เอาเลย|ทำเลย|ได้เลย|ทำได้|โอเค|ออเค|เออ|อือ|"
    r"ต่อไป|จัดไป|เอาสิ|ไปเลย|ทำไป|เผยแพร่เลย|ส่งเลย", re.IGNORECASE)


def ask(base, turn, timeout=120):
    payload = {"state": CONFIRM_STATE.format(turn=turn), "questions": {"q": CONFIRM_Q}}
    req = urllib.request.Request(f"{base}/v1/systemone", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    r = json.load(urllib.request.urlopen(req, timeout=timeout))
    return float(r["answers"]["q"]["noul"]), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("cases")
    ap.add_argument("--thr", type=float, default=0.75)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.cases) if l.strip()]
    if args.limit:
        rows = rows[: args.limit]
    res = []
    for i, r in enumerate(rows):
        text = r["text"]
        label = bool(r.get("label", r.get("regex")))
        try:
            p, dt = ask(args.base_url, text)
        except Exception as e:
            print(f"  ERR {i} {text[:50]}: {e}", file=sys.stderr)
            p, dt = None, 0
        res.append({"text": text, "label": label, "p": p, "s": round(dt, 2),
                    "thai": bool(THAI_RE.search(text)),
                    "embedded": len(text) > 60 and bool(AFFIRM_RE.search(text))})
        if i % 50 == 49:
            print(f"  ...{i+1}/{len(rows)}", file=sys.stderr)

    def acc(sub):
        ok = [x for x in sub if x["p"] is not None]
        if not ok:
            return None
        return round(sum((x["p"] >= args.thr) == x["label"] for x in ok) / len(ok), 4)

    def f1(sub):
        tp = sum(1 for x in sub if x["p"] is not None and x["p"] >= args.thr and x["label"])
        fp = sum(1 for x in sub if x["p"] is not None and x["p"] >= args.thr and not x["label"])
        fn = sum(1 for x in sub if x["p"] is not None and x["p"] < args.thr and x["label"])
        return round(2 * tp / max(1, 2 * tp + fp + fn), 4)

    summary = {
        "base_url": args.base_url, "thr": args.thr, "n": len(res),
        "acc": acc(res), "f1": f1(res),
        "acc_thai": acc([x for x in res if x["thai"]]),
        "acc_embedded": acc([x for x in res if x["embedded"]]),
        "acc_pos": acc([x for x in res if x["label"]]),
        "acc_neg": acc([x for x in res if not x["label"]]),
        "misses": [{"text": x["text"][:80], "label": x["label"], "p": x["p"]}
                   for x in res if x["p"] is not None and (x["p"] >= args.thr) != x["label"]][:40],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.out:
        json.dump({"summary": summary, "rows": res}, open(args.out, "w"), ensure_ascii=False)


if __name__ == "__main__":
    main()
