#!/usr/bin/env python3
"""Generate REAL speech PCM fixtures for voice-turn scenarios via Gemini TTS.

Unlike gen_voice_fixture.py (synthetic speaker signatures for the local
ECAPA model, not real speech), this produces actual intelligible speech
that Gemini Live's ASR transcribes — used for voice-vs-text consistency
scenarios (e.g. session_continuity_voice.yaml).

Output: PCM16 mono 16 kHz .pcm per prompt, plus a trailing 2.5s
near-silence pad (-pad variant). The pad matters: the server VAD measures
silence_duration_ms on the incoming STREAM — with no frames after the
utterance the turn never closes and the model never responds.

Usage:
  GEMINI_API_KEY=... python3 tests/fixtures/gen_speech_fixture.py \
      "What did we discuss in our recent sessions?" ask-recent
  (key falls back to ~/.config/secrets/ada-pi-pwa.env)
"""
import base64
import json
import math
import os
import struct
import sys
import urllib.request
from pathlib import Path

SR_TTS = 24000      # Gemini TTS returns L16 24 kHz
SR_OUT = 16000      # ws wants PCM16 16 kHz
MODEL = "gemini-2.5-flash-preview-tts"
VOICE = os.environ.get("ADA_TTS_VOICE", "Puck")


def key() -> str:
    for env in (os.environ.get("GEMINI_API_KEY"),
                os.environ.get("GOOGLE_API_KEY")):
        if env:
            return env
    for line in Path.home().joinpath(
            ".config/secrets/ada-pi-pwa.env").read_text().splitlines():
        if line.startswith(("GEMINI_API_KEY=", "GOOGLE_API_KEY=")):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no Gemini key found")


def tts(text: str) -> bytes:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{MODEL}:generateContent?key={key()}")
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": VOICE}}},
        },
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=60).read())
    return base64.b64decode(
        data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])


def resample_24k_to_16k(raw: bytes) -> bytes:
    n = len(raw) // 2
    samples = struct.unpack(f"<{n}h", raw)
    out = bytearray()
    for i in range(0, n, 3):          # 24000 -> 16000 = keep 2 of 3
        out += struct.pack("<hh", samples[i], samples[min(i + 1, n - 1)])
    return bytes(out)


def silence(seconds: float) -> bytes:
    # tiny noise floor so the stream reads as live audio, not a dead channel
    return b"".join(struct.pack("<h", int(200 * math.sin(i * 0.1)))
                    for i in range(int(SR_OUT * seconds)))


def main() -> None:
    text = sys.argv[1]
    name = sys.argv[2]
    outdir = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(__file__).parent
    pcm = resample_24k_to_16k(tts(text))
    (outdir / f"{name}.pcm").write_bytes(pcm)
    (outdir / f"{name}-pad.pcm").write_bytes(pcm + silence(2.5))
    print(f"wrote {name}.pcm ({len(pcm) / 32000:.1f}s) + -pad variant")


if __name__ == "__main__":
    main()
