"""Voice-corpus capture in the Telegram relay (card
tg-voice-corpus-capture).

voice/audio/video_note messages from enrolled senders (allowed chats /
mapped room members) are downloaded via getFile and saved under
TG_VOICE_CORPUS_DIR/<sender_slug>/<YYYYmmdd-HHMMSS>.<ext> with one JSONL
line per clip in manifest.jsonl at the corpus root. The sender gets a
short ack; the clip never reaches Ada. Senders behind the relay's
existing gate (unknown chat, unmapped group member) must not land on
disk.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend import tg_relay


def _upd(chat_id=111, sender_id=555, chat_type="private", **msg):
    return {"update_id": 1, "message": {
        "message_id": 9, "date": 1760000000,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": sender_id, "first_name": "Tony",
                 "username": "tonyxyz"},
        **msg}}


def _relay(tmpdir):
    relay = tg_relay.TgRelay()

    async def fake_tg(method, **params):
        if method == "getFile":
            return {"file_path": "voice/file_1.oga"}
        return {}

    relay.tg = fake_tg
    resp = MagicMock()
    resp.content = b"OGGDATA"
    resp.raise_for_status = lambda: None
    http = MagicMock()
    http.get = AsyncMock(return_value=resp)
    relay.http = http
    relay.send_text = AsyncMock()
    return relay


class VoiceClipTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self._patches = [
            patch.object(tg_relay, "VOICE_CORPUS_DIR", self.dir),
            patch.dict(os.environ, {
                "TELEGRAM_ALLOWED_CHAT_IDS": "111,-222",
                "TELEGRAM_USER_CALLERS": "555:user-tony",
            }),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _manifest(self):
        f = self.dir / "manifest.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] \
            if f.exists() else []

    async def test_voice_saved_and_acked(self):
        relay = _relay(self.dir)
        await relay.handle(_upd(voice={
            "file_id": "F1", "duration": 7, "mime_type": "audio/ogg"}))
        clips = list((self.dir / "tonyxyz").glob("*.ogg"))
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].read_bytes(), b"OGGDATA")
        [entry] = self._manifest()
        self.assertEqual(entry["sender"], "tonyxyz")
        self.assertEqual(entry["sender_id"], 555)
        self.assertEqual(entry["chat_id"], 111)
        self.assertEqual(entry["duration_s"], 7)
        self.assertEqual(entry["file_id"], "F1")
        self.assertEqual(entry["mime"], "audio/ogg")
        self.assertEqual(entry["path"], str(clips[0]))
        relay.send_text.assert_awaited_once()
        self.assertIn("Saved 7s voice clip",
                      relay.send_text.await_args.args[1])

    async def test_audio_and_video_note_kinds(self):
        relay = _relay(self.dir)
        await relay.handle(_upd(audio={
            "file_id": "F2", "duration": 12, "mime_type": "audio/mpeg"}))
        await relay.handle(_upd(video_note={
            "file_id": "F3", "duration": 3}))
        kinds = sorted((e["kind"], Path(e["path"]).suffix)
                       for e in self._manifest())
        self.assertEqual(kinds, [("audio", ".oga"), ("video_note", ".mp4")])

    async def test_second_clip_same_second_no_clobber(self):
        relay = _relay(self.dir)
        await relay.handle(_upd(voice={
            "file_id": "F1", "duration": 1, "mime_type": "audio/ogg"}))
        await relay.handle(_upd(voice={
            "file_id": "F4", "duration": 2, "mime_type": "audio/ogg"}))
        self.assertEqual(
            len(list((self.dir / "tonyxyz").glob("*.ogg"))), 2)
        self.assertEqual(len(self._manifest()), 2)

    async def test_group_caller_slug(self):
        relay = _relay(self.dir)
        await relay.handle(_upd(
            chat_id=-222, chat_type="supergroup",
            voice={"file_id": "F5", "duration": 4}))
        [entry] = self._manifest()
        self.assertEqual(entry["sender"], "user-tony")
        self.assertTrue((self.dir / "user-tony").is_dir())

    async def test_unauthorized_chat_not_saved(self):
        relay = _relay(self.dir)
        await relay.handle(_upd(chat_id=999, voice={
            "file_id": "F6", "duration": 1}))
        self.assertEqual(self._manifest(), [])
        self.assertFalse(list(self.dir.glob("**/*.ogg")))

    async def test_unmapped_group_sender_not_saved(self):
        relay = _relay(self.dir)
        await relay.handle(_upd(
            chat_id=-222, sender_id=777, chat_type="supergroup",
            voice={"file_id": "F7", "duration": 1}))
        self.assertEqual(self._manifest(), [])
        self.assertFalse(list(self.dir.glob("**/*.ogg")))


if __name__ == "__main__":
    unittest.main()
