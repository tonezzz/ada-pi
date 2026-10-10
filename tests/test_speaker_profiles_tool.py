"""speaker_profiles tools.d drop-in + speaker_id rename/alias/match paths.

Card speaker-profile-tool (2026-10-07): list|match|remove|re-enroll|alias
over the voiceprint store; enroll refusals name the matching profile;
ada_forget redirects speaker-shaped keys.
"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

os.environ.setdefault("ADA_SPEAKER_PROFILES", "/nonexistent-speaker-profiles.json")
os.environ.setdefault("ADA_SPEAKER_KEEP_AUDIO", "0")

try:
    import google.genai  # noqa: F401
except ImportError:
    # Dep-less CI host — tool_runner's import chain needs the module to
    # exist, not to work (no calls reach it in these tests).
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

from backend import memory_ops, speaker_id, tools_loader  # noqa: E402
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


class _FakeSession:
    """Stand-in SpeakerSession — match_buffer/enroll_from_buffer only."""

    def __init__(self, match=None, enroll_result=None, enroll_exc=None):
        self._match = match
        self._enroll_result = enroll_result
        self._enroll_exc = enroll_exc
        self.enroll_calls = []

    def match_buffer(self, seconds=15.0):
        return dict(self._match) if self._match is not None else {
            "buffered_s": 0.0, "threshold": 0.45, "match": None,
            "score": 0.0, "reason": "not enough speech captured in the buffer"}

    def enroll_from_buffer(self, name, ha_person=None, display_name=None,
                           seconds=15.0, force=False):
        self.enroll_calls.append({
            "name": name, "ha_person": ha_person,
            "display_name": display_name, "seconds": seconds,
            "force": force})
        if self._enroll_exc is not None:
            raise self._enroll_exc
        return self._enroll_result or {"name": name, "samples": 1,
                                      "ha_person": ha_person,
                                      "display_name": display_name}


def _runner(identity="person.tony", full=True, secondary=False,
            sess=None) -> SimpleNamespace:
    ha = SimpleNamespace()

    async def _resolve_person(name):
        table = {"kk": {"entity_id": "person.kk", "name": "KK"},
                 "person.kk": {"entity_id": "person.kk", "name": "KK"}}
        return table.get(str(name or "").strip().lower())

    ha.resolve_person = _resolve_person
    banks = SimpleNamespace(
        person_policies=(
            {"person.tony": {"full": True}} if full else {"person.kk": {}}),
        control_policies={})
    r = SimpleNamespace(
        speaker_session=sess,
        banks=banks,
        context=SimpleNamespace(ha_client=ha))
    r.policy_identity = lambda: identity
    r._is_secondary_turn = lambda: secondary
    return r


def _load_tool():
    reg = tools_loader.load()
    assert "speaker_profiles" in reg.tools, reg.errors
    return reg.tools["speaker_profiles"]


def _run(tool, runner, **args):
    return asyncio.run(tool.run(runner, **args))


class LoadTest(unittest.TestCase):
    def test_manifest_entry_loads(self):
        reg = tools_loader.load()
        self.assertIn("speaker_profiles", reg.tools)
        spec = reg.tools["speaker_profiles"]
        self.assertEqual(spec.policy, "read")
        self.assertTrue(spec.secondary_allowed)
        self.assertFalse(
            [e for e in reg.errors if "speaker_profiles" in e])


class ListMatchTest(unittest.TestCase):
    def setUp(self):
        self.ident = _identifier()
        self.tool = _load_tool()
        self._get = patch.object(
            speaker_id.SpeakerIdentifier, "get", return_value=self.ident)
        self._get.start()
        self.addCleanup(self._get.stop)

    def test_list(self):
        self.ident._enrolled = {"พรศิริ": _vec(1)}
        self.ident._metadata = {"พรศิริ": {
            "ha_person": "person.kk", "display_name": "KK",
            "samples": 2, "aliases": ["kk"]}}
        out = _run(self.tool, _runner(), action="list")
        self.assertTrue(out["ok"])
        self.assertEqual(out["profiles"][0]["name"], "พรศิริ")
        self.assertEqual(out["profiles"][0]["ha_person"], "person.kk")
        self.assertEqual(out["profiles"][0]["aliases"], ["kk"])

    def test_match_names_best_profile(self):
        sess = _FakeSession(match={
            "buffered_s": 12.0, "threshold": 0.45,
            "match": "พรศิริ", "best": "พรศิริ", "score": 0.87,
            "ha_person": "person.kk",
            "candidates": [{"name": "พรศิริ", "score": 0.87}]})
        out = _run(self.tool, _runner(sess=sess), action="match")
        self.assertTrue(out["ok"])
        self.assertEqual(out["match"], "พรศิริ")
        self.assertEqual(out["score"], 0.87)
        self.assertIn("say", out)

    def test_match_below_threshold_still_names_candidate(self):
        sess = _FakeSession(match={
            "buffered_s": 12.0, "threshold": 0.45,
            "match": None, "best": "พรศิริ", "score": 0.30})
        out = _run(self.tool, _runner(sess=sess), action="match")
        self.assertTrue(out["ok"])
        self.assertIsNone(out["match"])
        self.assertEqual(out["best"], "พรศิริ")

    def test_match_without_session_errors_honestly(self):
        out = _run(self.tool, _runner(sess=None), action="match")
        self.assertFalse(out["ok"])
        self.assertIn("not active", out["error"])

    def test_unknown_action(self):
        out = _run(self.tool, _runner(), action="explode")
        self.assertFalse(out["ok"])


class MutationGateTest(unittest.TestCase):
    """remove/re-enroll/alias: owner-tier or own-bound-person only."""

    def setUp(self):
        self.ident = _identifier()
        self.ident._enrolled = {"พรศิริ": _vec(1), "Tony": _vec(2)}
        self.ident._prints = {"พรศิริ": [_vec(1)], "Tony": [_vec(2)]}
        self.ident._metadata = {
            "พรศิริ": {"ha_person": "person.kk", "samples": 1},
            "Tony": {"ha_person": "person.tony", "samples": 1},
        }
        self.tool = _load_tool()
        self._get = patch.object(
            speaker_id.SpeakerIdentifier, "get", return_value=self.ident)
        self._get.start()
        self.addCleanup(self._get.stop)
        self._save = patch.object(self.ident, "_save_enrolled")
        self._save.start()
        self.addCleanup(self._save.stop)

    def test_owner_removes(self):
        out = _run(self.tool, _runner(), action="remove", name="พรศิริ")
        self.assertTrue(out["ok"])
        self.assertNotIn("พรศิริ", self.ident._enrolled)

    def test_remove_unknown_profile_lists_enrolled(self):
        out = _run(self.tool, _runner(), action="remove", name="Nobody")
        self.assertFalse(out["ok"])
        self.assertIn("Tony", out["error"])

    def test_non_owner_denied_foreign_profile(self):
        # person.kk trying to remove Tony's print — denied.
        out = _run(self.tool, _runner(identity="person.kk", full=False),
                   action="remove", name="Tony")
        self.assertFalse(out["ok"])
        self.assertIn("Tony", self.ident._enrolled)

    def test_self_service_remove_own_profile(self):
        # person.kk removing the profile bound to person.kk — allowed.
        out = _run(self.tool, _runner(identity="person.kk", full=False),
                   action="remove", name="พรศิริ")
        self.assertTrue(out["ok"])

    def test_secondary_turn_never_mutates(self):
        # Even with an owner-bound session key, a secondary-identified
        # voice cannot mutate voiceprints.
        out = _run(self.tool, _runner(secondary=True), action="remove",
                   name="Tony")
        self.assertFalse(out["ok"])
        self.assertIn("Tony", self.ident._enrolled)

    def test_reenroll_force_replaces(self):
        sess = _FakeSession(enroll_result={
            "name": "พรศิริ", "samples": 1, "ha_person": "person.kk"})
        out = _run(self.tool, _runner(sess=sess), action="re-enroll",
                   name="พรศิริ")
        self.assertTrue(out["ok"])
        self.assertTrue(sess.enroll_calls[0]["force"])

    def test_reenroll_refusal_carries_matched_profile(self):
        sess = _FakeSession(enroll_exc=speaker_id.EnrollConflict(
            "this voice matches enrolled speaker 'Tony' (90%)",
            matched="Tony", score=0.9))
        out = _run(self.tool, _runner(sess=sess), action="re-enroll",
                   name="พรศิริ")
        self.assertFalse(out["ok"])
        self.assertEqual(out["matched_profile"], "Tony")

    def test_alias_binds_person_both_ways(self):
        out = _run(self.tool, _runner(), action="alias", name="พรศิริ",
                   person="kk")
        self.assertTrue(out["ok"], out)
        self.assertEqual(
            self.ident._metadata["พรศิริ"]["ha_person"], "person.kk")
        # person.kk <-> พรศิริ resolve to one identity, and the person's
        # friendly name resolves too.
        self.assertEqual(self.ident.resolve_name("person.kk"), "พรศิริ")
        self.assertEqual(self.ident.resolve_name("KK"), "พรศิริ")
        self.assertEqual(self.ident.resolve_name("พรศิริ"), "พรศิริ")

    def test_alias_rename_to(self):
        out = _run(self.tool, _runner(), action="alias", name="พรศิริ",
                   rename_to="KK")
        self.assertTrue(out["ok"], out)
        self.assertIn("KK", self.ident._enrolled)
        self.assertNotIn("พรศิริ", self.ident._enrolled)
        # The old key keeps resolving via aliases.
        self.assertEqual(self.ident.resolve_name("พรศิริ"), "KK")
        # ha_person binding survives the rename.
        self.assertEqual(
            self.ident._metadata["KK"]["ha_person"], "person.kk")

    def test_alias_needs_a_change(self):
        out = _run(self.tool, _runner(), action="alias", name="พรศิริ")
        self.assertFalse(out["ok"])

    def test_reenroll_resolves_alias_to_canonical(self):
        self.ident._metadata["พรศิริ"]["aliases"] = ["kk"]
        sess = _FakeSession()
        out = _run(self.tool, _runner(sess=sess), action="re-enroll",
                   name="kk")
        self.assertTrue(out["ok"], out)
        # force-replace lands on the canonical profile, not a new 'kk' key.
        self.assertEqual(sess.enroll_calls[0]["name"], "พรศิริ")

    def test_reenroll_unknown_profile_errors(self):
        sess = _FakeSession()
        out = _run(self.tool, _runner(sess=sess), action="re-enroll",
                   name="Nobody")
        self.assertFalse(out["ok"])
        self.assertFalse(sess.enroll_calls)


class CallerSessionTest(unittest.TestCase):
    """_CALLER_SPEAKER_SESSION resolves the CALLING session's buffer —
    an explicit None (text channel, speaker ID off, a provider that
    reconnected before the session re-link) must never fall back to the
    shared runner field and read another session's live audio
    (card ada-speech-capture-degraded — same class as the 2026-09-29
    stomp bug the contextvar was added for)."""

    def setUp(self):
        self.ident = _identifier()
        self.tool = _load_tool()
        self._get = patch.object(
            speaker_id.SpeakerIdentifier, "get", return_value=self.ident)
        self._get.start()
        self.addCleanup(self._get.stop)

    def test_match_uses_callers_session_over_shared_field(self):
        from backend.tool_runner.common import _CALLER_SPEAKER_SESSION
        caller_sess = _FakeSession(match={
            "buffered_s": 12.0, "threshold": 0.45, "match": "พรศิริ",
            "best": "พรศิริ", "score": 0.87})
        other_sess = _FakeSession()  # shared-field decoy — never consulted
        token = _CALLER_SPEAKER_SESSION.set(caller_sess)
        try:
            out = _run(self.tool, _runner(sess=other_sess), action="match")
        finally:
            _CALLER_SPEAKER_SESSION.reset(token)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["match"], "พรศิริ")

    def test_explicit_none_never_reads_shared_buffer(self):
        from backend.tool_runner.common import _CALLER_SPEAKER_SESSION
        live = _FakeSession(match={
            "buffered_s": 12.0, "threshold": 0.45, "match": "พรศิริ",
            "best": "พรศิริ", "score": 0.87})
        token = _CALLER_SPEAKER_SESSION.set(None)
        try:
            out = _run(self.tool, _runner(sess=live), action="match")
        finally:
            _CALLER_SPEAKER_SESSION.reset(token)
        self.assertFalse(out["ok"])
        self.assertIn("not active", out["error"])


class SpeakerIdStoreTest(unittest.TestCase):
    """The store pieces the tool rides on: resolve/rename/aliases persist."""

    def setUp(self):
        self.ident = _identifier()

    def test_resolve_slug_and_alias(self):
        self.ident._enrolled = {"Guest Tester": _vec(3)}
        self.ident._metadata = {"Guest Tester": {
            "display_name": "GT", "aliases": ["ทดสอบ"]}}
        self.assertEqual(
            self.ident.resolve_name("guest-tester"), "Guest Tester")
        self.assertEqual(self.ident.resolve_name("gt"), "Guest Tester")
        self.assertEqual(self.ident.resolve_name("ทดสอบ"), "Guest Tester")
        self.assertIsNone(self.ident.resolve_name("nobody"))

    def test_rename_records_old_key_as_alias(self):
        self.ident._enrolled = {"พรศิริ": _vec(4)}
        self.ident._prints = {"พรศิริ": [_vec(4)]}
        self.ident._metadata = {"พรศิริ": {"ha_person": "person.kk"}}
        with patch.object(self.ident, "_save_enrolled"):
            self.assertEqual(self.ident.rename("พรศิริ", "KK"), "KK")
        self.assertEqual(self.ident.resolve_name("พรศิริ"), "KK")
        self.assertIsNone(self.ident.rename("KK", "Guest"))  # reserved

    def test_aliases_roundtrip_json(self):
        with tempfile.TemporaryDirectory() as td:
            self.ident._profiles_path = Path(td) / "p.json"
            self.ident._enrolled = {"KK": _vec(5)}
            self.ident._prints = {"KK": [_vec(5)]}
            self.ident._metadata = {"KK": {"aliases": ["พรศิริ"]}}
            self.ident._save_enrolled()
            fresh = speaker_id.SpeakerIdentifier()
            fresh._profiles_path = self.ident._profiles_path
            fresh._enrolled, fresh._prints, fresh._metadata = {}, {}, {}
            fresh._load_enrolled()
            self.assertEqual(fresh.resolve_name("พรศิริ"), "KK")

    def test_enroll_under_alias_extends_canonical(self):
        # 'kk' is an alias of 'KK' — enrolling under the alias must merge
        # into KK's prints, not trip the contamination guard against KK.
        voice = _vec(8)
        self.ident._enrolled = {"KK": voice}
        self.ident._prints = {"KK": [voice]}
        self.ident._metadata = {"KK": {"aliases": ["kk"]}}
        with patch.object(self.ident, "_save_enrolled"), \
                patch.object(self.ident, "_compute_embedding",
                             return_value=voice):
            out = self.ident.enroll("kk", b"\x00" * 1024)
        self.assertEqual(out["name"], "KK")
        self.assertEqual(out["samples"], 2)
        self.assertNotIn("kk", self.ident._enrolled)

    def test_match_buffer_scores_candidates(self):
        tony = _vec(6)
        self.ident._enrolled = {"Tony": tony, "KK": _vec(7)}
        self.ident._prints = {"Tony": [tony], "KK": [_vec(7)]}

        async def _noop(name, conf):
            return None

        sess = speaker_id.SpeakerSession(self.ident, _noop)
        sess._recent.extend(b"\x11" * speaker_id.MIN_CHUNK_BYTES)
        with patch.object(self.ident, "_compute_embedding",
                          return_value=tony):
            out = sess.match_buffer()
        self.assertEqual(out["match"], "Tony")
        self.assertGreaterEqual(out["score"], 0.45)
        self.assertEqual(out["candidates"][0]["name"], "Tony")


class EnrollRefusalTest(unittest.IsolatedAsyncioTestCase):
    """ada_enroll_speaker surfaces the EnrollConflict fields."""

    async def test_conflict_names_matching_profile(self):
        runner = ToolRunner.__new__(ToolRunner)
        runner.mddb = None
        runner.memory = None
        runner.chaba = None
        runner.events = None
        runner._denials = {}
        runner._confirm_tokens = {}
        runner.session_id = None
        runner.current_speaker_ha_person = "person.tony"
        runner.session_caller_name = "tony"
        runner.session_caller_ha_person = "person.tony"
        runner.session_owner_identity = "person.tony"
        runner.event_log = None
        runner.doc_log = None
        runner.speaker_session = _FakeSession(
            enroll_exc=speaker_id.EnrollConflict(
                "this voice matches enrolled speaker 'พรศิริ' (87%)",
                matched="พรศิริ", score=0.87))
        runner._banks = None
        runner._instance_id = "test"
        runner.context = SimpleNamespace(
            ha_client=SimpleNamespace(
                resolve_person=AsyncMock(return_value=None),
                get_state=AsyncMock(return_value={})))
        out = await runner.ada_enroll_speaker(name="NewName")
        self.assertIn("พรศิริ", out["error"])
        self.assertEqual(out["matched_profile"], "พรศิริ")
        self.assertEqual(out["match_score"], 0.87)
        self.assertIn("speaker_profiles", out["suggest"])

    async def test_success_returns_enrolled_payload(self):
        # Regression: the success path used to fall off the end and
        # return None — the model never saw the enrolled status.
        runner = ToolRunner.__new__(ToolRunner)
        runner.mddb = None
        runner.memory = None
        runner.chaba = None
        runner.events = None
        runner._denials = {}
        runner._confirm_tokens = {}
        runner.session_id = None
        runner.current_speaker_ha_person = "person.tony"
        runner.session_caller_name = "tony"
        runner.session_caller_ha_person = "person.tony"
        runner.session_owner_identity = "person.tony"
        runner.event_log = None
        runner.doc_log = None
        runner.speaker_session = _FakeSession(enroll_result={
            "name": "Tony", "samples": 3, "ha_person": "person.tony",
            "display_name": "Tony", "duration_s": 14.9})
        runner._banks = None
        runner._instance_id = "test"
        runner.context = SimpleNamespace(
            ha_client=SimpleNamespace(
                resolve_person=AsyncMock(return_value={
                    "entity_id": "person.tony", "name": "Tony"}),
                get_state=AsyncMock(return_value={})))
        out = await runner.ada_enroll_speaker(name="Tony")
        self.assertEqual(out["status"], "enrolled")
        self.assertEqual(out["name"], "Tony")

    async def test_no_voice_session_never_captures_shared_buffer(self):
        # A caller whose provider explicitly has no SpeakerSession (text
        # channel, speaker ID off) must get the honest "not active" error
        # — not a capture from whatever session last wrote the shared
        # runner field.
        from backend.tool_runner.common import _CALLER_SPEAKER_SESSION
        runner = ToolRunner.__new__(ToolRunner)
        runner.mddb = None
        runner.memory = None
        runner.chaba = None
        runner.events = None
        runner._denials = {}
        runner._confirm_tokens = {}
        runner.session_id = None
        runner.current_speaker_ha_person = None
        runner.session_caller_name = "testo"
        runner.session_caller_ha_person = None
        runner.session_owner_identity = "testo"
        runner.event_log = None
        runner.doc_log = None
        live = _FakeSession(enroll_result={
            "name": "Tony", "samples": 3, "ha_person": "person.tony",
            "display_name": "Tony", "duration_s": 14.9})
        runner.speaker_session = live
        runner._banks = None
        runner._instance_id = "test"
        runner.context = SimpleNamespace(
            ha_client=SimpleNamespace(
                resolve_person=AsyncMock(return_value=None),
                get_state=AsyncMock(return_value={})))
        token = _CALLER_SPEAKER_SESSION.set(None)
        try:
            out = await runner.ada_enroll_speaker(name="Tony")
        finally:
            _CALLER_SPEAKER_SESSION.reset(token)
        self.assertIn("not active", out["error"])
        self.assertFalse(live.enroll_calls)


class ForgetRedirectTest(unittest.IsolatedAsyncioTestCase):
    """ada_forget must refuse/redirect speaker-profile-looking keys."""

    def _registry(self, doc=None):
        mddb = MagicMock()
        mddb.get_document = AsyncMock(return_value=doc)
        bank = SimpleNamespace(
            name="personal", mddb_collection="c", writable=True,
            person_scope=None, key_scope=[], scope="shared")
        reg = MagicMock()
        reg.bank.return_value = bank
        reg.personal_bank_name.return_value = "personal"
        reg.bank_allowed.return_value = True
        return mddb, reg

    async def test_speaker_path_key_refused(self):
        mddb, reg = self._registry()
        with self.assertRaises(ValueError) as ctx:
            await memory_ops.forget(mddb, reg, "personal", "speaker/kk")
        self.assertIn("speaker_profiles", str(ctx.exception))
        mddb.get_document.assert_not_called()

    async def test_key_matching_enrolled_profile_redirects(self):
        mddb, reg = self._registry(doc=None)
        ident = _identifier()
        ident._enrolled = {"พรศิริ": _vec(8)}
        ident._metadata = {"พรศิริ": {"ha_person": "person.kk"}}
        with patch.object(speaker_id.SpeakerIdentifier, "get",
                          return_value=ident):
            with self.assertRaises(ValueError) as ctx:
                await memory_ops.forget(mddb, reg, "personal", "พรศิริ")
        self.assertIn("speaker_profiles", str(ctx.exception))
        self.assertIn("พรศิริ", str(ctx.exception))

    async def test_real_doc_still_retracts(self):
        doc = {"meta": {"kind": ["note"]}, "contentMd": "x"}
        mddb, reg = self._registry(doc=doc)
        mddb.update_document = AsyncMock(return_value={"ok": True})
        ident = _identifier()  # empty store — no lookalike
        with patch.object(speaker_id.SpeakerIdentifier, "get",
                          return_value=ident):
            out = await memory_ops.forget(mddb, reg, "personal", "kk")
        self.assertEqual(out["verb"], "retract")


if __name__ == "__main__":
    unittest.main()
