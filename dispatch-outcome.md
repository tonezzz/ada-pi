# Dispatch outcome — tools-merge-display (2026-10-05)

## Result

Done — the display tool group merged per the card spec. Declared surface
dropped 88 → 86 (vcast_list + vcast_say declarations removed; vcast_status
and vcast_shortcut were census-only journal names, never declared here).
Alias table now carries 27 retirees; 5 absorbed display names route to
their parents.

## What changed

- `backend/tool_runner.py`
  - `_ALIASES`: vcast_say/vcast_list/vcast_status/vcast_shortcut →
    cast_to_screen (capture_frame → vcast_snapshot already existed).
  - `_ALIAS_ARG_DEFAULTS`: each absorbed name implies its `action=` seat
    (say|list|status|shortcut).
  - `_alias_call_args`: vcast_shortcut's historical arg spellings
    (name/app/shortcut) funnel into `url`.
  - `cast_to_screen`: `screen` now optional; new actions —
    `list` → vcast_list body, `status` → new `_vcast_display_status`
    (per-screen filter of the list report), `say` → absorbed vcast_say
    body, `shortcut` → `_cast_shortcut_url` (app short name →
    `/apps/<name>/`, URLs/paths pass through), `cast` → probes the URL
    via `_frame_check` and auto-routes image/audio/play/nav (reports
    `action_routed`). The busy-screen interrupt gate now covers `cast`
    (pre-routing).
  - Gates: `cast_ungated` carve-out keeps action=list|status|say free of
    the secondary-speaker block and CONTROL_TOOLS gate — the absorbed
    seats were never gated; screen-ownership ACL still runs inside the
    method. `DEVIN_CONFIRMED_TOOLS`, `CAPTURE_CONFIRMED_TOOLS`
    (uplink-only) and confirm-strip unchanged — all key off the resolved
    canonical name.
  - `_CAPTURE_AWARE_TOOLS` uses canonical names only.
  - Model-facing error strings repointed to
    `cast_to_screen(action='list'|'say')`.
- `backend/realtime_provider.py`
  - `vcast_list` + `vcast_say` declarations removed; `cast_to_screen`
    rewritten with an explicit action enum (15 values incl. the five
    merged seats), `text` param, and naming of the absorbed operations
    (spec rule: descriptions keep legacy phrasing routable).
  - `ACTUATING_TOOLS`: vcast_say dropped (canonical holds the seat);
    action=list|status carve out of the per-turn actuation count.
  - FunctionResponse still echoes the as-called name (alias contract).
- `scripts/scenario-live.py`
  - `LEGACY_TOOL_ALIASES` + display rows and the calendar+plan rows the
    earlier card missed; `_expand_families` now alias-expands `@family`
    members (`@verify` carries vcast_list); `call_args_contain` accepts
    the canonical call only when it carries the implied seat args
    (new `LEGACY_ARG_DEFAULTS` mirror — a vcast_say check matches
    cast_to_screen(action='say'), not any cast).
- `docs/ssot/ssot.tool-surface.yml` — display family rewritten as two
  canonical seats (cast_to_screen + vcast_snapshot); capture_frame moved
  out of the camera family into vcast_snapshot's absorbed list;
  vcast_gesture documented as unchanged.
- `docs/assessments/tool-consolidation-spec-2026-10-04.md` — display row
  updated to the landed shape (supersedes the sketched `ada_display`).
- `tests/scenarios-live/tool_merge_display.yaml` — smoke-tier merge
  regression (list / say / status intents → cast_to_screen; gesture →
  vcast_gesture).
- `tests/test_tool_runner.py` — `DisplayMergeAliasTests`, 13 tests.
- `tests/benchmark.yml` — `write_allowed_in` += tool_merge_display,
  vcast_list (absorbed read/narrate calls now record as cast_to_screen,
  a write_tools member — without the exemption the suites would flag
  policy violations).
- `docs/ssot/jobs/ada/2026-10-05-tools-merge-display.yml` — job record.

## Verify

```
python3 scripts/tool-lint.py        # clean: 86 declared, 27 aliases
ADA_INSTANCE_ID=test PYTHONPATH=<genai deps> \
  python3 -m unittest tests.test_tool_audit tests.test_tool_runner
                                    # 109 tests, all pass (13 new)
```

Full sweep: 523 tests — the 5 failures are identical on clean HEAD
(verified via stash): memory-lifecycle/michael-technician scenario
fixtures need live mddb data; pose/hailo need `ai_edge_litert`. No merge
regressions.

## Not done here (by design / needs live stack)

- Live `tool_merge_display` run — the ada-ha-scenario-reports entry
  tools-merge-gate wants is produced by the first suite run after deploy.
- No commit/push/deploy — dispatch worktree only.
