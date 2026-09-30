#!/usr/bin/env python3
"""Fine-tune distilbert-multilingual on the Jev confirm corpus -> binary
noul classifier. Trains in a few minutes on a 1650-class GPU; a CPU run
takes ~20-30min. Corpus: merge jev-corpus.jsonl + jev-corpus-mined.jsonl
into JEV_CORPUS (or default /tmp/jev-all.json) as
{text, label, jev, src, tool} rows — see tests/bench/jev-bench.py.
Serve the result with scripts/serve-jev-student.py (FastAPI /v1/systemone
shim, noul-only)."""
import json, os, random, sys
import numpy as np
import torch
from transformers import (AutoTokenizer, AutoModelForSequenceClassification,
                          Trainer, TrainingArguments)

random.seed(7); np.random.seed(7); torch.manual_seed(7)
MODEL = "distilbert-base-multilingual-cased"
OUT = os.path.expanduser("~/jev-student/ckpt")

data = json.load(open(os.environ.get("JEV_CORPUS", "/tmp/jev-all.json")))
texts = [d["text"].strip() for d in data if d["text"].strip()]
labels = [int(d["label"]) for d in data if d["text"].strip()]
idx = list(range(len(texts))); random.shuffle(idx)
cut = int(len(idx) * 0.85)
tr_idx, va_idx = idx[:cut], idx[cut:]

tok = AutoTokenizer.from_pretrained(MODEL)
def enc(ii):
    return tok([texts[i] for i in ii], truncation=True, max_length=96,
               padding=True, return_tensors="pt")

class DS(torch.utils.data.Dataset):
    def __init__(self, ii):
        self.e = enc(ii); self.y = torch.tensor([labels[i] for i in ii])
    def __len__(self): return len(self.y)
    def __getitem__(self, k):
        return {**{k2: v[k] for k2, v in self.e.items()}, "labels": self.y[k]}

model = AutoModelForSequenceClassification.from_pretrained(MODEL, num_labels=2)
# class weight for the 5:1 negative skew
w = torch.tensor([1.0, sum(1 for l in labels if not l) / max(1, sum(labels))])
def weighted_loss(model, inputs, return_outputs=False, **kw):
    lab = inputs.pop("labels"); out = model(**inputs)
    loss = torch.nn.functional.cross_entropy(
        out.logits, lab, weight=w.to(out.logits.device))
    return (loss, out) if return_outputs else loss

class T(Trainer):
    def compute_loss(self, model, inputs, return_outputs=False, **kw):
        return weighted_loss(model, inputs, return_outputs)

args = TrainingArguments(
    output_dir="/tmp/jev-student-trainer", num_train_epochs=4,
    per_device_train_batch_size=16, per_device_eval_batch_size=32,
    learning_rate=2e-5, warmup_steps=10, logging_steps=20,
    eval_strategy="epoch", save_strategy="no", report_to=[],
    fp16=True, seed=7)

tr = T(model=model, args=args, train_dataset=DS(tr_idx), eval_dataset=DS(va_idx))
tr.train()

model.eval()
def probs(ii):
    e = enc(ii)
    with torch.no_grad():
        p = torch.softmax(model(**{k: v.to(model.device) for k, v in e.items()}).logits, -1)
    return p[:, 1].cpu().numpy()

va = probs(va_idx); yv = np.array([labels[i] for i in va_idx])
# threshold sweep — find the noul cutoff that maximizes accuracy
best_t, best_acc = 0.5, 0
for t in np.arange(0.3, 0.9, 0.05):
    acc = ((va >= t).astype(int) == yv).mean()
    if acc > best_acc: best_acc, best_t = acc, t
pred = (va >= best_t).astype(int)
tp = int(((pred == 1) & (yv == 1)).sum()); fp = int(((pred == 1) & (yv == 0)).sum())
fn = int(((pred == 0) & (yv == 1)).sum()); tn = int(((pred == 0) & (yv == 0)).sum())
print(f"val: n={len(yv)} acc={best_acc:.3f} thr={best_t:.2f} "
      f"prec={tp/max(1,tp+fp):.2f} rec={tp/max(1,tp+fn):.2f}")

model.config.custom_params = {"noul_threshold": float(best_t)}
model.save_pretrained(OUT); tok.save_pretrained(OUT)
print("saved", OUT)

# golden-set sanity — the jev-bench confirm cases verbatim
GOLD = [("yes, publish it",1),("Yes",1),("go ahead",1),("ยืนยันครับ",1),
        ("ใช่ ทำเลย",1),
        ("remember that the lab stack design is approved",0),
        ("turn off the TV ok",0),("what is the weather today",0),
        ("save this to memory please",0),("confirm delete the old page",0),
        ("연연",0),("준연",0),("我 问 了 道 念",0)]
gk = [g[0] for g in GOLD]; gp = probs(list(range(len(gk)))) if False else None
e = tok(gk, truncation=True, max_length=96, padding=True, return_tensors="pt")
with torch.no_grad():
    gp = torch.softmax(model(**{k: v.to(model.device) for k, v in e.items()}).logits, -1)[:, 1].cpu().numpy()
hit = sum(((p >= best_t) == bool(y)) for p, (_, y) in zip(gp, GOLD))
print(f"golden confirm: {hit}/{len(GOLD)}")
for p, (t, y) in zip(gp, GOLD):
    mark = "ok " if (p >= best_t) == bool(y) else "MISS"
    print(f"  {mark} p={p:.3f} exp={bool(y)} {t[:48]}")
