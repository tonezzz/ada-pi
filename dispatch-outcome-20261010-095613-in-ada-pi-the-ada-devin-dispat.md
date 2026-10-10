# dispatch-outcome: ada-dispatch-tailnet-target

## What changed

`backend/devin_dispatch.py` — the only place the dispatch SSH target lives:

- `HOST = os.environ.get("ADA_DEVIN_DISPATCH_HOST", "192.168.2.67")`
  -> default now `"tony-dell"` (env override unchanged).
- The stale comment block (lines 29-32) citing the tailscaled-ssh
  re-auth rationale rewritten to record the inversion: tony-dell's
  tailscaled runs RunSSH:false so it no longer intercepts :22; the LAN
  IP is unreachable from idc03 while the tailnet name resolves via
  MagicDNS to 100.68.142.13 where plain sshd answers. Points at the
  chaba-side doc `docs/ssot/jobs/infrastructure/2026-10-10-dispatch-ssh-tailnet.yml`.

All dispatch verbs (start/status/followup/logs) funnel through
`_run()` -> `ssh HOST <bin> <args>`, so the single constant covers
every command — no per-call sites needed touching.

`tools.d/devin.py` does not exist in this tree (the 2026-10-06 mixin
split moved it to `backend/tool_runner/devin.py`); its docstrings and
the `tool_guide.yml` `devin`/`devin_read` entries already say "on
tony-dell" the host name, not the IP — nothing to change there. A
repo-wide grep for `192\.168\.2\.67` / `ADA_DEVIN_DISPATCH_HOST` /
re-auth rationale finds no other dispatch references (remaining IP
hits are unrelated: bench topologies lane config, a pwa_server
trusted-networks comment).

Committed on the dispatch branch as `6d376fe` along with
`docs/ssot/jobs/ada/2026-10-10-devin-dispatch-tailnet-host.yml`.

## Result

`python3 -m pytest tests/test_devin_dispatch.py -x -q` -> 26 passed.

## How to verify

- `ssh -o BatchMode=yes -o ConnectTimeout=6 tony-dell ~/.local/bin/devin-dispatch status`
  — ran here on tony-dell: returned the full task registry (EXIT=0).
  The card's verify clause targets the Ada backend host (idc03); the
  same ssh-config mapping exists there per the card's live evidence,
  and this change is client-side only — it takes effect on idc03 at
  the next ada-pi deploy (`deploy-ada.sh`).
- `grep -n "HOST = " backend/devin_dispatch.py` shows the new default.

## Known issues

- `python3 scripts/tool-lint.py` reports 3 pre-existing violations on
  the base tree (voice_fx descsize + coverage, undeclared
  `ToolRunner.yt_cached_list`) — unrelated to this diff, but they will
  trip the deploy-ada.sh pre-flight gate on the next deploy if still
  unfixed on main.
- Board comms: `GET /cards` healthy but `POST /comment` timed out
  repeatedly (server accepts the body, never responds — looks like a
  stuck write lock; no comment landed). Final summary is in this file.

## lessons:
- tools.d/devin.py was absorbed into backend/tool_runner/devin.py
  (mixin split) — grep the tool_runner dir when a card names an old
  tools.d path.
- Board POST /comment can accept the write then hang the response —
  retrying on curl timeout posts duplicates (this session left 6).
  Fire once, then verify via GET /cards comms instead of retrying.
- All devin_dispatch ssh verbs share one HOST constant + _run() —
  target changes are a one-line default flip.
