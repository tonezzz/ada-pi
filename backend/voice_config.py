"""Per-instance output-voice preference for the Gemini Live session.

The Live API picks the speaking voice from
``speech_config.voice_config.prebuilt_voice_config.voice_name`` at session
connect — the same voices the Gemini app exposes in its "Gemini's voice"
setting. Ada persists the chosen voice in a small per-instance JSON file so
``ada_set_voice`` survives service restarts; each provider connects with the
resolved voice, so a mid-session switch only needs a provider reconnect.

Resolution order: voice file -> GEMINI_LIVE_VOICE env -> "Kore".
File: $ADA_VOICE_FILE, else ~/.config/ada/voice-<ADA_INSTANCE_ID>.json.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Gemini Live prebuilt voices (speech_config.prebuilt_voice_config).
GEMINI_VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]

_CANON = {v.lower(): v for v in GEMINI_VOICES}

DEFAULT_VOICE = "Kore"


def canonical_voice(name: object) -> str | None:
    return _CANON.get(str(name or "").strip().lower())


def voice_file() -> Path:
    override = os.environ.get("ADA_VOICE_FILE")
    if override:
        return Path(override)
    instance = (os.environ.get("ADA_INSTANCE_ID") or "default").strip() or "default"
    return Path.home() / ".config" / "ada" / f"voice-{instance}.json"


def current_voice() -> str:
    try:
        data = json.loads(voice_file().read_text())
        voice = canonical_voice(data.get("voice"))
        if voice:
            return voice
    except Exception:
        pass
    return canonical_voice(os.environ.get("GEMINI_LIVE_VOICE")) or DEFAULT_VOICE


def set_voice(name: object) -> str:
    voice = canonical_voice(name)
    if not voice:
        raise ValueError(
            f"unknown voice {name!r}; options: {', '.join(GEMINI_VOICES)}"
        )
    path = voice_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"voice": voice}) + "\n")
    return voice
