#!/usr/bin/env python3
"""Deterministic synthetic 'voice' fixtures for the speaker-enrollment
live scenario. Not real speech — a harmonic-rich, formant-like signal the
ECAPA embedding model treats as a stable speaker signature. Identical
output every run, so an enrolled clip re-identifies with cosine ~1.0.

Usage: python3 tests/fixtures/gen_voice_fixture.py [outdir]
Writes voice-guest.pcm and voice-other.pcm (PCM16 mono 16 kHz, ~5 s).
"""

import math
import struct
import sys
from pathlib import Path

SR = 16000
SECONDS = 5


def synth(base_hz: float, seed: int, seconds: float = SECONDS) -> bytes:
    """Voiced-speech-like signal: pitch contour + harmonics shaped by
    slowly moving 'formant' envelopes and syllable-rate amplitude gating."""
    n = int(SR * seconds)
    out = bytearray(n * 2)
    for i in range(n):
        t = i / SR
        # pitch: slow drift + vibrato (keeps embedding in the speech regime)
        f0 = base_hz * (1.0 + 0.06 * math.sin(2 * math.pi * 0.7 * t + seed)
                        + 0.02 * math.sin(2 * math.pi * 5.1 * t))
        # syllable gating ~4 Hz with deterministic jitter
        gate_phase = 2 * math.pi * 4.0 * t + seed * 0.3
        gate = 0.55 + 0.45 * math.sin(gate_phase)
        if gate < 0.12:
            gate = 0.0  # tiny pauses between 'syllables'
        # harmonics with two moving formant bands
        v = 0.0
        for h in range(1, 9):
            fh = f0 * h
            f1 = 0.5 + 0.5 * math.sin(2 * math.pi * 0.23 * t + seed)
            f2 = 0.5 + 0.5 * math.sin(2 * math.pi * 0.31 * t + seed * 1.7)
            amp = (1.0 / h) * (0.4 + 0.6 * (f1 if h <= 4 else f2))
            v += amp * math.sin(2 * math.pi * fh * t)
        # light deterministic noise for fricative texture
        nz = math.sin(i * 0.6180339 * seed) * 0.03
        sample = int(max(-1.0, min(1.0, (v * 0.22 + nz) * gate)) * 30000)
        struct.pack_into("<h", out, i * 2, sample)
    return bytes(out)


def main() -> None:
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
    outdir.mkdir(parents=True, exist_ok=True)
    for name, base, seed in [("voice-guest", 150.0, 11), ("voice-other", 210.0, 37)]:
        data = synth(base, seed)
        (outdir / f"{name}.pcm").write_bytes(data)
        print(f"wrote {outdir / (name + '.pcm')} ({len(data) / 32000:.1f}s)")


if __name__ == "__main__":
    main()
