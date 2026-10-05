# Dispatch outcome — playbook registry (ada-devteam-pipeline)

Task: close Ada's self-development loop — `devin_dispatch` gains
`playbook` + `params`; a YAML registry gates which playbooks may run,
and `build-tool` refuses unless a devteam-reviewed spec doc is
`status=pass` in the `ada-ha-bank-devin-handoff` bank.

## What changed

- `backend/playbooks/build-tool.yml`, `fix-scenario.yml`,
  `investigate.yml` — registry. Each declares `description`, `params`
  (required + optional `line:` sub-templates for clean optional text),
  `task_template` (`{param}` / `{param_line}` placeholders), `gate
  {confirm, identity}`, `memory_domains`, `verify` commands,
  `report_to`, `max_runtime_min`. `build-tool` adds
  `requires.spec_status: pass`.
- `backend/devin_dispatch.py` — playbook section: `list_playbooks`,
  `load_playbook` (full schema validation, template fields checked
  against declared params at load), `_resolve_params`, `_render_task`
  (template + contract trailer: playbook name, verify list, report_to,
  memory_domains, timebox, plus the caller's `task` as Request context),
  `_enforce_gate` (identity gate + spec-status lookup; **fails closed**
  when mddb is unavailable), `_stamp_contract` (writes
  `job/<task_id>/contract`, kind=job-contract, carrying playbook +
  verify for devin-dispatch-watch). `dispatch()` gained `playbook`,
  `params`, `mddb`, `caller`, `is_owner` kwargs — plain
  `dispatch(repo, task)` behavior unchanged; rendered tasks still run
  through the existing in-process + remote dedup.
- `backend/tool_runner.py` — `devin_dispatch()` signature gained
  `playbook`/`params` and passes `policy_identity()` +
  `_persona_admin()` through as `caller`/`is_owner`. Confirm-gate
  internals untouched (`DEVIN_CONFIRMED_TOOLS`, `_check_devin_confirmed`
  unchanged — `gate.confirm` stays declarative).
- `backend/realtime_provider.py` — `devin_dispatch` declaration gained
  the `playbook` enum + `params` object; `DEVIN_INSTRUCTIONS` tells the
  model to offer `ada_devteam_review` when the gate refuses.
- `tests/test_devin_dispatch.py` — +19 tests (`PlaybookRegistryTests`,
  `PlaybookDispatchTests`): registry loading/validation, all three
  playbooks, template rendering, owner/identified gates, spec missing /
  wrong kind / blocked / pass, bare-slug `spec/` prefix resolution,
  fail-closed without mddb, contract stamp meta, dedup on the rendered
  task, nonfatal stamp failure, unknown playbook/param/missing required
  refusals. `_FakeMddb` + mocked `_run` — no ssh, no network.
- `tests/scenarios-live/devin_build_tool_gate.yaml` — tier:full, two
  turns (confirm gate, then confirmed call hitting the playbook gate)
  exercising the real blocked spec `spec/20260930-125800-a-tool-that
  -reports-how`. Asserts `devin_dispatch` is called, the refusal surfaces,
  and the response steers to review.
- `docs/ada-tool-dev.md` — new "Playbooks" section + updated ship flow.
- `docs/ssot/jobs/ada/2026-10-05-playbook-registry.yml` — job trail.
- Chaba SSOT: `docs/ssot/apps/ssot.apps.ada_pi.yml` playbook-registry
  line delivered as **tonezzz/chaba PR #35** (branch
  `dispatch/playbook-registry-ssot`) — the worktree has no chaba
  checkout, so the SSOT line went through the GitHub API as a PR for
  review/merge.

## Design decisions

- Gate results return `{ok: false, gate, offer: "ada_devteam_review"}`
  so the model steers to the review pipeline instead of dispatching raw
  — that is the documented refusal contract, not a crash.
- The job stamp is a companion doc `job/<task_id>/contract`
  (kind=job-contract), not meta on `job/<id>`: devin-dispatch-watch's
  `mddb_job` rewrites the job doc's meta wholesale on every transition,
  which would drop the verify list exactly when needed. The `job/`
  prefix keeps it out of the handoff inbox; `kind != job` keeps it out
  of job-report feeds.

## Verification

```
PYTHONPATH=.teststubs python3 -m unittest tests.test_devin_dispatch   # 26 ok
PYTHONPATH=.teststubs python3 -m unittest tests.test_devin_dispatch tests.test_devteam tests.test_tools_loader tests.test_tool_lint  # 116 ok
python3 scripts/tool-lint.py                                        # clean, 12 pre-existing warnings
python3 -m unittest discover -s tests                               # 528 tests: 2F + 3E
```

Full-suite remainder is **baseline, not regression** (verified by
stashing and rerunning on clean HEAD):

- `test_detection`, `test_hailo_vision`, `test_pose` — `ai_edge_litert`
  not installed on this host.
- `test_memory_lifecycle`, `test_michael_technician` — FakeMddb search
  returns no `count`/hits on clean HEAD too (environment/baseline).

`.teststubs/` provides the `google.genai` shim for this SDK-less host
(already gitignored; recreated this session — dynamic stub types plus
`Part.from_bytes`/`from_text`).

## Not done / followups

- chaba `devin-dispatch-watch` does not yet consume the contract doc —
  follow-up card noted in the job SSOT.
- chaba PR #35 awaits review/merge.
- No deploy (per card rails); branch is
  `dispatch/20261005-173154-build-the-playbook-registry-th`, not pushed.
