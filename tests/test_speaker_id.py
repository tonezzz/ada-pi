"""Speaker-ID guards — contamination guard on enroll, margin rule on identify.

Regression for the KK-poisoning bug: a borderline misidentification
(Tony→KK at 51%) followed by ada_enroll_speaker overwrote KK's embedding
with Tony's voice, making every later session worse.
"""
import os
import unittest
from unittest.mock import patch

import numpy as np

os.environ.setdefault("ADA_SPEAKER_PROFILES", "/nonexistent-speaker-profiles.json")

from backend import speaker_id  # noqa: E402


def _vec(seed: int, dim: int = 192) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


class IdentifyMarginTest(unittest.TestCase):
    def setUp(self):
        self.ident = speaker_id.SpeakerIdentifier()
        self.ident._enrolled = {}

    def _audio(self, v: np.ndarray) -> bytes:
        return b"\x00" * 1000

    def _stub_emb(self, v: np.ndarray):
        return patch.object(self.ident, "_compute_embedding", return_value=v)

    def test_clear_winner_identified(self):
        tony, kk = _vec(1), _vec(2)
        self.ident._enrolled = {"Tony": tony, "KK": kk}
        near_tony = tony + 0.01 * _vec(3)
        near_tony /= np.linalg.norm(near_tony)
        with self._stub_emb(near_tony):
            name, score = self.ident.identify(self._audio(near_tony))
        self.assertEqual(name, "Tony")
        self.assertGreaterEqual(score, 0.45)

    def test_ambiguous_scores_unrecognized(self):
        # Two near-identical centroids: winner-take-all would pick one at
        # random; the margin rule must return None instead.
        shared = _vec(4)
        tony = shared + 0.001 * _vec(5)
        kk = shared + 0.002 * _vec(6)
        tony /= np.linalg.norm(tony)
        kk /= np.linalg.norm(kk)
        self.ident._enrolled = {"Tony": tony, "KK": kk}
        with self._stub_emb(shared):
            name, score = self.ident.identify(self._audio(shared))
        self.assertIsNone(name)

    def test_below_threshold_unrecognized(self):
        self.ident._enrolled = {"Tony": _vec(7)}
        far = _vec(8)  # orthogonal-ish
        with self._stub_emb(far):
            name, score = self.ident.identify(self._audio(far))
        self.assertIsNone(name)
        self.assertLess(score, speaker_id.DEFAULT_THRESHOLD)


class EnrollContaminationGuardTest(unittest.TestCase):
    def setUp(self):
        self.ident = speaker_id.SpeakerIdentifier()
        self.ident._enrolled = {}
        self.ident._metadata = {}

    def test_enroll_refuses_other_speakers_voice(self):
        tony = _vec(10)
        self.ident._enrolled = {"Tony": tony}
        # Enrolling Tony-like audio as 'KK' must refuse (the KK-poisoning bug).
        with patch.object(self.ident, "_compute_embedding", return_value=tony), \
             self.assertRaises(ValueError) as ctx:
            self.ident.enroll("KK", b"\x00" * 1000)
        self.assertIn("Tony", str(ctx.exception))
        self.assertNotIn("KK", self.ident._enrolled)

    def test_enroll_same_name_ok(self):
        tony = _vec(11)
        self.ident._enrolled = {"Tony": tony}
        new = tony + 0.02 * _vec(12)
        new /= np.linalg.norm(new)
        with patch.object(self.ident, "_compute_embedding", return_value=new), \
             patch.object(self.ident, "_save_enrolled"):
            out = self.ident.enroll("Tony", b"\x00" * 2000)
        self.assertEqual(out["name"], "Tony")

    def test_enroll_refuses_placeholder_name(self):
        # Regression: the model enrolled a real speaker (KK) as 'Guest'
        # when she didn't state a name — she then matched the sandbox
        # guest identity instead of being asked her name.
        for placeholder in ("Guest", "guest", "Unknown", "Test"):
            with patch.object(self.ident, "_compute_embedding", return_value=_vec(15)), \
                 self.assertRaises(ValueError):
                self.ident.enroll(placeholder, b"\x00" * 1000)
        self.assertNotIn("Guest", self.ident._enrolled)

    def test_enroll_unknown_voice_ok(self):
        self.ident._enrolled = {"Tony": _vec(13)}
        with patch.object(self.ident, "_compute_embedding", return_value=_vec(14)), \
             patch.object(self.ident, "_save_enrolled"):
            out = self.ident.enroll("KK", b"\x00" * 2000)
        self.assertEqual(out["name"], "KK")
        self.assertIn("KK", self.ident._enrolled)


if __name__ == "__main__":
    unittest.main()
