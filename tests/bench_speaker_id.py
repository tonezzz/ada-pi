#!/usr/bin/env python3
"""Speaker-ID benchmark — measures embedding-space stability of the
enrolled profiles without needing recorded audio.

Method: load speaker_profiles.json, report pairwise cosine distances
(the root-cause metric — two profiles of the same voice cluster >0.4),
then per profile run N trials of `embedding + Gaussian noise` through
identify() to measure: accuracy (correctly re-identified), margin
distribution, and flip count (wrong-name hits — the instability metric
from the 2026-09-27 กุ้ง↔NewSpeaker ping-pong).

Usage (on idc01 or anywhere with numpy):
    ADA_SPEAKER_PROFILES=/path/profiles.json python3 tests/bench_speaker_id.py
    python3 tests/bench_speaker_id.py --sigma 0.05 --trials 200 --json
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ADA_SPEAKER_PROFILES",
                      str(Path.home() / ".local/share/ada-pi/speaker_profiles.json"))
from backend import speaker_id  # noqa: E402


def load_profiles() -> dict[str, np.ndarray]:
    path = Path(os.environ["ADA_SPEAKER_PROFILES"])
    data = json.loads(path.read_text())
    out = {}
    for name, prof in (data.get("profiles") or data).items():
        emb = prof.get("embedding") if isinstance(prof, dict) else prof
        if isinstance(emb, str):
            v = np.frombuffer(base64.b64decode(emb), dtype=np.float32)
            out[name] = v / (np.linalg.norm(v) or 1)
    return out


def cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sigma", type=float, default=0.05,
                    help="Gaussian noise level applied to each probe embedding")
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--threshold", type=float, default=speaker_id.DEFAULT_THRESHOLD)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    profiles = load_profiles()
    names = sorted(profiles)
    rng = np.random.default_rng(0)

    # 1. pairwise distances — two profiles of the same voice cluster high
    pairs = []
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            pairs.append({"a": x, "b": y, "cosine": round(cos(profiles[x], profiles[y]), 3)})
    pairs.sort(key=lambda p: -p["cosine"])

    # 2. per-speaker accuracy/flip/margin under embedding noise
    ident = speaker_id.SpeakerIdentifier.__new__(speaker_id.SpeakerIdentifier)
    ident._model = True  # never touched — we stub the embedding source
    ident._enrolled = dict(profiles)
    rows = []
    for name in names:
        ref = profiles[name]
        ok = flips = unknown = 0
        scores, flip_to = [], {}
        for _ in range(a.trials):
            probe = ref + a.sigma * rng.standard_normal(ref.shape).astype(np.float32)
            probe /= np.linalg.norm(probe)
            emb_call = lambda *_, **__: probe  # noqa: E731
            orig = ident._compute_embedding if hasattr(ident, "_compute_embedding") else None
            ident._compute_embedding = emb_call  # type: ignore[attr-defined]
            got, score = ident.identify(b"\x00" * 64000)
            if orig is not None:
                ident._compute_embedding = orig  # type: ignore[attr-defined]
            else:
                del ident._compute_embedding
            scores.append(score)
            if got == name:
                ok += 1
            elif got is None:
                unknown += 1
            else:
                flips += 1
                flip_to[got] = flip_to.get(got, 0) + 1
        rows.append({
            "speaker": name, "trials": a.trials, "sigma": a.sigma,
            "ok": ok, "unknown": unknown, "flips": flips,
            "flip_to": flip_to,
            "acc": round(ok / a.trials, 3),
            "score_min": round(min(scores), 3),
            "score_med": round(float(np.median(scores)), 3),
            "score_max": round(max(scores), 3),
        })

    if a.json:
        print(json.dumps({"pairwise": pairs, "per_speaker": rows}, indent=1))
        return 0

    print(f"profiles: {len(names)}  sigma={a.sigma}  trials={a.trials}  "
          f"threshold={a.threshold}")
    print("\n== pairwise cosine (suspicious if > 0.40 = likely same voice) ==")
    for p in pairs:
        flag = "  <-- SAME-VOICE?" if p["cosine"] > 0.40 else ""
        print(f"  {p['a']:<14} vs {p['b']:<14} {p['cosine']:.3f}{flag}")
    print("\n== per-speaker stability ==")
    for r in rows:
        ft = (",".join(f"{k}x{v}" for k, v in r["flip_to"].items()) or "-")
        print(f"  {r['speaker']:<14} acc={r['acc']:.0%} unknown={r['unknown']} "
              f"flips={r['flips']}({ft})  score {r['score_min']}-"
              f"{r['score_med']}-{r['score_max']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
