"""Speaker identification using speechbrain ECAPA-TDNN embeddings.

The model loads lazily on first use (~5s, ~500 MB RAM).  Subsequent
identifications take ~200-300 ms per 2 s audio chunk on a single CPU
core.  Audio is never blocked: SpeakerSession.feed() buffers in the
background and runs identification in a thread executor so the Gemini
Live audio path stays untouched.

Enrolled voiceprints are stored as base64 float32 vectors in a JSON
file (default ~/.local/share/ada-pi/speaker_profiles.json).  The file
is local-only — never committed or sent to a third party.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

import numpy as np

logger = logging.getLogger("voice.speaker_id")

# Import speechbrain at module level to avoid circular-import issues when
# loaded lazily inside a server process that has already partially imported
# the package through a transitive dependency.  The import is optional — if
# speechbrain/torch are not installed, the module still loads and speaker ID
# is simply unavailable (the server starts, endpoints return 503).
try:
    from speechbrain.inference.speaker import SpeakerRecognition  # noqa: F401
    _SPEAKERBRAIN_AVAILABLE = True
except Exception as exc:
    SpeakerRecognition = None  # type: ignore[assignment,misc]
    _SPEAKERBRAIN_AVAILABLE = False
    logger.info("speechbrain not available (%s) — speaker ID disabled", exc)

SAMPLE_RATE = 16000
# 2 seconds of 16-bit mono PCM at 16 kHz.
MIN_CHUNK_BYTES = SAMPLE_RATE * 2 * 2  # 64 000 bytes
# RMS threshold below which a buffer is treated as silence and skipped.
SILENCE_RMS = 0.01
# Minimum cosine similarity to accept a match.
DEFAULT_THRESHOLD = 0.45
# Drop the buffer if it grows beyond this (prevents unbounded growth when
# identification is slower than audio arrival).
MAX_BUFFER_BYTES = SAMPLE_RATE * 6 * 2  # 6 s
# Consecutive failed identifications on real speech before the
# on_unrecognized callback fires (once per session until a match).
UNKNOWN_AFTER_MISSES = 2
# Minimum margin between the best and runner-up cosine scores to accept an
# identification. A winner-take-all match at ~0.5 between two enrolled
# speakers is genuinely ambiguous — "unrecognized" (sandbox) is safer than
# naming the wrong person (wrong memory banks, wrong name in conversation).
MIN_MARGIN = 0.05

# Placeholder names that must never become enrolled profiles — the model
# once enrolled a real speaker (KK) under "Guest" when she didn't state a
# name, which then identified her as a sandbox guest instead of prompting
# for her real name.
RESERVED_NAMES = {"guest", "unknown", "someone", "anon", "anonymous", "test", "tester"}


def _rms(float_samples: np.ndarray) -> float:
    if float_samples.size == 0:
        return 0.0
    return float(math.sqrt(float(float_samples @ float_samples) / float_samples.size))


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    dot = float(a @ b)
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class SpeakerIdentifier:
    """Singleton wrapping the speechbrain ECAPA-TDNN model.

    Use ``SpeakerIdentifier.get()`` to obtain the shared instance.  The
    model is loaded on first ``identify`` or ``enroll`` call so the server
    starts fast even when the libraries are installed.

    Enrolled profiles store:
      - embedding: base64 float32 voiceprint
      - ha_person: optional HA person entity_id (e.g. "person.tony")
      - display_name: optional friendly name for Gemini context
    """

    _instance: "SpeakerIdentifier | None" = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "SpeakerIdentifier":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self) -> None:
        self._model: Any = None
        self._model_lock = threading.Lock()
        self._enrolled: dict[str, np.ndarray] = {}
        self._metadata: dict[str, dict[str, Any]] = {}  # name -> {ha_person, display_name}
        self._profiles_path = Path(
            os.environ.get(
                "ADA_SPEAKER_PROFILES",
                Path.home() / ".local/share/ada-pi/speaker_profiles.json",
            )
        )
        self._load_enrolled()

    # -- model -----------------------------------------------------------

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:
                return
            if not _SPEAKERBRAIN_AVAILABLE:
                raise RuntimeError("speechbrain is not installed")
            import torch  # local import — heavy, only needed for inference

            cache = Path(
                os.environ.get("ADA_SPEAKER_MODEL_DIR", "/tmp/ada-speaker-model")
            )
            cache.mkdir(parents=True, exist_ok=True)
            logger.info("loading ECAPA-TDNN speaker model (first use)...")
            self._model = SpeakerRecognition.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir=str(cache),
                run_opts={"device": "cpu"},
            )
            logger.info("speaker model loaded")

    def _compute_embedding(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
        """Return a 192-dim float32 embedding for a PCM16 chunk."""
        import torch

        self._ensure_model()
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size < sample_rate // 2:  # need at least 0.5 s
            raise ValueError(f"audio too short: {audio.size} samples ({audio.size / sample_rate:.1f}s)")
        wav = torch.from_numpy(audio).unsqueeze(0)  # [1, time]
        emb = self._model.encode_batch(wav)  # [1, 1, emb_dim]
        return emb.squeeze().cpu().numpy()

    # -- enrolled profiles ----------------------------------------------

    def _load_enrolled(self) -> None:
        try:
            data = json.loads(self._profiles_path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for name, entry in data.items():
            if isinstance(entry, dict) and "embedding" in entry:
                raw = base64.b64decode(entry["embedding"])
                self._enrolled[name] = np.frombuffer(raw, dtype=np.float32).copy()
                self._metadata[name] = {
                    "ha_person": entry.get("ha_person"),
                    "display_name": entry.get("display_name"),
                }
        logger.info("loaded %d enrolled speaker profiles from %s", len(self._enrolled), self._profiles_path)

    def _save_enrolled(self) -> None:
        self._profiles_path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for name, emb in self._enrolled.items():
            meta = self._metadata.get(name, {})
            data[name] = {
                "embedding": base64.b64encode(emb.tobytes()).decode("ascii"),
                "dim": int(emb.shape[0]),
                "ha_person": meta.get("ha_person"),
                "display_name": meta.get("display_name"),
            }
        self._profiles_path.write_text(json.dumps(data, indent=2))

    # -- public API ------------------------------------------------------

    def enrolled_names(self) -> list[str]:
        return sorted(self._enrolled)

    def enrolled_info(self) -> list[dict[str, Any]]:
        """Return full profile info including HA person mapping."""
        return [
            {
                "name": name,
                "ha_person": self._metadata.get(name, {}).get("ha_person"),
                "display_name": self._metadata.get(name, {}).get("display_name"),
            }
            for name in sorted(self._enrolled)
        ]

    def get_ha_person(self, name: str) -> str | None:
        """Return the HA person entity_id for an enrolled speaker, or None."""
        return self._metadata.get(name, {}).get("ha_person")

    def get_display_name(self, name: str) -> str | None:
        """Return the display name for an enrolled speaker, or the raw name."""
        return self._metadata.get(name, {}).get("display_name") or name

    def set_metadata(self, name: str, ha_person: str | None = None,
                     display_name: str | None = None) -> bool:
        """Update HA person mapping and/or display name for an enrolled speaker."""
        if name not in self._enrolled:
            return False
        meta = self._metadata.setdefault(name, {})
        if ha_person is not None:
            meta["ha_person"] = ha_person or None
        if display_name is not None:
            meta["display_name"] = display_name or None
        self._save_enrolled()
        logger.info("updated metadata for '%s': ha_person=%s display_name=%s",
                    name, meta.get("ha_person"), meta.get("display_name"))
        return True

    def enroll(self, name: str, pcm16: bytes, sample_rate: int = SAMPLE_RATE,
               ha_person: str | None = None,
               display_name: str | None = None) -> dict[str, Any]:
        """Compute and store an embedding for *name*.

        Contamination guard: if the buffered audio matches a DIFFERENT
        enrolled speaker above the identify threshold, refuse — enrolling it
        under *name* would poison that profile (this happened: Tony's voice
        overwrote 'KK' after a borderline misidentification)."""
        if name.strip().lower() in RESERVED_NAMES:
            raise ValueError(
                f"'{name}' is a placeholder, not a real name — ask the "
                "speaker for their name first, then enroll under that."
            )
        emb = self._compute_embedding(pcm16, sample_rate)
        best_other, best_other_score = None, 0.0
        for other, ref in self._enrolled.items():
            if other == name:
                continue
            score = _cosine_similarity(emb, ref)
            if score > best_other_score:
                best_other, best_other_score = other, score
        if best_other is not None and best_other_score >= DEFAULT_THRESHOLD:
            raise ValueError(
                f"this voice matches enrolled speaker '{best_other}' "
                f"({best_other_score:.0%}) — refusing to enroll it as '{name}'. "
                "Confirm who is actually speaking, or remove the stale profile first."
            )
        self._enrolled[name] = emb
        self._metadata[name] = {
            "ha_person": ha_person,
            "display_name": display_name,
        }
        self._save_enrolled()
        logger.info("enrolled speaker '%s' (dim=%d, ha_person=%s)", name, emb.shape[0], ha_person)
        try:
            from backend.event_log import log_event
            log_event("speaker-enrolled", name, "voice",
                      f"ha_person={ha_person or '-'}")
        except Exception:
            pass
        return {"name": name, "samples": len(pcm16) // 2,
                "duration_s": round(len(pcm16) / 2 / sample_rate, 1),
                "ha_person": ha_person, "display_name": display_name}

    def remove(self, name: str) -> bool:
        if name not in self._enrolled:
            return False
        del self._enrolled[name]
        self._metadata.pop(name, None)
        self._save_enrolled()
        logger.info("removed speaker '%s'", name)
        try:
            from backend.event_log import log_event
            log_event("speaker-removed", name, "voice")
        except Exception:
            pass
        return True

    def identify(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE,
                 threshold: float = DEFAULT_THRESHOLD) -> tuple[str | None, float]:
        """Return (name, confidence) or (None, best_score)."""
        if not self._enrolled:
            return None, 0.0
        emb = self._compute_embedding(pcm16, sample_rate)
        best_name: str | None = None
        best_score = 0.0
        second_score = 0.0
        for name, ref in self._enrolled.items():
            score = _cosine_similarity(emb, ref)
            if score > best_score:
                second_score = best_score
                best_name, best_score = name, score
            elif score > second_score:
                second_score = score
        if best_score < threshold:
            return None, best_score
        if best_score - second_score < MIN_MARGIN:
            logger.info(
                "identify ambiguous: %s=%.2f vs runner-up %.2f (margin %.2f < %.2f) — unrecognized",
                best_name, best_score, second_score,
                best_score - second_score, MIN_MARGIN)
            return None, best_score
        return best_name, best_score


class SpeakerSession:
    """Per-voice-session buffer that identifies speakers in the background.

    Feed every incoming PCM16 packet via ``feed()``.  When enough speech
    audio has accumulated, identification runs in a thread executor and
    ``on_identified(name, confidence)`` is awaited with the result.  The
    audio forward path is never blocked.
    """

    def __init__(
        self,
        identifier: SpeakerIdentifier,
        on_identified: Callable[[str, float], Awaitable[None]],
        threshold: float = DEFAULT_THRESHOLD,
        on_unrecognized: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._identifier = identifier
        self._on_identified = on_identified
        self._on_unrecognized = on_unrecognized
        self._threshold = threshold
        self._buffer = bytearray()
        self._identifying = False
        self._last_name: str | None = None
        self._closed = False
        self._task: asyncio.Task[None] | None = None
        self._miss_count = 0
        self._unrecognized_notified = False
        # Rolling tail of ALL fed audio (not consumed by identification) —
        # enroll_from_buffer needs the voice that was just speaking even
        # after identify() consumed its chunk.
        self._recent = bytearray()

    async def feed(self, pcm16: bytes) -> None:
        if self._closed:
            return
        self._buffer.extend(pcm16)
        self._recent.extend(pcm16)
        # Drop excess if the buffer grew while identification was running.
        if len(self._buffer) > MAX_BUFFER_BYTES:
            del self._buffer[: len(self._buffer) - MAX_BUFFER_BYTES]
        if len(self._recent) > MAX_BUFFER_BYTES:
            del self._recent[: len(self._recent) - MAX_BUFFER_BYTES]
        if self._identifying or len(self._buffer) < MIN_CHUNK_BYTES:
            return
        # Quick energy check — skip near-silence buffers.
        chunk = bytes(self._buffer[:MIN_CHUNK_BYTES])
        audio = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        if _rms(audio) < SILENCE_RMS:
            self._buffer.clear()
            return
        # Consume the chunk and start identification.
        del self._buffer[:MIN_CHUNK_BYTES]
        self._identifying = True
        self._task = asyncio.create_task(self._identify(chunk))

    async def _identify(self, chunk: bytes) -> None:
        try:
            loop = asyncio.get_running_loop()
            name, confidence = await loop.run_in_executor(
                None, self._identifier.identify, chunk, SAMPLE_RATE, self._threshold
            )
            if name is not None and name != self._last_name:
                self._last_name = name
                self._miss_count = 0
                self._unrecognized_notified = False
                logger.info("speaker identified: %s (%.0f%%)", name, confidence * 100)
                await self._on_identified(name, confidence)
            elif name is None:
                self._miss_count += 1
                if (
                    self._on_unrecognized is not None
                    and self._miss_count >= UNKNOWN_AFTER_MISSES
                    and not self._unrecognized_notified
                    and self._last_name is None
                ):
                    self._unrecognized_notified = True
                    logger.info(
                        "speaker not recognized after %d attempts (best %.0f%%)",
                        self._miss_count, confidence * 100,
                    )
                    await self._on_unrecognized(confidence)
        except Exception as exc:
            logger.warning("speaker identification failed: %s", exc)
        finally:
            self._identifying = False

    @property
    def current_speaker(self) -> str | None:
        return self._last_name

    def enroll_from_buffer(
        self,
        name: str,
        ha_person: str | None = None,
        display_name: str | None = None,
        seconds: float = 3.5,
    ) -> dict[str, Any]:
        """Capture the last *seconds* of buffered audio and enroll *name*.

        Uses whatever audio is currently in the buffer — the user's voice
        that was just speaking is already there. Returns the enroll result
        dict from SpeakerIdentifier.enroll().
        """
        # Cap at available audio
        max_bytes = min(len(self._recent), int(seconds * SAMPLE_RATE * 2))
        if max_bytes < MIN_CHUNK_BYTES // 2:
            raise ValueError(
                f"not enough audio buffered ({max_bytes / (SAMPLE_RATE * 2):.1f}s); "
                "speak for a few seconds first"
            )
        pcm16 = bytes(self._recent[-max_bytes:])
        result = self._identifier.enroll(
            name, pcm16, ha_person=ha_person, display_name=display_name
        )
        return result

    async def close(self) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
