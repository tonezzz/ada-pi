# Dispatch outcome — cms-regen-autofix (20261005-225638)

Built the cms-regen auto-fix loop for CMS automations on the ada-pi
side. `cms-auto-health` (chaba) cards stale `ada-cms-automation` registry
docs as `cms-auto-<slug>`; this is the act step that makes those cards
self-healing.

## What changed

- `backend/playbooks/cms-regen.yml` — playbook contract (registry
  existed, so no tools.d tool was needed). Params: `slug` (required),
  `card`, `managed_by`, `timeout_s`. Gate `identity: owner`,
  `confirm: false` per the card ("regen is idempotent — confirm not
  required"; voice dispatch stays confirm-gated at the `devin` tool).
  Verify runs `tests.test_cms_regen`; report_to devin-handoff.
- `scripts/ada/cms_regen.py` — stdlib-only executor + importable
  `run()`: POST `/api/cms/pages/<slug>/regenerate` (x-api-key), poll the
  registry doc until `last_run` advances / `run_now` is consumed /
  bounded timeout, then health re-check (registry `last_status` + page
  `updated` + `/verify` parse). Command-generator responses handled
  inline; a 422 "unknown generator" or unconsumed queue plus a FRESH
  page => phantom: writes `interval_min=0` + `managed_by=<owner>` (page
  `generated_by`/`written_by`, `--managed-by` override) + clears
  `run_now`. Fresh window = `max(26h, 2*interval_min)`; a page whose
  `updated` advances mid-wait counts too. Stale page + dead queue =>
  `stalled` (escalate, no write). Results post to card comms (`--card`,
  actor `devin` — board whitelist) and a `job/cms-regen-<slug>-<ts>`
  `kind=job` doc lands in `ada-ha-bank-devin-handoff`. `--dry-run`
  classifies with zero writes.
- `scripts/ada/kanban_autofix.py` — the `auto_fix: [cms-auto-*]` rule:
  `handle_card(card_id, column)` fires only on `column=review`, only on
  cards matching the prefix table (`cms-auto-*` -> `cms-regen`, slug =
  card id minus prefix), dedups on a playbook comms line within 30min.
  `--mode dispatch` (default) goes through `backend.devin_dispatch`
  (`caller=kanban-autofix`, `is_owner=True` — the dispatcher is the
  owner's automation); `--mode run` executes cms_regen in-process.
  Non-matching cards are a hard no-op.
- `tests/test_cms_regen.py` — 25 tests, all transports mocked (Wire
  fakes MDDB /get /add + CMS API + board comms; injected clock):
  regen/consumed/error, phantom mark + managed_by precedence + dry-run
  + stale-page stalled + generator error/timeout/422, unreachable MDDB,
  rule match/skip/dedup/dispatch/run.
- `tests/test_devin_dispatch.py` — registry list now expects
  `cms-regen`.
- `.env.example` — documented `ADA_CMS_API_URL`.
- `docs/ssot/jobs/ada/2026-10-05-cms-regen-autofix.yml` — job ledger
  record incl. the chaba-side wiring followup.

## Verified

- `python3 -m unittest tests.test_cms_regen tests.test_devin_dispatch`
  — 51 tests OK.
- `python3 scripts/tool-lint.py` — clean (53 pre-existing warnings).
- Playbook loads/validates/renders via `devin_dispatch.load_playbook`.
- CLI smoke: `cms_regen.py flood-report --timeout 3` on this host →
  honest `mddb_unreachable` (no MDDB here), exit 1, no writes.
- Full suite: 254 tests, 28 errors — all `google.genai` import
  failures, the known missing dep on this host (identical to HEAD
  baseline; no venv here).

## Not done / notes

- The kanban-dispatch call site (`auto_fix` trigger on column-enter)
  lives in the chaba repo — unreachable from this worktree. The hook +
  contract ship here; chaba wiring is a followup (documented in the
  SSOT job file + card comms).
- `confirm`-gate internals untouched; no auto-dispatch on non-cms-auto
  cards; no deploy.
