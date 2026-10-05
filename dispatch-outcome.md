# Dispatch outcome — tools-merge-camera (20261005-004926)

Merged the camera tool group 5 → 2 per the consolidation spec. Branch
`dispatch/20261005-004926-merge-the-camera-tool-group-5-`, ada-pi
worktree. No commit, no push, no deploy.

## What changed

**Canonical surface (2 tools):** `ada_camera_snapshot` (all single
frames — `source=auto|vms|traffic`, `view=`, `screen=`/`target=`) and
`vcast_snapshot` (display self-capture). `cctv_wall` deliberately
unchanged — a wall is a live grid, not a frame.

**3 absorbed names → `_ALIASES` rows** (callable, hidden from the
declared surface):

- `cctv_snapshot` → `ada_camera_snapshot` `{source: vms, target: tv}`
- `traffic_camera` → `ada_camera_snapshot` `{source: traffic}`
- `capture_frame` → `vcast_snapshot` (the GEV remote command it already
  fires internally — it was never a standalone model declaration)

## Files

- `backend/tool_runner.py` — alias tables populated; runner
  `ada_camera_snapshot` (VMS/go2rtc/traffic + display push) and
  `vcast_snapshot` (snap-request + internal `capture_frame` + frame
  poll) methods; `cctv_snapshot` method retired; `CAPTURE_CONFIRMED_TOOLS`
  seat moved to the canonical name, gated only when `screen=`/`target=`
  pushes to a display — a describe-only snapshot stays open.
- `backend/realtime_provider.py` — merged `CAMERA_DECLARATION`
  (view/source/mode/screen/target + legacy channel/camera + traffic
  query/lat/lon/heading + confirmed) declared unconditionally —
  `source=traffic` needs no VMS shim; `cctv_snapshot` declaration
  removed; dispatch resolves `_resolve_alias` at loop top so legacy
  names land canonical; actuation/capture-gate/`confirm_strip` all key
  off the resolved name; `_camera_snapshot` routes `source`, falls back
  to the runner's `_cctv_grab` chain for home cams/YT the shim doesn't
  know; `_snap_display` pushes via `cast_to_screen`/`tv_action`;
  `_grabbed_result` attaches grabbed frames to the describe contract.
- `docs/ssot/ssot.tool-surface.yml` — camera family: canonical
  `ada_camera_snapshot`, absorbed list per above.
- `docs/assessments/tool-consolidation-spec-2026-10-04.md` — family row
  synced to the shipped shape.
- `tests/scenarios-live/tool_merge_camera.yaml` — **new** family
  regression (tier: full): VMS describe, traffic source, snap-to-screen
  push, `vcast_snapshot` self-capture, teardown.
- `tests/scenarios-live/*.yaml` — 15 files re-pointed at canonical
  names; `tests/benchmark.yml` write_tools canonical swap.
- `docs/ssot/jobs/ada/2026-10-05-tools-merge-camera.yml` — job trail.

## Freshness behavior (gate requirement) — unchanged

`mode='cached'` returns the wall's stored frame instantly with age;
`mode='live'` pulls through the vms-snap shim; live failure falls back
to the stale frame marked `STALE — camera offline`; the frame still
attaches to the result for description. Same code path, same semantics.

## Verification

- `python3 scripts/tool-lint.py` → **clean**: 104 declared (101 builtin
  + 3 tools.d), cap 106, **3 aliases**, 12 warnings (pre-existing
  coverage debt + the intentional `capture_frame`→`vcast_snapshot`
  cross-family note).
- `python3 -m pytest tests/ -q` (throwaway venv with google-genai —
  system python lacks the SDK): **469 passed, 8 failures all
  pre-existing/environmental** — `ai_edge_litert` not installed
  (detection/pose/hailo), live mddb refused (scenario memory tests),
  and 3 CMS tests asserting `add_document.call_args` is the page write
  when HEAD's publish flow already ends with the `reports-index` regen
  (my diff never touches `cms_publish_page`).
- `tests/test_tool_audit.py` + `test_traffic_camera.py` → **20/20 pass**.
- Alias routing verified end-to-end through `ToolRunner.execute`:
  `cctv_snapshot` → VMS path, `traffic_camera` → Longdo/iTIC search,
  `capture_frame` → vcast snap-request flow.

## Open item (expected)

`tools-merge-gate.py --card-id tools-merge-camera` exits 1:
`tool_merge_camera` exists but has no pass/flaky report — MDDB was
unreachable from this box anyway. The scenario is `tier: full` and
needs a real VMS shim + Longdo/iTIC + a real vcast display, i.e. an
idc02 staging run before the card can move past review.
