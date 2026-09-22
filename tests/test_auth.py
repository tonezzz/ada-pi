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
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(auth.configured())
            self.assertIsNone(auth.caller_name(_request()))
            self.assertTrue(auth.websocket_authorized(_ws()))

    def test_single_key(self):
        with patch.dict(os.environ, {"ADA_API_KEY": "k1"}, clear=True):
            req = _request(headers={"x-api-key": "k1"})
            self.assertEqual(auth.caller_name(req), "default")

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


if __name__ == "__main__":
    unittest.main()
