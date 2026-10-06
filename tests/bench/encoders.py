"""Voice-encoder adapters — one class per embedding backend.

Every adapter exposes the same surface so voice-bench-models.py can score
them identically:

    slug        unique short name (scorecard row)
    title       human-readable model id
    params_m    millions of parameters (declared — used in report only)
    size_mb     on-disk model size, measured after download
    embed_dim   embedding size, measured on first encode
    embed(wav)  np.float32[1..N] 16kHz mono → np.float32[n] L2-normalized
    load_s      model load seconds, measured
    available() (bool, reason) — False when deps/files are missing

Model files cache under $ADA_VOICE_MODELS (default
~/.cache/ada-voice-bench/). Downloads happen once; a bench run with no
network just reports unavailable adapters instead of failing.

Backends are lazy — importing this module never imports torch/onnxruntime.
"""
from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Shared implementations — the prod shadow path uses the same fbank,
# cosine, and ONNX session wrapper.
from backend.voice_encoders import (  # noqa: E402
    OnnxSpeakerEncoder, SAMPLE_RATE, cosine, fbank80 as _fbank80,
    normalize as _normalize,
)

MODEL_DIR = Path(os.environ.get(
    "ADA_VOICE_MODELS", "~/.cache/ada-voice-bench")).expanduser()


def _download(urls: list[str], dest: Path) -> Path | None:
    """First working URL wins; returns None when everything fails."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for url in urls:
        try:
            tmp = dest.with_suffix(dest.suffix + ".part")
            urllib.request.urlretrieve(url, tmp)
            tmp.rename(dest)
            return dest
        except Exception:
            continue
    return None


class BaseEncoder:
    slug = "base"
    title = ""
    params_m = 0.0
    load_s = 0.0
    embed_dim = 0

    def available(self) -> tuple[bool, str]:
        return True, ""

    def model_size_mb(self) -> float:
        return 0.0

    def embed(self, wav: np.ndarray) -> np.ndarray:
        raise NotImplementedError


class SpeechBrainEcapa(BaseEncoder):
    """Prod baseline — speechbrain/spkrec-ecapa-voxceleb."""
    slug = "ecapa-tdnn"
    title = "speechbrain/spkrec-ecapa-voxceleb (ECAPA-TDNN)"
    params_m = 20.1

    def __init__(self):
        self._m = None

    def available(self):
        try:
            import torch  # noqa: F401
            from speechbrain.inference.speaker import SpeakerRecognition  # noqa
        except Exception as e:
            return False, f"speechbrain/torch missing: {e}"
        return True, ""

    def _load(self):
        if self._m is None:
            import time as _t
            import torch
            from speechbrain.inference.speaker import SpeakerRecognition
            t0 = _t.monotonic()
            self._m = SpeakerRecognition.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                run_opts={"device": "cpu"})
            self.load_s = _t.monotonic() - t0
            self._torch = torch
        return self._m

    def embed(self, wav):
        m = self._load()
        t = self._torch.tensor(
            np.asarray(wav, dtype=np.float32), dtype=self._torch.float32
        ).unsqueeze(0)
        emb = m.encode_batch(t).detach().cpu().numpy().ravel()
        if self.embed_dim == 0:
            self.embed_dim = int(emb.size)
        return _normalize(emb)


class _OnnxFbankEncoder(BaseEncoder):
    """Shared base for ONNX speaker models that take fbank [1,T,80] and
    return an embedding directly."""
    filename = ""
    urls: list[str] = []

    def __init__(self):
        self._path = MODEL_DIR / self.filename
        self._enc = OnnxSpeakerEncoder(self._path)

    def available(self):
        try:
            import onnxruntime  # noqa: F401
        except Exception as e:
            return False, f"onnxruntime missing: {e}"
        if not self._path.exists():
            return False, f"model file absent: {self._path}"
        return True, ""

    def model_size_mb(self):
        return self._path.stat().st_size / 1e6 if self._path.exists() else 0.0

    def embed(self, wav):
        out = self._enc.embed(wav)
        self.load_s = self._enc.load_s
        if self.embed_dim == 0:
            self.embed_dim = int(out.size)
        return out


_WS_ZOO = ("https://wespeaker-1256283475.cos.ap-shanghai.myqcloud.com"
           "/models/voxceleb")


class OnnxWespeaker(_OnnxFbankEncoder):
    slug = "wespeaker-r34"
    title = "wespeaker voxceleb_resnet34_LM (ONNX)"
    params_m = 23.4
    filename = "voxceleb_resnet34_LM.onnx"
    urls = [f"{_WS_ZOO}/voxceleb_resnet34_LM.onnx"]


class OnnxCampplus(_OnnxFbankEncoder):
    slug = "campplus"
    title = "wespeaker voxceleb_CAM++_LM (ONNX)"
    params_m = 7.2
    filename = "voxceleb_CAM++_LM.onnx"
    urls = [f"{_WS_ZOO}/voxceleb_CAM%2B%2B_LM.onnx"]


class OnnxEcapa512(_OnnxFbankEncoder):
    slug = "ecapa512-onnx"
    title = "wespeaker voxceleb_ECAPA512_LM (ONNX)"
    params_m = 14.7
    filename = "voxceleb_ECAPA512_LM.onnx"
    urls = [f"{_WS_ZOO}/voxceleb_ECAPA512_LM.onnx"]


class OnnxResnet221(_OnnxFbankEncoder):
    slug = "resnet221"
    title = "wespeaker voxceleb_resnet221_LM (ONNX) — size anchor"
    params_m = 145.0
    filename = "voxceleb_resnet221_LM.onnx"
    urls = [f"{_WS_ZOO}/voxceleb_resnet221_LM.onnx"]


class OnnxEres2net(_OnnxFbankEncoder):
    """No official ONNX published — adapter ready, drop the file into
    $ADA_VOICE_MODELS to enable."""
    slug = "eres2net"
    title = "ERes2NetV2-base voxceleb (ONNX)"
    params_m = 6.9
    filename = "eres2net_voxceleb.onnx"
    urls: list[str] = []


class ResemblyzerDvec(BaseEncoder):
    slug = "dvector"
    title = "resemblyzer d-vector"
    params_m = 1.8

    def __init__(self):
        self._enc = None

    def available(self):
        try:
            import resemblyzer  # noqa: F401
        except Exception as e:
            return False, f"resemblyzer missing: {e}"
        return True, ""

    def _load(self):
        if self._enc is None:
            import time as _t
            from resemblyzer import VoiceEncoder
            t0 = _t.monotonic()
            self._enc = VoiceEncoder(device="cpu")
            self.load_s = _t.monotonic() - t0
        return self._enc

    def embed(self, wav):
        from resemblyzer.audio import preprocess_wav
        emb = self._load().embed_utterance(
            preprocess_wav(np.asarray(wav, dtype=np.float32),
                           source_sr=SAMPLE_RATE))
        if self.embed_dim == 0:
            self.embed_dim = int(emb.size)
        return _normalize(np.asarray(emb, dtype=np.float32))


REGISTRY: list[type[BaseEncoder]] = [
    SpeechBrainEcapa,
    OnnxWespeaker,
    OnnxCampplus,
    OnnxEcapa512,
    OnnxResnet221,
    OnnxEres2net,
    ResemblyzerDvec,
]


def encoders(selected: list[str] | None = None) -> list[BaseEncoder]:
    out = []
    for cls in REGISTRY:
        if selected and cls.slug not in selected:
            continue
        out.append(cls())
    return out


def ensure_models() -> None:
    """Download any missing ONNX model files (best-effort; adapters that
    can't fetch simply report unavailable)."""
    for cls in REGISTRY:
        path = MODEL_DIR / getattr(cls, "filename", "")
        if getattr(cls, "filename", None) and not path.exists():
            _download(cls.urls, path)
