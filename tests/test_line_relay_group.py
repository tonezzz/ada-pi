"""Group-room routing in the LINE relay (card line-multihome-group).

An allowed group's messages are MENTION-routed: a member addresses a
persona by name (ada|kaewta|chaba per LINE_NAME_BACKENDS) and the turn
goes to that persona's own ws backend, answered "[persona->sender] ...".
Unaddressed room chatter is ignored unless LINE_DEFAULT_PERSONA is set.
Identity is the per-member LINE_USER_CALLERS key — no admin fallback in
a room. Voice clips follow the tg voice rule (local corpus + ack) and
mirror the ack/transcript to the speaker's DM lane.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend import line_relay


def _ev(text=None, user_id="Ukk1", group_id="G1", mtype="text",
        msg_id="m1", mark_token="T1", etype="message", src_type=None,
        **msg_kw):
    if group_id:
        key = "roomId" if src_type == "room" else "groupId"
        src = {"type": src_type or "group", key: group_id,
               "userId": user_id}
    else:
        src = {"type": "user", "userId": user_id}
    msg = {"id": msg_id, "type": mtype, **msg_kw}
    if text is not None:
        msg["text"] = text
    return {"type": etype, "replyToken": "RT", "source": src,
            "markAsReadToken": mark_token, "message": msg}


class _FakeSession:
    """Stands in for ChatSession — records routing, never dials out."""
    made = []

    def __init__(self, user_id, relay, ws_url=None, caller=None):
        self.user_id = user_id
        self.ws_url = ws_url
        self.caller = caller
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


class MentionParseTests(unittest.TestCase):

    def setUp(self):
        self._p = patch.dict(os.environ, {
            "LINE_NAME_BACKENDS":
                "ada:ws://127.0.0.1:8002,kaewta:ws://127.0.0.1:8007,"
                "chaba:",
            "LINE_DEFAULT_PERSONA": "",
        })
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_name_backends(self):
        self.assertEqual(line_relay._name_backends(), {
            "ada": "ws://127.0.0.1:8002/ws",
            "kaewta": "ws://127.0.0.1:8007/ws",
            "chaba": ""})

    def test_bare_host_gets_ws_path(self):
        with patch.dict(os.environ, {
                "LINE_NAME_BACKENDS":
                    "ada:ws://h:1,kaewta:wss://h:2/ws"}):
            self.assertEqual(line_relay._name_backends()["ada"],
                             "ws://h:1/ws")
            self.assertEqual(line_relay._name_backends()["kaewta"],
                             "wss://h:2/ws")

    def test_mention_variants(self):
        f = line_relay._mention_persona
        self.assertEqual(f("ada, lights off"), "ada")
        self.assertEqual(f("@Kaewta lunch?"), "kaewta")
        self.assertEqual(f("tell chaba hi"), "chaba")
        self.assertIsNone(f("adagio for strings"))
        self.assertIsNone(f("the parade was nice"))
        self.assertIsNone(f("see you later"))

    def test_first_mention_wins(self):
        self.assertEqual(
            line_relay._mention_persona("kaewta ask ada something"),
            "kaewta")

    def test_strip_lead(self):
        s = line_relay._strip_lead_mention
        self.assertEqual(s("ada, lights off", "ada"), "lights off")
        self.assertEqual(s("@ada: hi", "ada"), "hi")
        self.assertEqual(s("tell ada hi", "ada"), "tell ada hi")

    def test_name_th(self):
        """Thai-script message -> Thai persona display name in tags."""
        with patch.dict(os.environ,
                        {"LINE_NAME_TH": "kaewta:แก้วตา,chaba:ชบา"}):
            self.assertEqual(line_relay._name_th()["kaewta"],
                             "แก้วตา")
            d = line_relay._display_name
            self.assertEqual(d("kaewta", "สวัสดีค่ะ"), "แก้วตา")
            self.assertEqual(d("kaewta", "hi there"), "kaewta")
            self.assertEqual(d("ada", "สวัสดีค่ะ"), "ada")
        # unset -> always ascii
        self.assertEqual(line_relay._display_name("kaewta", "สวัสดี"),
                         "kaewta")

    def test_route(self):
        r = line_relay._route
        self.assertEqual(r("ada, hi", True), ("ada", "hi"))
        self.assertEqual(r("random chatter", True), (None, "random chatter"))
        self.assertEqual(r("random chatter", False), ("ada", "random chatter"))
        with patch.dict(os.environ, {"LINE_DEFAULT_PERSONA": "kaewta"}):
            self.assertEqual(r("random chatter", True),
                             ("kaewta", "random chatter"))

    def test_route_unknown_default_ignored(self):
        """LINE_DEFAULT_PERSONA naming no configured persona must not
        crash on _name_backends()[name] — it resolves to ignore."""
        with patch.dict(os.environ, {"LINE_DEFAULT_PERSONA": "ghost"}):
            self.assertEqual(line_relay._route("random chatter", True),
                             (None, "random chatter"))


class GroupEventTests(unittest.IsolatedAsyncioTestCase):

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
                "LINE_ALLOWED_GROUP_IDS": "G1",
                "LINE_USER_CALLERS": "Utony1:user-tony,Ukk1:user-kk",
                "LINE_NAME_BACKENDS":
                    "ada:ws://127.0.0.1:8002,kaewta:ws://127.0.0.1:8007,"
                    "chaba:",
                "LINE_DEFAULT_PERSONA": "",
                "LINE_VOICE_DM_MIRROR": "1",
            }),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _sent(self, relay):
        return [c.args for c in relay.send_text.await_args_list]

    async def test_group_mention_routes_and_tags(self):
        relay = _relay()
        await relay.handle_event(_ev("kaewta, what's for dinner?"))
        [sess] = _FakeSession.made
        self.assertEqual(sess.ws_url, "ws://127.0.0.1:8007/ws")
        self.assertEqual(sess.caller, "user-kk")
        sess.send_turn.assert_awaited_once()
        sent_text = sess.send_turn.await_args.args[0]
        self.assertIn("[LINE group from kk]", sent_text)
        self.assertIn("what's for dinner?", sent_text)
        relay._deliver.assert_awaited_once()
        to, msgs = relay._deliver.await_args.args[:2]
        self.assertEqual(to, "G1")
        self.assertTrue(msgs[0]["text"].startswith("[kaewta->kk] "))
        self.assertIn("pong", msgs[0]["text"])

    async def test_thai_message_uses_thai_name(self):
        with patch.dict(os.environ,
                        {"LINE_NAME_TH": "kaewta:แก้วตา,chaba:ชบา"}):
            relay = _relay()
            await relay.handle_event(
                _ev("kaewta สวัสดีค่ะ อาหารเย็นมีอะไรบ้าง"))
            [sess] = _FakeSession.made
            self.assertEqual(sess.ws_url, "ws://127.0.0.1:8007/ws")
            msgs = relay._deliver.await_args.args[1]
            self.assertTrue(msgs[0]["text"].startswith("[แก้วตา->"))

    async def test_unaddressed_group_message_ignored(self):
        relay = _relay()
        await relay.handle_event(_ev("did you see the game?"))
        self.assertEqual(_FakeSession.made, [])
        relay._deliver.assert_not_awaited()
        relay.send_text.assert_not_awaited()

    async def test_unallowed_group_ignored(self):
        relay = _relay()
        await relay.handle_event(_ev("ada hi", group_id="Gbad"))
        self.assertEqual(_FakeSession.made, [])
        relay.send_text.assert_not_awaited()

    async def test_unmapped_member_refused_once(self):
        relay = _relay()
        await relay.handle_event(_ev("ada, hi", user_id="Unhong9"))
        await relay.handle_event(_ev("ada, hi again", user_id="Unhong9"))
        refusals = [c for c in self._sent(relay)
                    if "Not authorized in this room" in c[1]]
        self.assertEqual(len(refusals), 1)
        self.assertIn("Unhong9", refusals[0][1])
        self.assertEqual(_FakeSession.made, [])

    async def test_offline_persona_not_home(self):
        relay = _relay()
        await relay.handle_event(_ev("chaba are you there?"))
        self.assertEqual(_FakeSession.made, [])
        [call] = self._sent(relay)
        self.assertEqual(call[0], "G1")
        self.assertIn("[chaba->kk]", call[1])
        self.assertIn("isn't home yet", call[1])

    async def test_dm_defaults_to_ada_untagged(self):
        relay = _relay()
        await relay.handle_event(_ev("hello ada", group_id=None,
                                     user_id="Utony1"))
        [sess] = _FakeSession.made
        self.assertEqual(sess.ws_url, "ws://127.0.0.1:8002/ws")
        _, msgs = relay._deliver.await_args.args[:2]
        self.assertEqual(msgs[0]["text"], "pong")

    async def test_dm_unauthorized_user(self):
        relay = _relay()
        await relay.handle_event(_ev("hi", group_id=None,
                                     user_id="Ustranger"))
        self.assertEqual(_FakeSession.made, [])
        [call] = self._sent(relay)
        self.assertIn("Not authorized", call[1])
        self.assertIn("Ustranger", call[1])

    async def test_join_announces_group_id(self):
        relay = _relay()
        await relay.handle_event(_ev(etype="join", mtype=""))
        [call] = self._sent(relay)
        self.assertEqual(call[0], "G1")
        self.assertIn("groupId=G1", call[1])
        # join events carry a replyToken — the free reply path, not push
        self.assertEqual(
            relay.send_text.await_args_list[0].kwargs.get("reply_token"),
            "RT")

    async def test_unknown_default_persona_no_crash(self):
        """A LINE_DEFAULT_PERSONA typo must not KeyError the turn."""
        relay = _relay()
        with patch.dict(os.environ, {"LINE_DEFAULT_PERSONA": "ghost"}):
            await relay.handle_event(_ev("random chatter"))
        self.assertEqual(_FakeSession.made, [])
        relay._deliver.assert_not_awaited()
        relay.send_text.assert_not_awaited()

    async def test_room_source_uses_room_profile_endpoint(self):
        """'room'-type events fetch the member profile under /room/,
        not /group/ (which 404s rooms)."""
        relay = _relay()
        http_get = AsyncMock(return_value=MagicMock(status_code=200))
        http_get.return_value.json.return_value = {"displayName": "Nhong"}
        relay.http.get = http_get
        env = {"LINE_ALLOWED_GROUP_IDS": "R1",
               "LINE_USER_CALLERS": "Unhong1:user-nhong"}
        with patch.dict(os.environ, env):
            await relay.handle_event(_ev(
                "ada hi", group_id="R1", src_type="room",
                user_id="Unhong1"))
        url = http_get.await_args.args[0]
        self.assertIn("/v2/bot/room/R1/member/Unhong1", url)
        _, msgs = relay._deliver.await_args.args[:2]
        self.assertTrue(msgs[0]["text"].startswith("[ada->Nhong] "))

    async def test_mark_read_uses_event_token(self):
        relay = _relay()
        await relay.handle_event(_ev("ada hi"))
        relay.line.assert_any_await(
            "markAsRead", {"markAsReadToken": "T1"})

    async def test_group_audio_saved_acked_mirrored(self):
        relay = _relay()
        relay._fetch_content = AsyncMock(
            return_value=(b"M4ADATA", "audio/mp4"))
        await relay.handle_event(_ev(
            mtype="audio", msg_id="a1", duration=5000))
        clips = list((Path(self._tmp.name) / "user-kk").glob("*.m4a"))
        self.assertEqual(len(clips), 1)
        [entry] = [json.loads(l) for l in
                   (Path(self._tmp.name) / "manifest.jsonl")
                   .read_text().splitlines()]
        self.assertEqual(entry["sender"], "user-kk")
        self.assertEqual(entry["duration_s"], 5)
        sent = self._sent(relay)
        self.assertIn(("G1", "Saved 5s voice clip to the corpus."), sent)
        mirrors = [c for c in sent if c[0] == "Ukk1"]
        self.assertEqual(len(mirrors), 1)

    async def test_group_audio_unmapped_sender_not_saved(self):
        relay = _relay()
        relay._fetch_content = AsyncMock(
            return_value=(b"M4ADATA", "audio/mp4"))
        await relay.handle_event(_ev(
            mtype="audio", msg_id="a2", user_id="Unhong9", duration=1000))
        self.assertFalse(list(Path(self._tmp.name).glob("**/*.m4a")))

    async def test_group_audio_transcript_routes(self):
        relay = _relay()
        relay._fetch_content = AsyncMock(
            return_value=(b"M4ADATA", "audio/mp4"))
        with patch.object(line_relay, "_transcribe",
                          lambda p: "ada turn on the fan"):
            await relay.handle_event(_ev(
                mtype="audio", msg_id="a3", duration=3000))
        [sess] = _FakeSession.made
        self.assertEqual(sess.ws_url, "ws://127.0.0.1:8002/ws")
        self.assertIn("turn on the fan",
                      sess.send_turn.await_args.args[0])
        to, msgs = relay._deliver.await_args.args[:2]
        self.assertEqual(to, "G1")
        self.assertTrue(msgs[0]["text"].startswith("[ada->kk] "))


if __name__ == "__main__":
    unittest.main()
