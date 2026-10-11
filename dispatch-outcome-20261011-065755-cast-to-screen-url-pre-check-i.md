# cast-precheck-vantage — cast_to_screen URL pre-check is vantage-blind

## What was wrong

`_frame_check` HEADs the cast target from the Ada backend host before
pubbing, and marks timeout/refused/DNS failures as `dead` (hard refuse,
"URL is unreachable"). On idc03 — Ada's current home — there is no
route to 192.168.2.x, so any LAN-only URL was uncastable even though
the vcast displays (and tony-dell) live on that LAN and could fetch it
fine. Seen 2026-10-10: a yt-live `.mp4` on `http://192.168.2.67/...`
serving 206 on LAN/tailnet refused to screen 9.

The card's option list is truncated in the board note (ends at
"(a) downgrade the"). Implemented the superset: keep the invented-URL
guard but require the display's side of the world to agree before
declaring `dead`.

## Changes (backend/tool_runner/screens.py)

- `_frame_vantage_blind(url)` — True when a transport failure proves
  nothing: literal non-global IP (RFC1918/loopback/link-local/CGNAT,
  incl. tailnet 100.x), a hostname resolving only to non-global
  addresses, or a name the backend can't resolve at all. Public hosts
  return False → `dead` stands with no extra hop.
- `_lan_frame_probe(url)` — re-HEAD from the LAN vantage:
  `ssh $ADA_CCTV_SSH curl -sSI -m 8 <url>` (default `tony-dell-m2m`,
  the same m2m lane `_cctv_grab`/`_yt_publish` already use). curl rc
  5/6/7/28 → `dead` (the vantage that matters also fails); ssh-down or
  other rc → `None` (inconclusive); loopback targets declined (the
  display's own localhost is a third vantage).
- `_frame_check` — on a dead-looking `URLError` to a vantage-blind
  host, the LAN probe's verdict wins; an inconclusive probe downgrades
  to `warn` ("unreachable from this backend … proceeding unverified")
  surfaced as `frame_warn` — the display tries, and the existing
  post-cast screen_state/render_warn check still catches a black pane.
- `_frame_hdr_verdict(url, get_hdr)` — shared Content-Type /
  X-Frame-Options / CSP verdict for both probe paths, so
  action='cast' also routes LAN media correctly (mp4 → play).

## Result

- LAN URLs reachable from the display's side now cast (verified
  mechanics live: `ssh tony-dell "curl -sSI http://192.168.2.67/..."`
  returns headers from this runner; `tony-dell-m2m` resolves on idc03
  where the lane is actually used).
- Genuinely dead LAN URLs still refuse — both vantages must agree.
- Dead public URLs unchanged — no ssh hop is added.
- If the LAN vantage is down, casts proceed unverified with a warning
  instead of refusing.

## Verify

- `pytest tests/test_tool_runner.py -k 'CastLanProbe or lan_url_casts
  or CastToScreenRoute'` → 16 passed (10 new unit cases + e2e cast).
- `pytest tests/ -x -q` → 1264 passed, 1 skipped.
- `python3 scripts/tool-lint.py` → clean.
- After next idc03 deploy: cast_to_screen play
  http://192.168.2.67/apps/yt-live/<file>.mp4 to a screen should
  deliver instead of refusing.

Trail: docs/ssot/jobs/ada/2026-10-11-cast-precheck-vantage.yml
