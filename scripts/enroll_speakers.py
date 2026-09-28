#!/usr/bin/env python3
"""Bulk-enroll speaker voiceprints from audio files — no live session.

Each file becomes one enrolled profile named after its stem (e.g.
kk.pcm -> speaker "KK" with --display or default title-cased name).
PCM files are raw s16le mono 16 kHz; WAV files must be 16 kHz mono s16
(convert otherwise: ffmpeg -i in.m4a -ar 16000 -ac 1 -f s16le out.pcm).

Record ~5-10 s of clean speech per sample. Re-running the same speaker
with more clips merges them into a stronger voiceprint (samples counter
in the profiles file grows).

Storage is ~1 KB per speaker — a 192-dim float32 embedding; raw audio
is never kept.

Usage:
  python3 scripts/enroll_speakers.py samples/*.pcm \
      --profiles ~/.local/share/ada-pi/speaker_profiles.json \
      --ha-person KK=person.kk --ha-person Tony=person.tony
  python3 scripts/enroll_speakers.py tests/fixtures/voice-*.pcm \
      --profiles /tmp/scenario-speakers.json
"""

from __future__ import annotations

import argparse
import os
import sys
import wave
from pathlib import Path

SAMPLE_RATE = 16000


def _read_audio(path: Path) -> bytes:
    """Return PCM16 s16le 16 kHz mono bytes from .pcm or .wav."""
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as w:
            bad = []
            if w.getframerate() != SAMPLE_RATE:
                bad.append(f"rate {w.getframerate()}")
            if w.getnchannels() != 1:
                bad.append(f"{w.getnchannels()}ch")
            if w.getsampwidth() != 2:
                bad.append(f"{w.getsampwidth() * 8}-bit")
            if bad:
                raise ValueError(
                    f"{path.name}: need 16 kHz mono s16 ({', '.join(bad)}); "
                    "convert with: ffmpeg -i in -ar 16000 -ac 1 -f s16le out.pcm")
            return w.readframes(w.getnframes())
    return path.read_bytes()


def _kv_map(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        name, _, val = p.partition("=")
        if name.strip() and val.strip():
            out[name.strip().lower()] = val.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", type=Path,
                    help=".pcm/.wav files; stem becomes the speaker name")
    ap.add_argument("--profiles", default=None,
                    help="speaker_profiles.json path (default: ADA_SPEAKER_PROFILES "
                         "or ~/.local/share/ada-pi/speaker_profiles.json)")
    ap.add_argument("--ha-person", action="append", default=[],
                    metavar="NAME=person.id", help="bind speaker to an HA person")
    ap.add_argument("--display", action="append", default=[],
                    metavar="NAME=Label", help="friendly display name")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate files without enrolling")
    args = ap.parse_args()

    if args.profiles:
        os.environ["ADA_SPEAKER_PROFILES"] = args.profiles
    ha_people = _kv_map(args.ha_person)
    displays = _kv_map(args.display)

    files: list[Path] = []
    for f in args.files:
        files.extend(sorted(f.parent.glob(f.name)) if any(
            c in str(f) for c in "*?[") else [f])
    if not files:
        print("no input files", file=sys.stderr)
        return 1

    todo = []
    for f in files:
        try:
            pcm = _read_audio(f)
            dur = len(pcm) / 2 / SAMPLE_RATE
            if dur < 0.5:
                raise ValueError(f"too short: {dur:.1f}s (need >= 0.5s)")
            todo.append((f, pcm, dur))
            print(f"  {f.name}: {dur:.1f}s ok")
        except Exception as exc:
            print(f"  {f.name}: SKIP — {exc}", file=sys.stderr)
    if args.dry_run or not todo:
        return 0

    from backend.speaker_id import SpeakerIdentifier  # heavy import (torch)

    ident = SpeakerIdentifier()
    failures = 0
    for f, pcm, _dur in todo:
        name = f.stem
        key = name.lower()
        try:
            res = ident.enroll(
                name, pcm, sample_rate=SAMPLE_RATE,
                ha_person=ha_people.get(key),
                display_name=displays.get(key),
            )
            print(f"enrolled {res['name']} samples={res['samples']} "
                  f"ha_person={res.get('ha_person') or '-'}")
        except Exception as exc:
            failures += 1
            print(f"enroll {name}: FAILED — {exc}", file=sys.stderr)
    print(f"profiles: {ident._profiles_path} ({len(ident.enrolled_names())} enrolled)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
