#!/usr/bin/env python3
"""voice-bench-models: compare speaker-embedding backends on the corpus.

Same corpus and enroll→identify protocol as voice-bench.py, but run once
per encoder adapter (tests/bench/encoders.py) instead of only against the
prod SpeakerIdentifier. The bench compares *embeddings*, not pipeline
behavior — contamination guards, auto-learn, and margin rules are
SpeakerIdentifier features and stay in voice-bench.py.

Per model:
  enroll clip0 of every qualified person → identify clips 1..N
  (argmax-cosine vs enrolled prints). Genuine vs impostor score arrays
  give EER, suggested threshold, and margin; latency/size come from the
  adapter.

Cases per probe clip:
  top1            argmax == true speaker (threshold-free accuracy)
  gated@0.45      argmax == true AND score >= prod threshold
  gated@eer       same, at the model's own EER threshold
  cross_negative  probe never top-scores a *wrong* speaker above EER thr

Flags:
  --models slug,slug   subset (default: every available adapter)
  --list               print registry + availability, no run
  --fetch-models       try downloading missing ONNX files first
  --corpus PATH        (default tests/voice-corpus)
  --include-pending    include qualified:pending speakers
  --max-clips N        cap clips per speaker (smoke mode)
  --mddb URL           write a timestamped doc to ada-ha-scenario-reports
  --report-cms         upsert the bench-voice-models CMS page
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import wave
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import encoders  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--models", default="")
parser.add_argument("--list", action="store_true")
parser.add_argument("--fetch-models", action="store_true")
parser.add_argument("--corpus", default=str(REPO / "tests" / "voice-corpus"))
parser.add_argument("--include-pending", action="store_true")
parser.add_argument("--max-clips", type=int, default=0)
parser.add_argument("--mddb", default=os.environ.get(
    "ADA_MDDB_URL", "http://100.102.134.91:11023/v1"))
parser.add_argument("--report-cms", action="store_true")
# parse_known_args: the contract test exec's this module under pytest,
# whose own argv must not be consumed.
ARGS, _ = parser.parse_known_args()

PROD_THRESHOLD = 0.45  # ADA_SPEAKER_THRESHOLD default on idc01


def load_wav_f32(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError(f"{path.name}: need 16-bit mono wav")
        frames = w.readframes(w.getnframes())
        rate = w.getframerate()
    pcm = np.frombuffer(frames, dtype=np.int16)
    if rate != 16000:
        import audioop
        frames = audioop.ratecv(frames.tobytes(), 2, 1, rate, 16000, None)[0]
        pcm = np.frombuffer(frames, dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


def check_corpus(corpus: Path, speakers: list[dict]) -> list[str]:
    """Corpus integrity preflight — returns problems, never raises."""
    problems = []
    for sp in speakers:
        for c in sp.get("clips") or []:
            p = corpus / "clips" / c
            if not p.exists():
                problems.append(f"{sp['id']}: missing {c}")
                continue
            try:
                wav = load_wav_f32(p)
                if float(np.sqrt(np.mean(wav ** 2))) < 0.005:
                    problems.append(f"{sp['id']}: {c} near-silent")
            except Exception as e:
                problems.append(f"{sp['id']}: {c} undecodable ({e})")
    return problems


def load_registry(corpus: Path) -> list[dict]:
    reg = yaml.safe_load((corpus / "registry.yaml").read_text()) or {}
    out = []
    for sp in reg.get("speakers") or []:
        if sp.get("qualified") == "ok" or (
                ARGS.include_pending and sp.get("qualified") == "pending"):
            sp = dict(sp)
            sp["kind"] = sp.get("kind") or "person"
            if len(sp.get("clips") or []) >= 2:
                out.append(sp)
    return out


def eer_threshold(genuine: list[float], impostor: list[float]) -> tuple[float, float]:
    """Sweep cosine thresholds; return (eer, threshold_at_eer)."""
    if not genuine or not impostor:
        return 0.0, 0.5
    g = np.asarray(genuine)
    i = np.asarray(impostor)
    best = (1.0, 0.5)      # (eer, threshold)
    best_gap = 2.0
    for t in np.linspace(0.0, 1.0, 1001):
        far = float(np.mean(i >= t))   # impostor accepted
        frr = float(np.mean(g < t))    # genuine rejected
        gap = abs(far - frr)
        if gap < best_gap or (gap == best_gap and (far + frr) / 2 < best[0]):
            best_gap = gap
            best = ((far + frr) / 2, float(t))
    return best


def bench_model(enc: encoders.BaseEncoder, speakers: list[dict],
                emb: dict[str, list[np.ndarray]]) -> dict:
    enrolled = {sp["id"]: emb[sp["id"]][0] for sp in speakers}
    names = list(enrolled)
    genuine, impostor = [], []
    top1 = 0
    gated_prod = 0
    probes = 0
    wrong_named = []
    for sp in speakers:
        for probe in emb[sp["id"]][1:]:
            scores = [(n, encoders.cosine(probe, enrolled[n])) for n in names]
            scores.sort(key=lambda x: x[1], reverse=True)
            best_name, best = scores[0]
            own = [s for n, s in scores if n == sp["id"]][0]
            genuine.append(own)
            impostor.extend(s for n, s in scores if n != sp["id"])
            probes += 1
            if best_name == sp["id"]:
                top1 += 1
                if own >= PROD_THRESHOLD:
                    gated_prod += 1
            elif best >= 0:  # recorded regardless of threshold
                wrong_named.append((sp["id"], best_name, best))
    eer, thr = eer_threshold(genuine, impostor)
    gated_eer = sum(1 for g in genuine if g >= thr)
    cross_fp = sum(1 for s in impostor if s >= thr)
    g_sorted = sorted(genuine)
    return {
        "probes": probes,
        "top1": top1,
        "top1_acc": top1 / probes if probes else 0.0,
        "gated_prod_acc": gated_prod / probes if probes else 0.0,
        "gated_eer_acc": gated_eer / probes if probes else 0.0,
        "eer": eer,
        "eer_thr": thr,
        "same_min": min(genuine) if genuine else 0,
        "same_med": g_sorted[len(g_sorted) // 2] if genuine else 0,
        "cross_max": max(impostor) if impostor else 0,
        "cross_fp_at_eer": cross_fp,
        "margin": (g_sorted[len(g_sorted) // 2]
                   - max(impostor)) if genuine and impostor else 0,
        "wrong_named": wrong_named,
    }


def main() -> int:
    if ARGS.fetch_models:
        encoders.ensure_models()
    selected = [s.strip() for s in ARGS.models.split(",") if s.strip()]
    adapters = encoders.encoders(selected or None)
    if ARGS.list:
        for a in adapters:
            ok, why = a.available()
            print(f"{a.slug:16s} {'ok' if ok else 'unavailable: ' + why}")
        return 0

    corpus = Path(ARGS.corpus)
    speakers = load_registry(corpus)
    persons = [s for s in speakers if s["kind"] == "person"]
    if ARGS.max_clips:
        for sp in persons:
            sp["clips"] = sp["clips"][:ARGS.max_clips]
    problems = check_corpus(corpus, persons)
    if not persons:
        print("No qualified speakers — harvest + review the corpus first.")
        return 1

    wavs: dict[str, list[np.ndarray]] = {
        sp["id"]: [load_wav_f32(corpus / "clips" / c) for c in sp["clips"]
                   if (corpus / "clips" / c).exists()]
        for sp in persons}

    scorecards: list[dict] = []
    for enc in adapters:
        ok, why = enc.available()
        if not ok:
            print(f"[skip] {enc.slug}: {why}")
            scorecards.append({"slug": enc.slug, "title": enc.title,
                               "skipped": why})
            continue
        print(f"[bench] {enc.slug} …", flush=True)
        lat = []
        emb: dict[str, list[np.ndarray]] = {}
        try:
            for sp in persons:
                emb[sp["id"]] = []
                for wav in wavs[sp["id"]]:
                    t0 = time.monotonic()
                    emb[sp["id"]].append(enc.embed(wav))
                    lat.append((time.monotonic() - t0) * 1000)
        except Exception as exc:
            print(f"[fail] {enc.slug}: {exc}")
            scorecards.append({"slug": enc.slug, "title": enc.title,
                               "skipped": f"embed failed: {exc}"})
            continue
        lat.sort()
        res = bench_model(enc, persons, emb)
        res.update({
            "slug": enc.slug, "title": enc.title,
            "params_m": enc.params_m, "size_mb": round(enc.model_size_mb(), 1),
            "embed_dim": enc.embed_dim, "load_s": round(enc.load_s, 1),
            "lat_p50": round(lat[len(lat) // 2], 0) if lat else 0,
            "lat_p95": round(lat[int(len(lat) * 0.95)], 0) if lat else 0,
        })
        scorecards.append(res)

    report = render_report(persons, problems, scorecards)
    print(report)

    if ARGS.mddb:
        _mddb_doc(report, scorecards)
    if ARGS.report_cms:
        _cms_page(report)
    return 0


def render_report(persons, problems, cards) -> str:
    now = datetime.now()
    langs = {}
    for sp in persons:
        langs[sp.get("lang") or "?"] = langs.get(sp.get("lang") or "?", 0) + 1
    lines = [
        f"# Voice encoder benchmark — {now:%Y-%m-%d %H:%M}",
        "",
        f"corpus: {len(persons)} person speakers "
        f"({', '.join(f'{k}×{v}' for k, v in langs.items())}), "
        f"{sum(len(s['clips']) for s in persons)} clips · "
        "protocol: enroll clip0, identify clips 1..N",
        "",
        "## scorecard",
        "",
        "| model | params | size | dim | load s | enc p50 ms | top1 acc | "
        "acc@0.45 | acc@EER | EER | thr@EER | margin |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c in cards:
        if "skipped" in c:
            lines.append(f"| {c['slug']} | — | — | — | — | — | "
                         f"skipped ({c['skipped'][:40]}) | — | — | — | — | — |")
            continue
        lines.append(
            f"| {c['slug']} | {c['params_m']}M | {c['size_mb']}MB | "
            f"{c['embed_dim']} | {c['load_s']} | {c['lat_p50']:.0f} | "
            f"{c['top1_acc']:.0%} | {c['gated_prod_acc']:.0%} | "
            f"{c['gated_eer_acc']:.0%} | {c['eer']:.2%} | "
            f"{c['eer_thr']:.2f} | {c['margin']:+.2f} |")
    details = []
    for c in cards:
        if "skipped" in c:
            continue
        wn = c["wrong_named"]
        details += [
            "",
            f"## {c['slug']} — {c['title']}",
            "",
            f"- same-speaker scores: min={c['same_min']:.2f} "
            f"med={c['same_med']:.2f} · cross-speaker max={c['cross_max']:.2f} "
            f"(fp@EER-thr: {c['cross_fp_at_eer']})",
            f"- probes={c['probes']} top1={c['top1']} · "
            f"enc p50={c['lat_p50']:.0f}ms p95={c['lat_p95']:.0f}ms",
        ]
        if wn:
            details.append("- wrong-name top scores (argmax ≠ true):")
            details += [f"  - {a} → {b} @{s:.2f}" for a, b, s in wn[:8]]
    if problems:
        details += ["", "## corpus problems", ""]
        details += [f"- {p}" for p in problems[:20]]
    lines += details
    lines += [
        "",
        "---",
        "Auto-generated by `tests/bench/voice-bench-models.py` — do not "
        "hand-edit; rerun the bench to refresh.",
    ]
    return "\n".join(lines)


def _post(url: str, payload: dict) -> bool:
    try:
        urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}), timeout=30)
        return True
    except Exception as exc:
        print(f"[post] {url} failed: {exc}")
        return False


def _mddb_doc(report: str, cards: list[dict]) -> None:
    ok_cards = [c for c in cards if "skipped" not in c]
    best = max(ok_cards, key=lambda c: c["top1_acc"], default=None)
    _post(f"{ARGS.mddb.rstrip('/')}/add", {
        "collection": "ada-ha-scenario-reports",
        "key": f"voice-models-bench-{datetime.now():%Y%m%d-%H%M%S}",
        "lang": "en", "contentMd": report,
        "meta": {"kind": ["benchmark"], "bench": ["voice-models"],
                 "score": [f"{best['slug']} top1 {best['top1_acc']:.0%}"
                           if best else "no models"],
                 "models": [c["slug"] for c in ok_cards]}})
    print("\n[mddb] report doc written")


def _cms_page(report: str) -> None:
    """Upsert the bench-voice-models page — same meta contract as the
    auto-report page (validate via backend.report_meta if importable)."""
    from backend.report_meta import validate_report_meta
    now = datetime.now()
    meta = {"kind": ["page"], "slug": ["bench-voice-models"],
            "title": ["Voice encoder benchmark"], "format": ["markdown"],
            "instance": ["tony"], "domain": ["bench"],
            "summary": ["Speaker-embedding model comparison on the voice "
                        "corpus — scorecard per model: accuracy, EER, "
                        "latency, size."],
            "fresh_for": ["7d"],
            "confidence": ["high"],
            "timeline": [f"{now.isoformat(timespec='minutes')}: bench run"],
            "updated": [now.isoformat(timespec="seconds")]}
    check = validate_report_meta(meta)
    for w in check["warnings"]:
        print(f"  meta warning: {w}", file=sys.stderr)
    if not check["ok"]:
        print(f"[cms] NOT published — meta contract violation: "
              f"{check['missing']}", file=sys.stderr)
        return
    if _post(f"{ARGS.mddb.rstrip('/')}/add", {
            "collection": "ada-cms-pages", "key": "bench-voice-models",
            "lang": "en", "contentMd": report, "meta": meta}):
        print("[cms] bench-voice-models updated")


if __name__ == "__main__":
    raise SystemExit(main())
