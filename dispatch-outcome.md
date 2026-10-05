# Dispatch outcome — tools-merge-tasks-status (10 -> 3)

Task: merge the tasks+status tool group per the consolidation spec —
canonical `tasks`, `home_status`, and an expanded `chat_send` absorb
sixteen legacy names, which stay callable but hidden via
`tool_runner._ALIASES`. Card `tools-merge-tasks-status` (chaba kanban).

## What changed

- `backend/tool_runner.py`
  - `_ALIASES` + `_ALIAS_ARG_DEFAULTS`: 16 new rows —
    `tasks_add|list|complete|move` → `tasks(action=add|list|done|move)`;
    `get_battery_status|get_battery_detail|get_power_summary|
    get_inverter_status|get_pool_status|get_dashboard_tab|
    get_habit_status` → `home_status(what=battery[|battery_index=1]|
    power|inverter|pool|dashboard|habit)`;
    `photos_pick|photos_picked` → `chat_send(photo=pick|picked)`;
    `sys_show_uploaded_document|process_document_upload|
    doc_upload_card_action` → `chat_send(doc=show|process|card)`.
  - `_alias_call_args`: doc-card shim maps its own `action`→`op` and
    `intake_key`→`key` so the implied `doc=` slot stays a sub-action.
  - Gates: `tasks` takes the `CALENDAR_WRITE_TOOLS` seat with a
    per-action early return — `action=list` (absorbed `tasks_list`) was
    never confirm-gated; add/done/move still require `confirmed=true`
    and respect READ_ONLY. `chat_send` joined `DRIVE_TOOLS` with
    per-flow carve-outs: only `photo=`/`doc=` calls keep the owner-tier
    documents-bank + secondary-speaker gates the absorbed `photos_*`
    names had; a plain text send was never bank-scoped.
    `DEVIN_CONFIRMED_TOOLS` and `_CONFIRM_GATED_TOOLS`/confirm_strip
    internals untouched — all gate checks key off the resolved
    canonical name (alias resolution happens first in `execute()`).
  - Canonical methods: `tasks()` dispatches per-action onto the
    unchanged `tasks_*` methods; `home_status()` does the same for the
    seven status getters; `chat_send()` gained `photo=` and `doc=`
    sub-flows (`_chat_send_photo` reuses `photos_pick`/`photos_picked`;
    `_chat_send_doc` reads the document_check held-intake store and
    `doc=card, op=archive` re-dispatches `ada_doc_archive` through
    `execute()` so its confirm gate still applies).
    `photos_pick`'s stale "then call photos_picked" hint now points at
    `chat_send photo='picked'`.
- `backend/document_check.py` — `engine.latest()` returns the newest
  held intake (FIFO ordered) so `chat_send doc='show'` works without a
  key. Held store is RAM-only; expiry message says to upload again.
- `backend/realtime_provider.py`
  - 13 declarations removed (4 `tasks_*` + 7 status + 2 `photos_*);
    `tasks` and `home_status` declared with explicit `action`/`what`
    enums; `chat_send` schema extended (`photo`, `session_id`,
    `screen`, `show`, `doc`, `key`, `op`). Declared surface 88 → 77.
  - Dispatch: the seven inline HA-client branches collapsed into one
    `home_status` branch keyed on `what=`; `what='habit'` still reads
    `habit_state_getter`. `FunctionResponse` keeps echoing the
    as-called name (alias included) — unchanged.
  - Alias resolution now also applies `_alias_call_args` on the merged
    args at resolve time — the provider rewrites `call.name` to the
    canonical tool before `runner.execute`, so the runner would never
    see the as-called alias and surrogate remaps (slug→key,
    end→day, action→op) were previously skipped on this path.
  - Group sets: `CALENDAR_TOOLS`, `HABIT_TOOLS`, `DRIVE_TOOLS`,
    `CHABA_ALLOW`, `ACTUATING_TOOLS` carry canonical names;
    `home_status` takes `get_habit_status`'s habit-exclusion seat (no
    live config uses `ADA_EXCLUDED_TOOLS` for it — noted in comment).
    Instructions rewritten for canonical names; DRIVE_INSTRUCTIONS
    steers "show my photos" to `chat_send photo='pick'`.
- `backend/home_assistant.py` — battery-status result hint now points
  at `home_status what='battery' battery_index=1|2|3`.
- `scripts/scenario-live.py` — `LEGACY_TOOL_ALIASES` resynced with the
  full `_ALIASES` table (+16 this card, +8 calendar-plan rows that card
  missed).
- `docs/ssot/ssot.tool-surface.yml` — `tasks`, `home_status`,
  `chat_send` merge families added; the 11 retired names pruned from
  `coverage_debt`.
- `docs/assessments/tool-consolidation-spec-2026-10-04.md` — family
  table gained the tasks+status row.
- `tests/scenarios-live/tool_merge_tasks_status.yaml` — live scenario:
  alias calls for `tasks_list`, `get_battery_status`,
  `get_power_summary`, `photos_pick`; canonical `tasks(action='add')`
  confirm-gate check; `chat_send` still declared/callable.
- `tests/test_tool_runner.py` — `TasksStatusMergeAliasTests`, 13 tests:
  every absorbed name routes to its parent, task writes stay
  confirm-gated while `list` is free, unknown `action`/`what` rejected,
  doc `show`/`process`/`card` flows against a held intake, card
  `archive` re-dispatch still hits `ada_doc_archive`'s confirm gate,
  `print` on an unarchived key declines with guidance.
- `tests/test_provider_events.py` — canonical `home_status(what='habit')`
  test alongside the retained legacy-alias echo test, plus a
  `doc_upload_card_action` provider-path test proving the surrogate-arg
  remap (action→op, intake_key→key) applies before dispatch while the
  FunctionResponse still echoes the as-called name.
- `tests/test_frontend_contract.py` — declaration assertion moved to
  `home_status`.
- `docs/ssot/jobs/ada/2026-10-05-tools-merge-tasks-status.yml` — job
  trail.

All seven absorbed names stay registered as hidden aliases in
`tool_runner._ALIASES` + `_ALIAS_ARG_DEFAULTS` (30 aliases total now) and are
removed from the declared model-facing surface (5 builtin declarations + the
chaba `guest_register` declaration removed; `ada_ops` declared; persona/enroll
declarations extended). `ada_set_voice`'s `action=set|show|list` collides with
persona's own actions, so `_alias_call_args` remaps them onto the `*_voice`
forms.

- Canonical name is `home_status`, not `status`: the card's
  "NEW home_status" wording follows the calendar card's "NEW X"
  convention (`cal(...)` shorthand landed as `calendar_read`/`write`).
- The three `*_document_upload`/doc-card names are chaba-side — never
  declared in this repo — so they're registered as forward aliases into
  `chat_send`. ada-pi has no upload pipeline; uploads enter via
  `POST /api/documents/intake` into the RAM held store, which `doc=show`
  and `doc=process` now surface (with `/api/documents/{key}/preview`
  and `/pdf` links) before archive.
- `op='print'` on a held doc declines honestly — `ada_doc_print` needs
  an archived `slug`; the error says archive first, then print.
- `benchmark.yml` unchanged: `tasks`/`chat_send` are mixed-use tools
  (list/plain send are free), so name-based `write_tools` would
  false-positive; server-side confirm gates cover the write actions.

## Files changed

```
python3 scripts/tool-lint.py --verbose   # exit 0 — 77 declared
                                         # (88→77: -13 absorbed, +2 new),
                                         # 39 aliases, 56 covered, 21 debt,
                                         # 12 pre-existing warnings, 0 failures
PYTHONPATH=.teststubs ADA_INSTANCE_ID=test \
  python3 -m unittest tests.test_tool_runner tests.test_provider_events \
    tests.test_frontend_contract tests.test_tools_loader \
    tests.test_tool_audit tests.test_doc_archive_tool   # 165 ok
python3 -m unittest discover -s tests                  # 541 tests: 2F + 3E
```

Full-suite remainder is **baseline, not regression** — same signature
documented on the calendar-plan card (`2026-10-05-tools-merge-calendar
-plan.yml`, verified on clean HEAD there):

- `test_detection`, `test_hailo_vision`, `test_pose` —
  `ai_edge_litert`/Hailo runtime not installed on this host.
- `test_memory_lifecycle`, `test_michael_technician` — FakeMddb lacks
  `is_ops_routed`; environment/baseline.

`.teststubs/` provides the `google.genai` shim for this SDK-less host
(gitignored, recreated this session).

## Not done / followups

- Card has no `pipeline:` field — no card-pipeline CI run.
- `latest ada-ha-scenario-reports` entry for the merge gate is owned by
  the scenario runner on idc02, not this worktree.
- No commit/push/deploy (per dispatch rails). Branch:
  `dispatch/20261005-183350-merge-the-tasks-status-group-1`.
