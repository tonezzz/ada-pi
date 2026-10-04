#!/usr/bin/env python3
"""Train a jevlike attention head on frozen Gemma features — the torch port
of open-jev's MLX `features`/`train` path (open-jev upstream requires Apple
silicon; this runs on the GTX 1650 on tony-omen).

For the noul confirm task the option set is fixed ("yes"/"no"), so option
vectors are extracted once and every corpus row contributes only its
context hidden states — the head learns q_yes/q_no attention over the
rendered prompt, exactly as docs/design/per-task-finetuning-with-gemma.md
specifies (options standalone, masked-mean pooled; context keeps all
tokens; no context leak into option features).

Usage:
  python3 train-gemma-head.py --corpus train-all.jsonl --model <gemma dir>
      --out runs/head.pt [--limit N] [--epochs 8] [--rank 256]
"""
import argparse, json, math, random, time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from transformers import AutoModelForCausalLM, AutoTokenizer

CONFIRM_STATE = (
    'Ada, a voice assistant, asked the user to confirm a memory write. '
    'The user turn was: "{turn}"'
)
INSTRUCTIONS = (
    "Did the user explicitly affirm or confirm? The turn counts as "
    "affirmation only when it is a standalone short affirmation (like "
    "yes, ok, confirm, go ahead, ยืนยัน) or begins with an affirmation. "
    "An approval word embedded inside a longer request does NOT count.")
C_TRUE = "the whole turn is a short affirmation, or it leads with one"
C_FALSE = ("no affirmation present, or an affirmative word is buried "
           "inside a longer request")
OPTIONS = [" yes", " no"]


def render_noul(turn: str) -> str:
    return (f"State:\n{CONFIRM_STATE.format(turn=turn)}\n\n"
            f"Question:\n{INSTRUCTIONS}\n\n"
            f"Answer yes when:\n{C_TRUE}\n\n"
            f"Answer no when:\n{C_FALSE}\n\n"
            "Answer yes or no.\n\nAnswer:\n")


class AttentionHead(nn.Module):
    """jevlike AttentionHead, torch port: option vector queries context
    tokens; per-option logit = q . attended-context."""

    def __init__(self, hidden: int, rank: int):
        super().__init__()
        self.rank = rank
        self.ctx_norm = nn.LayerNorm(hidden)
        self.opt_norm = nn.LayerNorm(hidden)
        self.query = nn.Linear(hidden, rank, bias=False)
        self.key = nn.Linear(hidden, rank, bias=False)
        self.value = nn.Linear(hidden, rank, bias=False)

    def forward(self, ctx, ctx_mask, opt):
        """ctx (B,Lc,H) fp32, ctx_mask (B,Lc), opt (B,N,H) -> logits (B,N)."""
        ctx = self.ctx_norm(ctx.float())
        opt = self.opt_norm(opt.float())
        q = self.query(opt)                                  # (B,N,r)
        k = self.key(ctx)                                    # (B,Lc,r)
        v = self.value(ctx)
        scores = torch.einsum("bnr,blr->bnl", q, k) / math.sqrt(self.rank)
        scores = scores.masked_fill(ctx_mask[:, None, :] == 0, -1e9)
        attn = torch.softmax(scores, dim=-1)
        attended = attn @ v                                  # (B,N,r)
        return (q * attended).sum(-1) / math.sqrt(self.rank)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="train-all.jsonl")
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", default="runs/gemma-head.pt")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--rank", type=int, default=256)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=5e-4)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--feat-cache", default="feat-cache.npz")
    args = ap.parse_args()
    torch.manual_seed(args.seed); np.random.seed(args.seed); random.seed(args.seed)

    rows = [json.loads(l) for l in open(args.corpus) if l.strip()]
    if args.limit:
        rows = rows[: args.limit]
    labels = np.array([1 if r["label"] else 0 for r in rows], dtype=np.int64)
    # options index: 0 = " yes" (affirmed), 1 = " no"  -> label True maps to 0
    labels = 1 - labels

    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tok = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForCausalLM.from_pretrained(
        args.model, torch_dtype=torch.float16 if dev.type == "cuda" else torch.float32,
    ).to(dev).eval()
    hidden = model.config.hidden_size

    cache = Path(args.feat_cache)
    if cache.exists():
        z = np.load(cache, allow_pickle=True)
        ctxs = [torch.tensor(c) for c in z["ctxs"]]
        opt_vecs = torch.tensor(z["opts"]).float()
        labels = torch.tensor(z["labels"])
        print(f"features: loaded cache {cache} ({len(ctxs)} rows)")
    else:
        # option vectors once (standalone after BOS, masked mean)
        with torch.inference_mode():
            opt_vecs = []
            for opt in OPTIONS:
                ids = tok(opt, add_special_tokens=True)["input_ids"]
                h = model(input_ids=torch.tensor([ids], device=dev),
                          output_hidden_states=True).hidden_states[-1][0]
                opt_vecs.append(h.float().mean(0).cpu().clone())
            opt_vecs = torch.stack(opt_vecs)                # (2, H)

        ctxs = []
        t0 = time.time()
        for i, r in enumerate(rows):
            ids = tok(render_noul(r["text"]), add_special_tokens=True,
                      truncation=True, max_length=512)["input_ids"]
            with torch.inference_mode():
                h = model(input_ids=torch.tensor([ids], device=dev),
                          output_hidden_states=True).hidden_states[-1][0]
            ctxs.append(h.float().cpu().clone())
            if i % 200 == 199:
                dt = time.time() - t0
                print(f"  feat {i+1}/{len(rows)} ({dt:.0f}s, "
                      f"{dt/(i+1)*1000:.0f}ms/row)", flush=True)
        np.savez(cache, ctxs=np.array([c.numpy().astype(np.float16) for c in ctxs],
                                      dtype=object),
                 opts=opt_vecs.numpy(), labels=np.asarray(labels))
        ctxs = [c for c in ctxs]
        labels = torch.tensor(labels)

    idx = list(range(len(ctxs))); random.shuffle(idx)
    cut = int(len(idx) * 0.85)
    tr_idx, va_idx = idx[:cut], idx[cut:]

    head = AttentionHead(hidden, args.rank)
    n_params = sum(p.numel() for p in head.parameters())
    print(f"train {len(tr_idx)} / val {len(va_idx)} — head {n_params/1e6:.2f}M params, rank {args.rank}")
    opt = torch.optim.AdamW(head.parameters(), lr=args.lr, weight_decay=1e-4)
    # class weight — affirmed (class 0) is the minority, upweight it
    n_pos = int((labels[tr_idx] == 0).sum()); n_neg = int((labels[tr_idx] == 1).sum())
    w = torch.tensor([n_neg / max(1, n_pos), 1.0])

    def batch(ii):
        cs = [ctxs[i] for i in ii]
        L = max(c.shape[0] for c in cs)
        B = len(cs)
        x = torch.zeros(B, L, hidden); m = torch.zeros(B, L)
        for j, c in enumerate(cs):
            x[j, : c.shape[0]] = c; m[j, : c.shape[0]] = 1
        o = opt_vecs.unsqueeze(0).expand(B, -1, -1)
        return x, m, o, labels[ii]

    def evaluate(ii):
        head.eval(); correct = 0
        with torch.no_grad():
            for s in range(0, len(ii), args.batch_size):
                x, m, o, y = batch(ii[s:s + args.batch_size])
                correct += int((head(x, m, o).argmax(-1) == y).sum())
        head.train()
        return correct / len(ii)

    best, best_state = -1.0, None
    for ep in range(1, args.epochs + 1):
        t0 = time.time(); random.shuffle(tr_idx); tot = 0.0
        for s in range(0, len(tr_idx), args.batch_size):
            x, m, o, y = batch(tr_idx[s:s + args.batch_size])
            loss = nn.functional.cross_entropy(head(x, m, o), y, weight=w)
            opt.zero_grad(); loss.backward()
            torch.nn.utils.clip_grad_norm_(head.parameters(), 1.0)
            opt.step(); tot += loss.item() * len(y)
        va = evaluate(va_idx)
        print(json.dumps({"epoch": ep, "train_loss": round(tot / len(tr_idx), 4),
                          "val_top1": round(va, 4),
                          "s": round(time.time() - t0, 1)}), flush=True)
        if va > best:
            best = va
            best_state = {k: v.clone() for k, v in head.state_dict().items()}
    head.load_state_dict(best_state)

    # noul = P(yes) = softmax index 0 (label True -> class 0); sweep threshold
    head.eval()
    probs = []
    with torch.no_grad():
        for s in range(0, len(va_idx), args.batch_size):
            x, m, o, y = batch(va_idx[s:s + args.batch_size])
            probs.append(torch.softmax(head(x, m, o), -1)[:, 0])
    pv = torch.cat(probs).numpy(); yv = (1 - labels[va_idx]).numpy()
    best_t, best_acc = 0.5, 0.0
    for t in np.arange(0.2, 0.95, 0.05):
        acc = ((pv >= t).astype(int) == yv).mean()
        if acc > best_acc:
            best_acc, best_t = float(acc), float(t)
    print(f"val: n={len(yv)} acc={best_acc:.3f} thr={best_t:.2f}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    torch.save({"head": best_state, "hidden": hidden, "rank": args.rank,
                "noul_threshold": best_t, "model": args.model,
                "options": OPTIONS, "val_acc": best_acc}, args.out)
    print("saved", args.out)


if __name__ == "__main__":
    main()
