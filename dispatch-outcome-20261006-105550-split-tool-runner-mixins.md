# Dispatch outcome — tool-runner-split (20261006-105550)

Split `backend/tool_runner.py` (7,066 lines, 209 class members) into a
`backend/tool_runner/` package of domain mixins. `ToolRunner` composes
them via multiple inheritance; **zero behavior change** — every member
is byte-identical except five deliberate micro-edits listed below.

## Layout

| file | class | contents |
|---|---|---|
| `common.py` (1047) | — | module prelude verbatim: imports, constants, contextvars, `_ALIASES`/`_ALIAS_ARG_DEFAULTS`, helpers, `ToolContext`, `AdaMemoryStore`; ends with computed `__all__` |
| `__init__.py` (991) | `ToolRunner` | `from .common import *` + `class ToolRunner(*Mixins)` holding `__init__`, identity/speaker/owner context, `execute()`/`_execute_gated`, all `_*_confirmed`/`_check_*` gate machinery |
| `screens.py` (1608) | `ScreensMixin` | yt/cctv/cast/vcast: `yt*`, `cctv_wall`, `vcast_*`, `cast_to_screen`, `ada_camera_snapshot`, helpers |
| `cms.py` (950) | `CmsMixin` | `cms_read`/`cms_publish_page`/`cms_edit`/`cms_verify_page` + `_cms_*` + automation registry |
| `memory.py` (561) | `MemoryMixin` | `ada_memory_search`, `ada_remember`, `ada_persona`, `ada_forget`, `ada_session_recall`, vocab, `ada_enroll_speaker`, `guest_*` |
| `devin.py` (487) | `DevinMixin` | `devin`, `devin_read`, dispatch/status/followup/answer, job ledger, `ada_devteam_review` |
| `ha.py` (469) | `HaMixin` | home state/search/control/history, sensors, confidence, habit |
| `docs_drive.py` (292) | `DocsDriveMixin` | `docs`, `ada_doc_*`, `drive`, `drive_*`, `photos_*`, doc/drive confirm gates |
| `chat.py` (246) | `ChatMixin` | `chat_send` + queue/photo/doc/run helpers |
| `calendar.py` (229) | `CalendarMixin` | `calendar_*`, `tasks*`, `plan_day`, `ada_resolve_action` |
| `meta.py` (143) | `MetaMixin` | `ada_usage_summary`, `ada_daily_summary`, `ada_weekly_comparison`, `ada_outcome`, `ada_ops` |
| `gev.py` (131) | `GevMixin` | `gev_command`, `_gev_tour_card`, `_TOURS_PATH` |
| `web.py` (116) | `WebMixin` | `web_search` + gemini/ddg impls |

## Why this shape

- `common.py` ends with `__all__ = [n for n in dir() if not
  n.startswith('__')]` and every mixin + `__init__` does
  `from .common import *` — each module's global namespace is identical
  to the old single file, so `from backend.tool_runner import X`,
  `patch("backend.tool_runner.doc_archive_client.*")`,
  `patch("backend.tool_runner.devin_dispatch_mod.*")`,
  `patch("backend.tool_runner.tools_loader.registry")`, and
  `patch("backend.tool_runner.CONFIRM_TOKEN_TTL_S", -1)` (read by the
  confirm gates, which stayed in `__init__.py`) all keep working.
- `scripts/tool-lint.py`: `runner_src()` returns the package dir (or the
  legacy file); `runner_tables`/`runner_methods` walk all `*.py` in it;
  `runner_methods` collects public async seats from `ToolRunner` plus
  transitive package-internal base classes. Mini-repo tests still pass
  a single file. Impl errors now print `file:line`.

## Deliberate micro-edits (resolved value/behavior identical)

- `common.py::_GUIDE_PATH`, `gev.py::_TOURS_PATH` → `.parent.parent`
  (package files sit one level below `backend/`).
- `screens.py::_vms_publish/_yt_publish/_cctv_grab` gained
  `from . import ToolRunner` (deferred import — a module-level import
  would be circular; keeps `patch.object(ToolRunner, "_vcast_api")`
  interception working).

## Verification

- `python3 scripts/tool-lint.py` — clean, **38 declared / 90 aliases**,
  identical to pre-split baseline.
- `.venv/bin/python -m pytest tests/ -q` — **679 passed, 18 subtests**
  (borrowed the main checkout's `.venv`; this worktree has none).
- AST equivalence check: all 209 members byte-identical except the five
  edits above; module-level name surface identical (`ToolRunner` lives
  in `__init__`).
- Dispatch smoke: all 36 runner-seat declared tools + 90 alias targets
  resolve to async methods on the composed class.

## Not run here

`scenario-live` smoke needs a live Ada backend + MDDB — this dispatch
host has neither (tony-omen; ada serves on idc03). Run
`scripts/ci-scenarios.sh smoke` post-merge where the backend lives.
Trail doc: `docs/ssot/jobs/ada/2026-10-06-tool-runner-split.yml`.
