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
