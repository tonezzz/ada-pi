#!/usr/bin/env python3
"""Bankq student — memory-bank router served as /v1/systemone
(card nest-bank-router, Phase 2/3). Answers 'choice' questions by
scoring the user turn (or search query) with the multi-label
distilbert checkpoint — argmax class -> choice, sigmoid prob ->
confidence.

  BANKQ_CKPT=~/bankq-student/ckpt serve-student.py --port 8791
"""
import argparse, json, os, re, time

import torch
from fastapi import FastAPI
from transformers import AutoTokenizer, AutoModelForSequenceClassification

ap = argparse.ArgumentParser()
ap.add_argument("--ckpt", default=os.path.expanduser(
    os.environ.get("BANKQ_CKPT", "~/bankq-student/ckpt")))
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8791)
ap.add_argument("--skip-thr", type=float, default=0.0,
                help="skip must beat this prob to win (0 = plain argmax)")
args = ap.parse_args()

tok = AutoTokenizer.from_pretrained(args.ckpt)
model = AutoModelForSequenceClassification.from_pretrained(args.ckpt).eval()
cfg = getattr(model.config, "custom_params", None) or {}
LABELS = cfg.get("bankq_labels") or [
    l.rsplit(".", 1)[-1] for l in sorted(
        (model.config.id2label or {}).values())]
NAME = f"bankq-student:{os.path.basename(args.ckpt.rstrip('/'))}"
app = FastAPI()


def turn_from_state(state) -> str:
    if isinstance(state, dict):
        state = json.dumps(state, ensure_ascii=False)
    if isinstance(state, list):
        state = " ".join(map(str, state))
    s = str(state)
    m = re.search(r'The user turn was:\s*"([^"]+)"', s)
    if m:
        return m.group(1)
    m = re.search(r'User said:\s*"([^"]+)"', s)
    if m:
        return m.group(1)
    return s[-300:]


@app.get("/health")
def health():
    return {"ok": True, "model": NAME, "labels": LABELS,
            "skip_thr": args.skip_thr}


@app.post("/v1/systemone")
def systemone(body: dict):
    t0 = time.time()
    questions = body.get("questions") or {}
    turn = turn_from_state(body.get("state", ""))
    enc = tok(turn, truncation=True, max_length=96, return_tensors="pt")
    with torch.no_grad():
        p = torch.sigmoid(
            model(**enc).logits)[0].tolist()
    ranked = sorted(range(len(p)), key=lambda i: p[i], reverse=True)
    top = ranked[0]
    if (args.skip_thr and LABELS[top] == "skip"
            and p[top] < args.skip_thr):
        top = next(i for i in ranked if LABELS[i] != "skip")
    answers = {}
    for qid, q in questions.items():
        qtype = (q or {}).get("type")
        if qtype == "choice":
            answers[qid] = {
                "type": "choice", "choice": LABELS[top],
                "confidence": round(p[top], 4),
                "scores": {LABELS[i]: round(p[i], 4)
                           for i in ranked[:4]},
            }
        elif qtype == "noul":
            answers[qid] = {"type": "noul", "noul": p[top]}
        else:
            answers[qid] = {"type": qtype or "unknown", "score": p[top]}
    return {"model": NAME, "answers": answers,
            "usage": {"input_tokens": int(enc["input_ids"].shape[1]),
                      "output_tokens": 0,
                      "elapsed_s": round(time.time() - t0, 3)}}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
