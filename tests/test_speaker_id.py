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
os.environ.setdefault("ADA_SPEAKER_KEEP_AUDIO", "0")

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


class MediaSinkTest(unittest.TestCase):
    """media:true profiles are noise sinks — they may win only when no
    person is within MIN_MARGIN (regression: Tony matched 'NewSpeaker'
    media profile 0.48 and Ada muted him as ambient audio)."""

    def setUp(self):
        self.ident = speaker_id.SpeakerIdentifier()
        self.ident._enrolled = {}
        self.ident._metadata = {}

    def _media(self, name: str, v: np.ndarray) -> None:
        self.ident._enrolled[name] = v
        self.ident._metadata[name] = {"media": True}

    def test_confident_person_beats_media_within_margin(self):
        # Person 0.50, media 0.52 — person wins (media can't shadow a
        # confident person match).
        person = _vec(10)
        media = _vec(10) + 0.02 * _vec(11)
        media /= np.linalg.norm(media)
        self.ident._enrolled = {"Tony": person}
        self._media("NewSpeaker", media)
        with patch.object(self.ident, "_compute_embedding", return_value=person):
            name, score = self.ident.identify(b"\x00" * 1000)
        self.assertEqual(name, "Tony")

    def test_media_wins_when_no_person_close(self):
        # Genuine TV audio: media sink >> any person → media verdict so
        # the session mutes it as ambient.
        media = _vec(12)
        person = _vec(13)
        self.ident._enrolled = {"Tony": person}
        self._media("NewSpeaker", media)
        near_media = media + 0.005 * _vec(14)
        near_media /= np.linalg.norm(near_media)
        with patch.object(self.ident, "_compute_embedding", return_value=near_media):
            name, score = self.ident.identify(b"\x00" * 1000)
        self.assertEqual(name, "NewSpeaker")

    def test_media_and_person_both_weak_unrecognized(self):
        media = _vec(15)
        person = _vec(16)
        self.ident._enrolled = {"Tony": person}
        self._media("NewSpeaker", media)
        far = _vec(17)
        with patch.object(self.ident, "_compute_embedding", return_value=far):
            name, score = self.ident.identify(b"\x00" * 1000)
        self.assertIsNone(name)

    def test_person_below_threshold_within_media_margin_unrecognized(self):
        # person and media both sub-threshold and close — nobody
        # confident → None, NOT the media label (a real near-miss person
        # must not be muted as ambient).
        person = _vec(18)
        media = _vec(19)
        self.ident._enrolled = {"Tony": person}
        self._media("NewSpeaker", media)
        half = person + media + 3.0 * _vec(20)  # ~0.3 cosine to both
        half /= np.linalg.norm(half)
        with patch.object(self.ident, "_compute_embedding", return_value=half):
            name, score = self.ident.identify(b"\x00" * 1000)
        self.assertIsNone(name)


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

    def test_reenroll_merges_samples(self):
        tony = _vec(20)
        self.ident._enrolled = {"Tony": tony.copy()}
        self.ident._metadata = {"Tony": {"samples": 1}}
        near = tony + 0.02 * _vec(21)
        near /= np.linalg.norm(near)
        with patch.object(self.ident, "_compute_embedding", return_value=near), \
             patch.object(self.ident, "_save_enrolled"):
            out = self.ident.enroll("Tony", b"\x00" * 2000)
        self.assertEqual(out["samples"], 2)
        # Merged print stays unit-norm and closer to the new sample than
        # the old print alone was.
        merged = self.ident._enrolled["Tony"]
        self.assertAlmostEqual(float(np.linalg.norm(merged)), 1.0, places=4)
        self.assertGreater(
            speaker_id._cosine_similarity(merged, near),
            speaker_id._cosine_similarity(tony, near),
        )

    def test_reenroll_refuses_foreign_voice(self):
        self.ident._enrolled = {"Tony": _vec(22)}
        self.ident._metadata = {"Tony": {"samples": 1}}
        with patch.object(self.ident, "_compute_embedding", return_value=_vec(23)), \
             self.assertRaises(ValueError):
            self.ident.enroll("Tony", b"\x00" * 2000)
        self.assertEqual(self.ident._metadata["Tony"]["samples"], 1)


class MultiPrintTest(unittest.TestCase):
    """Schema v2: per-sample prints, scoring modes, outlier pruning."""

    def setUp(self):
        self.ident = speaker_id.SpeakerIdentifier()
        self.ident._enrolled = {}
        self.ident._prints = {}
        self.ident._metadata = {}

    def _enroll_with(self, name: str, v: np.ndarray) -> dict:
        with patch.object(self.ident, "_compute_embedding", return_value=v), \
             patch.object(self.ident, "_save_enrolled"):
            return self.ident.enroll(name, b"\x00" * 2000)

    def test_enroll_accumulates_prints(self):
        self._enroll_with("Tony", _vec(30))
        self._enroll_with("Tony", _vec(30) + 0.01 * _vec(31))
        self.assertEqual(len(self.ident._prints["Tony"]), 2)
        self.assertEqual(self.ident._metadata["Tony"]["samples"], 2)

    def test_v1_centroid_migrates_into_prints(self):
        self.ident._enrolled = {"Tony": _vec(32)}
        self.ident._metadata = {"Tony": {"samples": 1}}
        near = _vec(32) + 0.01 * _vec(33)
        near /= np.linalg.norm(near)
        self._enroll_with("Tony", near)
        self.assertEqual(len(self.ident._prints["Tony"]), 2)
        self.assertEqual(self.ident._metadata["Tony"]["samples"], 2)

    def test_prune_drops_outlier_not_newest(self):
        base = _vec(34)
        with patch.dict(os.environ, {"ADA_SPEAKER_MAX_PRINTS": "3"}):
            for i in range(3):
                v = base + 0.01 * i * _vec(35)
                v /= np.linalg.norm(v)
                self._enroll_with("Tony", v)
            # A stale/mis-mic'd print already in the store (the guard would
            # refuse it as a fresh enroll — pruning exists to evict these).
            outlier = _vec(36)
            self.ident._prints["Tony"].append(outlier)
            self.ident._enrolled["Tony"] = speaker_id._centroid(
                self.ident._prints["Tony"])
            near = base + 0.01 * _vec(37)
            near /= np.linalg.norm(near)
            self._enroll_with("Tony", near)
        sims = [speaker_id._cosine_similarity(p, outlier)
                for p in self.ident._prints["Tony"]]
        self.assertTrue(all(s < 0.5 for s in sims))
        self.assertEqual(len(self.ident._prints["Tony"]), 3)

    def test_score_modes(self):
        a, b = _vec(40), _vec(41)
        self.ident._enrolled = {"Tony": speaker_id._centroid([a, b])}
        self.ident._prints = {"Tony": [a, b]}
        with patch.object(self.ident, "_compute_embedding", return_value=a), \
             patch.dict(os.environ, {"ADA_SPEAKER_SCORE": "max"}):
            _, sc_max = self.ident.identify(b"\x00" * 1000)
        with patch.object(self.ident, "_compute_embedding", return_value=a), \
             patch.dict(os.environ, {"ADA_SPEAKER_SCORE": "centroid"}):
            _, sc_c = self.ident.identify(b"\x00" * 1000)
        self.assertGreater(sc_max, sc_c)  # exact print beats the centroid

    def test_media_flag_roundtrip(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "p.json")
            self.ident._profiles_path = speaker_id.Path(path)
            self.ident._enrolled = {"TV": _vec(50)}
            self.ident._prints = {"TV": [_vec(50)]}
            self.ident._metadata = {"TV": {"media": True, "samples": 1}}
            self.ident._save_enrolled()
            data = json.loads(open(path).read())
            self.assertTrue(data["TV"]["media"])
            self.assertEqual(len(data["TV"]["prints"]), 1)


class SpeakerSwitchHysteresisTest(unittest.IsolatedAsyncioTestCase):
    """SpeakerSession must not flip identity on a single borderline chunk —
    the 2026-09-27 log showed กุ้ง↔NewSpeaker alternating every ~15 s at
    46-64 % confidence. SWITCH_AFTER consecutive wins are required to
    change an established speaker."""

    async def _run(self, sequence):
        ident = speaker_id.SpeakerIdentifier()
        calls = []
        async def on_ident(name, conf):
            calls.append(name)
        sess = speaker_id.SpeakerSession(ident, on_ident)
        for name, conf in sequence:
            with patch.object(ident, "identify_scored",
                              return_value=(name, conf, name, conf)):
                await sess._identify(b"\x00" * 1000)
        return calls, sess.current_speaker

    async def test_single_outlier_does_not_switch(self):
        calls, cur = await self._run([
            ("Tony", 0.9),          # establishes Tony
            ("Kung", 0.6),          # one borderline flip attempt
            ("Tony", 0.8),          # back to Tony
        ])
        self.assertEqual(calls, ["Tony"])
        self.assertEqual(cur, "Tony")

    async def test_two_consecutive_switch(self):
        calls, cur = await self._run([
            ("Tony", 0.9),
            ("Kung", 0.6), ("Kung", 0.65),  # sustained second speaker
        ])
        self.assertEqual(calls, ["Tony", "Kung"])
        self.assertEqual(cur, "Kung")

    async def test_alternating_flips_never_commit(self):
        calls, cur = await self._run([
            ("Tony", 0.9),
            ("Kung", 0.6), ("Tony", 0.7),
            ("Kung", 0.62), ("Tony", 0.75),
        ])
        self.assertEqual(calls, ["Tony"])
        self.assertEqual(cur, "Tony")

    async def test_first_identification_immediate(self):
        calls, cur = await self._run([("KK", 0.7)])
        self.assertEqual(calls, ["KK"])
        self.assertEqual(cur, "KK")

    async def test_miss_resets_pending_switch(self):
        calls, cur = await self._run([
            ("Tony", 0.9),
            ("Kung", 0.6),            # pending Kung=1
            (None, 0.4),              # miss resets
            ("Kung", 0.6),            # back to 1 — still not enough
            ("Tony", 0.8),
        ])
        self.assertEqual(calls, ["Tony"])
        self.assertEqual(cur, "Tony")


if __name__ == "__main__":
    unittest.main()
