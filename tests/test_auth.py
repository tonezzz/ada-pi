import os
import unittest
from unittest.mock import MagicMock, patch

from backend import auth


def _request(headers=None, query=None, cookies=None):
    req = MagicMock()
    req.headers = headers or {}
    req.query_params = query or {}
    req.cookies = cookies or {}
    return req


def _ws(query=None, cookies=None):
    ws = MagicMock()
    ws.query_params = query or {}
    ws.cookies = cookies or {}
    return ws


KEYS_ENV = {"ADA_API_KEY": "single-key", "ADA_API_KEYS": "tony:tony-key,michael:michael-key"}


class AuthTests(unittest.TestCase):
    def test_not_configured_allows_everything(self):
        with patch.dict(os.environ, {"ADA_KEYS_FILE": "/nonexistent-keys.json"}, clear=True):
            self.assertFalse(auth.configured())
            self.assertIsNone(auth.caller_name(_request()))
            self.assertTrue(auth.websocket_authorized(_ws()))

    def test_single_key(self):
        with patch.dict(os.environ, {"ADA_API_KEY": "k1"}, clear=True):
            req = _request(headers={"x-api-key": "k1"})
            self.assertEqual(auth.caller_name(req), "admin")

    def test_named_keys(self):
        with patch.dict(os.environ, KEYS_ENV, clear=True):
            self.assertTrue(auth.configured())
            self.assertEqual(auth.caller_name(_request(headers={"x-api-key": "michael-key"})), "michael")
            self.assertEqual(auth.caller_name(_request(query={"api_key": "tony-key"})), "tony")
            self.assertIsNone(auth.caller_name(_request(headers={"x-api-key": "wrong"})))
            self.assertIsNone(auth.caller_name(_request()))

    def test_session_issue_and_validate(self):
        with patch.dict(os.environ, KEYS_ENV, clear=True):
            issued = auth.issue_session("michael-key")
            self.assertIsNotNone(issued)
            name, token = issued
            self.assertEqual(name, "michael")
            req = _request(cookies={auth.SESSION_COOKIE: token})
            self.assertEqual(auth.caller_name(req), "michael")
            self.assertTrue(auth.websocket_authorized(_ws(cookies={auth.SESSION_COOKIE: token})))

    def test_session_rejects_tampered_and_revoked(self):
        with patch.dict(os.environ, KEYS_ENV, clear=True):
            name, token = auth.issue_session("tony-key")
            bad = token[:-1] + ("0" if token[-1] != "0" else "1")
            self.assertIsNone(auth.caller_name(_request(cookies={auth.SESSION_COOKIE: bad})))
            # Revoking tony's key kills his session but not michael's.
            _, mtok = auth.issue_session("michael-key")
            with patch.dict(os.environ, {"ADA_API_KEYS": "michael:michael-key", "ADA_API_KEY": ""}, clear=True):
                self.assertIsNone(auth.caller_name(_request(cookies={auth.SESSION_COOKIE: token})))
                self.assertEqual(auth.caller_name(_request(cookies={auth.SESSION_COOKIE: mtok})), "michael")

    def test_session_expired(self):
        with patch.dict(os.environ, KEYS_ENV, clear=True):
            name, token = auth.issue_session("tony-key")
            parts = token.rsplit(".", 2)
            stale = f"{parts[0]}.1.{parts[2]}"
            self.assertIsNone(auth.caller_name(_request(cookies={auth.SESSION_COOKIE: stale})))

    def test_issue_session_bad_key(self):
        with patch.dict(os.environ, KEYS_ENV, clear=True):
            self.assertIsNone(auth.issue_session("nope"))

    def test_websocket_authorized_by_query_key(self):
        with patch.dict(os.environ, KEYS_ENV, clear=True):
            self.assertTrue(auth.websocket_authorized(_ws(query={"api_key": "tony-key"})))
            self.assertFalse(auth.websocket_authorized(_ws(query={"api_key": "bad"})))
            self.assertFalse(auth.websocket_authorized(_ws()))

    def test_shared_star_key_skips_device_binding(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"viewer": {"key": "viewer-key", "device": "*"}}, f)
            path = f.name
        try:
            env = {"ADA_KEYS_FILE": path}
            with patch.dict(os.environ, env, clear=True):
                # Any device id (or none) authenticates, and nothing rebinds it.
                self.assertEqual(
                    auth.caller_name(_request(headers={"x-api-key": "viewer-key"})),
                    "viewer",
                )
                self.assertEqual(
                    auth.caller_name(_request(
                        headers={"x-api-key": "viewer-key", "x-device-id": "a"})),
                    "viewer",
                )
                self.assertEqual(
                    auth.caller_name(_request(
                        headers={"x-api-key": "viewer-key", "x-device-id": "b"})),
                    "viewer",
                )
                self.assertIsNone(
                    auth.caller_name(_request(headers={"x-api-key": "nope"}))
                )
        finally:
            os.unlink(path)


class InviteKeyTests(unittest.TestCase):
    def _keys_file(self, data=None):
        import json
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(data or {}, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_create_key_with_ha_person(self):
        path = self._keys_file()
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            key = auth.create_key("user-kk", ha_person="person.kk")
            self.assertIsNotNone(key)
            self.assertEqual(auth.ha_person_for_key("user-kk"), "person.kk")
            self.assertIn("user-kk", auth.issued_key_names())
            details = auth.issued_key_details()["user-kk"]
            self.assertEqual(details["ha_person"], "person.kk")
            self.assertIsNone(details["device"])

    def test_create_key_without_ha_person(self):
        path = self._keys_file()
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            self.assertIsNotNone(auth.create_key("user-testo"))
            self.assertIsNone(auth.ha_person_for_key("user-testo"))

    def test_bind_device_preserves_ha_person(self):
        path = self._keys_file()
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            auth.create_key("user-kk", ha_person="person.kk")
            self.assertTrue(auth.bind_device("user-kk", "dev-1"))
            self.assertEqual(auth.ha_person_for_key("user-kk"), "person.kk")

    def test_set_key_ha_person_updates_and_clears(self):
        path = self._keys_file({"user-kk": {"key": "k1", "device": None}})
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            self.assertTrue(auth.set_key_ha_person("user-kk", "person.kk"))
            self.assertEqual(auth.ha_person_for_key("user-kk"), "person.kk")
            self.assertTrue(auth.set_key_ha_person("user-kk", None))
            self.assertIsNone(auth.ha_person_for_key("user-kk"))
            self.assertFalse(auth.set_key_ha_person("ghost", "person.x"))

    def test_create_key_rejects_duplicates_and_bad_names(self):
        path = self._keys_file({"user-kk": {"key": "k1"}})
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path, "ADA_API_KEY": "adm"}, clear=True):
            self.assertIsNone(auth.create_key("user-kk"))
            self.assertIsNone(auth.create_key("admin"))  # env name is taken
            self.assertIsNone(auth.create_key("bad name!"))


if __name__ == "__main__":
    unittest.main()
