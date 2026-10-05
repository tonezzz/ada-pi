# Dispatch outcome — tools-merge-devin-mcp (20261005-143702)

Merged the devin tool group 8 → 2 per the card's self-contained spec.
Eight names absorbed into canonical tools via `tool_runner._ALIASES` —
the repo's established hidden-alias mechanism (registered in the runner,
absent from declarations; there is no literal `x-legacy` field — this IS
the x-legacy semantics).

## Alias map (all verified by tests)

| absorbed name        | resolves to                          |
|----------------------|--------------------------------------|
| `devin_dispatch`     | `devin` action=`dispatch`            |
| `devin_followup`     | `devin` action=`followup`            |
| `devin_answer`       | `devin` action=`answer`              |
| `devin_status`       | `devin_read` action=`status`         |
| `devin_jobs`         | `devin_read` action=`jobs`           |
| `devin_pending`      | `devin_read` action=`pending`        |
| `devin_job_report`   | `devin_read` action=`report`         |
| `ada_devteam_review` | `devin_read` action=`review`         |

## What changed

- `backend/tool_runner.py`
  - `_ALIASES` + `_ALIAS_ARG_DEFAULTS`: the eight rows above. No
    `_alias_call_args` shim needed — every absorbed name's args map 1:1.
  - `devin(action, repo, task, task_id, message)` — per-action dispatch
    onto the unchanged absorbed methods; per-action required-arg guards
    (the flat schema can't express per-action `required`).
  - `devin_read(action, task_id, status, limit, publish, confirmed,
    confirm_token, request, title)` — routes the four reads plus the
    ported `ada_devteam_review` pipeline.
  - `ada_devteam_review(request, title)` — the tools.d module's run()
    ported verbatim onto the runner: manifest `owner_only` +
    `secondary_allowed=false` re-implemented as an inline gate
    (secondary turn OR no `full: true` person_policy → PermissionError),
    guest-mode mddb guard added, `timeout_s=300` kept via
    `asyncio.wait_for` in the review branch.
  - `DEVIN_CONFIRMED_TOOLS = {"devin"}` — the canonical name holds the
    seat; every `devin` action is a session write so the whole tool
    stays confirm-gated. Gate keys off the resolved name — alias and
    canonical calls are gated identically; confirm_strip / token
    binding / SECONDARY_BLOCKED (`devin_confirmed` group token) cover it
    unchanged. `devin_read` stays ungated except `report`'s publish
    path, which re-checks confirmation internally (fingerprint now
    under `devin_read`).
  - `_CHANGE_LOG_TOOLS`: `devin_*` quartet → `devin` + `devin_read`,
    with devin_read filtered to `report`/`review` so reads don't spam
    the change feed.

- `backend/realtime_provider.py`
  - Seven declarations removed; `devin` declared with
    `action` enum `dispatch|followup|answer`, `devin_read` with
    `status|jobs|pending|report|review`; descriptions name the absorbed
    tools for legacy phrasing.
  - `DEVIN_TOOLS` → `{"devin", "devin_read"}` (ADA_EXCLUDED_TOOLS strip
    set); `ACTUATING_TOOLS` `devin_dispatch` → `devin`;
    `DEVIN_INSTRUCTIONS` rewritten in action= phrasing.

- `backend/devin_dispatch.py` — model-facing note strings now say
  `devin_read action='status'`.

- `backend/tools.d/manifest.yml` — `ada_devteam_review` entry removed;
  `backend/tools.d/ada_devteam_review.py` deleted (logic lives on the
  runner now).

- `docs/ssot/ssot.tool-surface.yml` — devin family split into `devin` +
  `devin_read` sub-families (cms/calendar precedent);
  `devin_followup`/`devin_job_report` pruned from coverage_debt.
- `docs/assessments/tool-consolidation-spec-2026-10-04.md` — devin row
  updated to the landed 8→2 design.
- `tests/benchmark.yml` — `write_tools` `devin_dispatch` → `devin`.
- `tests/scenarios-live/tool_merge_devin.yaml` — new family regression
  scenario (lint requires it once the merge starts);
  devin_status_accuracy / devin_pending_list / devin_answer_gate /
  shared_memory_audit / tool_selection_accuracy /
  deep_research_tiny_models / devteam_review repointed to canonical
  names.
- `tests/test_tool_runner.py` — `DevinMergeAliasTests` (9 tests): every
  absorbed name routes to its parent, write aliases keep the
  confirmation gate, report publish re-checks on the canonical name,
  review denies without a full policy and runs for a full-policy
  identity.
- `docs/ssot/jobs/ada/2026-10-05-tools-merge-devin-mcp.yml` — this job's
  SSOT trail.
- `.venv-test/` (local only, removed at cleanup) — minimal venv with
  google-genai so the suite runs on this SDK-less host.

- `python3 scripts/tool-lint.py` — clean; 85 declared (was 88),
  27 aliases, 41 warnings (all pre-existing; HEAD had 43).
- `YtMergeAliasTests`: 5/5 pass — every absorbed alias routes to `yt`.
- Full suite: 546 passed, 2 failed — identical to HEAD baseline
  (`test_memory_lifecycle`, `test_michael_technician` need live mddb on
  this host; verified same failures existed before this change).
- `DEVIN_CONFIRMED_TOOLS` / confirm_strip untouched — gates resolve on
  the canonical name; yt family was never confirm-gated.

- `python3 scripts/tool-lint.py` → clean: **82 declared (88 → 82), 31
  aliases (23 → 31)**, 0 violations (12 pre-existing warnings).
- `ADA_INSTANCE_ID=test .venv-test/bin/python -m unittest
  tests.test_tool_runner tests.test_tool_audit tests.test_devteam
  tests.test_devin_dispatch tests.test_tools_loader` → **126 tests, OK**
  (with a bare interpreter the runner/devteam modules need
  `google-genai`, absent here — hence the venv; ADA_INSTANCE_ID is
  required by memory-collection identity codepaths).

## Notes / decisions

- The card's read-side enum listed `status|jobs|pending|report` but
  also assigned `ada_devteam_review` to `devin_read` — `review` is the
  fifth enum value; it runs an LLM panel and files a spec doc, so none
  of the four read verbs describe it. Flagged in card comms.
- Deploy caveat: any `ADA_EXCLUDED_TOOLS` config listing the old
  `devin_*` names no longer strips anything — use `devin,devin_read`
  (env files live outside this repo).
- `tools-merge-gate.py` needs a passing `tool_merge_devin` run in
  `ada-ha-scenario-reports` before the card can close — the scenario
  file ships here; the live run is idc02's lane.
- The card's second half — the chaba-side MCP-server audit (mcp-health
  vs mcp-debug overlap, ha/michael-ha/michael-dev dups) — is NOT done:
  it reads chaba/Devin config an ada-pi worktree cannot reach. Raised as
  a board request on the card per the card's own instruction.
- Not committed to the default branch, not pushed, not deployed
  (dispatch rules).
