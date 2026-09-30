"""Jev student — confirm-gate classifier served as /v1/systemone.
Answers noul questions by scoring the user turn extracted from state."""
import json, os, re, time
import torch
from fastapi import FastAPI
from transformers import AutoTokenizer, AutoModelForSequenceClassification

CKPT = os.path.expanduser("~/jev-student/ckpt")
tok = AutoTokenizer.from_pretrained(CKPT)
model = AutoModelForSequenceClassification.from_pretrained(CKPT).eval()
THR = float((getattr(model.config, "custom_params", None) or {}).get("noul_threshold", 0.70))
app = FastAPI()

def turn_from_state(state) -> str:
    if isinstance(state, dict): state = json.dumps(state, ensure_ascii=False)
    if isinstance(state, list): state = " ".join(map(str, state))
    s = str(state)
    m = re.search(r'The user turn was:\s*"([^"]+)"', s)
    if m: return m.group(1)
    m = re.search(r'User said:\s*"([^"]+)"', s)
    if m: return m.group(1)
    return s[-300:]

@app.get("/health")
def health(): return {"ok": True, "model": "jev-student-distilmulti", "thr": THR}

@app.post("/v1/systemone")
def systemone(body: dict):
    t0 = time.time()
    state = body.get("state", "")
    questions = body.get("questions") or {}
    turn = turn_from_state(state)
    enc = tok(turn, truncation=True, max_length=96, return_tensors="pt")
    with torch.no_grad():
        p = torch.softmax(model(**enc).logits, -1)[0, 1].item()
    answers = {}
    for qid, q in questions.items():
        qtype = (q or {}).get("type")
        if qtype == "noul":
            answers[qid] = {"type": "noul", "noul": p}
        elif qtype == "choice":
            answers[qid] = {"type": "choice", "choice": "answer", "confidence": 0.0}
        else:
            answers[qid] = {"type": qtype or "unknown", "score": p}
    return {"model": "jev-student-distilmulti", "answers": answers,
            "usage": {"input_tokens": int(enc["input_ids"].shape[1]),
                      "output_tokens": 0,
                      "elapsed_s": round(time.time() - t0, 3)}}
