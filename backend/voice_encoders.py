"""ONNX speaker-encoder wrapper — shared by the multi-model bench
(tests/bench/encoders.py) and the speaker-id shadow path
(backend/speaker_id.py).

numpy fbank + onnxruntime only — no torch, no speechbrain. The prod
identifier keeps its SpeechBrain ECAPA model; this module exists so a
second backend can score the same audio in parallel (shadow mode) or be
benched standalone.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000


def fbank80(wav: np.ndarray) -> np.ndarray:
    """Kaldi-compatible 80-mel fbank, numpy-only: 25ms/10ms frames,
    preemphasis 0.97, power spectrum, log — then per-utterance mean
    normalization (wespeaker-style CMN)."""
    wav = np.asarray(wav, dtype=np.float32)
    if wav.size < 400:
        wav = np.pad(wav, (0, 400 - wav.size))
    wav = np.concatenate([[wav[0]], wav[1:] - 0.97 * wav[:-1]])
    fl, fs_ = int(0.025 * SAMPLE_RATE), int(0.010 * SAMPLE_RATE)
    n_f = 1 + (wav.size - fl) // fs_
    idx = np.arange(fl)[None, :] + fs_ * np.arange(n_f)[:, None]
    frames = wav[idx] * np.hamming(fl)[None, :]
    spec = np.abs(np.fft.rfft(frames, n=512, axis=1)) ** 2
    # mel filterbank (slaney-style, 0–8kHz)
    def hz2mel(f): return 2595.0 * math.log10(1 + f / 700.0)
    def mel2hz(m): return 700.0 * (10 ** (m / 2595.0) - 1)
    mels = np.linspace(0, hz2mel(SAMPLE_RATE / 2), 82)
    freqs = np.linspace(0, SAMPLE_RATE / 2, spec.shape[1])
    fb = np.zeros((80, spec.shape[1]), dtype=np.float32)
    for i in range(80):
        lo, c, hi = mel2hz(mels[i]), mel2hz(mels[i + 1]), mel2hz(mels[i + 2])
        fb[i] = np.clip(
            np.minimum((freqs - lo) / max(c - lo, 1e-6),
                       (hi - freqs) / max(hi - c, 1e-6)), 0, None)
    feat = np.log(np.maximum(spec @ fb.T, 1e-6)).astype(np.float32)
    return feat - feat.mean(axis=0, keepdims=True)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float32).ravel()
    b = np.asarray(b, dtype=np.float32).ravel()
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).ravel()
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


class OnnxSpeakerEncoder:
    """Lazy onnxruntime session for wespeaker-style models: input fbank
    [1,T,80], output one embedding vector."""

    def __init__(self, model_path: str | os.PathLike) -> None:
        self.model_path = Path(model_path)
        self._sess = None
        self.load_s = 0.0

    @property
    def available(self) -> bool:
        if not self.model_path.exists():
            return False
        try:
            import onnxruntime  # noqa: F401
        except Exception:
            return False
        return True

    def _load(self):
        if self._sess is None:
            import time
            import onnxruntime as ort
            t0 = time.monotonic()
            self._sess = ort.InferenceSession(
                str(self.model_path), providers=["CPUExecutionProvider"])
            self.load_s = time.monotonic() - t0
        return self._sess

    def embed(self, wav: np.ndarray) -> np.ndarray:
        """float32 16kHz mono → L2-normalized embedding."""
        sess = self._load()
        feat = fbank80(np.asarray(wav, dtype=np.float32))[None, :, :]
        out = sess.run(None, {sess.get_inputs()[0].name: feat})[0].ravel()
        return normalize(out)
