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
import re
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
# Minimum cosine similarity to accept a match. Env override per service:
# far-field/satellite mics score ~0.1-0.2 below clean enrollment captures —
# on ada-ha-tony 0.45 sat above Tony's live voice even with a clean profile.
DEFAULT_THRESHOLD = float(os.environ.get("ADA_SPEAKER_THRESHOLD", "0.45"))
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

# Shadow mode: ADA_SPEAKER_SHADOW_MODEL points at a wespeaker-style ONNX
# file — the shadow encoder scores every identify() chunk in parallel and
# reports agree/disagree, but ECAPA stays the decider. Shadow voiceprints
# are re-embedded from the stored enrollment audio (speaker_samples/*.pcm)
# so both models see the same enrollment data.
SHADOW_MODEL = os.environ.get("ADA_SPEAKER_SHADOW_MODEL", "")
# campplus EER threshold from voice-bench-models (2026-10-06).
SHADOW_THRESHOLD = float(os.environ.get("ADA_SPEAKER_SHADOW_THRESHOLD", "0.39"))

# Placeholder names that must never become enrolled profiles — the model
# once enrolled a real speaker (KK) under "Guest" when she didn't state a
# name, which then identified her as a sandbox guest instead of prompting
# for her real name.
RESERVED_NAMES = {"guest", "unknown", "someone", "anon", "anonymous",
                  "test", "tester", "newspeaker", "new speaker",
                  "new_speaker", "unnamed", "user"}
# UI/template placeholder *patterns* — "(user input required)" slipped
# through RESERVED_NAMES verbatim on 2026-10-01 because it was a form
# default, not a listed word. Any parenthesized or template-worded name
# is a UI artifact, never a person.
_PLACEHOLDER_PATTERN = re.compile(
    r"^\s*\(|\b(input|required|placeholder|enter\s+name|your\s+name)\b",
    re.IGNORECASE)
# Consecutive identical chunks required to CHANGE an established speaker —
# a single borderline frame flipping กุ้ง↔NewSpeaker at 46-64% was observed
# 2026-09-27 (same voice enrolled twice under two names). First-time
# identification still accepts a single confident chunk.
SWITCH_AFTER = 2
# First-time identification needs FIRST_AFTER consecutive chunks naming the
# same speaker — a single ambient-noise hit once greeted a session as the
# wrong speaker.  (ADA_SPEAKER_FIRST_ID_CONF is retired: confidence is not
# evidence of multiplicity — 2026-10-03 a confident 84% first-chunk hit was
# junk audio on a polluted profile.)
FIRST_AFTER = int(os.environ.get("ADA_SPEAKER_FIRST_AFTER", "2"))

# Soft-owner hint: when a chunk's best person match IS the enrolled owner of
# this session's key but below the identify threshold (far-field mics sit at
# 0.2-0.5), surface a "likely the owner" hint after OWNER_HINT_AFTER
# consecutive sub-threshold hits. Personalizes the conversation without
# widening authorization — the session owner is already pinned by the key.
OWNER_HINT_MIN = float(os.environ.get("ADA_SPEAKER_OWNER_HINT_MIN", "0.30"))
OWNER_HINT_AFTER = int(os.environ.get("ADA_SPEAKER_OWNER_HINT_AFTER", "2"))
# Owner reclaim: a stale non-owner label (one ambient hit pins it ~90s)
# must be displaceable by the session owner's own voice even when it
# scores under the hard threshold — far-field owner audio on the bound
# device sits at 0.25-0.5 (2026-10-03: Tony on the iPad card maxed at
# 0.49; one KK chunk at t=0 branded the whole session 'person.kk').
# RECLAIM_MIN is lower than OWNER_HINT_MIN on purpose: the streak of
# consecutive owner-best chunks is the real evidence, not one score.
OWNER_RECLAIM_MIN = float(os.environ.get("ADA_SPEAKER_OWNER_RECLAIM_MIN", "0.25"))
OWNER_RECLAIM_AFTER = int(os.environ.get("ADA_SPEAKER_OWNER_RECLAIM_AFTER", "3"))

# Enrollment capture window — seconds of trailing audio kept for
# enroll_from_buffer. 6s (the old MAX_BUFFER_BYTES cap) only ever held the
# speaker's last reply; 30s spans several utterances so an enroll call has
# real voice to work with (2026-09-28: repeated "insufficient audio"
# failures mid-conversation).
RECENT_SECONDS = float(os.environ.get("ADA_SPEAKER_RECENT_S", "30"))
# Auto-learn floor: an identification at or above this confidence appends
# the chunk as an extra print — profiles improve with use instead of
# staying frozen at first-enrollment quality.
AUTO_LEARN_MIN_CONF = float(os.environ.get("ADA_SPEAKER_AUTO_LEARN_MIN", "0.60"))

# Per-speaker print cap (ADA_SPEAKER_MAX_PRINTS). When full, the print least
# similar to the speaker's own centroid is dropped — outlier pruning keeps
# the centroid clean without letting a bad capture live forever.
def _max_prints() -> int:
    try:
        return max(1, int(os.environ.get("ADA_SPEAKER_MAX_PRINTS", "12")))
    except ValueError:
        return 12


# Whether enroll() also persists the raw PCM clip (ADA_SPEAKER_KEEP_AUDIO=0
# disables). Retained clips are what bench_speaker_id.py replays.
def _keep_audio() -> bool:
    return os.environ.get("ADA_SPEAKER_KEEP_AUDIO", "1") != "0"


# Retained .pcm clips expire after this many days — bounded retention so
# raw voice audio doesn't accumulate forever (the vectors-only posture is
# ADA_SPEAKER_KEEP_AUDIO=0; this keeps recent clips for the LOO bench).
AUDIO_TTL_DAYS = float(os.environ.get("ADA_SPEAKER_AUDIO_TTL_DAYS", "14"))


def _centroid(prints: list[np.ndarray]) -> np.ndarray:
    """L2-normalized mean of per-sample prints — same geometry as the
    historical running-average merge."""
    m = np.mean(np.stack(prints), axis=0)
    n = float(np.linalg.norm(m))
    return m / n if n else m


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
        self._enrolled: dict[str, np.ndarray] = {}  # name -> centroid embedding
        # Per-sample voiceprints (schema v2). _enrolled stays the merged
        # centroid for back-compat consumers; _prints is the source of truth
        # for scoring modes and benchmark leave-one-out.
        self._prints: dict[str, list[np.ndarray]] = {}
        self._metadata: dict[str, dict[str, Any]] = {}  # name -> {ha_person, display_name}
        self._shadow: Any = None
        self._shadow_prints: dict[str, list[np.ndarray]] = {}
        self._shadow_tried = False
        # callable(dict) — the server wires ops events on disagreements.
        self.on_shadow: Callable[[dict], None] | None = None
        self.shadow_stats = {"probes": 0, "agree": 0, "disagree": 0}
        self._profiles_path = Path(
            os.environ.get(
                "ADA_SPEAKER_PROFILES",
                Path.home() / ".local/share/ada-pi/speaker_profiles.json",
            )
        )
        self._samples_dir = Path(
            os.environ.get(
                "ADA_SPEAKER_SAMPLES_DIR",
                str(self._profiles_path.parent / "speaker_samples"),
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

    # -- shadow backend --------------------------------------------------

    def _init_shadow(self) -> None:
        """Lazy-load the shadow ONNX encoder and re-embed each enrolled
        speaker's stored sample audio into shadow voiceprints. Runs once;
        failures disable shadow quietly (primary path untouched)."""
        if self._shadow_tried or not SHADOW_MODEL:
            return
        self._shadow_tried = True
        try:
            from backend.voice_encoders import OnnxSpeakerEncoder
            enc = OnnxSpeakerEncoder(SHADOW_MODEL)
            if not enc.available:
                logger.info("speaker shadow unavailable: %s", SHADOW_MODEL)
                return
            for name, meta in self._metadata.items():
                prints: list[np.ndarray] = []
                for rel in meta.get("audio") or []:
                    p = self._samples_dir.parent / rel
                    try:
                        wav = np.frombuffer(
                            p.read_bytes(), dtype=np.int16
                        ).astype(np.float32) / 32768.0
                        if wav.size >= SAMPLE_RATE // 2:
                            prints.append(enc.embed(wav))
                    except OSError:
                        continue
                if prints:
                    self._shadow_prints[name] = prints
            self._shadow = enc
            logger.info(
                "speaker shadow ready: %s — %d/%d speakers re-embedded",
                os.path.basename(SHADOW_MODEL), len(self._shadow_prints),
                len(self._metadata))
        except Exception as exc:
            logger.warning("speaker shadow init failed: %s", exc)
            self._shadow = None

    def _shadow_identify(self, pcm16: bytes) -> tuple[str | None, float]:
        """Best person match under the shadow backend — same max-over-
        prints scoring as the primary centroid pool."""
        emb = self._shadow.embed(
            np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0)
        best_name: str | None = None
        best = 0.0
        for name, prints in self._shadow_prints.items():
            if self.is_media(name):
                continue
            s = max(_cosine_similarity(emb, p) for p in prints)
            if s > best:
                best_name, best = name, s
        return (best_name if best >= SHADOW_THRESHOLD else None), best

    def _maybe_shadow(self, pcm16: bytes, primary_name: str | None,
                      primary_score: float) -> None:
        """Score the same chunk with the shadow backend and report
        agreement. Never raises, never changes the primary result."""
        if not SHADOW_MODEL:
            return
        self._init_shadow()
        if self._shadow is None or not self._shadow_prints:
            return
        try:
            sname, sscore = self._shadow_identify(pcm16)
        except Exception as exc:
            logger.info("speaker shadow embed failed: %s", exc)
            return
        agree = sname == primary_name
        st = self.shadow_stats
        st["probes"] += 1
        st["agree" if agree else "disagree"] += 1
        logger.info(
            "speaker_shadow primary=%s@%.2f shadow=%s@%.2f agree=%s",
            primary_name, primary_score, sname, sscore, agree)
        if self.on_shadow:
            try:
                self.on_shadow({
                    "primary_name": primary_name,
                    "primary_score": primary_score,
                    "shadow_name": sname,
                    "shadow_score": sscore,
                    "agree": agree,
                    "model": os.path.basename(SHADOW_MODEL),
                })
            except Exception:
                pass

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
                prints: list[np.ndarray] = []
                for p in entry.get("prints") or []:
                    prints.append(
                        np.frombuffer(base64.b64decode(p), dtype=np.float32).copy())
                if not prints:
                    raw = base64.b64decode(entry["embedding"])
                    prints.append(np.frombuffer(raw, dtype=np.float32).copy())
                self._prints[name] = prints
                self._enrolled[name] = _centroid(prints)
                meta = {
                    "ha_person": entry.get("ha_person"),
                    "display_name": entry.get("display_name"),
                    "samples": len(prints),
                    "audio": list(entry.get("audio") or []),
                }
                if entry.get("media"):
                    meta["media"] = True
                self._metadata[name] = meta
        logger.info("loaded %d enrolled speaker profiles from %s", len(self._enrolled), self._profiles_path)

    def _prune_stale_audio(self) -> None:
        """Drop audio refs + unlink files older than AUDIO_TTL_DAYS."""
        cutoff = time.time() - AUDIO_TTL_DAYS * 86400
        for meta in self._metadata.values():
            kept = []
            for rel in meta.get("audio") or []:
                p = self._samples_dir.parent / rel
                try:
                    if p.stat().st_mtime >= cutoff:
                        kept.append(rel)
                        continue
                except OSError:
                    continue  # file gone — drop the ref too
                p.unlink(missing_ok=True)
            if "audio" in meta and len(kept) != len(meta["audio"]):
                meta["audio"] = kept

    def _save_enrolled(self) -> None:
        self._profiles_path.parent.mkdir(parents=True, exist_ok=True)
        self._prune_stale_audio()
        data = {}
        for name, emb in self._enrolled.items():
            meta = self._metadata.get(name, {})
            entry: dict[str, Any] = {
                "embedding": base64.b64encode(emb.tobytes()).decode("ascii"),
                "dim": int(emb.shape[0]),
                "ha_person": meta.get("ha_person"),
                "display_name": meta.get("display_name"),
                "samples": len(self._prints.get(name) or []) or (meta.get("samples") or 1),
            }
            prints = self._prints.get(name)
            if prints:
                entry["prints"] = [
                    base64.b64encode(p.tobytes()).decode("ascii") for p in prints
                ]
            if meta.get("audio"):
                entry["audio"] = meta["audio"]
            if meta.get("media"):
                entry["media"] = True
            data[name] = entry
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
                "samples": self._metadata.get(name, {}).get("samples") or 1,
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
                     display_name: str | None = None,
                     media: bool | None = None) -> bool:
        """Update HA person mapping, display name, or media flag for an
        enrolled speaker. ``media`` marks a non-person source (TV/video/
        podcast voice) so the pipeline steers Ada to ignore its content
        instead of answering it."""
        if name not in self._enrolled:
            return False
        meta = self._metadata.setdefault(name, {})
        if ha_person is not None:
            meta["ha_person"] = ha_person or None
        if display_name is not None:
            meta["display_name"] = display_name or None
        if media is not None:
            meta["media"] = bool(media)
        self._save_enrolled()
        logger.info("updated metadata for '%s': ha_person=%s display_name=%s media=%s",
                    name, meta.get("ha_person"), meta.get("display_name"),
                    meta.get("media"))
        return True

    def is_media(self, name: str) -> bool:
        """True when the profile is flagged as a media/device voice."""
        return bool(self._metadata.get(name, {}).get("media"))

    def enroll(self, name: str, pcm16: bytes, sample_rate: int = SAMPLE_RATE,
               ha_person: str | None = None,
               display_name: str | None = None,
               force: bool = False) -> dict[str, Any]:
        """Compute and store an embedding for *name*.

        Contamination guard: if the buffered audio matches a DIFFERENT
        enrolled speaker above the identify threshold, refuse — enrolling it
        under *name* would poison that profile (this happened: Tony's voice
        overwrote 'KK' after a borderline misidentification).

        force=True (owner-confirmed only) skips BOTH guards and REPLACES
        the profile — the recovery path when a real speaker is locked out
        by a stale/poisoned print."""
        if (name.strip().lower() in RESERVED_NAMES
                or _PLACEHOLDER_PATTERN.search(name)):
            raise ValueError(
                f"'{name}' is a placeholder, not a real name — ask the "
                "speaker for their name first, then enroll under that."
            )
        emb = self._compute_embedding(pcm16, sample_rate)
        if not force:
            # Contamination guard: match against EVERY stored print of every
            # other speaker (max), not just centroids — a foreign voice sitting
            # near any single sample is still foreign.
            best_other, best_other_score = None, 0.0
            for other in self._enrolled:
                if other == name:
                    continue
                for ref in self._prints.get(other) or [self._enrolled[other]]:
                    score = _cosine_similarity(emb, ref)
                    if score > best_other_score:
                        best_other, best_other_score = other, score
            if best_other is not None and best_other_score >= DEFAULT_THRESHOLD:
                raise ValueError(
                    f"this voice matches enrolled speaker '{best_other}' "
                    f"({best_other_score:.0%}) — refusing to enroll it as '{name}'. "
                    "Confirm who is actually speaking, or remove the stale profile first."
                )
        prev = self._metadata.get(name, {})
        if force:
            # replace outright — merging into a stale/poisoned profile is
            # the failure mode we're recovering from
            self._prints[name] = []
        elif name not in self._prints and name in self._enrolled:
            # v1→v2 migration: seed prints with the existing centroid so the
            # first v2 enrollment extends rather than replaces the profile.
            self._prints[name] = [self._enrolled[name]]
        if self._prints.get(name) and not force:
            # Own-voice check vs the speaker's best stored print (max) —
            # centroids alone get diluted as samples accumulate and would
            # reject a legitimate re-enrollment.
            own = max(
                _cosine_similarity(emb, p) for p in self._prints[name]
            )
            if own < DEFAULT_THRESHOLD:
                raise ValueError(
                    f"this voice does not match the existing '{name}' profile "
                    f"({own:.0%}) — refusing to merge a foreign voice. If this "
                    f"really is {name}, the stored print may be stale; remove "
                    "it first, then enroll fresh."
                )
        # Append the print, then prune: past the cap, drop the print LEAST
        # similar to the speaker centroid (the outlier) rather than the
        # oldest — a stray/mis-mic'd capture shouldn't outlive good ones.
        prints = self._prints.setdefault(name, [])
        prints.append(emb)
        centroid = _centroid(prints)
        while len(prints) > _max_prints():
            worst = min(
                range(len(prints)),
                key=lambda i: _cosine_similarity(prints[i], centroid),
            )
            prints.pop(worst)
            audio_list = prev.get("audio") or []
            if worst < len(audio_list):
                rel = audio_list.pop(worst)
                try:
                    (self._samples_dir.parent / rel).unlink(missing_ok=True)
                except OSError:
                    pass
            centroid = _centroid(prints)
        self._enrolled[name] = centroid
        prev["samples"] = len(prints)

        if _keep_audio():
            rel = self._save_sample_audio(name, pcm16)
            if rel:
                prev.setdefault("audio", []).append(rel)
                # keep audio list aligned with prints after pruning
                prev["audio"] = prev["audio"][-len(prints):]

        prev["ha_person"] = ha_person if ha_person is not None else prev.get("ha_person")
        prev["display_name"] = (
            display_name if display_name is not None else prev.get("display_name")
        )
        self._metadata[name] = prev
        self._save_enrolled()
        logger.info("enrolled speaker '%s' (dim=%d, ha_person=%s, samples=%d)",
                    name, emb.shape[0], prev.get("ha_person"), prev["samples"])
        try:
            from backend.event_log import log_event
            log_event("speaker-enrolled", name, "voice",
                      f"ha_person={prev.get('ha_person') or '-'}")
        except Exception:
            pass
        return {"name": name, "samples": prev["samples"],
                "duration_s": round(len(pcm16) / 2 / sample_rate, 1),
                "ha_person": prev.get("ha_person"), "display_name": prev.get("display_name")}

    def auto_learn(self, name: str, pcm16: bytes,
                   sample_rate: int = SAMPLE_RATE) -> bool:
        """Append a confidently-identified chunk as an extra print.

        Called from SpeakerSession when a chunk already identified as
        *name* at >= AUTO_LEARN_MIN_CONF — the profile self-improves with
        use rather than staying frozen at first-enrollment quality. The
        own-voice guard re-checks the chunk against the speaker's best
        stored print (a confident-but-marginal edge hit doesn't get to
        pull the centroid toward a foreign voice). No audio file, no
        event-log spam — this fires per identified chunk."""
        if name not in self._enrolled or self.is_media(name):
            return False
        emb = self._compute_embedding(pcm16, sample_rate)
        prints = self._prints.setdefault(name, [self._enrolled[name]])
        own = max(_cosine_similarity(emb, p) for p in prints)
        if own < AUTO_LEARN_MIN_CONF:
            return False
        prints.append(emb)
        centroid = _centroid(prints)
        while len(prints) > _max_prints():
            worst = min(range(len(prints)),
                        key=lambda i: _cosine_similarity(prints[i], centroid))
            prints.pop(worst)
            centroid = _centroid(prints)
        self._enrolled[name] = centroid
        meta = self._metadata.setdefault(name, {})
        meta["samples"] = len(prints)
        self._save_enrolled()
        logger.info("auto-learned voiceprint for '%s' (own=%.2f, samples=%d)",
                    name, own, len(prints))
        return True

    def _save_sample_audio(self, name: str, pcm16: bytes) -> str | None:
        """Persist the enrollment clip under speaker_samples/<slug>/ and
        return its path relative to the profiles dir, or None on failure."""
        try:
            slug = re.sub(r"[^A-Za-z0-9ก-๙_-]+", "-", name.strip()) or "speaker"
            d = self._samples_dir / slug
            d.mkdir(parents=True, exist_ok=True)
            rel = d / f"{int(time.time() * 1000)}.pcm"
            rel.write_bytes(pcm16)
            return str(rel.relative_to(self._samples_dir.parent))
        except OSError as exc:
            logger.warning("speaker sample audio save failed: %s", exc)
            return None

    @staticmethod
    def _name_slug(name: str) -> str:
        return "".join(c if c.isalnum() else " " for c in name.lower()).strip()

    def remove(self, name: str) -> bool:
        if name not in self._enrolled:
            # Slug-tolerant lookup — callers pass 'guest-tester',
            # 'guest tester', 'Guest_Tester' for the same profile; an
            # unambiguous normalized match is enough.
            want = self._name_slug(name)
            hits = [n for n in self._enrolled if self._name_slug(n) == want]
            if len(hits) == 1:
                name = hits[0]
            else:
                return False
        del self._enrolled[name]
        self._prints.pop(name, None)
        meta = self._metadata.pop(name, {})
        for rel in meta.get("audio") or []:
            try:
                (self._samples_dir.parent / rel).unlink(missing_ok=True)
            except OSError:
                pass
        self._save_enrolled()
        logger.info("removed speaker '%s'", name)
        try:
            from backend.event_log import log_event
            log_event("speaker-removed", name, "voice")
        except Exception:
            pass
        return True

    def _speaker_score(self, name: str, emb: np.ndarray,
                       centroid: np.ndarray, mode: str) -> float:
        """Score an embedding against one speaker's prints.

        ADA_SPEAKER_SCORE:
          centroid — cosine vs merged centroid (default; historical behavior)
          max      — best single-print match (sensitive, can over-fire)
          mean     — mean of per-print cosines
          hybrid   — 0.5*max + 0.5*mean
        """
        prints = self._prints.get(name)
        if not prints or mode == "centroid":
            return _cosine_similarity(emb, centroid)
        sims = [_cosine_similarity(emb, p) for p in prints]
        if mode == "max":
            return max(sims)
        if mode == "mean":
            return sum(sims) / len(sims)
        if mode == "hybrid":
            return 0.5 * max(sims) + 0.5 * (sum(sims) / len(sims))
        return _cosine_similarity(emb, centroid)

    def speaker_for_person(self, ha_person: str | None) -> str | None:
        """Enrolled speaker name bound to an HA person entity, or None."""
        if not ha_person:
            return None
        for name, meta in self._metadata.items():
            if meta.get("ha_person") == ha_person and not meta.get("media"):
                return name
        return None

    def identify(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE,
                 threshold: float = DEFAULT_THRESHOLD) -> tuple[str | None, float]:
        """Return (name, confidence) or (None, best_score)."""
        name, score, _, _ = self.identify_scored(pcm16, sample_rate, threshold)
        return name, score

    def identify_scored(
        self, pcm16: bytes, sample_rate: int = SAMPLE_RATE,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> tuple[str | None, float, str | None, float]:
        """identify() plus the raw best person candidate — the soft-owner
        hint needs to know WHO nearly matched even when the result is
        unrecognized. Returns (name, conf, best_person_name, best_person_score)."""
        result = self._identify_scored_core(pcm16, sample_rate, threshold)
        self._maybe_shadow(pcm16, result[0], result[1])
        return result

    def _identify_scored_core(
        self, pcm16: bytes, sample_rate: int = SAMPLE_RATE,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> tuple[str | None, float, str | None, float]:
        if not self._enrolled:
            return None, 0.0, None, 0.0
        emb = self._compute_embedding(pcm16, sample_rate)
        # Score persons and media sinks in separate pools — a media-flagged
        # profile is a noise sink (TV/ambient), not a person in the room.
        # Letting it win winner-take-all made Ada treat a real speaker as
        # background noise whenever their voice drifted near the media
        # cluster (observed 2026-09-27: Tony matched 'NewSpeaker' media
        # profile at 0.48 and was muted as ambient audio).
        best_name: str | None = None
        best_score = 0.0
        second_score = 0.0
        media_name: str | None = None
        media_score = 0.0
        score_mode = os.environ.get("ADA_SPEAKER_SCORE", "centroid")
        for name, ref in self._enrolled.items():
            score = self._speaker_score(name, emb, ref, score_mode)
            if self.is_media(name):
                if score > media_score:
                    media_name, media_score = name, score
                continue
            if score > best_score:
                second_score = best_score
                best_name, best_score = name, score
            elif score > second_score:
                second_score = score
        # A confident person within margin of the media sink wins the person
        # path — the media label is only legitimate when no person is close.
        if best_score >= threshold and best_score >= media_score - MIN_MARGIN:
            pass  # fall through to the person margin check below
        elif media_score >= threshold and media_score - best_score >= MIN_MARGIN:
            return media_name, media_score, best_name, best_score
        else:
            logger.info(
                "identify ambiguous: person %s=%.2f vs media %s=%.2f — unrecognized",
                best_name, best_score, media_name, media_score)
            return None, max(best_score, media_score), best_name, best_score
        if best_score < threshold:
            return None, best_score, best_name, best_score
        if best_score - second_score < MIN_MARGIN:
            logger.info(
                "identify ambiguous: %s=%.2f vs runner-up %.2f (margin %.2f < %.2f) — unrecognized",
                best_name, best_score, second_score,
                best_score - second_score, MIN_MARGIN)
            return None, best_score, best_name, best_score
        return best_name, best_score, best_name, best_score


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
        owner_speaker: str | None = None,
        on_likely_owner: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._identifier = identifier
        self._on_identified = on_identified
        self._on_unrecognized = on_unrecognized
        self._on_likely_owner = on_likely_owner
        # Enrolled NAME of the session owner's voiceprint (person.tony →
        # "Tony") — lets a sub-threshold best-match still personalize.
        self._owner_speaker = owner_speaker
        self._owner_hint_streak = 0
        self._owner_hint_fired = False
        self._threshold = threshold
        self._buffer = bytearray()
        self._identifying = False
        self._last_name: str | None = None
        self._closed = False
        self._task: asyncio.Task[None] | None = None
        self._miss_count = 0
        self._unrecognized_notified = False
        self._switch_pending: str | None = None
        self._switch_count = 0
        # Rolling tail of ALL fed audio (not consumed by identification) —
        # enroll_from_buffer needs the voice that was just speaking even
        # after identify() consumed its chunk. Capped at RECENT_SECONDS.
        self._recent = bytearray()
        # Voiced audio accrued while the speaker is UNRECOGNIZED — the
        # "yes, enroll me" fix: by the time an unknown speaker agrees to
        # enroll, their voice has been accruing all along, so the call
        # doesn't depend on how much they said in the last reply.
        self._pending_voice = bytearray()
        # Diagnostics (D): distinguish "bridge fed nothing" from "user
        # didn't speak" when enrollment reports insufficient audio.
        self._fed_bytes = 0
        self._voiced_bytes = 0
        # Freshness of the current speaker label: monotonic time of the
        # last completed hit for _last_name (pending-switch chunks don't
        # count — they haven't won yet). Consumers use speaker_age_s() to
        # expire a stale label instead of trusting it forever.
        self._last_hit_at: float | None = None

    async def feed(self, pcm16: bytes) -> None:
        if self._closed:
            return
        self._buffer.extend(pcm16)
        self._recent.extend(pcm16)
        self._fed_bytes += len(pcm16)
        # Drop excess if the buffer grew while identification was running.
        if len(self._buffer) > MAX_BUFFER_BYTES:
            del self._buffer[: len(self._buffer) - MAX_BUFFER_BYTES]
        recent_cap = int(RECENT_SECONDS * SAMPLE_RATE * 2)
        if len(self._recent) > recent_cap:
            del self._recent[: len(self._recent) - recent_cap]
        if len(self._pending_voice) > recent_cap:
            del self._pending_voice[: len(self._pending_voice) - recent_cap]
        if self._identifying or len(self._buffer) < MIN_CHUNK_BYTES:
            return
        # Quick energy check — skip near-silence buffers.
        chunk = bytes(self._buffer[:MIN_CHUNK_BYTES])
        audio = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        if _rms(audio) < SILENCE_RMS:
            self._buffer.clear()
            return
        self._voiced_bytes += MIN_CHUNK_BYTES
        # Consume the chunk and start identification.
        del self._buffer[:MIN_CHUNK_BYTES]
        self._identifying = True
        self._task = asyncio.create_task(self._identify(chunk))


    async def _identify(self, chunk: bytes) -> None:
        try:
            loop = asyncio.get_running_loop()
            name, confidence, best_name, best_score = await loop.run_in_executor(
                None, self._identifier.identify_scored, chunk, SAMPLE_RATE,
                self._threshold,
            )
            # Soft-owner hint: best match is the session owner's enrolled
            # voice but under threshold — flag "likely the owner" so the
            # greeting/personalization isn't name-blind. Auth never widens:
            # the owner is already pinned by the key binding.
            if (
                name is None
                and self._owner_speaker is not None
                and best_name == self._owner_speaker
                and best_score >= OWNER_RECLAIM_MIN
            ):
                self._owner_hint_streak += 1
                if (
                    best_score >= OWNER_HINT_MIN
                    and not self._owner_hint_fired
                    and self._owner_hint_streak >= OWNER_HINT_AFTER
                ):
                    self._owner_hint_fired = True
                    logger.info(
                        "speaker likely owner '%s' (%.0f%% best, sub-threshold)",
                        self._owner_speaker, best_score * 100)
                    if self._on_likely_owner is not None:
                        await self._on_likely_owner(self._owner_speaker)
                if (
                    self._last_name is not None
                    and self._last_name != self._owner_speaker
                    and self._owner_hint_streak >= OWNER_RECLAIM_AFTER
                ):
                    # The owner keeps winning best-match under threshold
                    # while a stale non-owner label sits pinned — promote
                    # the streak to a real identification so the label
                    # returns to the session owner.
                    prev = self._last_name
                    self._last_name = self._owner_speaker
                    self._last_hit_at = time.monotonic()
                    self._miss_count = 0
                    self._unrecognized_notified = False
                    self._switch_pending = None
                    self._switch_count = 0
                    logger.info(
                        "speaker reclaimed as owner '%s' (%.0f%% best x%d "
                        "sub-threshold — displaced '%s')",
                        self._owner_speaker, best_score * 100,
                        self._owner_hint_streak, prev)
                    await self._on_identified(self._owner_speaker, best_score)
            elif (
                best_name != self._owner_speaker
                and best_score >= OWNER_RECLAIM_MIN
            ):
                # A different voice that is itself a plausible match
                # breaks the streak — weak/quiet chunks where a foreign
                # print merely edges ahead (Ratree=0.18 while Tony=0.14)
                # carry no signal and must not reset owner recovery.
                self._owner_hint_streak = 0
            if name is not None and name != self._last_name:
                # Hysteresis: a NEW label — first-time identification AND
                # switching alike — needs consecutive wins for that name.
                # No confidence bypass: one 2 s chunk CAN score high on a
                # polluted profile (2026-10-03 session e564b39502: a junk
                # session-start chunk hit "KK" at 84%, stamped Tony's own
                # session as a guest, auto-learned the bad audio into KK's
                # profile, and the capture gate then demanded owner
                # confirmation on every camera call).  Earlier incident:
                # 2026-09-29 transcript 855a65dab8.
                after = SWITCH_AFTER if self._last_name is not None else FIRST_AFTER
                if name == self._switch_pending:
                    self._switch_count += 1
                else:
                    self._switch_pending = name
                    self._switch_count = 1
                if self._switch_count < after:
                    return
                self._switch_pending = None
                self._switch_count = 0
                self._last_name = name
                self._last_hit_at = time.monotonic()
                self._miss_count = 0
                self._unrecognized_notified = False
                # The accrued unknown-voice buffer is now attributed —
                # drop it so a later enroll can't claim it under the
                # wrong name.
                self._pending_voice.clear()
                logger.info("speaker identified: %s (%.0f%%)", name, confidence * 100)
                if confidence >= AUTO_LEARN_MIN_CONF:
                    self._identifier.auto_learn(name, chunk)
                await self._on_identified(name, confidence)
            else:
                # stable same-speaker hit or a miss — either way a pending
                # switch doesn't accrue (single-frame outliers shouldn't
                # count toward changing identity)
                self._switch_pending = None
                self._switch_count = 0
                if name is not None:
                    self._last_hit_at = time.monotonic()
                if name is not None and confidence >= AUTO_LEARN_MIN_CONF:
                    # stable hit — profile still improves with use
                    self._identifier.auto_learn(name, chunk)
                if name is None:
                    self._miss_count += 1
                    # Unrecognized voiced speech — keep accruing so a
                    # later "yes, enroll me" has more than the reply tail.
                    self._pending_voice.extend(chunk)
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

    def speaker_age_s(self) -> float | None:
        """Seconds since the current speaker label last re-confirmed —
        None when nobody has been identified. Voiced chunks that keep
        missing the threshold stop refreshing it, so an old one-off
        identification ages out instead of pinning the label forever."""
        if self._last_name is None or self._last_hit_at is None:
            return None
        return time.monotonic() - self._last_hit_at

    def identify_buffer(self, seconds: float = 15.0) -> tuple[str | None, float]:
        """Identify the voice in the enroll buffer WITHOUT enrolling —
        used by the owner-enrollment carve-out to prove the buffered
        voice isn't the currently-identified secondary speaker."""
        want = int(seconds * SAMPLE_RATE * 2)
        src = self._pending_voice if len(self._pending_voice) >= MIN_CHUNK_BYTES else self._recent
        take = min(len(src), want)
        pcm16 = bytes(src[-take:])
        if len(pcm16) < MIN_CHUNK_BYTES // 2:
            return None, 0.0
        return self._identifier.identify(pcm16, SAMPLE_RATE)

    def enroll_from_buffer(
        self,
        name: str,
        ha_person: str | None = None,
        display_name: str | None = None,
        seconds: float = 15.0,
        force: bool = False,
    ) -> dict[str, Any]:
        """Capture buffered audio and enroll *name*.

        Two sources, best-first:
          1. _pending_voice — voiced audio accrued while the speaker was
             unrecognized (spans the whole unidentified stretch, not just
             the last reply).
          2. _recent — the trailing RECENT_SECONDS ring of all fed audio.

        Returns the enroll result dict from SpeakerIdentifier.enroll().
        """
        want = int(seconds * SAMPLE_RATE * 2)
        src = self._pending_voice if len(self._pending_voice) >= MIN_CHUNK_BYTES else self._recent
        take = min(len(src), want)
        pcm16 = bytes(src[-take:])
        if len(pcm16) < MIN_CHUNK_BYTES // 2:
            fed_s = self._fed_bytes / (SAMPLE_RATE * 2)
            voiced_s = self._voiced_bytes / (SAMPLE_RATE * 2)
            if self._fed_bytes == 0:
                diag = "no audio arrived on this session — the capture bridge may be dead; restart the conversation"
            elif self._voiced_bytes < MIN_CHUNK_BYTES:
                diag = f"{voiced_s:.1f}s voiced audio seen of {fed_s:.1f}s fed — ask for a longer natural sentence"
            else:
                diag = "speak for a few more seconds"
            raise ValueError(
                f"not enough speech captured ({len(pcm16) / (SAMPLE_RATE * 2):.1f}s); {diag}"
            )
        result = self._identifier.enroll(
            name, pcm16, ha_person=ha_person, display_name=display_name,
            force=force,
        )
        # Enrolled — the pending voice has been claimed by a profile.
        self._pending_voice.clear()
        return result

    async def close(self) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
