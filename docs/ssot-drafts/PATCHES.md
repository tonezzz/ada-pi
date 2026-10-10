# SSOT deltas — apply to chaba when approved

Files here are staged drafts; move/copy into `~/CascadeProjects/chaba/docs/ssot/`
after review, then `node scripts/ssot-validate-all.mjs` and commit.

## 1. `ssot.apps.ada-memory-banks.yml` — add the reports bank + session_security

In the banks spec section, add:

```yaml
reports:
  scope: instance                       # ada-ha-reports-{instance}
  mddb_collection: ada-ha-reports-{instance}
  kinds: [report, rollup, session-summary]
  writable: true
  write_policy: direct                  # system writes; allowed_tools: []
  allowed_tools: []
```

In the schema.fields list, add: `ref`, `level`, `date`, `session_id`,
`owner`, `parent`, `turn`.

In the rendered `~/.config/ada/memory-banks.json` spec (render source —
wherever the JSON is generated), add top-level:

```yaml
session_security:
  secondary_blocked:                  # group tokens + literal tool names
    - control                         # CONTROL_TOOLS
    - memory_write                    # MEMORY_WRITE_TOOLS
    - calendar_write
    - cms_write
    - devin_confirmed
    - doc
    - persona_write                   # ada_persona set/reset only
    - ada_enroll_speaker
    - ada_memory_search
    - ada_ha_set_device_confidence
    - ada_resolve_action
```

Absent/malformed → tool_runner fails closed to the built-in default
(same set). `backend/tool_runner._secondary_blocked_tools` resolves it.

## 2. `ssot.values.yml` — add canonical values (use ${ssot(...)} consumers)

```yaml
paths:
  reports_dir: ~/.local/share/ada/reports
  runs_dir: ~/.local/share/ada/runs
collections:
  reports_prefix: ada-ha-reports-
reports:
  keep_days: {raw: 90, l1: 365}
benchmarks:
  ttft_ms: {good: 800, acceptable: 1500, broken: 3000}
  ttft_tool_ms: {good: 1500, acceptable: 3000, broken: 5000}
  mddb_search_ms: {good: 200, acceptable: 500}
  nlm_recall_ms: {good: 5000, acceptable: 10000}
```

## 3. `ssot.terminology.yml` — add to a section (e.g. Core Concepts)

```yaml
- label: Dynamic SSOT
  text: "Cross-system canonical values referenced by ${ssot(path)} /
        __ref__ / !ssot_ref in spec files and resolved by
        mcp_query_ssot/mcp_ssot_get. Spec-side only — runtime report data
        uses plain-string refs (report:, transcript:, mddb://) because its
        consumers do not resolve SSOT tags. 'In all three memories' means
        one canonical definition synced to MDDB + agents' native read
        paths, never three copies (ssot.policy.memory-hierarchy P-R5)."
- label: Report ref
  text: "Stable drill-down address: report:<date>-<sid>[#ev<n>],
        transcript:<date>-<sid>[#t<n>], run:<kind>/<date>-<id>,
        mddb://<collection>/<key>, file:<abs-path>. Format v1."
```

## 4. `ssot.windsurf.common.md` + ada repo `AGENTS.md` + Devin global rules — one-liner

"Memory/report work follows the facts→derived-views hierarchy: reports
link by ref, never copy raw data upward; escalations decide what reaches
the top. Canonical policy: docs/ssot/ssot.policy.memory-hierarchy.yml.
Either party may challenge a suspected violation — check SSOT, don't
argue from memory."

## 5. `devin-kb/docs/work-policy.md` — new file linking this policy

Short doc: the collaboration pact + links to chaba SSOT + ada repo docs.
Commit so it syncs to other machines.

## 6. Existing bridge — don't duplicate

`scripts/ada/sync-devin-summaries.py` + `devin-summaries-sync.timer`
already index `~/.local/share/devin/{cli/summaries,summaries}` into
`ada-ha-bank-devin-tony` hourly. The ada-repo `devin-memory-bridge.py`
only publishes dispatch outcomes/run records. NOTE: the sync service is
currently FAILED — it posts to idc01 (`100.74.146.0:11023`) which times
out; the tony-dell MDDB is a read-only follower on `100.68.142.13:11023`
(no loopback, no embedding provider). Worth an SSOT note in
`ssot.values.yml`: `mddb.primary` vs `mddb.local_follower`.

## 7. `ssot.apps.ada-memory-banks.yml` — write_guard + staged_writes

Card ada-memory-injection-scan (guard) + ada-memory-staged-writes (this
dispatch). Add a top-level block next to `person_policies`:

```yaml
write_guard:
  # backend/memory_write_guard.py — string-level pre-write scan, every
  # memory write path runs it BEFORE the store write AND before staging.
  # Trusted identities are scanned too (the guard protects the bank).
  classes: [prompt_injection, credential_shape, invisible_unicode,
            token_flood, homoglyph_mix]
  refusal: {ok: false, error: memory_scan_refused, matched_class: <cls>}
  event: memory-scan-refused          # events.md — class only, no payload

staged_writes:
  # backend/memory_pending.py — the approval lane (card
  # ada-memory-staged-writes). Reuses person_policies — no new ACL:
  #   {full: true} identities (tool_runner._persona_admin) -> direct write
  #   restricted/unknown/guest identities -> staged pending entry
  #   no policy map at all -> direct (nothing to route by; guest route
  #   still stages — guest-store content is untrusted by definition)
  store: file                          # vault pending/ dir, NOT mddb:
                                       # chaba has no MDDB but still
                                       # stages guest writes
  dir: ~/.local/share/ada/memory-pending   # ADA_MEMORY_PENDING_DIR
  entry: {id, identity, bank, key, text, staged_at, source_session,
          route, status, text_sha256, args, guest, instance}
  pinning: text_sha256                 # approval applies staged bytes
                                       # verbatim; mismatch -> refuse,
                                       # keep pending (Hermes pinning)
  single_use: true                     # resolved entries can't re-apply
  approver:                            # existing surfaces, no new UI
    notify: [events.md memory-staged,  # -> ada-review digest
             board comment (ADA_MEMORY_REVIEW_CARD, opt-in)]
    respond: scripts/ada/memory-pending.py list|approve|reject
  routes: [bank, guest, guest_private, vocab]
  out_of_scope: [ada_forget, ada_ops outcome]   # reversible/telemetry
```

`write_policy: confirmed` per bank stays complementary: it gates the
tool CALL; staging gates untrusted CONTENT.

## 8. `ssot.apps.ada-cms-reports.yml` — meta_contract is now enforced

Card ada-report-quality (dispatch 20261005-222628) turned the SHOULD into
enforcement in ada-pi. Suggested doc edits:

- In `meta_contract`, mark `summary`, `domain`, `fresh_for`,
  `confidence`, `timeline`, `updated` as REQUIRED for `cms_publish_page`
  writes (the confirm-gate rejects before the handshake is registered);
  `links`/`supersedes` stay optional.
- Add `medium` to the confidence vocabulary (`high|low|unverified`) —
  derived aggregates (report-distill digests, tool-usage census) are
  neither measured-direct nor unverified.

Implementation (ada-pi repo):
`backend/report_meta.py` is the shared validator
(`validate_report_meta` / `missing_publish_fields` / `fresh_for_seconds`).
Reject layer: `ToolRunner._check_cms_write_allowed`. Warn layer:
`cms_publish_page`/`cms_note_update` return `meta_contract` on gaps and
`cms_verify_page` reports it on reads. `reports-index` reuses
`fresh_for_seconds`. Script publishers (ops-health-report,
tool-usage-report, report-distill, ada/lab-results, scenario-benchmark)
validate before `/add` and refuse to publish on missing fields.
`scripts/report-meta-check.py` audits ada-cms-pages (exit 1 on missing).
