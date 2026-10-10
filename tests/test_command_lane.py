"""command_lane (card use-local-model-for-short-voice-commands): the
local-model short-command lane — candidate gate, deterministic
direction/verb extraction, the two systemone questions (route + entity
pick), the enforce path through tool_runner.execute, and the shadow
probe's corpus rows."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GEMINI_API_KEY", "x")

from backend import command_lane  # noqa: E402

_CORPUS = tempfile.NamedTemporaryFile(
    delete=False, suffix=".jsonl").name

_ENV = {
    "ADA_COMMAND_LANE": "enforce",
    "ADA_COMMAND_LLM_URL": "http://lane.test",
    "ADA_JEV_URL": "",
    "ADA_COMMAND_LANE_CORPUS": _CORPUS,
    "ADA_COMMAND_LANE_MIN_CONF": "0.65",
}


def _resp(answer: dict):
    """urlopen fake — returns a context-free object whose .read() yields
    the systemone envelope."""
    body = json.dumps({"answers": {"q": answer}}).encode()
    m = MagicMock()
    m.read.return_value = body
    return m


def _urlopen_seq(answers):
    """side_effect serving canned answers in call order."""
    it = iter(answers)

    def _open(req, timeout=None):
        return _resp(next(it))

    return _open


def _runner(entities=(), execute_return="Turned off light.bed_fan: {}"):
    ha = MagicMock()
    ha.entities = AsyncMock(return_value=list(entities))
    ctx = MagicMock()
    ctx.ha_client = ha
    runner = MagicMock()
    runner.context = ctx
    runner.execute = AsyncMock(return_value=execute_return)
    return runner


class CandidateTests(unittest.TestCase):

    def test_short_english_is_candidate(self):
        self.assertTrue(command_lane.is_candidate("turn off the fan"))
        self.assertTrue(command_lane.is_candidate("is the light on?"))

    def test_long_turn_is_not(self):
        self.assertFalse(command_lane.is_candidate(
            "tell me about the history of the house and then " * 3))

    def test_thai_char_cap(self):
        # Unsegmented Thai has no spaces — the char cap keeps it a
        # candidate while a longer Thai sentence drops out.
        self.assertTrue(command_lane.is_candidate("ปิดไฟห้องนอน"))
        self.assertFalse(command_lane.is_candidate("ก" * 60))

    def test_junk_is_not(self):
        self.assertFalse(command_lane.is_candidate(""))
        self.assertFalse(command_lane.is_candidate("   "))
        self.assertFalse(command_lane.is_candidate("!!!"))


class DirectionTests(unittest.TestCase):

    def test_power(self):
        self.assertEqual(command_lane._direction("turn off the fan"), "off")
        self.assertEqual(command_lane._direction("turn on the light"), "on")

    def test_state_check_beats_power_verb(self):
        self.assertEqual(
            command_lane._direction("is the fan on?"), "state")
        self.assertEqual(
            command_lane._direction("is the garage door closed"), "state")

    def test_media_and_cover_verbs(self):
        self.assertEqual(command_lane._direction("pause the tv"), "pause")
        self.assertEqual(command_lane._direction("mute the speaker"), "mute")
        self.assertEqual(command_lane._direction("open the blinds"), "open")
        self.assertEqual(command_lane._direction("stop the fan"), "stop")

    def test_ambiguous_returns_none(self):
        self.assertIsNone(command_lane._direction("the fan please"))
        self.assertIsNone(command_lane._direction("thanks ada"))

    def test_thai_off(self):
        self.assertEqual(command_lane._direction("ปิดไฟห้องนอน"), "off")

    def test_action_for_domains(self):
        self.assertEqual(
            command_lane._action_for("media_player", "off"), "turn_off")
        self.assertEqual(
            command_lane._action_for("media_player", "pause"), "media_pause")
        self.assertEqual(command_lane._action_for("cover", "off"), "close")
        self.assertEqual(command_lane._action_for("cover", "open"), "open")
        self.assertEqual(command_lane._action_for("button", "on"), "press")
        self.assertEqual(command_lane._action_for("switch", "off"), "off")
        self.assertEqual(command_lane._action_for("switch", "stop"), "off")
        self.assertIsNone(command_lane._action_for("light", "play"))


class TimeReplyTests(unittest.TestCase):

    def test_time_and_date(self):
        self.assertIsNotNone(command_lane._time_reply("what time is it"))
        self.assertIsNotNone(command_lane._time_reply("what day is today"))
        self.assertIsNotNone(command_lane._time_reply("กี่โมง"))
        self.assertIsNone(command_lane._time_reply("turn off the fan"))


class ClassifyTests(unittest.TestCase):

    def test_parses_choice_answer(self):
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=_urlopen_seq(
                    [{"choice": "control", "confidence": 0.9}])):
            out = command_lane.classify("turn off the fan")
        self.assertEqual(out, {"action": "control", "confidence": 0.9})

    def test_failed_call_returns_none(self):
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=OSError("down")):
            self.assertIsNone(command_lane.classify("turn off the fan"))

    def test_no_url_returns_none(self):
        env = {**_ENV, "ADA_COMMAND_LLM_URL": "", "ADA_JEV_URL": ""}
        with patch.dict(os.environ, env):
            self.assertIsNone(command_lane.classify("turn off the fan"))

    def test_jev_url_is_fallback(self):
        env = {**_ENV, "ADA_COMMAND_LLM_URL": "",
               "ADA_JEV_URL": "http://jev.test/"}
        with patch.dict(os.environ, env):
            self.assertEqual(command_lane.llm_url(), "http://jev.test")


class PickEntityTests(unittest.TestCase):

    _ENTS = [
        {"entity_id": "light.bed_fan", "domain": "light",
         "name": "Bedroom Fan", "state": "on"},
        {"entity_id": "switch.kettle", "domain": "switch",
         "name": "Kettle", "state": "off"},
    ]

    def test_pick_returns_entity_and_conf(self):
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=_urlopen_seq(
                    [{"choice": "light.bed_fan", "confidence": 0.8}])):
            out = command_lane.pick_entity(
                "turn off the bedroom fan", self._ENTS)
        self.assertEqual(out["entity_id"], "light.bed_fan")
        self.assertEqual(out["name"], "Bedroom Fan")
        self.assertAlmostEqual(out["confidence"], 0.8)

    def test_none_pick(self):
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=_urlopen_seq(
                    [{"choice": "none", "confidence": 0.9}])):
            self.assertIsNone(command_lane.pick_entity(
                "turn off the teleport pad", self._ENTS))

    def test_no_candidates_no_call(self):
        # No token overlap → no candidates → no model call at all.
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=AssertionError("should not be called")):
            self.assertIsNone(command_lane.pick_entity(
                "turn off the fan", []))

    def test_candidates_prefilter(self):
        ents = self._ENTS + [
            {"entity_id": "light.hall", "domain": "light",
             "name": "Hall", "state": "on"}]
        cands = command_lane._entity_candidates("bedroom fan", ents)
        self.assertEqual(cands[0]["entity_id"], "light.bed_fan")
        self.assertNotIn("switch.kettle",
                         [c["entity_id"] for c in cands])


class TryHandleTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        Path(_CORPUS).write_text("")

    def _last_row(self):
        rows = [json.loads(l) for l in
                Path(_CORPUS).read_text().splitlines() if l.strip()]
        return rows[-1] if rows else None

    async def test_time_reply_needs_no_model(self):
        runner = _runner()
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=AssertionError("no model call expected")):
            out = await command_lane.try_handle(
                "what time is it", runner)
        self.assertIsNotNone(out)
        self.assertIn("route", out)
        runner.execute.assert_not_called()

    async def test_control_actuates_picked_entity(self):
        runner = _runner(entities=[
            {"entity_id": "light.bed_fan", "domain": "light",
             "name": "Bedroom Fan", "state": "on"}])
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=_urlopen_seq([
                    {"choice": "control", "confidence": 0.9},
                    {"choice": "light.bed_fan", "confidence": 0.85}])):
            out = await command_lane.try_handle(
                "turn off the bedroom fan", runner,
                identity="person.tony", speaker="person.tony",
                owner="person.tony", session="s1")
        self.assertIsNotNone(out)
        self.assertEqual(out["tool"], "control_entity")
        self.assertEqual(out["args"], {
            "entity_id": "light.bed_fan", "on": False})
        self.assertIn("Bedroom Fan", out["reply"])
        pos, kw = runner.execute.call_args
        self.assertEqual(pos[0], "control_entity")
        self.assertEqual(kw["owner"], "person.tony")
        row = self._last_row()
        self.assertTrue(row["handled"])
        self.assertEqual(row["pick"], "light.bed_fan")

    async def test_media_player_maps_action(self):
        runner = _runner(entities=[
            {"entity_id": "media_player.tv", "domain": "media_player",
             "name": "TV", "state": "playing"}])
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=_urlopen_seq([
                    {"choice": "control", "confidence": 0.9},
                    {"choice": "media_player.tv", "confidence": 0.8}])):
            out = await command_lane.try_handle(
                "pause the tv", runner)
        self.assertEqual(out["args"]["action"], "media_pause")

    async def test_state_check_reads_not_actuates(self):
        runner = _runner(entities=[
            {"entity_id": "light.bed_fan", "domain": "light",
             "name": "Bedroom Fan", "state": "on"}],
            execute_return={"entity_id": "light.bed_fan", "state": "on"})
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=_urlopen_seq([
                    {"choice": "control", "confidence": 0.9},
                    {"choice": "light.bed_fan", "confidence": 0.8}])):
            out = await command_lane.try_handle(
                "is the bedroom fan on?", runner)
        self.assertEqual(out["tool"], "get_home_state")
        self.assertIn("on", out["reply"])

    async def test_pass_route_falls_through(self):
        runner = _runner()
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=_urlopen_seq(
                    [{"choice": "pass", "confidence": 0.9}])):
            self.assertIsNone(await command_lane.try_handle(
                "tell me a story", runner))
        runner.execute.assert_not_called()

    async def test_low_confidence_falls_through(self):
        runner = _runner()
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=_urlopen_seq(
                    [{"choice": "control", "confidence": 0.3}])):
            self.assertIsNone(await command_lane.try_handle(
                "turn off the fan", runner))
        runner.execute.assert_not_called()

    async def test_gate_denial_falls_through(self):
        runner = _runner(entities=[
            {"entity_id": "cover.gate", "domain": "cover",
             "name": "Gate", "state": "closed"}])
        runner.execute = AsyncMock(side_effect=PermissionError("dangerous"))
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=_urlopen_seq([
                    {"choice": "control", "confidence": 0.9},
                    {"choice": "cover.gate", "confidence": 0.9}])):
            self.assertIsNone(await command_lane.try_handle(
                "open the gate", runner))
        row = self._last_row()
        self.assertIn("error", row)

    async def test_shadow_mode_never_handles(self):
        env = {**_ENV, "ADA_COMMAND_LANE": "shadow"}
        runner = _runner()
        with patch.dict(os.environ, env):
            self.assertIsNone(await command_lane.try_handle(
                "turn off the fan", runner))
        runner.execute.assert_not_called()

    async def test_ambiguous_direction_never_actuates(self):
        runner = _runner(entities=[
            {"entity_id": "light.bed_fan", "domain": "light",
             "name": "Bedroom Fan", "state": "on"}])
        with patch.dict(os.environ, _ENV), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=_urlopen_seq(
                    [{"choice": "control", "confidence": 0.95}])):
            self.assertIsNone(await command_lane.try_handle(
                "the bedroom fan please", runner))
        runner.execute.assert_not_called()


class ProbeTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        Path(_CORPUS).write_text("")

    async def test_probe_writes_row_and_emits(self):
        env = {**_ENV, "ADA_COMMAND_LANE": "shadow"}
        events = []
        with patch.dict(os.environ, env), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=_urlopen_seq(
                    [{"choice": "control", "confidence": 0.9}])):
            command_lane.probe(
                "turn off the fan", surface="voice",
                session_id="s9", tools=["control_entity"],
                emit=lambda *a, **k: events.append(a))
            await asyncio.sleep(0.1)
        rows = [json.loads(l) for l in
                Path(_CORPUS).read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["route"], "control")
        self.assertEqual(row["surface"], "voice")
        self.assertTrue(row["servable"])
        self.assertEqual(row["tools"], ["control_entity"])
        self.assertTrue(events)
        self.assertEqual(events[0][0], "command_lane")

    async def test_probe_skips_long_turns(self):
        env = {**_ENV, "ADA_COMMAND_LANE": "shadow"}
        with patch.dict(os.environ, env), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=AssertionError("should not classify")):
            command_lane.probe(
                "tell me everything about the solar system today " * 3,
                surface="voice")
            await asyncio.sleep(0.05)

    async def test_probe_off_mode_is_quiet(self):
        env = {**_ENV, "ADA_COMMAND_LANE": "off"}
        with patch.dict(os.environ, env), patch(
                "backend.command_lane.urllib.request.urlopen",
                side_effect=AssertionError("should not classify")):
            command_lane.probe("turn off the fan", surface="voice")
            await asyncio.sleep(0.05)
        self.assertEqual(Path(_CORPUS).read_text().strip(), "")


class ConfigTests(unittest.TestCase):

    def test_defaults_shadow(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ADA_COMMAND") and k != "ADA_JEV_URL"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(command_lane.mode(), "shadow")
            self.assertFalse(command_lane.armed())
            self.assertFalse(command_lane.enforced())


if __name__ == "__main__":
    unittest.main()
