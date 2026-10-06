"""Contract tests for tests/bench/encoders.py — the multi-model voice
bench adapters. Model-dependent checks skip when a backend's deps or
weights aren't installed (CI never downloads)."""
import os
import sys
import unittest
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "bench"))

import encoders  # noqa: E402
import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location(
    "voice_bench_models", REPO / "tests" / "bench" / "voice-bench-models.py")


class EncoderContractTests(unittest.TestCase):

    def test_registry_shape(self) -> None:
        slugs = [c.slug for c in encoders.REGISTRY]
        self.assertEqual(len(slugs), len(set(slugs)), "duplicate slugs")
        for cls in encoders.REGISTRY:
            enc = cls()
            self.assertTrue(enc.slug)
            self.assertTrue(enc.title)
            self.assertGreaterEqual(enc.params_m, 0)
            ok, _why = enc.available()
            self.assertIsInstance(ok, bool)

    def test_normalize_and_cosine(self) -> None:
        v = np.array([3.0, 4.0], dtype=np.float32)
        n = encoders._normalize(v)
        self.assertAlmostEqual(float(np.linalg.norm(n)), 1.0, places=5)
        self.assertAlmostEqual(encoders.cosine(v, v), 1.0, places=5)
        self.assertAlmostEqual(encoders.cosine(v, -v), -1.0, places=5)
        self.assertEqual(float(np.linalg.norm(encoders._normalize(
            np.zeros(4, dtype=np.float32)))), 0.0)

    def test_fbank_shape(self) -> None:
        wav = np.random.RandomState(0).randn(16000).astype(np.float32) * 0.05
        feat = encoders._fbank80(wav)
        self.assertEqual(feat.shape[1], 80)
        self.assertGreater(feat.shape[0], 90)
        self.assertTrue(np.isfinite(feat).all())

    def _load_driver(self):
        mod = _ilu.module_from_spec(_spec)
        os.environ.setdefault("ADA_VOICE_MODELS", "/tmp/nonexistent-models")
        sys.modules.setdefault("voice_bench_models", mod)
        _spec.loader.exec_module(mod)
        return mod

    def test_eer_separable(self) -> None:
        mod = self._load_driver()
        genuine = [0.6, 0.7, 0.65, 0.8]
        impostor = [0.1, 0.2, 0.15, 0.05]
        eer, thr = mod.eer_threshold(genuine, impostor)
        self.assertAlmostEqual(eer, 0.0, places=2)
        self.assertGreater(thr, 0.2)
        self.assertLess(thr, 0.6)

    def test_eer_overlapping(self) -> None:
        mod = self._load_driver()
        genuine = [0.4, 0.5, 0.45, 0.3]
        impostor = [0.35, 0.42, 0.5, 0.55]
        eer, thr = mod.eer_threshold(genuine, impostor)
        self.assertGreater(eer, 0.1)


class CorpusIntegrityTests(unittest.TestCase):
    """Only runs when the corpus clips are checked out (they live on the
    bench host, not in git)."""

    def test_qualified_clips_decode(self) -> None:
        corpus = REPO / "tests" / "voice-corpus"
        if not (corpus / "clips").exists():
            self.skipTest("corpus clips not present on this host")
        mod = self._load_driver()
        import yaml
        reg = yaml.safe_load((corpus / "registry.yaml").read_text())
        speakers = [s for s in reg["speakers"] if s.get("qualified") == "ok"]
        problems = mod.check_corpus(corpus, speakers)
        self.assertEqual(problems, [])

    def _load_driver(self):
        return EncoderContractTests._load_driver(self)


if __name__ == "__main__":
    unittest.main()
