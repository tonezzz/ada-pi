#!/usr/bin/env python3
"""Train the bankq student — distilbert-multilingual multi-label seq-cls
over the memory-bank label space + 'skip' (card nest-bank-router,
Phase 2). Trains in a few minutes on a 1650-class GPU.

Input: JSONL rows {text, labels:[bank|sessions|skip, ...]} from
scripts/bankq/build-train.py. The label space comes from
tests/bench/domains.yml bankq criteria keys so the model's classes and
the bench's choice options can never drift.

The model emits a sigmoid score per bank — the server maps argmax to a
'choice' answer (and can widen to a bank set for fan-out routing).
'skip' as a positive means "no memory search warranted".
"""
import json, os, random, sys
from pathlib import Path

import numpy as np
import torch
import yaml
from transformers import (AutoTokenizer, AutoModelForSequenceClassification,
                          Trainer, TrainingArguments)

random.seed(7); np.random.seed(7); torch.manual_seed(7)
MODEL = "distilbert-base-multilingual-cased"
OUT = os.path.expanduser(os.environ.get("BANKQ_OUT", "~/bankq-student/ckpt"))
HERE = Path(__file__).resolve().parent
BENCH = HERE.parent.parent / "tests" / "bench"

dom = yaml.safe_load((BENCH / "domains.yml").read_text())
LABELS = list(dom["domains"]["bankq"]["question"]["criteria"].keys())
N = len(LABELS)
LIDX = {l: i for i, l in enumerate(LABELS)}
SKIP = LIDX["skip"]

data = [json.loads(l) for l in
        open(os.environ.get("BANKQ_TRAIN", "/tmp/bankq-train.jsonl"))]
texts = [d["text"].strip() for d in data if d["text"].strip()]
Y = np.zeros((len(texts), N), dtype=np.float32)
for i, d in enumerate(d for d in data if d["text"].strip()):
    for l in d["labels"]:
        Y[i, LIDX[l]] = 1.0

idx = list(range(len(texts))); random.shuffle(idx)
cut = int(len(idx) * 0.85)
tr_idx, va_idx = idx[:cut], idx[cut:]

tok = AutoTokenizer.from_pretrained(MODEL)
def enc(ii):
    return tok([texts[i] for i in ii], truncation=True, max_length=96,
               padding=True, return_tensors="pt")

class DS(torch.utils.data.Dataset):
    def __init__(self, ii):
        self.e = enc(ii); self.y = torch.tensor(Y[ii])
    def __len__(self): return len(self.y)
    def __getitem__(self, k):
        return {**{k2: v[k] for k2, v in self.e.items()}, "labels": self.y[k]}

model = AutoModelForSequenceClassification.from_pretrained(
    MODEL, num_labels=N, problem_type="multi_label_classification")

# pos_weight = neg/pos per class, clamped — devin/sessions dominate the
# mined corpus (single-digit rows for note/documents/kb-features would
# never fire without it).
pos = Y[tr_idx].sum(0)
pw = np.clip((len(tr_idx) - pos) / np.maximum(pos, 1.0), 1.0, 6.0)
POS_W = torch.tensor(pw, dtype=torch.float32)

class T(Trainer):
    def compute_loss(self, model, inputs, return_outputs=False, **kw):
        lab = inputs.pop("labels"); out = model(**inputs)
        loss = torch.nn.functional.binary_cross_entropy_with_logits(
            out.logits, lab, pos_weight=POS_W.to(out.logits.device))
        return (loss, out) if return_outputs else loss

args = TrainingArguments(
    output_dir="/tmp/bankq-trainer", num_train_epochs=8,
    per_device_train_batch_size=24, learning_rate=3e-5,
    warmup_ratio=0.06, weight_decay=0.01, logging_steps=25,
    save_strategy="no", report_to=[], seed=7)
trainer = T(model=model, args=args, train_dataset=DS(tr_idx))
trainer.train()
model.eval()

def probs(ii):
    e = enc(ii)
    with torch.no_grad():
        return torch.sigmoid(
            model(**{k: v.to(model.device) for k, v in e.items()}).logits
        ).cpu().numpy()

# val: top-1 pick inside the gold label set, plus skip precision/recall
vp = probs(va_idx)
top = vp.argmax(1)
hit1 = sum(LABELS[t] in [LABELS[i] for i in np.where(Y[j] > 0)[0]]
           for j, t in zip(va_idx, top))
skip_pred = top == SKIP
skip_true = Y[va_idx, SKIP] > 0
tp = int((skip_pred & skip_true).sum()); fp = int((skip_pred & ~skip_true).sum())
fn = int((~skip_pred & skip_true).sum())
print(f"val: n={len(va_idx)} top1-in-labels={hit1/len(va_idx):.3f} "
      f"skip: prec={tp/max(1,tp+fp):.2f} rec={tp/max(1,tp+fn):.2f}")

model.config.custom_params = {"bankq_labels": LABELS, "skip_index": SKIP}
model.save_pretrained(OUT); tok.save_pretrained(OUT)
print("saved", OUT)
