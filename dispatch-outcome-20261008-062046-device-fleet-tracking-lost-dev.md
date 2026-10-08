# Dispatch outcome — device fleet tracking (lost-device locate)

## Result

`ada_track_device` rewritten into the full multi-source locate layer the
card specifies. Ada can now answer "where is my <device>" by merging:

1. **HA companion app** — `device_tracker.*` (state, lat/lon,
   gps_accuracy, battery attr), `person.*` links via
   `device_trackers` attr, companion sensors
   `sensor.<slug>_{battery_level,battery_state,ssid,bssid,
   geocoded_location,last_update_trigger}` +
   `binary_sensor.<slug>_charging`.
2. **device-telemetry beacon** — point-read `<dev>/latest` in the MDDB
   `device-telemetry` collection (search ordering not relied on);
   WAN geo → city-level sighting, battery, login users, top processes.
3. **Tailscale** — `tailscale status --json` keyed on **DNSName**
   (iOS nodes report `HostName: localhost` — unusable as identity).
   Online → "online" sighting; `LastSeen` → "last-seen" sighting that is
   **always low confidence** (aliveness, not a location).
4. **LAN scan** — `<ha>/local/ha/network-scan-latest.json`; device match
   by registry MAC (chaba `infrastructure-ssot.mac-address-registry.tony`
   doc in MDDB) then hostname; `discovered-*` registry rows excluded from
   the fleet pool.
5. **HA logbook** — `action='history'` returns beacon history keys +
   logbook entries for the matched tracker entity.

Every answer carries `confidence` (high = fresh gps/home/zone/wifi;
medium = fresh city/online or <2h evidence; low = stale or last-seen)
and per-source `age`; >15 min staleness appends "STALE — not a live
fix". Alias table + `my`/`the` prefix stripping resolve
`iphone|tony_ip -> iphone-15`, `ipad-2 -> kk-ipad`, `my ipad ->
tony-ipad`, `homeassistant -> michael-ha`, etc.

Actions: `where` (default), `list`, `history`, `lost` (arm volatile
in-process watcher — polls every `interval_s` (min 60, default 300),
pushes to LINE :8912/TG :8911 `/send` only on fingerprint deltas
(loc kind/place, GPS ~100m rounding, online flags, battery 20% bucket,
charging); 12h default lifetime; dies with backend — by design),
`found`, `watches`. `gev_pin=true` pins coords via
`gev_command annotate_map`. iOS app-usage is honestly absent
(Android-only `last_used_app` not surfaced); "what was it doing" only
answers for beaconed hosts via `top_procs`.

## Files changed

- `backend/tools.d/ada_track_device.py` — rewrite (~960 lines)
- `backend/tools.d/manifest.yml` — timeout_s 20 → 30
- `tests/scenario_engine.py` — `ha_raw_states` + `ha_logbook` fixtures;
  `__NOW__`/`__NOW_M_<s>__`/`__NOWISO__`/`__NOWISO_M_<s>__` time
  placeholders
- `tests/scenarios/device_track_merge.yaml` — new offline scenario
- `tests/test_track_device.py` — new, 14 tests
- `docs/ssot/jobs/ada/2026-10-08-device-fleet-tracking.yml` — SSOT trail

## Verification

- `pytest tests/test_track_device.py` → **14 passed**
- `pytest tests` → **39 passed**
- Live smoke (tony-dell, real tony-ha + tailnet + idc03 beacon):
  - `where tony-omen` → "home LAN; as of 13min ago; battery 50%
    (Not charging); tailnet online; on LAN at 192.168.2.84" —
    beacon WAN geo/users/procs merged, MAC matched via registry
  - `where iphone-15` → "at 304/72 Moo 12 (GPS ±8m); as of 9h ago;
    battery 55% (Not Charging); tailnet online; STALE — not a live fix"
  - `my ipad` → tony-ipad; `ipad-2` → kk-ipad; `list` → 14-device fleet

## Caveats / follow-ups

- HA logbook on tony-ha times out (>20 s, recorder slow) — tool degrades
  to `[]` inside the client's 5 s timeout; check michael-ha in prod.
- `device_tracker.tony_ip` enrolled + reporting (GPS, battery, ssid).
  Apple Watch tracker stale/unknown. `sensor.tony_omen_battery_level` +
  `sensor.kk_macbook_battery_level` exist but `unknown` — companion
  registered, sensors not flowing. **tony-ipad / kk-ipad have no HA
  entities** — iPads reachable via tailnet + LAN only; installing the
  companion app + location permission on them is the enrollment gap.
- No commit/push/deploy — worktree only; prod picks it up via the next
  `deploy-ada.sh` on idc03.
