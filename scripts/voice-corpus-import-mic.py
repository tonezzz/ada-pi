#!/usr/bin/env python3
"""voice-corpus-import-mic — harvest stored enrollment audio into the
voice corpus so the model bench covers the mic domain, not just YouTube
clips.

Reads speaker_samples/<speaker>/*.pcm (raw 16kHz mono PCM16, written by
SpeakerIdentifier._save_sample_audio), converts each file to a WAV under
<corpus>/clips/mic-<speaker>-<pcmid>.wav, and upserts a registry entry:

  - id: mic-<slug>
    source: mic-enrollment
    kind: person
    qualified: pending          # review → 'ok' to include in benches
    lang: <from --lang or existing entry>
    clips: [...]

Idempotent — re-running after new enrollment sessions appends only new
clips (numbered after the highest existing -NN).

Usage:
  # on the host holding ~/.local/share/ada-pi/speaker_samples, or after
  # rsync'ing that dir somewhere local:
  python3 scripts/voice-corpus-import-mic.py \
      --samples-dir /path/to/speaker_samples \
      --corpus tests/voice-corpus [--lang th] [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import wave
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 16000

parser = argparse.ArgumentParser()
parser.add_argument("--samples-dir",
                    default="~/.local/share/ada-pi/speaker_samples")
parser.add_argument("--corpus", default=str(REPO / "tests" / "voice-corpus"))
parser.add_argument("--lang", default="th")
parser.add_argument("--dry-run", action="store_true")
ARGS = parser.parse_args()


def pcm_to_wav(pcm: Path, wav: Path) -> int:
    data = pcm.read_bytes()
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(data)
    return len(data) // 2 // SAMPLE_RATE


def main() -> int:
    samples = Path(ARGS.samples_dir).expanduser()
    corpus = Path(ARGS.corpus)
    clips_dir = corpus / "clips"
    if not samples.is_dir():
        print(f"no samples dir: {samples}")
        return 1
    reg_path = corpus / "registry.yaml"
    reg = yaml.safe_load(reg_path.read_text()) if reg_path.exists() else {}
    speakers = reg.setdefault("speakers", [])

    for speaker_dir in sorted(p for p in samples.iterdir() if p.is_dir()):
        # same slug rules as SpeakerIdentifier._save_sample_audio
        slug = re.sub(r"[^A-Za-z0-9ก-๙_-]+", "-", speaker_dir.name).strip("-")
        if not slug:
            continue
        spk_id = f"mic-{slug}"
        pcms = sorted(speaker_dir.glob("*.pcm"))
        usable = [p for p in pcms if len(p.read_bytes()) // 2 // SAMPLE_RATE >= 1]
        if not usable:
            print(f"{spk_id}: no usable clips — skipped")
            continue
        entry = next((s for s in speakers if s["id"] == spk_id), None)
        if entry is None:
            entry = {"id": spk_id, "source": "mic-enrollment",
                     "lang": ARGS.lang, "kind": "person",
                     "qualified": "pending",
                     "note": "enrollment audio — review before qualifying",
                     "clips": []}
            speakers.append(entry)
        existing = set(entry["clips"])
        added = 0
        for pcm in usable:
            # clip name derives from the source pcm filename — stable
            # across re-runs, so re-importing is naturally idempotent.
            wav_name = f"{spk_id}-{pcm.stem}.wav"
            if wav_name in existing:
                continue
            secs = len(pcm.read_bytes()) // 2 // SAMPLE_RATE
            if secs < 1:
                print(f"  skip {pcm.name} — {secs}s too short")
                continue
            if not ARGS.dry_run:
                clips_dir.mkdir(parents=True, exist_ok=True)
                pcm_to_wav(pcm, clips_dir / wav_name)
            entry["clips"].append(wav_name)
            added += 1
            print(f"  + {wav_name} ({secs}s)")
        print(f"{spk_id}: {added} new clip(s), {len(entry['clips'])} total")

    if not ARGS.dry_run:
        reg_path.write_text(yaml.safe_dump(reg, allow_unicode=True,
                                           sort_keys=False))
        print(f"registry updated: {reg_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
