#!/usr/bin/env python3
"""Serve the Gemma-feature attention head as /v1/systemone (noul only).

Loads gemma-3-1b once (CUDA fp16), extracts context hidden states per
request, scores with the trained AttentionHead. Choice/score questions
get a stub answer — the head is a per-task noul model.

  serve-gemma-head.py --ckpt runs/gemma-head.pt --host 127.0.0.1 --port 8780
"""
import argparse, json, re, sys, time

import torch
import torch.nn as nn
import uvicorn
from fastapi import FastAPI
from transformers import AutoModelForCausalLM, AutoTokenizer

sys.path.insert(0, str(__file__ and __import__("pathlib").Path(__file__).parent))
from importlib.machinery import SourceFileLoader
_mod = SourceFileLoader("tgh", str(__import__("pathlib").Path(__file__).with_name("train-gemma-head.py"))).load_module()
AttentionHead = _mod.AttentionHead
render_noul = _mod.render_noul

ap = argparse.ArgumentParser()
ap.add_argument("--ckpt", required=True)
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8780)
args = ap.parse_args()

ck = torch.load(args.ckpt, map_location="cpu", weights_only=False)
MODEL_DIR = ck["model"]
THR = float(ck["noul_threshold"])
dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
tok = AutoTokenizer.from_pretrained(MODEL_DIR)
model = AutoModelForCausalLM.from_pretrained(
    MODEL_DIR, torch_dtype=torch.float16 if dev.type == "cuda" else torch.float32
).to(dev).eval()
head = AttentionHead(ck["hidden"], ck["rank"])
head.load_state_dict(ck["head"]); head.eval()

with torch.inference_mode():
    opt_vecs = []
    for opt in ck["options"]:
        ids = tok(opt, add_special_tokens=True)["input_ids"]
        h = model(input_ids=torch.tensor([ids], device=dev),
                  output_hidden_states=True).hidden_states[-1][0]
        opt_vecs.append(h.float().mean(0).cpu())
    opt_vecs = torch.stack(opt_vecs)

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


def noul_prob(turn: str) -> float:
    ids = tok(render_noul(turn), add_special_tokens=True,
              truncation=True, max_length=512)["input_ids"]
    with torch.inference_mode():
        h = model(input_ids=torch.tensor([ids], device=dev),
                  output_hidden_states=True).hidden_states[-1][0].float().cpu()
    ctx = h.unsqueeze(0); mask = torch.ones(1, h.shape[0])
    opt = opt_vecs.unsqueeze(0)
    with torch.no_grad():
        return torch.softmax(head(ctx, mask, opt), -1)[0, 0].item()


@app.get("/health")
def health():
    return {"ok": True, "model": f"gemma-head:{MODEL_DIR}", "thr": THR}


@app.post("/v1/systemone")
def systemone(body: dict):
    t0 = time.time()
    turn = turn_from_state(body.get("state", ""))
    p = noul_prob(turn)
    answers = {}
    for qid, q in (body.get("questions") or {}).items():
        qtype = (q or {}).get("type")
        if qtype == "noul":
            answers[qid] = {"type": "noul", "noul": p}
        elif qtype == "choice":
            answers[qid] = {"type": "choice", "choice": "answer", "confidence": 0.0}
        else:
            answers[qid] = {"type": qtype or "unknown", "score": p}
    return {"model": "gemma-1b-head", "answers": answers,
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "elapsed_s": round(time.time() - t0, 3)}}


uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
