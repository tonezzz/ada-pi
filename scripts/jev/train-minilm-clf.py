#!/usr/bin/env python3
"""Tiny confirm-gate classifier: frozen MiniLM-L6-v2 embeddings + logistic
head. The "43MB model" — encoder saved fp16 (~45MB), head is a few KB.

  train-minilm-clf.py --corpus train-all.jsonl --out minilm-clf/
      [--model sentence-transformers/all-MiniLM-L6-v2]

Outputs in --out:
  encoder/        HF dir (fp16 weights ~45MB)
  head.npz        logistic head W,b + threshold + meta
  result.json     val/golden metrics
"""
import argparse, json, random
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import precision_score, recall_score
from transformers import AutoModel, AutoTokenizer

GOLD = [("yes, publish it",1),("Yes",1),("go ahead",1),("ยืนยันครับ",1),
        ("ใช่ ทำเลย",1),
        ("remember that the lab stack design is approved",0),
        ("turn off the TV ok",0),("what is the weather today",0),
        ("save this to memory please",0),("confirm delete the old page",0),
        ("연연",0),("준연",0),("我 问 了 道 念",0)]


def mean_pool(model, tok, texts, dev, bs=64):
    out = []
    for s in range(0, len(texts), bs):
        enc = tok(texts[s:s + bs], truncation=True, max_length=96,
                  padding=True, return_tensors="pt").to(dev)
        with torch.no_grad():
            h = model(**enc).last_hidden_state
        m = enc["attention_mask"][..., None].float()
        out.append(((h * m).sum(1) / m.sum(1)).cpu().numpy())
    return np.concatenate(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="train-all.jsonl")
    ap.add_argument("--out", default="minilm-clf")
    ap.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    random.seed(args.seed); np.random.seed(args.seed)

    rows = [json.loads(l) for l in open(args.corpus) if l.strip()]
    texts = [r["text"] for r in rows]
    labels = np.array([int(r["label"]) for r in rows])
    idx = list(range(len(rows))); random.shuffle(idx)
    cut = int(len(idx) * 0.85)
    tr_i, va_i = idx[:cut], idx[cut:]

    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tok = AutoTokenizer.from_pretrained(args.model)
    model = AutoModel.from_pretrained(args.model).to(dev).eval()
    X = mean_pool(model, tok, texts, dev)
    Xtr, Xva, ytr, yva = X[tr_i], X[va_i], labels[tr_i], labels[va_i]

    # candidate heads: logreg and a small MLP — keep whichever vals higher
    heads = {}
    lr = LogisticRegression(class_weight="balanced", C=2.0, max_iter=2000)
    lr.fit(Xtr, ytr)
    heads["logreg"] = ("lr", lr.predict_proba(Xva)[:, 1])

    torch.manual_seed(args.seed)
    mlp = nn.Sequential(nn.Linear(X.shape[1], 128), nn.GELU(),
                        nn.Dropout(0.1), nn.Linear(128, 1)).to(dev)
    pos_w = torch.tensor([(ytr == 0).sum() / max(1, int((ytr == 1).sum()))],
                         device=dev, dtype=torch.float32)
    opt = torch.optim.AdamW(mlp.parameters(), lr=1e-3, weight_decay=1e-4)
    xt = torch.tensor(Xtr, device=dev); yt = torch.tensor(ytr[:, None], dtype=torch.float32, device=dev)
    for ep in range(300):
        mlp.train()
        loss = nn.functional.binary_cross_entropy_with_logits(
            mlp(xt), yt, pos_weight=pos_w)
        opt.zero_grad(); loss.backward(); opt.step()
    mlp.eval()
    with torch.no_grad():
        pm = torch.sigmoid(mlp(torch.tensor(Xva, device=dev))).cpu().numpy()[:, 0]
    heads["mlp"] = ("mlp", pm)

    def sweep(pv):
        best_t, best_acc = 0.5, 0.0
        for t in np.arange(0.2, 0.95, 0.05):
            acc = ((pv >= t).astype(int) == yva).mean()
            if acc > best_acc:
                best_acc, best_t = float(acc), float(t)
        return best_t, best_acc

    scored = {k: sweep(v[1]) for k, v in heads.items()}
    for k, (t, a) in scored.items():
        print(f"  {k}: val acc={a:.3f} thr={t:.2f}")
    best_name = max(scored, key=lambda k: scored[k][1])
    kind = heads[best_name][0]
    best_t, best_acc = scored[best_name]
    pv = heads[best_name][1]
    pred = (pv >= best_t).astype(int)
    print(f"val: n={len(yva)} acc={best_acc:.3f} thr={best_t:.2f} "
          f"prec={precision_score(yva, pred):.2f} rec={recall_score(yva, pred):.2f} head={best_name}")

    def predict(Xq):
        if kind == "mlp":
            mlp.eval()
            with torch.no_grad():
                return torch.sigmoid(mlp(torch.tensor(Xq, device=dev))).cpu().numpy()[:, 0]
        return lr.predict_proba(Xq)[:, 1]

    # golden sanity
    Xg = mean_pool(model, tok, [g[0] for g in GOLD], dev)
    pg = predict(Xg)
    hit = sum((p >= best_t) == bool(y) for p, (_, y) in zip(pg, GOLD))
    print(f"golden confirm: {hit}/{len(GOLD)}")
    for p, (t, y) in zip(pg, GOLD):
        print(f"  {'ok ' if (p >= best_t) == bool(y) else 'MISS'} p={p:.3f} exp={bool(y)} {t[:48]}")

    out = Path(args.out); (out / "encoder").mkdir(parents=True, exist_ok=True)
    model.half().cpu().save_pretrained(out / "encoder")   # fp16 ~45MB
    tok.save_pretrained(out / "encoder")
    save = {"kind": np.array(kind), "thr": np.array(best_t), "model": np.array(args.model),
            "W": lr.coef_.astype(np.float32), "b": lr.intercept_.astype(np.float32)}
    if kind == "mlp":
        for k, v in mlp.state_dict().items():
            save[f"mlp.{k}"] = v.cpu().numpy().astype(np.float32)
    np.savez(out / "head.npz", **save)
    (out / "result.json").write_text(json.dumps({
        "val_acc": best_acc, "thr": best_t, "golden": f"{hit}/{len(GOLD)}",
        "n_train": len(tr_i), "n_val": len(va_i)}, indent=2))
    print("saved", out)


if __name__ == "__main__":
    main()
