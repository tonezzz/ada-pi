"""File-input speaker ID + relay voice context (card
line-voice-enroll-recognize).

speaker_id.decode_audio_file/embed_file/match_file/enroll_from_file —
the no-live-buffer path for LINE/TG voice clips. line_relay scores each
saved clip and tags the persona turn with [voice: speaker=..]; unknown
callers arm the send-2-3-clips enrollment. speaker_profiles gains
match_file/enroll_file for ops. Boss rule: an owner-tier voice keeps
owner rights on a member lane.
"""
import asyncio
import os
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

os.environ.setdefault("ADA_SPEAKER_PROFILES", "/nonexistent-speaker-profiles.json")
os.environ.setdefault("ADA_SPEAKER_KEEP_AUDIO", "0")

try:
    import google.genai  # noqa: F401
except ImportError:
    import sys as _sys
    import types as _types
    _genai = _types.ModuleType("google.genai")
    _genai_types = _types.ModuleType("google.genai.types")
    _genai.types = _genai_types
    _sys.modules.setdefault("google.genai", _genai)
    _sys.modules.setdefault("google.genai.types", _genai_types)
    try:
        import google as _google
        _google.genai = _genai
    except ImportError:
        pass

from backend import line_relay, speaker_id, tools_loader  # noqa: E402
from backend.tool_runner import ToolRunner  # noqa: E402


def _vec(seed: int, dim: int = 192) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _identifier() -> speaker_id.SpeakerIdentifier:
    ident = speaker_id.SpeakerIdentifier()
    ident._enrolled = {}
    ident._prints = {}
    ident._metadata = {}
    return ident


def _wav(path: Path, seconds: float = 1.0, rate: int = 16000,
         channels: int = 1) -> Path:
    """Write a real WAV (440 Hz sine-ish ramp) — PyAV decodes it."""
    n = int(seconds * rate)
    pcm = np.tile(
        (np.sin(np.linspace(0, 440 * 2 * np.pi, rate)) * 8000)
        .astype(np.int16), int(np.ceil(seconds)))[:n]
    if channels == 2:
        pcm = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


class DecodeFileTest(unittest.TestCase):

    def test_decode_wav_via_av(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("PyAV not installed")
        with tempfile.TemporaryDirectory() as td:
            p = _wav(Path(td) / "clip.wav", seconds=1.5, rate=44100,
                     channels=2)
            pcm = speaker_id.decode_audio_file(p)
        self.assertAlmostEqual(len(pcm), int(1.5 * 16000) * 2, delta=64)

    def test_decode_resamples_and_caps(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("PyAV not installed")
        with tempfile.TemporaryDirectory() as td:
            p = _wav(Path(td) / "clip.wav", seconds=3.0)
            pcm = speaker_id.decode_audio_file(p, max_seconds=1.0)
        self.assertEqual(len(pcm), 16000 * 2)

    def test_decode_missing_file(self):
        with self.assertRaises(ValueError):
            speaker_id.decode_audio_file("/nonexistent/clip.m4a")

    def test_decode_too_short(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("PyAV not installed")
        with tempfile.TemporaryDirectory() as td:
            p = _wav(Path(td) / "clip.wav", seconds=0.1)
            with self.assertRaises(ValueError):
                speaker_id.decode_audio_file(p)

    def test_decode_ffmpeg_fallback(self):
        pcm = b"\x01" * 64000
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "clip.m4a"
            p.write_bytes(b"JUNK")
            with patch.object(speaker_id, "_decode_with_av",
                              side_effect=ImportError("no av")), \
                    patch.object(speaker_id, "_decode_with_ffmpeg",
                                 return_value=pcm) as ff:
                out = speaker_id.decode_audio_file(p)
        self.assertEqual(out, pcm)
        ff.assert_called_once()


class PersonEntityTest(unittest.TestCase):

    def test_normalizes_caller_and_person_forms(self):
        f = speaker_id._person_entity
        self.assertEqual(f("person.kk"), "person.kk")
        self.assertEqual(f("kk"), "person.kk")
        self.assertEqual(f("user-kk"), "person.kk")
        self.assertEqual(f("user-tonezzzz"), "person.tonezzzz")
        self.assertIsNone(f(""))
        self.assertIsNone(f(None))


class MatchEnrollFileTest(unittest.TestCase):

    def setUp(self):
        self.ident = _identifier()

    def test_embed_file_decodes_then_embeds(self):
        emb = _vec(9)
        with patch.object(speaker_id, "decode_audio_file",
                          return_value=b"\x00" * 64000) as dec, \
                patch.object(self.ident, "_compute_embedding",
                             return_value=emb):
            out = self.ident.embed_file("/tmp/clip.m4a")
        dec.assert_called_once_with("/tmp/clip.m4a")
        self.assertIs(out, emb)

    def test_match_file_names_speaker(self):
        tony = _vec(6)
        self.ident._enrolled = {"Tony": tony, "KK": _vec(7)}
        self.ident._prints = {"Tony": [tony], "KK": [_vec(7)]}
        self.ident._metadata = {"Tony": {"ha_person": "person.tony"}}
        with patch.object(speaker_id, "decode_audio_file",
                          return_value=b"\x11" * 64000), \
                patch.object(self.ident, "_compute_embedding",
                             return_value=tony):
            out = self.ident.match_file("/tmp/clip.m4a")
        self.assertEqual(out["match"], "Tony")
        self.assertGreaterEqual(out["score"], 0.45)
        self.assertEqual(out["ha_person"], "person.tony")
        self.assertEqual(out["all_scores"][0]["name"], "Tony")

    def test_match_file_miss_still_names_best(self):
        a, b = _vec(1), _vec(2)
        far = _vec(3)
        self.ident._enrolled = {"Tony": a, "KK": b}
        self.ident._prints = {"Tony": [a], "KK": [b]}
        with patch.object(speaker_id, "decode_audio_file",
                          return_value=b"\x11" * 64000), \
                patch.object(self.ident, "_compute_embedding",
                             return_value=far):
            out = self.ident.match_file("/tmp/clip.m4a")
        self.assertIsNone(out["match"])
        self.assertIn("best", out)
        self.assertTrue(out["all_scores"])

    def test_enroll_from_file_binds_person(self):
        voice = _vec(8)
        with tempfile.TemporaryDirectory() as td:
            self.ident._profiles_path = Path(td) / "p.json"
            with patch.object(speaker_id, "decode_audio_file",
                              return_value=b"\x00" * 64000), \
                    patch.object(self.ident, "_compute_embedding",
                                 return_value=voice):
                out = self.ident.enroll_from_file(
                    "/tmp/clip.m4a", "KK", person="user-kk")
        self.assertEqual(out["name"], "KK")
        self.assertEqual(out["ha_person"], "person.kk")
        self.assertEqual(out["path"], "/tmp/clip.m4a")

    def test_enroll_roundtrip_then_match(self):
        """3 clips enroll -> a 4th clip of the same voice matches."""
        voice = _vec(8)
        near = voice + 0.01 * _vec(3)
        near /= np.linalg.norm(near)
        with tempfile.TemporaryDirectory() as td:
            self.ident._profiles_path = Path(td) / "p.json"
            with patch.object(speaker_id, "decode_audio_file",
                              return_value=b"\x00" * 64000), \
                    patch.object(self.ident, "_compute_embedding",
                                 return_value=voice):
                for i in range(3):
                    self.ident.enroll_from_file(f"/tmp/c{i}.m4a", "KK")
            with patch.object(speaker_id, "decode_audio_file",
                              return_value=b"\x11" * 64000), \
                    patch.object(self.ident, "_compute_embedding",
                                 return_value=near):
                out = self.ident.match_file("/tmp/c4.m4a")
        self.assertEqual(out["match"], "KK")


class VoiceContextTest(unittest.TestCase):

    def test_recognized_line(self):
        ctx = line_relay._voice_context(
            {"match": "KK", "score": 0.62, "ha_person": "person.kk"})
        self.assertEqual(ctx, "[voice: speaker=KK score=0.62 "
                              "person=person.kk]")

    def test_unknown_line_with_best_and_pending(self):
        ctx = line_relay._voice_context(
            {"match": None, "score": 0.21, "best": "Ratree",
             "best_score": 0.21},
            {"got": 1})
        self.assertIn("speaker=unknown", ctx)
        self.assertIn("best=Ratree@0.21", ctx)
        self.assertIn("enroll-armed:1/", ctx)

    def test_just_enrolled_line(self):
        ctx = line_relay._voice_context({}, enrolled="kk")
        self.assertIn("just-enrolled", ctx)
        self.assertIn("speaker=kk", ctx)


class _FakeSession:
    made = []

    def __init__(self, user_id, relay, ws_url=None, caller=None, chat_id=None):
        self.user_id = user_id
        self.ws_url = ws_url
        self.caller = caller
        self.chat_id = chat_id
        self.ws = None
        self.last_active = 0.0
        self.send_turn = AsyncMock(return_value=("pong", []))
        self.send_image_turn = AsyncMock(return_value=("pic pong", []))
        _FakeSession.made.append(self)


def _relay():
    relay = line_relay.LineRelay()
    relay.send_text = AsyncMock()
    relay._deliver = AsyncMock()
    relay.line = AsyncMock(return_value=MagicMock(status_code=200))
    http = MagicMock()
    http.get = AsyncMock(
        return_value=MagicMock(status_code=404, content=b"x"))
    relay.http = http
    return relay


def _audio_ev(user_id="Ukk1", group_id=None, msg_id="a1", duration=3000):
    src = {"type": "user", "userId": user_id} if not group_id else {
        "type": "group", "groupId": group_id, "userId": user_id}
    return {"type": "message", "replyToken": "RT", "source": src,
            "message": {"id": msg_id, "type": "audio",
                        "duration": duration}}


class RelayVoiceTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _FakeSession.made = []
        self._patches = [
            patch.object(line_relay, "ChatSession", _FakeSession),
            patch.object(line_relay, "VOICE_CORPUS_DIR",
                         Path(self._tmp.name)),
            patch.object(line_relay, "MEDIA_ARCHIVE_DIR",
                         Path(self._tmp.name) / "line-media"),
            patch.dict(os.environ, {
                "LINE_ALLOWED_USER_IDS": "Utony1,Ukk1",
                "LINE_USER_CALLERS": "Utony1:user-tony,Ukk1:user-kk",
                "LINE_VOICE_TRANSCRIBE": "0",
                "LINE_VOICE_ENROLL": "1",
            }),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _sent(self, relay):
        return [c.args for c in relay.send_text.await_args_list]

    async def _clip(self, relay, user_id="Ukk1", msg_id="a1",
                    match=None, enroll=None):
        relay._fetch_content = AsyncMock(
            return_value=(b"M4ADATA", "audio/mp4"))
        with patch.object(line_relay, "_match_voice_file",
                          MagicMock(return_value=match or {})), \
                patch.object(line_relay, "_enroll_voice_file",
                             MagicMock(
                                 return_value=enroll or {})):
            await relay.handle_event(_audio_ev(
                user_id=user_id, msg_id=msg_id))

    async def test_recognized_clip_names_speaker_in_ack(self):
        relay = _relay()
        await self._clip(relay, match={
            "match": "KK", "score": 0.62, "ha_person": "person.kk"})
        sent = self._sent(relay)
        self.assertTrue(any("KK" in text and "62%" in text
                            for _, text, *k in sent), sent)

    async def test_unknown_clip_offers_enrollment_and_arms(self):
        relay = _relay()
        await self._clip(relay, match={"match": None, "score": 0.21})
        self.assertIn("user-kk", relay._enroll_pending)
        pend = relay._enroll_pending["user-kk"]
        self.assertEqual(pend["name"], "kk")
        self.assertEqual(pend["person"], "person.kk")
        self.assertTrue(any("2-3 short clips" in t for _, t, *k
                            in self._sent(relay)))

    async def test_pending_clips_enroll_then_confirm(self):
        relay = _relay()
        await self._clip(relay, msg_id="a1",
                         match={"match": None, "score": 0.2})
        # next clips: still unknown -> enroll_from_file per clip
        for i, samples in enumerate((1, 2, 3)):
            await self._clip(relay, msg_id=f"b{i}",
                             match={"match": None, "score": 0.2},
                             enroll={"name": "KK", "samples": samples})
        self.assertNotIn("user-kk", relay._enroll_pending)
        texts = [t for _, t, *k in self._sent(relay)]
        self.assertTrue(any("enrolled KK, 3 prints" in t for t in texts),
                        texts)

    async def test_matched_clip_clears_pending(self):
        relay = _relay()
        relay._enroll_pending["user-kk"] = {
            "name": "kk", "person": "person.kk", "got": 1,
            "armed_at": time.monotonic()}
        await self._clip(relay, match={"match": "KK", "score": 0.7})
        self.assertNotIn("user-kk", relay._enroll_pending)

    async def test_voice_ctx_reaches_persona_turn(self):
        relay = _relay()
        relay._fetch_content = AsyncMock(
            return_value=(b"M4ADATA", "audio/mp4"))
        with patch.object(line_relay, "_transcribe",
                          lambda p: "hi ada"), \
                patch.object(line_relay, "_match_voice_file",
                             MagicMock(return_value={
                                 "match": "KK", "score": 0.62,
                                 "ha_person": "person.kk"})):
            await relay.handle_event(_audio_ev(msg_id="a9"))
        [sess] = _FakeSession.made
        sent_text = sess.send_turn.await_args.args[0]
        self.assertIn("[voice: speaker=KK score=0.62 "
                      "person=person.kk]", sent_text)


def _runner(owner="person.kk", speaker=None, caller=None,
            full=("person.tony",)):
    """Bare ToolRunner with just the fields _is_secondary_turn /
    policy_identity / _speaker_is_owner_tier read."""
    if caller is None:
        caller = owner.split(".", 1)[-1] if owner else None
    r = ToolRunner.__new__(ToolRunner)
    r.session_owner_identity = owner
    r.current_speaker_ha_person = speaker
    r.session_caller_name = caller
    r.session_caller_ha_person = owner
    r.event_log = None
    r._banks = SimpleNamespace(
        person_policies={p: {"full": True} for p in full},
        control_policies={p: {"full": True} for p in full})
    r._instance_id = "test"
    return r


class BossRuleTest(unittest.TestCase):
    """person.tony identified on a member lane keeps owner-tier."""

    def test_owner_tier_speaker_not_secondary(self):
        r = _runner(owner="person.kk", speaker="person.tony")
        self.assertFalse(r._is_secondary_turn())

    def test_member_speaker_still_secondary(self):
        r = _runner(owner="person.tony", speaker="person.kk")
        self.assertTrue(r._is_secondary_turn())

    def test_policy_identity_elevates_to_owner_voice(self):
        r = _runner(owner="person.kk", speaker="person.tony")
        self.assertEqual(r.policy_identity(), "person.tony")

    def test_member_keeps_owner_identity(self):
        # owner's own lane, member voice: identity stays the owner
        r = _runner(owner="person.tony", speaker="person.kk")
        self.assertEqual(r.policy_identity(), "person.tony")
        r2 = _runner(owner="person.kk", speaker="person.kk")
        self.assertEqual(r2.policy_identity(), "person.kk")

    def test_stale_owner_label_does_not_elevate(self):
        # An aged-out label is treated as unrecognized: the turn keeps
        # the lane owner's rights (not secondary — fail-open) but is
        # NOT elevated to the speaker's owner-tier identity.
        from backend.tool_runner.common import (
            _CALLER_SPEAKER_SESSION, SPEAKER_STALE_S)
        r = _runner(owner="person.kk", speaker="person.tony")
        stale = SimpleNamespace(
            speaker_age_s=lambda: SPEAKER_STALE_S + 10)
        token = _CALLER_SPEAKER_SESSION.set(stale)
        try:
            self.assertFalse(r._is_secondary_turn())
            self.assertEqual(r.policy_identity(), "person.kk")
        finally:
            _CALLER_SPEAKER_SESSION.reset(token)

    def test_banks_failure_fails_closed(self):
        r = _runner(owner="person.kk", speaker="person.tony")
        r._banks = SimpleNamespace(person_policies={},
                                   control_policies={})
        self.assertTrue(r._is_secondary_turn())


class ToolFileActionsTest(unittest.TestCase):
    """speaker_profiles match_file/enroll_file — ops + testing surface."""

    def setUp(self):
        self.ident = _identifier()
        reg = tools_loader.load()
        self.assertIn("speaker_profiles", reg.tools)
        self.tool = reg.tools["speaker_profiles"]
        self._get = patch.object(
            speaker_id.SpeakerIdentifier, "get",
            return_value=self.ident)
        self._get.start()
        self.addCleanup(self._get.stop)

    def _runner(self, identity="person.tony", full=True):
        banks = SimpleNamespace(
            person_policies=(
                {"person.tony": {"full": True}} if full
                else {"person.kk": {}}),
            control_policies={})
        r = SimpleNamespace(
            banks=banks,
            speaker_session=None,
            context=SimpleNamespace(
                ha_client=SimpleNamespace(
                    resolve_person=AsyncMock(return_value=None))))
        r.policy_identity = lambda: identity
        r._is_secondary_turn = lambda: False
        return r

    def test_match_file_names_profile(self):
        self.ident.match_file = MagicMock(return_value={
            "match": "KK", "score": 0.62, "threshold": 0.45,
            "all_scores": [{"name": "KK", "score": 0.62}]})
        out = asyncio.run(self.tool.run(
            self._runner(), action="match_file",
            path="/tmp/clip.m4a"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["match"], "KK")
        self.assertIn("say", out)

    def test_match_file_needs_path(self):
        out = asyncio.run(self.tool.run(
            self._runner(), action="match_file"))
        self.assertFalse(out["ok"])
        self.assertIn("path", out["error"])

    def test_enroll_file_enrolls_clip(self):
        self.ident.enroll_from_file = MagicMock(return_value={
            "name": "KK", "samples": 3, "ha_person": "person.kk",
            "path": "/tmp/clip.m4a"})
        out = asyncio.run(self.tool.run(
            self._runner(), action="enroll_file", path="/tmp/clip.m4a",
            name="kk", person="person.kk"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["enrolled"], "KK")
        args, kw = self.ident.enroll_from_file.call_args
        self.assertEqual(args[:2], ("/tmp/clip.m4a", "kk"))
        self.assertEqual(kw["person"], "person.kk")

    def test_enroll_file_foreign_person_denied(self):
        out = asyncio.run(self.tool.run(
            self._runner(identity="person.kk", full=False),
            action="enroll_file", path="/tmp/clip.m4a",
            name="Tony", person="person.tony"))
        self.assertFalse(out["ok"])

    def test_enroll_file_conflict_carries_match(self):
        self.ident.enroll_from_file = MagicMock(
            side_effect=speaker_id.EnrollConflict(
                "this voice matches enrolled speaker 'Tony' (90%)",
                matched="Tony", score=0.9))
        out = asyncio.run(self.tool.run(
            self._runner(), action="enroll_file",
            path="/tmp/clip.m4a", name="kk"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["matched_profile"], "Tony")


if __name__ == "__main__":
    unittest.main()
