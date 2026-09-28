#!/usr/bin/env python3
"""voice-harvest: pull speech clips from YouTube into the voice corpus.

Pipeline per URL:
  yt-dlp → wav 16kHz mono → silero-vad speech segments → ECAPA embeddings
  → greedy speaker clustering → clip files + pending registry entries.

Each discovered speaker cluster becomes one registry entry
(qualified: pending) — Tony reviews and flips to ok/reject.
Media/music segments fail the speech gate or cluster apart from voices.

Deps (ada-pi venv): yt-dlp binary, ffmpeg, torch, speechbrain (ECAPA),
silero-vad via torch.hub (first run downloads ~2MB).

Usage:
  .venv/bin/python scripts/voice-harvest.py <youtube-url> [<url>...]
    [--corpus tests/voice-corpus] [--lang th] [--min-seg 6] [--max-seg 30]
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
import yaml  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("urls", nargs="+")
parser.add_argument("--corpus", default=str(REPO / "tests" / "voice-corpus"))
parser.add_argument("--lang", default="")
parser.add_argument("--min-seg", type=float, default=6.0)
parser.add_argument("--max-seg", type=float, default=30.0)
parser.add_argument("--same-cluster", type=float, default=0.55,
                    help="cosine cutoff for merging segments into one speaker")
ARGS = parser.parse_args()

CORPUS = Path(ARGS.corpus)
CLIPS = CORPUS / "clips"
CLIPS.mkdir(parents=True, exist_ok=True)
REG = CORPUS / "registry.yaml"


def _ffmpeg() -> str:
    import shutil
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _ytdlp() -> str:
    import shutil
    yd = shutil.which("yt-dlp")
    if yd:
        return yd
    return str(REPO / ".venv" / "bin" / "yt-dlp")


def ytdlp(url: str, out: Path) -> Path:
    wav = out / "src.wav"
    subprocess.run([_ytdlp(), "-x", "--audio-format", "wav",
                    "--ffmpeg-location", _ffmpeg(),
                    "-o", str(out / "src.%(ext)s"), url], check=True,
                   capture_output=True)
    # normalize: 16kHz mono s16
    subprocess.run([_ffmpeg(), "-y", "-i", str(wav), "-ar", "16000",
                    "-ac", "1", "-f", "wav", str(out / "n.wav")],
                   check=True, capture_output=True)
    return out / "n.wav"


def segments(path: Path) -> list[tuple[int, int, np.ndarray]]:
    """Return [(start_sample, end_sample, float32 audio)] via silero-vad."""
    import wave
    import torch
    with wave.open(str(path), "rb") as w:
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    wav = torch.from_numpy(raw.astype(np.float32) / 32768.0)
    vad, utils = torch.hub.load("snakers4/silero-vad", "silero_vad",
                                trust_repo=True, verbose=False)
    get_ts = utils[0]
    ts = get_ts(wav, vad, sampling_rate=16000)
    return [(t["start"], t["end"], wav[t["start"]:t["end"]].numpy())
            for t in ts]


def embed(pcm_f32: np.ndarray) -> np.ndarray:
    from backend.speaker_id import SpeakerIdentifier
    pcm16 = (np.clip(pcm_f32, -1, 1) * 32767).astype(np.int16).tobytes()
    return SpeakerIdentifier.get()._compute_embedding(pcm16, 16000)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def write_clip(f32: np.ndarray, out: Path) -> None:
    import wave
    pcm = (np.clip(f32, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())


def video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{6,})", url)
    return m.group(1) if m else re.sub(r"\W+", "-", url)[-24:]


def process(url: str) -> list[dict]:
    vid = video_id(url)
    with tempfile.TemporaryDirectory() as td:
        wav = ytdlp(url, Path(td))
        segs = segments(wav)
    segs = [s for s in segs
            if ARGS.min_seg <= (s[1] - s[0]) / 16000 <= ARGS.max_seg]
    if not segs:
        print(f"  {vid}: no qualifying speech segments")
        return []

    # cluster segments into speaker groups by embedding similarity
    clusters: list[list[int]] = []
    cents: list[np.ndarray] = []
    for i, (_, _, audio) in enumerate(segs):
        e = embed(audio)
        best, bs = -1, 0.0
        for j, c in enumerate(cents):
            s = cosine(e, c)
            if s > bs:
                best, bs = j, s
        if best >= 0 and bs >= ARGS.same_cluster:
            clusters[best].append(i)
            cents[best] = (cents[best] * len(clusters[best]) + e) / (
                len(clusters[best]) + 1)
        else:
            clusters.append([i])
            cents.append(e)

    entries = []
    for ci, idxs in enumerate(clusters):
        if len(idxs) < 2:
            continue  # need >=2 clips to enroll+test
        spk = f"yt-{vid}-spk{ci}"
        names = []
        for n, i in enumerate(idxs[:6]):  # cap clips per speaker
            fn = f"{spk}-{n + 1:02d}.wav"
            write_clip(segs[i][2], CLIPS / fn)
            names.append(fn)
        total_s = sum((segs[i][1] - segs[i][0]) / 16000 for i in idxs)
        entries.append({
            "id": spk,
            "source": url,
            "lang": ARGS.lang or "unknown",
            "kind": "person",
            "qualified": "pending",
            "note": f"{len(idxs)} segments, {total_s:.0f}s speech",
            "clips": names,
        })
        print(f"  {vid} spk{ci}: {len(idxs)} segs, {total_s:.0f}s → {spk} (pending)")
    return entries


def main() -> int:
    reg = yaml.safe_load(REG.read_text()) if REG.exists() else {}
    reg.setdefault("speakers", [])
    existing = {s["id"] for s in reg["speakers"]}
    for url in ARGS.urls:
        print(f"harvesting {url}")
        try:
            for e in process(url):
                if e["id"] not in existing:
                    reg["speakers"].append(e)
        except Exception as exc:
            print(f"  FAILED {url}: {exc}")
    REG.write_text(yaml.safe_dump(reg, allow_unicode=True, sort_keys=False))
    print(f"\nregistry: {len(reg['speakers'])} speaker entries — "
          "review 'pending' ones in tests/voice-corpus/registry.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
