"""Unit tests for POST /api/secret-drop (card ada-secret-drop).

The route is invoked as a coroutine with a mocked Request — the app is
imported for real, the secrets dir is redirected to a tmp dir, and ssh
delivery is faked. Compliance property under test: the secret value must
never land in logs, the ops/events file, transcripts, or the response.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ADA_INSTANCE_ID", "test")
os.environ.setdefault("GEMINI_API_KEY", "x")

from fastapi import HTTPException

import pwa_server


KEYS = {
    "viewer": {"key": "view-only-key", "device": "*", "apps": ["view"]},
    "member": {"key": "member-key", "device": "*", "apps": ["chat"]},
    "legacy": {"key": "legacy-key", "device": "*"},   # no apps -> voice+chat
}

SENTINEL = "sk-livetest-DoNotLeakMe-9f8e7d6c5b"


def _request(key=None, body=None):
    req = MagicMock()
    req.headers = {"x-api-key": key} if key else {}
    req.query_params = {}
    req.cookies = {}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


class SecretDropTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._keys_tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False)
        json.dump(KEYS, self._keys_tmp)
        self._keys_tmp.close()
        self._secrets = tempfile.TemporaryDirectory()
        self._events = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False)
        self._events.close()
        self._env = patch.dict(os.environ, {
            "ADA_KEYS_FILE": self._keys_tmp.name,
            "ADA_API_KEY": "",
            "ADA_API_KEYS": "",
            "ADA_SECRETS_DIR": self._secrets.name,
            "ADA_EVENTS_FILE": self._events.name,
        })
        self._env.start()
        self._local = patch.object(
            pwa_server, "_local_hostnames", return_value={"idc03"})
        self._local.start()
        pwa_server._secret_drop_hits.clear()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._local.stop)
        self.addCleanup(self._secrets.cleanup)
        self.addCleanup(os.unlink, self._keys_tmp.name)
        self.addCleanup(os.unlink, self._events.name)

    async def _drop(self, key="member-key", host="idc03",
                    name="openai-key", value=SENTINEL):
        return await pwa_server.api_secret_drop(_request(
            key=key, body={"host": host, "name": name, "value": value}))

    async def test_no_key_401(self):
        with self.assertRaises(HTTPException) as cm:
            await self._drop(key=None)
        self.assertEqual(cm.exception.status_code, 401)

    async def test_bad_key_401(self):
        with self.assertRaises(HTTPException) as cm:
            await self._drop(key="nope")
        self.assertEqual(cm.exception.status_code, 401)

    async def test_view_only_key_403(self):
        with self.assertRaises(HTTPException) as cm:
            await self._drop(key="view-only-key")
        self.assertEqual(cm.exception.status_code, 403)

    async def test_local_drop_writes_0600_and_receipt(self):
        out = await self._drop()
        self.assertTrue(out["ok"])
        self.assertEqual(out["via"], "local")
        self.assertEqual(out["path"], "idc03:~/.config/secrets/openai-key")
        self.assertEqual(
            out["sha8"], hashlib.sha256(SENTINEL.encode()).hexdigest()[:8])
        target = os.path.join(self._secrets.name, "openai-key")
        with open(target) as f:
            self.assertEqual(f.read(), SENTINEL)
        mode = stat.S_IMODE(os.stat(target).st_mode)
        self.assertEqual(mode, 0o600)

    async def test_redrop_normalizes_mode(self):
        target = os.path.join(self._secrets.name, "openai-key")
        await self._drop()
        os.chmod(target, 0o644)          # drifted loose
        await self._drop(value="rotated-value")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        with open(target) as f:
            self.assertEqual(f.read(), "rotated-value")

    async def test_legacy_key_may_drop(self):
        out = await self._drop(key="legacy-key", name="legacy-secret")
        self.assertTrue(out["ok"])

    async def test_bad_host_422(self):
        with self.assertRaises(HTTPException) as cm:
            await self._drop(host="kk-macbook")
        self.assertEqual(cm.exception.status_code, 422)

    async def test_bad_names_422(self):
        for name in ("../etc/passwd", "UPPER", "a b", "", "x" * 65):
            with self.assertRaises(HTTPException) as cm:
                await self._drop(name=name)
            self.assertEqual(cm.exception.status_code, 422, name)

    async def test_reserved_keys_filename_422(self):
        """Dropping over ada-ha-<inst>-keys.json would corrupt auth."""
        with self.assertRaises(HTTPException) as cm:
            await self._drop(name="ada-ha-test-keys.json")
        self.assertEqual(cm.exception.status_code, 422)

    async def test_empty_and_nonstr_value_422(self):
        with self.assertRaises(HTTPException) as cm:
            await self._drop(value="")
        self.assertEqual(cm.exception.status_code, 422)
        with self.assertRaises(HTTPException) as cm:
            await self._drop(value=None)
        self.assertEqual(cm.exception.status_code, 422)

    async def test_rate_limit_five_per_hour(self):
        for i in range(5):
            out = await self._drop(name=f"s{i}")
            self.assertTrue(out["ok"])
        with self.assertRaises(HTTPException) as cm:
            await self._drop(name="one-too-many")
        self.assertEqual(cm.exception.status_code, 429)
        # A different caller has their own window.
        out = await self._drop(key="legacy-key", name="other-caller")
        self.assertTrue(out["ok"])

    async def test_remote_host_ssh_fanout(self):
        with patch.object(pwa_server, "_ssh_secret_drop",
                          new=AsyncMock()) as ssh:
            out = await self._drop(host="tony-dell")
        self.assertTrue(out["ok"])
        self.assertEqual(out["via"], "ssh")
        self.assertEqual(out["path"], "tony-dell:~/.config/secrets/openai-key")
        ssh.assert_awaited_once_with("tony-dell", "openai-key", SENTINEL)

    async def test_ssh_failure_is_502(self):
        with patch.object(pwa_server, "_ssh_secret_drop",
                          new=AsyncMock(side_effect=HTTPException(
                              502, "ssh delivery to tony-dell failed"))):
            with self.assertRaises(HTTPException) as cm:
                await self._drop(host="tony-dell")
        self.assertEqual(cm.exception.status_code, 502)

    async def test_ssh_argv_never_carries_value(self):
        """The value must ride ssh stdin — argv is visible in ps."""
        captured = {}

        class FakeProc:
            returncode = 0

            async def communicate(self, data=None):
                captured["stdin"] = data
                return (b"", b"")

        async def fake_exec(*argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw
            return FakeProc()

        with patch.object(pwa_server.asyncio, "create_subprocess_exec",
                          side_effect=fake_exec):
            await pwa_server._ssh_secret_drop("tony-dell", "openai-key",
                                              SENTINEL)
        argv = " ".join(str(a) for a in captured["argv"])
        self.assertNotIn(SENTINEL, argv)
        self.assertIn("openai-key", argv)
        self.assertEqual(captured["stdin"], SENTINEL.encode())
        self.assertEqual(captured["kw"]["stdin"],
                         pwa_server.asyncio.subprocess.PIPE)

    async def test_value_absent_from_logs_events_and_response(self):
        """Compliance: after a drop the value must not appear in the log
        stream, the ops/events file, live transcripts, or the receipt."""
        records = []

        class Cap(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        cap = Cap()
        root = logging.getLogger()
        root.addHandler(cap)
        # basicConfig is a no-op under pytest (root already has handlers),
        # so the module logger sits at WARNING — set INFO to mirror prod.
        prev_level = pwa_server.logger.level
        pwa_server.logger.setLevel(logging.INFO)
        try:
            out = await self._drop()
        finally:
            pwa_server.logger.setLevel(prev_level)
            root.removeHandler(cap)
        # response is metadata only
        self.assertNotIn(SENTINEL, json.dumps(out))
        self.assertNotIn("value", out)
        # no log record carries it
        self.assertTrue(records)  # the drop IS audited, just not the value
        for msg in records:
            self.assertNotIn(SENTINEL, msg)
        # ops/events file records path+sha8, not the value
        events = open(self._events.name).read()
        self.assertIn("secret-drop", events)
        self.assertIn(out["sha8"], events)
        self.assertNotIn(SENTINEL, events)
        # nothing lands in live session transcripts either
        self.assertNotIn(SENTINEL, repr(pwa_server._live_sessions))


if __name__ == "__main__":
    unittest.main()
