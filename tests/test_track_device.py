"""Unit tests for backend/tools.d/ada_track_device.py.

All sources are faked — no tailnet, no mddb, no HA, no network.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend import tools_loader

TOOL_PATH = (Path(__file__).resolve().parent.parent
             / "backend" / "tools.d" / "ada_track_device.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "tools_d.ada_track_device_test", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


track = _load_module()
NOW = int(time.time())


class FakeMddb:
    """Minimal mddb stand-in: get_document + search_documents."""

    def __init__(self, docs=None):
        self.docs = docs or {}

    async def get_document(self, collection, key, lang="en",
                           prefer_leader=False):
        return self.docs.get(f"{collection}|{key}")

    async def search_documents(self, collection, query="*", limit=10,
                               filter_meta=None):
        return [d for d in self.docs.values()
                if isinstance(d, dict) and d.get("_list")]


def _runner(mddb=None, states=None, logbook=None):
    r = MagicMock()
    r.mddb = mddb
    ha = MagicMock()
    ha.base_url = "http://ha.local:8123"
    ha._states = AsyncMock(return_value=states or [])
    ha.logbook = AsyncMock(side_effect=lambda entity_id=None,
                           hours=24, end=None:
                           (logbook or {}).get(entity_id, []))
    r.context.ha_client = ha
    return r


def _tracker(eid="device_tracker.tony_ip", state="home", ts=None,
             **attrs):
    row = {
        "entity_id": eid,
        "state": state,
        "last_changed": ts or "2026-10-08T00:00:00+00:00",
        "last_updated": ts or "2026-10-08T00:00:00+00:00",
        "attributes": {"friendly_name": attrs.pop("name", eid), **attrs},
    }
    return row


def _sensor(eid, state, ts="2026-10-08T00:00:00+00:00"):
    return {"entity_id": eid, "state": state,
            "last_changed": ts, "last_updated": ts, "attributes": {}}


class CanonTest(unittest.TestCase):

    def test_alias_resolution(self):
        self.assertEqual(track._canon("TONY-IP"), "iphone-15")
        self.assertEqual(track._canon("tony_ip"), "iphone-15")
        self.assertEqual(track._canon("iPhone"), "iphone-15")
        self.assertEqual(track._canon("tony-mn"), "mn01")
        self.assertEqual(track._canon("KKs-MacBook-Pro"), "kk-macbook")
        self.assertEqual(track._canon("iPad-2"), "kk-ipad")
        self.assertEqual(track._canon("tony-omen"), "tony-omen")

    def test_tailnet_keys_on_dnsname(self):
        st = {
            "Self": {"DNSName": "me.tail.net.", "HostName": "me",
                     "Online": True},
            "Peer": {"p1": {"DNSName": "iphone-15.tail.net.",
                            "HostName": "localhost", "Online": True,
                            "TailscaleIPs": ["100.1.1.1"], "OS": "iOS"},
                     "p2": {"HostName": "no-dns", "Online": False,
                            "LastSeen": "2026-10-07T10:00:00Z"}},
        }
        with patch.dict("os.environ",
                        {"ADA_TRACK_TAILSCALE_JSON": __import__("json")
                         .dumps(st)}):
            nodes = track._tailnet()
        self.assertIn("iphone-15", nodes)
        self.assertIn("no-dns", nodes)          # HostName fallback only
        self.assertNotIn("localhost", nodes)
        self.assertEqual(nodes["iphone-15"]["ips"], ["100.1.1.1"])


class MergeTest(unittest.TestCase):

    def test_gps_wins_over_city(self):
        desc = track._describe(
            "iphone-15",
            tele={"ts": NOW - 60,
                  "wan": {"city": "Bang Phli", "loc": "13.6,100.7"}},
            node={"online": True, "ips": [], "last_seen_ts": 0,
                  "os": "iOS"},
            ha={"trackers": [{
                "entity_id": "device_tracker.tony_ip", "name": "TONY-IP",
                "state": "home", "lat": 13.61, "lon": 100.71,
                "gps_accuracy": 9, "battery_level": 80,
                "ts": NOW - 120, "comp_ts": 0}]},
            lan_hits=[], lan_ts=NOW - 60, now=NOW)
        self.assertEqual(desc["location"]["kind"], "gps")
        self.assertEqual(desc["confidence"], "high")
        self.assertFalse(desc["stale"])
        self.assertIn("GPS", desc["summary"])

    def test_lan_beats_beacon_when_fresher_rank(self):
        # rank: home(3) > city(2) — LAN presence wins the headline.
        desc = track._describe(
            "tony-omen",
            tele={"ts": NOW - 300, "wan": {"city": "Bang Phli"}},
            node={"online": True, "ips": [], "last_seen_ts": 0,
                  "os": "linux"},
            ha=None,
            lan_hits=[{"ip": "192.168.2.70", "mac": None,
                       "hostname": "tony-omen", "vendor": "Intel"}],
            lan_ts=NOW - 60, now=NOW)
        self.assertEqual(desc["location"]["kind"], "home")
        self.assertIn("home LAN", desc["summary"])

    def test_last_seen_only_is_low_confidence(self):
        desc = track._describe(
            "tony-ipad", tele=None,
            node={"online": False, "last_seen_ts": NOW - 3600,
                  "os": "iOS", "ips": []},
            ha=None, lan_hits=[], lan_ts=0, now=NOW)
        self.assertEqual(desc["location"]["kind"], "last-seen")
        self.assertEqual(desc["confidence"], "low")
        self.assertTrue(desc["stale"])

    def test_battery_freshest_wins(self):
        desc = track._describe(
            "iphone-15", tele=None, node=None,
            ha={"trackers": [{
                "entity_id": "device_tracker.tony_ip", "name": "TONY-IP",
                "state": "home", "lat": None, "lon": None,
                "gps_accuracy": None, "battery_level": 80,
                "ts": NOW - 120, "comp_ts": NOW - 30,
                "battery_pct": "55", "battery_state": "Not Charging",
                "ssid": None, "bssid": None, "geocoded": None,
                "update_trigger": None, "charging": False}]},
            lan_hits=[], lan_ts=0, now=NOW)
        self.assertEqual(desc["battery"]["pct"], 55)

    def test_no_sources_says_never_phoned_home(self):
        desc = track._describe("ghost", tele=None, node=None, ha=None,
                               lan_hits=[], lan_ts=0, now=NOW)
        self.assertEqual(desc["confidence"], "none")
        self.assertIn("never phoned home", desc["summary"])


class HaFleetTest(unittest.TestCase):

    def test_tracker_person_companion_fold(self):
        states = [
            _tracker(state="home", latitude=1.0, longitude=2.0,
                     gps_accuracy=5, battery_level=88, name="TONY-IP"),
            _sensor("sensor.tony_ip_battery_level", "77"),
            _sensor("sensor.tony_ip_ssid", "HOME-WIFI"),
            {"entity_id": "person.tony", "state": "home",
             "last_changed": "2026-10-08T00:00:00+00:00",
             "attributes": {"friendly_name": "Tony",
                            "device_trackers": ["device_tracker.tony_ip"]}},
        ]
        fleet = track._ha_fleet(states)
        self.assertIn("iphone-15", fleet)
        t = fleet["iphone-15"]["trackers"][0]
        self.assertEqual(t["lat"], 1.0)
        self.assertEqual(t["battery_pct"], "77")
        self.assertEqual(t["ssid"], "HOME-WIFI")
        self.assertEqual(fleet["iphone-15"]["person"]["state"], "home")

    def test_alias_table_folds_tracker_slug(self):
        states = [_tracker("device_tracker.kk_ipad_2", name="KK iPad")]
        fleet = track._ha_fleet(states)
        # kk_ipad_2 aliases onto the tailnet canon 'kk-ipad'
        self.assertIn("kk-ipad", fleet)

    def test_unknown_tracker_keeps_own_canon(self):
        states = [_tracker("device_tracker.party_tablet",
                           name="Party Tablet")]
        fleet = track._ha_fleet(states)
        self.assertIn("party-tablet", fleet)


class LostModeTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.env = patch.dict("os.environ", {"ADA_TRACK_PUSH_DRYRUN": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    async def test_arm_push_delta_disarm(self):
        runner = _runner()
        desc1 = {"summary": "iphone-15: home; battery 50%",
                 "location": {"kind": "gps", "lat": 1.0, "lon": 2.0,
                              "place": "home"},
                 "battery": {"pct": 50},
                 "sources": {"tailnet": {"online": True}}}
        desc2 = dict(desc1)
        desc2["location"] = {"kind": "gps", "lat": 1.5, "lon": 2.5,
                             "place": "away"}
        desc2["summary"] = "iphone-15: away; battery 50%"
        descs = [desc1, desc2, desc2]
        idx = 0

        async def _merge(r, c, **kw):
            nonlocal idx
            d = descs[min(idx, len(descs) - 1)]
            idx += 1
            return d

        with patch.object(track, "_merge_one", _merge):
            watch = {"interval_s": 0.01, "channel": "line",
                     "until": time.time() + 5, "armed_at": NOW}
            runner._track_watches = {"iphone-15": watch}
            task = asyncio.ensure_future(
                track._watch_loop(runner, "iphone-15", watch))
            await asyncio.sleep(0.15)
            runner._track_watches.pop("iphone-15", None)
            await asyncio.wait_for(task, timeout=2)

        pushes = runner._track_push_log
        self.assertEqual(len(pushes), 2)          # baseline + one delta
        self.assertIn("[lost-mode iphone-15]", pushes[0]["text"])
        self.assertIn("[delta iphone-15]", pushes[1]["text"])
        self.assertIn("away", pushes[1]["text"])
        self.assertEqual(pushes[0]["channel"], "line")

    async def test_arm_lost_lifecycle_via_run(self):
        runner = _runner()
        nodes = {"iphone-15": {"online": True, "os": "iOS", "ips": [],
                               "last_seen_ts": 0, "hostname": "localhost"}}
        with patch.object(track, "_tailnet", return_value=nodes), \
                patch.object(track, "_lan_scan",
                             AsyncMock(return_value={})), \
                patch.object(track, "_mac_registry",
                             AsyncMock(return_value={})), \
                patch.object(track, "_mddb_docs",
                             AsyncMock(return_value=[])):
            out = await track.run(runner, action="lost",
                                  device="iphone-15", interval_s=60)
            self.assertTrue(out["armed"])
            self.assertIn("iphone-15", track._watches(runner))
            out = await track.run(runner, action="found",
                                  device="iphone-15")
            self.assertEqual(out["disarmed"], ["iphone-15"])
            self.assertNotIn("iphone-15", track._watches(runner))
            # cancel the orphaned task so it can't outlive the test loop
            for w in list(getattr(runner, "_track_watches", {}).values()):
                t = w.get("task")
                if t:
                    t.cancel()

    def test_fingerprint_detects_geo_move(self):
        a = {"location": {"kind": "gps", "lat": 1.0001, "lon": 2.0001},
             "battery": {"pct": 50}, "sources": {}}
        b = {"location": {"kind": "gps", "lat": 1.5, "lon": 2.5},
             "battery": {"pct": 50}, "sources": {}}
        self.assertNotEqual(track._fingerprint(a), track._fingerprint(b))
        c = {"location": {"kind": "gps", "lat": 1.0004, "lon": 2.0004},
             "battery": {"pct": 50}, "sources": {}}
        self.assertEqual(track._fingerprint(a), track._fingerprint(c))


class ManifestWiringTest(unittest.TestCase):

    def test_tool_loads_from_real_manifest(self):
        reg = tools_loader.load()  # real backend/tools.d
        self.assertIn("ada_track_device", reg.tools, reg.errors)
        tool = reg.tools["ada_track_device"]
        self.assertEqual(tool.policy, "owner_only")
        self.assertFalse(tool.secondary_allowed)
        self.assertEqual(tool.declaration["name"], "ada_track_device")


if __name__ == "__main__":
    unittest.main()
