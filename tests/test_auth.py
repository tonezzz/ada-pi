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

    def test_key_apps_roundtrip(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({}, f)
            path = f.name
        try:
            with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
                self.assertIsNotNone(auth.create_key("viewer", apps=["view"]))
                self.assertIsNotNone(auth.create_key("dev1"))
                apps = auth.issued_key_apps()
                self.assertEqual(apps["viewer"], ["view"])
                self.assertIsNone(apps["dev1"])
                # Unknown/empty apps collapse to the default (None).
                self.assertIsNotNone(auth.create_key("junk", apps=["bogus"]))
                self.assertIsNone(auth.issued_key_apps()["junk"])
        finally:
            os.unlink(path)

    def test_key_can_dispatch_capability(self):
        import json
        import tempfile
        keys = {
            "viewer": {"key": "v-key", "device": "*", "apps": ["view"]},
            "tony": {"key": "t-key", "device": "*",
                     "apps": ["view", "dispatch"]},
            "legacy": {"key": "l-key", "device": "*"},  # apps absent
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(keys, f)
            path = f.name
        try:
            env = {"ADA_KEYS_FILE": path, "ADA_API_KEY": "adm-key"}
            with patch.dict(os.environ, env, clear=True):
                # dispatch survives the apps cleaner and gates correctly
                self.assertIn("dispatch", auth.VALID_KEY_APPS)
                self.assertFalse(auth.key_can("viewer", "dispatch"))
                self.assertTrue(auth.key_can("tony", "dispatch"))
                # legacy default is voice+chat — no view, no dispatch
                self.assertTrue(auth.key_can("legacy", "voice"))
                self.assertFalse(auth.key_can("legacy", "view"))
                self.assertFalse(auth.key_can("legacy", "dispatch"))
                # env/operator keys are unscoped
                self.assertTrue(auth.key_can("admin", "dispatch"))
        finally:
            os.unlink(path)

    def test_bind_device_preserves_apps(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"viewer": {"key": "viewer-key", "device": None,
                                  "issued": "2026-09-26", "apps": ["view"]}}, f)
            path = f.name
        try:
            with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
                self.assertTrue(auth.bind_device("viewer", "dev-abc"))
                self.assertEqual(auth.bound_device("viewer"), "dev-abc")
                self.assertEqual(auth.issued_key_apps()["viewer"], ["view"])
                self.assertEqual(auth.issued_key_timeline()["viewer"]["issued"],
                                 "2026-09-26")
                # unbind drops the device but keeps apps too
                self.assertTrue(auth.unbind_device("viewer"))
                self.assertIsNone(auth.bound_device("viewer"))
                self.assertEqual(auth.issued_key_apps()["viewer"], ["view"])
        finally:
            os.unlink(path)

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


class MemberInviteTests(unittest.TestCase):
    """Pending/approved/revoked key states + invite tokens —
    docs/kb/ada-member-invite.md."""

    def _keys_file(self, data=None):
        import json
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(data or {}, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_pending_key_cannot_authenticate(self):
        path = self._keys_file()
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            key = auth.create_key("user-kk", approved=False, person="KK",
                                  invite=True)
            self.assertIsNotNone(key)
            self.assertEqual(auth.key_status("user-kk"), "pending")
            # Pending keys are excluded from the auth map entirely.
            self.assertIsNone(auth.caller_name(
                _request(headers={"x-api-key": key})))
            self.assertIsNone(auth.issue_session(key))
            self.assertIsNone(auth.issue_session_for_name("user-kk"))
            self.assertFalse(auth.websocket_authorized(
                _ws(query={"api_key": key})))
            # ...but the name still exists for admin surfaces.
            self.assertIn("user-kk", auth.issued_key_names())
            self.assertEqual(auth.issued_key_details()["user-kk"]["status"],
                             "pending")

    def test_approve_flips_pending_to_live(self):
        path = self._keys_file()
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            key = auth.create_key("user-kk", approved=False)
            self.assertIsNone(auth.caller_name(
                _request(headers={"x-api-key": key})))
            self.assertTrue(auth.approve_key("user-kk"))
            self.assertEqual(auth.key_status("user-kk"), "issued")
            self.assertEqual(
                auth.caller_name(_request(headers={"x-api-key": key})),
                "user-kk")
            # Approve is idempotent-false: only pending -> issued flips.
            self.assertFalse(auth.approve_key("user-kk"))

    def test_approve_missing_and_revoked(self):
        path = self._keys_file()
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            self.assertFalse(auth.approve_key("ghost"))
            auth.create_key("user-kk", approved=False)
            auth.revoke_key("user-kk")
            self.assertFalse(auth.approve_key("user-kk"))

    def test_revoke_tombstone_kills_auth_and_session(self):
        path = self._keys_file()
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            key = auth.create_key("user-kk")
            name, token = auth.issue_session(key)
            self.assertTrue(auth.revoke_key("user-kk"))
            self.assertEqual(auth.key_status("user-kk"), "revoked")
            self.assertIsNone(auth.caller_name(
                _request(headers={"x-api-key": key})))
            self.assertIsNone(auth.caller_name(
                _request(cookies={auth.SESSION_COOKIE: token})))
            # Tombstone keeps the name taken and listed.
            self.assertIsNone(auth.create_key("user-kk"))
            details = auth.issued_key_details()["user-kk"]
            self.assertEqual(details["status"], "revoked")
            self.assertIsNotNone(details["revoked_at"])
            # Double revoke is a no-op.
            self.assertFalse(auth.revoke_key("user-kk"))

    def test_invite_token_roundtrip_and_rotate(self):
        path = self._keys_file()
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            auth.create_key("user-kk", approved=False, person="KK",
                            invite=True)
            token = auth.invite_token_for("user-kk")
            self.assertIsNotNone(token)
            self.assertEqual(auth.invite_for_token(token), "user-kk")
            self.assertIsNone(auth.invite_for_token("bogus-token"))
            new_token = auth.rotate_invite("user-kk")
            self.assertNotEqual(token, new_token)
            self.assertIsNone(auth.invite_for_token(token))     # old link dead
            self.assertEqual(auth.invite_for_token(new_token), "user-kk")

    def test_invite_details_member_safe(self):
        path = self._keys_file()
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            auth.create_key("user-kk", approved=False, person="KK",
                            ha_person="person.kk", invite=True)
            d = auth.invite_details("user-kk")
            self.assertEqual(d["person"], "KK")
            self.assertEqual(d["ha_person"], "person.kk")
            self.assertEqual(d["status"], "pending")
            self.assertFalse(d["claimed"])
            self.assertNotIn("key", d)          # never key material
            self.assertNotIn("invite", d)       # never the token
            self.assertIsNone(auth.invite_details("ghost"))

    def test_claim_invite_records_once(self):
        path = self._keys_file()
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            auth.create_key("user-kk", approved=False, invite=True)
            auth.claim_invite("user-kk", "dev-a")
            auth.claim_invite("user-kk", "dev-b")   # first touch wins
            import json
            data = json.loads(open(path).read())
            self.assertEqual(data["user-kk"]["invite_claimed"]["device"],
                             "dev-a")
            self.assertTrue(
                auth.issued_key_details()["user-kk"]["claimed"])
            # Claim never binds the key's device — the Safari->PWA
            # localStorage hop must not strand the member.
            self.assertIsNone(data["user-kk"]["device"])
            # No invite token -> no claim recorded (direct keys).
            auth.create_key("ops-key")
            auth.claim_invite("ops-key", "dev-c")
            data = json.loads(open(path).read())
            self.assertNotIn("invite_claimed", data["ops-key"])

    def test_pending_redeem_path_gate(self):
        """enforce_device refuses pending/revoked keys — invite redeem
        can only hand out an approved key."""
        path = self._keys_file()
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            auth.create_key("user-kk", approved=False, invite=True)
            self.assertIsNone(auth.enforce_device("user-kk", "dev-a"))
            auth.approve_key("user-kk")
            self.assertEqual(auth.enforce_device("user-kk", "dev-a"),
                             "user-kk")                       # TOFU binds
            self.assertIsNone(auth.enforce_device("user-kk", "dev-b"))
            self.assertEqual(auth.enforce_device("user-kk", "dev-a"),
                             "user-kk")
            auth.revoke_key("user-kk")
            self.assertIsNone(auth.enforce_device("user-kk", "dev-a"))

    def test_reinvite_resurrects_revoked(self):
        path = self._keys_file()
        with patch.dict(os.environ, {"ADA_KEYS_FILE": path}, clear=True):
            auth.create_key("user-kk", person="KK", invite=True)
            old_token = auth.invite_token_for("user-kk")
            auth.bind_device("user-kk", "dev-x")
            auth.revoke_key("user-kk")
            new_token = auth.reinvite_key("user-kk", person="KK2")
            self.assertIsNotNone(new_token)
            self.assertNotEqual(old_token, new_token)
            self.assertIsNone(auth.invite_for_token(old_token))
            self.assertEqual(auth.key_status("user-kk"), "pending")
            self.assertIsNone(auth.bound_device("user-kk"))
            self.assertEqual(auth.invite_details("user-kk")["person"], "KK2")
            # Reinvite on a live key refuses.
            self.assertIsNone(auth.reinvite_key("user-kk"))


if __name__ == "__main__":
    unittest.main()
