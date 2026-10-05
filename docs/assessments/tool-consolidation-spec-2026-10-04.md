# Tool consolidation spec — 2026-10-04

Status: **audit-contract landed** (tools-ci-audit). Merge implementation pending
(tools-merge-* cards). This document was reconstructed from the
tool-consolidation-design / tool-surface-census card notes — the original
draft was closed without a committed file, so this is the reference copy.

## Why

The model-facing tool surface is **103 builtin declarations**
(`realtime_provider.function_declarations` + CHABA/VMS/TRAFFIC) plus
drop-in `tools.d` entries — 106 total at census. Every session pays the
schema cost in Live context, tool-selection accuracy degrades as
near-duplicate names accumulate (three ways to snapshot a camera, four
ways to read HA state), and the 2026-10-01 tool-storm showed what an
overloaded surface costs.

Target: **~38 canonical tools**. Consolidation pattern: one tool per
domain carrying an `action` param instead of N sibling names.

## Merge families

Six cards (`tools-merge-*`), one family each. Canonical names and the
retired ("absorbed") names are declared in
`docs/ssot/ssot.tool-surface.yml` — that file is the machine-readable
half of this spec; `scripts/tool-lint.py` fails when the code and the
SSOT disagree.

| family  | merge card            | canonical    | absorbed (→ `action=`/`source=`) |
|---------|-----------------------|--------------|------------------------|
| camera  | tools-merge-camera    | `ada_camera_snapshot` (`source=`) | cctv_snapshot, traffic_camera, capture_frame (→ `vcast_snapshot`). cctv_wall stays — a wall is a live grid, not a frame |
| cms     | tools-merge-cms       | `cms_publish_page` (unchanged), `cms_read` (`action=get\|list\|verify`), `cms_edit` (`action=note\|delete\|automate`, `op=` carries the registry op) | cms_list_pages, cms_get_page, cms_verify_page → `cms_read`; cms_note_update, cms_delete_page, cms_automation → `cms_edit`. Landed 7→3 instead of the original single-`cms_page` design |
| calendar+plan | tools-merge-calendar-plan | `calendar_read` (`action=events\|calendars\|freebusy`), `calendar_write` (`action=create\|delete\|shift`, confirmed-gated), `plan_day` (`period=today\|tomorrow\|week`, `day=` overrides the target) | calendar_list_events, calendar_list_calendars, calendar_freebusy → `calendar_read`; calendar_create_event, calendar_delete_event, calendar_shift_overdue → `calendar_write`; ada_daily_summary → `plan_day` (alias-internal `period=digest`); ada_weekly_comparison → `plan_day` (`period=week`, `end`→`day`) |
| devin   | tools-merge-devin-mcp | `devin`       | devin_dispatch, devin_status, devin_followup, devin_job_report, devin_pending, devin_jobs, devin_answer |
| display | tools-merge-display   | `ada_display` | cast_to_screen, vcast_list, vcast_say, vcast_snapshot, vcast_gesture, yt_cast, yt_cast_status, yt_cast_stop |
| ha      | tools-merge-ha        | `ada_ha`      | control_entity, control_cover, press_button, control_media_player, list_home_devices, search_home_devices, get_home_state, list_sensors, search_sensors, get_logbook, get_recent_events, get_entity_events, ada_ha_* (7 — ada_ha_recall moved to the memory family) |
| memory  | tools-merge-memory    | `ada_memory_search`, `ada_remember`, `ada_session_recall` (+ `ada_forget` unchanged) | guest_recall, vocab_note, report_habit_observation, guest_remember, guest_remember_private, ada_ha_recall (→ `scope=`/`kind=`) |
| meta/voice | tools-merge-meta-voice | `ada_persona` (adds `set_voice`/`show_voice`/`list_voices`), `ada_ops` (`action=outcome\|usage\|health\|check\|research`), `ada_enroll_speaker` (`who=speaker\|guest`) | ada_set_voice → `ada_persona` (`action=set\|show\|list` → `*_voice` via shim); ada_outcome, ada_usage_summary, ada_mddb_health, ada_decision_check, ada_deep_research → `ada_ops` (`action=`); guest_register → `ada_enroll_speaker` (`who=guest`). outcome keeps the memory-write gate seat; check/research stay provider-dispatched. Landed 8→3 |

Roughly 56 absorbed names retire into 6 canonical tools; with the
un-merged remainder the surface lands near the ~38 target.

## `action=` param pattern

Canonical tools take a required `action` enum whose values map 1:1 onto
the absorbed tools' behaviors (`cms_page action=publish` ≡
`cms_publish_page`). Rules:

- One method on `ToolRunner` per canonical tool; absorbed-name logic
  moves into per-action branches — gates and helpers are reused, not
  duplicated.
- The `action` enum must be explicit in the declaration schema
  (`enum: [...]`), not freeform — Gemini Live validates it for free.
- Descriptions name the absorbed operations ("…also handles what used
  to be cctv_snapshot") so the model still routes legacy phrasing.

## Alias plan — `tool_runner._ALIASES`

Merged names stay callable as **soft aliases**: `execute()` resolves an
absorbed name to the canonical tool before dispatch, mapping
`surrogate args → action=`. The table lives at
`backend/tool_runner.py::_ALIASES`:

```python
_ALIASES: dict[str, str] = {
    "cctv_snapshot": "ada_camera",   # …one row per absorbed name
}
```

Contract (enforced by `scripts/tool-lint.py`):

- Alias **key** must never be a declared tool name — a soft alias is
  off-surface by definition.
- Alias **value** must be a declared tool (the family's canonical).
- Every name in a family's `absorbed` list that is no longer declared
  MUST have an `_ALIASES` row — retirement without an alias is a lint
  failure.
- No alias chains, no stray aliases not in the spec's absorbed lists.
- Alias hits are logged (`tool alias <old> -> <new>`) and counted by
  `scripts/tool-usage-report.py` — when an alias's call count decays to
  ~0 for weeks, the row is a candidate for removal.

Arg-level mapping (e.g. absorbed `cctv_snapshot {ch}` →
`ada_camera {action:"snapshot", ch}`) is handled inside `execute()`'s
alias shim per family; keep it explicit, not generic.

## Gates preserved

Consolidation must not dilute the gate sets in `tool_runner.py`
(`CONTROL_TOOLS`, `MEMORY_WRITE_TOOLS`, `CALENDAR_WRITE_TOOLS`,
`CMS_WRITE_TOOLS`, `DEVIN_CONFIRMED_TOOLS`, `DOC_TOOLS`, `DRIVE_TOOLS`,
`CAPTURE_CONFIRMED_TOOLS`, `SECONDARY_BLOCKED_TOOLS`). When an absorbed
name leaves a set, its **canonical** name enters the same set — e.g.
`ada_camera` joins `CONTROL_TOOLS`/`CAPTURE_CONFIRMED_TOOLS` when the
cctv names retire. `tool-lint.py` fails on any gate member that
resolves to nothing (undeclared and un-aliased) — the "gates preserved"
property is checked, not asserted. The same check runs on
`tests/benchmark.yml policy.write_tools`.

## Self-auditing machinery (this is the tools-ci-audit card)

1. **`scripts/tool-lint.py`** — offline invariant check, runs in the
   unittest suite (`tests/test_tool_audit.py`) and is the audit-stage
   hook for chaba's `ssot-validate-all.mjs` / CI runner:
   `python3 scripts/tool-lint.py` (exit 1 on violation, `--json` for
   tooling). Fails on: declared count > `count_cap`, absorbed-but-missing
   alias, alias pointing at undeclared canonical, gate member resolving
   to nothing, declared tool with no scenario reference that isn't in
   `coverage_debt`, merged family without its scenario file.
2. **`scripts/tool-usage-report.py` + `ada-tool-usage.timer`** — journal
   scan of `tool <name> args=` and `tool alias` lines → publishes CMS
   page `report/tool-usage` (ada-cms-pages). Keeps the census live:
   per-tool 24h/7d counts, zero-use list, unknown-name calls,
   alias-hit decay. Runs daily on idc01 (systemd user timer).
3. **`scripts/tools-merge-gate.py`** — merge-card close gate:
   a `tools-merge-*` card may not sit in `done`/`closed` unless its
   family's scenario file exists AND the newest
   `ada-ha-scenario-reports` entry for it is pass|flaky. Chaba's
   board-api `/action close` and `ssot-validate-all.mjs` are the
   enforcement call-sites (they live in the chaba repo — this repo ships
   the checker and its contract).

## Out of scope here

- The merges themselves (`tools-merge-*` cards).
- The regression scenarios themselves (`tools-regression-scenarios`;
  families name theirs in the SSOT file).
- Chaba-side wiring (board-api close hook, `ssot-validate-all.mjs`
  audit stage invocation) — declared in
  `docs/ssot/jobs/ada/2026-10-04-tools-ci-audit.yml` for that repo's
  owner to consume.
