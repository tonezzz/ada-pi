#!/usr/bin/env python3
"""Evaluate the minilm-clf artifact directly (no server) on a labeled jsonl.

  eval-minilm.py minilm-clf/ eval-all.jsonl --thr 0.75 --out minilm-evalall.json
"""
import argparse, json, sys

import numpy as np
import torch
import torch.nn as nn
from transformers import AutoModel, AutoTokenizer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("corpus")
    ap.add_argument("--thr", type=float, default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    z = np.load(f"{args.dir}/head.npz", allow_pickle=True)
    kind = str(z["kind"]); thr = args.thr if args.thr is not None else float(z["thr"])
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tok = AutoTokenizer.from_pretrained(f"{args.dir}/encoder")
    model = AutoModel.from_pretrained(f"{args.dir}/encoder").to(dev).eval()

    H = model.config.hidden_size
    if kind == "mlp":
        mlp = nn.Sequential(nn.Linear(H, 128), nn.GELU(), nn.Dropout(0.0),
                            nn.Linear(128, 1)).to(dev)
        sd = {k[4:]: torch.tensor(z[k]) for k in z.files
              if k.startswith("mlp.")}
        mlp.load_state_dict(sd); mlp.eval()

    def probs(texts):
        out = []
        for s in range(0, len(texts), 64):
            enc = tok(texts[s:s + 64], truncation=True, max_length=96,
                      padding=True, return_tensors="pt").to(dev)
            with torch.no_grad():
                h = model(**enc).last_hidden_state
            m = enc["attention_mask"][..., None].float()
            emb = (h * m).sum(1) / m.sum(1)
            if kind == "mlp":
                with torch.no_grad():
                    p = torch.sigmoid(mlp(emb.float()))[:, 0]
            else:
                x = emb.float().cpu().numpy()
                p = 1 / (1 + np.exp(-(x @ z["W"].T + z["b"])))
                p = torch.tensor(p[:, 0])
            out.append(p.float().cpu().numpy())
        return np.concatenate(out)

    rows = [json.loads(l) for l in open(args.corpus) if l.strip()]
    ps = probs([r["text"] for r in rows])
    res = []
    for p, r in zip(ps, rows):
        lab = bool(r["label"]); got = bool(p >= thr)
        res.append({"text": r["text"], "label": lab, "p": float(p),
                    "ok": got == lab})

    def acc(sel): s = [x for x in res if sel(x)]; return round(
        np.mean([x["ok"] for x in s]), 4) if s else None
    thai = lambda x: any("\u0e00" <= c <= "\u0e7f" for c in x["text"])
    emb = lambda x: any(w in x["text"].lower() for w in
                        ("confirm", "ต่อไป", "ยืนยัน", "ok ", " ok"))
    summ = {
        "thr": thr, "n": len(res),
        "acc": acc(lambda x: True),
        "acc_pos": acc(lambda x: x["label"]),
        "acc_neg": acc(lambda x: not x["label"]),
        "acc_thai": acc(thai),
        "acc_embedded": acc(emb),
        "misses": [x for x in res if not x["ok"]],
    }
    p_lbl = np.array([x["p"] >= thr for x in res])
    y = np.array([x["label"] for x in res])
    tp = int(((p_lbl) & (y)).sum()); fp = int((p_lbl & ~y).sum())
    fn = int((~p_lbl & y).sum())
    summ["precision"] = round(tp / max(1, tp + fp), 3)
    summ["recall"] = round(tp / max(1, tp + fn), 3)
    summ["f1"] = round(2 * tp / max(1, 2 * tp + fp + fn), 3)
    print(json.dumps({k: v for k, v in summ.items() if k != "misses"}))
    for m in summ["misses"]:
        print(" MISS", round(m["p"], 3), m["label"], m["text"][:60])
    if args.out:
        open(args.out, "w").write(json.dumps(summ, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
