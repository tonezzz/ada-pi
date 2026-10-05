# Dispatch outcome — 20261005-222628-enforce-the-report-meta-contra

Card: `ada-report-quality` — "Improve standard of Ada-generated reports".
Scope delivered: the report meta contract
(`meta_contract` in `ssot.apps.ada-cms-reports.yml` — summary ~240 chars,
domain, fresh_for, confidence, timeline, updated) is now enforced at
report-write time, and every in-repo report generator emits it.

## What changed

**New shared validator — `backend/report_meta.py`** (stdlib-only so
scripts reuse it): `validate_report_meta(meta) -> {ok, missing,
warnings}`; `missing_publish_fields(args)` for the model-supplied four;
`fresh_for_seconds()` (units m/h/d — same semantics the reports-index
staleness check used inline). Required: summary, domain, fresh_for,
confidence, timeline, updated. Warn-only findings: summary >240 chars,
unparseable fresh_for/updated, confidence outside
{high, medium, low, unverified} (`medium` is an extension over the SSOT's
high|low|unverified for derived aggregates — flagged in PATCHES.md #7).

**Reject layer — `backend/tool_runner.py`**: `_check_cms_write_allowed`
refuses `cms_publish_page` calls missing/blank `summary`, `domain`,
`fresh_for`, `confidence` (or with an unparseable `fresh_for`) with a
ValueError naming the fields — BEFORE the pending-confirm slot is
registered, so the model fixes the call instead of asking the user to
approve a doomed write.

**Warn layer**: `cms_publish_page` validates the post-merge meta and
returns `meta_contract` in the result on gaps (covers internal republish
paths like `_cms_edit_sections` on legacy pages — no breakage).
`cms_note_update` does the same ungated (merge-only path). New internal
caller coverage: `devin_job_report` now publishes with contract args;
`_cms_reports_index` emits `confidence`+`timeline` itself and reuses
`fresh_for_seconds`. `cms_verify_page` now reads the doc directly and
returns a `meta_contract` block alongside the format parse check — the
read-side surface Ada can act on.

**Generator prompts — `backend/realtime_provider.py` +
`backend/tool_guide.yml`**: `summary`, `domain`, `fresh_for`,
`confidence` are `required` in the `cms_publish_page` schema and their
descriptions say REQUIRED; CMS_INSTRUCTIONS states the contract
(rejects without them; updated/timeline auto-stamped).

**Script publishers — all emit complete meta and validate before
`/add`, refusing to publish on missing fields** (warnings log but don't
block): `scripts/ops-health-report.py` (+confidence=high),
`scripts/tool-usage-report.py` (+confidence=medium),
`scripts/report-distill.py` (+confidence=medium; exit 1 if any digest
skipped), `scripts/ada/lab-results.py` (added domain/summary/fresh_for/
confidence/timeline — previously had none), `scripts/scenario-benchmark.py`
(both `benchmark-<suite>` and `auto-report` pages; confidence reflects
run_valid).

**New audit tool — `scripts/report-meta-check.py`**: scans
`ada-cms-pages` (kind page|report), prints per-page violations, exit 1
on any missing required field, exit 2 if MDDB unreachable. Read-only —
safe against the tony-dell follower.

**Docs**: `docs/ssot-drafts/PATCHES.md` §7 proposes marking the six
fields REQUIRED for cms_publish_page writes and adding `medium` to the
confidence vocab; job record at
`docs/ssot/jobs/ada/2026-10-05-report-meta-contract.yml`.

## Verify

- Missing meta fails: `test_publish_rejects_missing_report_meta` /
  `test_publish_rejects_unparseable_fresh_for` — ValueError, no pending
  armed, no write.
- Conformant reports pass: `test_publish_with_confirmed_writes_page_doc`
  asserts all six fields land in stored meta;
  `test_verify_page_reports_meta_contract` covers both directions.
- `.venv/bin/python -m unittest discover -s tests` — 629 tests, OK
  except 3 pre-existing `ai_edge_litert` env errors (Pi-only dep, absent
  on this machine; identical on HEAD).
- `python3 scripts/tool-lint.py` — clean.
- Live audit: `python3 scripts/report-meta-check.py --mddb
  http://100.68.142.13:11023/v1` — MDDB was unreachable from this
  worktree session; expect FAIL rows for pre-contract legacy pages
  (they'll pass once republished with the fields).

## Notes / follow-ups

- Branch `dispatch/20261005-222628-enforce-the-report-meta-contra`,
  rebased onto origin/main @3f452c9, two commits, not pushed.
- Out of scope: `ada-ha-scenario-reports` ops docs (TTL'd, not CMS
  pages) and generators living in the chaba repo (flood-news worker,
  host-services-cms) — PATCHES.md #7 flags them to adopt the same check.
- Deploy to idc01 needs the usual approval (`deploy-ada.sh`).
