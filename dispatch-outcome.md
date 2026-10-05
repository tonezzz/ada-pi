# Dispatch outcome — tools-merge-cms (20261005-100603)

Merged the cms tool group 7 → 3 per the card's self-contained spec.
`cms_publish_page` is unchanged (the big writer); six names absorbed
into two new canonical tools via `tool_runner._ALIASES` — the repo's
established hidden-alias mechanism (registered in the runner, absent
from declarations; there is no literal `x-legacy` field — this IS the
x-legacy semantics).

## Alias map (all verified by tests)

| absorbed name      | resolves to                                   |
|--------------------|-----------------------------------------------|
| `cms_list_pages`   | `cms_read` action=`list`                       |
| `cms_get_page`     | `cms_read` action=`get` (slug→key)             |
| `cms_verify_page`  | `cms_read` action=`verify` (slug→key)          |
| `cms_note_update`  | `cms_edit` action=`note`                       |
| `cms_delete_page`  | `cms_edit` action=`delete`                     |
| `cms_automation`   | `cms_edit` action=`automate`, op=`<action>`    |

## What changed

- `backend/tool_runner.py`
  - `_ALIASES` + `_ALIAS_ARG_DEFAULTS`: the six rows above.
  - `_alias_call_args`: `cms_get_page`/`cms_verify_page` map `slug`→`key`;
    `cms_automation` moves the caller's `action` to `op` and re-stamps
    `action="automate"` (implied defaults merge under caller args, so
    without the shim the caller's action would shadow the canonical's).
  - `cms_read(action=get|list|verify, key=, slug=, lang=, limit=)` and
    `cms_edit(action=note|delete|automate, slug, note, summary, lang, op,
    + automation knobs)` — per-action dispatch onto the unchanged
    absorbed methods.
  - Pre-existing private `cms_edit` (page/section ops for the PWA edit
    drawer) renamed `_cms_edit_sections`; `pwa_server.py` call updated.
  - `CMS_WRITE_TOOLS` = {`cms_publish_page`, `cms_edit`} — canonical holds
    the seat. `_check_cms_write_allowed` keys the per-action split off
    args: `note` and `automate` op=list|get stay free (pre-merge parity:
    cms_note_update ungated, cms_automation reads free); `delete` and
    automate writes keep confirmation; `cms_publish_page` keeps its
    pending-request handshake. `cms_edit` action=`note` carved out of
    the secondary-speaker block (cms_note_update was never blocked).
    `DEVIN_CONFIRMED_TOOLS`/confirm_strip untouched — gates key off the
    resolved name, so `_CONFIRM_GATED_TOOLS` covers `cms_edit` via
    `CMS_WRITE_TOOLS` automatically.
  - `_CHANGE_LOG_TOOLS`: absorbed names → `cms_edit` (same coverage).

- `backend/realtime_provider.py`
  - Six declarations removed; `cms_read` + `cms_edit` declared with
    explicit `action` enums (Gemini validates for free); descriptions
    name the absorbed tools for legacy phrasing.
  - `CMS_TOOLS` → {`cms_publish_page`, `cms_read`, `cms_edit`};
    `CMS_INSTRUCTIONS` + one memory paragraph rewritten for canonical
    names.

- `docs/ssot/ssot.tool-surface.yml` — cms family split into `cms_read` /
  `cms_edit` sub-families (memory-merge precedent); spec doc table
  updated to the landed 7→3 design.
- `tests/benchmark.yml` — `write_tools` cms_delete_page → cms_edit;
  `tool_merge_cms` added to `write_allowed_in`.
- `tests/scenarios-live/tool_merge_cms.yaml` — family regression
  scenario (lint requires it once the merge starts).
- `tests/test_tool_runner.py` — `CmsMergeAliasTests` (8 tests): every
  absorbed name routes to its parent, slug→key and action→op shims,
  gate parity (note ungated, delete/automate-writes confirmed).
- `tests/test_tool_audit.py` — declared-count floor was a fixed `>= 100`
  that every merge card trips (the program ratchets DOWN toward target);
  now compares against `surface.target` from the SSOT.
- `.gitignore` — ignore `.teststubs/` (local import stubs for hosts
  without google-genai; needed to run the suite).

## Verify

- `python3 scripts/tool-lint.py` → clean: 94 declared (98 on HEAD −6 +2),
  15 aliases, 0 violations.
- `PYTHONPATH=.teststubs python3 -m unittest tests.test_tool_runner` →
  66 tests; only the 3 pre-existing CMS publish mock-drift failures
  (identical on HEAD — verified via stash).
- Full suite: 478 tests, 5F/16E — identical failure set to HEAD
  (environmental: mddb/hailo absent + the publish drift). The stale
  `>=100` lint-floor failure that was already red on HEAD now passes.

## Notes

- `tools-merge-gate.py` still needs a passing `tool_merge_cms` run in
  `ada-ha-scenario-reports` before the card can close — scenario file
  ships here; the live run is idc02's lane.
- Not pushed/deployed (dispatch rules).
