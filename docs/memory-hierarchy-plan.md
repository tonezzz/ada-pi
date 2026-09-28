# Memory & Report Hierarchy — Unified Plan

Status: **for review** — nothing in this doc is implemented beyond what is
noted as DONE. Companion to `docs/session-security-policy.md` (approved).

## 0. Principles

- **P-R1 Facts are immutable; views are derived.** L0 raw facts never
  change; every report above L0 is regeneratable by re-digesting lower
  layers. Never hand-edit a derived report — re-dig.
- **P-R2 References, not copies.** Every timeline/overview entry links to
  its source by `ref`; raw content never travels upward. (BI drill-through
  / transclusion / event-sourcing pattern.)
- **P-R3 Policy exists at three altitudes** — prose (SSOT doc), rendered
  config (spec → JSON), executable (server gates + scenario tests). No
  single layer is trusted alone (swiss-cheese model).
- **P-R4 Mutual check, never zero.** Humans and AI both err; either party
  may flag a suspected policy violation and the response is to consult
  the SSOT — not to argue from memory (CRM-style challenge-and-check).
- **P-R5 One canonical definition, many read paths.** Terms, policies,
  and facts have exactly one home; Ada, Devin, and Tony read it through
  their own surfaces. No triplicated prose.

## A. Report hierarchy (L0 → L3)

```
L3 weekly/monthly rollup   refs → daily reports
L2 daily rollup            refs → L1 session reports        (MISSING today)
L1 session report          refs → session_items, doc_items, transcript
L0 raw facts               transcript .md, session_items, doc_items, MDDB keys
```

Ref scheme (versioned — `ref_v: 1` in each report):

```
report:<date>-<sid>           L1 report JSON
report:daily/<date>           L2 rollup doc
transcript:<date>-<sid>#t<n>  raw transcript, turn n
session:<sid>#ev<n>           session_items entry
mddb://<collection>/<key>     any MDDB doc
```

### A1. Refs in L0 items (small diff)
- `session_items`/`doc_items` entries gain `turn` (transcript index) where
  known; transcript header gains `ref: transcript:<date>-<sid>`.
- DONE already: `session_items` log + `_fold_session_items` into L1 report
  (`report["session_events"]` + `memory_block` line) — this session.

### A2. L2 daily rollup (new)
- `scripts/report-rollup.py --date YYYY-MM-DD` scans `reports/*.json`,
  emits `reports/daily/<date>.json` + MDDB doc `report/daily/<date>`:
  `{date, sessions:[{ref, owner, speakers, barge_ins, denials,
  summary_line}], totals, notable:[refs]}`.
- `session-memory.md` entries append `ref: report:<date>-<sid>`.

### A3. L3 weekly/monthly
- Same rollup function over daily docs — one schema, recursive.

### A4. Dig surface
- `GET /api/reports/<level>/<id>` (or an `ada_report` tool) resolves a ref
  to that level's content. Optional; files+MDDB already suffice for grep.

## B. SSOT integration (static + dynamic)

New/updated files in `~/CascadeProjects/chaba/docs/ssot/` (outside this
worktree — stage or move with approval):

```yaml
# ssot.apps.ada-reports.yml — NEW app spec
banks:
  reports:
    scope: instance
    mddb_collection: ada-ha-reports-{instance}   # ${ssot(...)} for prefix
    notebooklm_group: reports                    # deep tier, whole history
    kinds: [report, rollup, session-event]
    writable: true
    write_policy: direct          # system writes only
    allowed_tools: []             # reads via ada_memory_search / REST
```

- `ssot.values.yml` gains canonicals: `paths.reports_dir`,
  `paths.transcripts_dir`, `collections.reports_prefix`,
  `reports.keep.{session,daily,weekly}`, `session_security.*` — consumers
  reference via `${ssot(...)}` / `__ref__` (per
  `ssot.dynamic-references.yml`; NEVER in files read by plain yaml.load —
  runtime report refs stay plain strings).
- `ssot.apps.ada-memory-banks.yml`: add the `reports` bank + schema meta
  fields `ref, level, date, session_id, owner, parent, turn` (keeps
  `validate_meta` quiet, enables meta-filtered recall).
- `person_policies` then gate report visibility per person for free —
  a guest session can't search owner's session reports.
- `session_security.secondary_blocked` rendered config → `tool_runner`
  reads it instead of the hardcoded set (~20 lines).
- Terminology: canonical definition of "Dynamic SSOT" (and future terms)
  lives in `ssot.terminology.yml`; `sync-ssot-to-mddb.py` already pushes
  SSOT to MDDB — Ada + Devin both read from there (P-R5).

## C. Policy persistence — who reads what

| Actor | Surface carrying the policy | Mechanism |
|---|---|---|
| Devin/agents | `devin-kb/docs/work-policy.md` + `AGENTS.md` pointer; one line in `ssot.windsurf.common.md`; one line in `~/.codeium/.../global_rules.md`; `AGENTS.md` in ada repo | auto-loaded every session |
| Ada | `policy/memory-hierarchy` doc in a writable spec bank, `kind: procedure`, `prime: true` → session prime; violations self-report via `session_events` | primed every voice session |
| Tony | canonical SSOT files + `ssot.focus.current.yml` entry while active | human review + focus tracking |
| All | scenario tests (`session-owner-policy.yaml`) + server gates | CI — cannot be forgotten |

Violation handling: flag → check SSOT file → fix prose OR config OR code
(update all altitudes in one change). Blameless postmortem if it slipped.

## D. Devin ↔ Ada shared memory

Current Devin flow: continuation summaries
(`~/.local/share/devin/cli/summaries/`, 98 files unindexed), auto-loaded
rules, devin-kb (synced, curated), MCP servers, `dispatch-outcome.md`.
Storage is fine; **recall bridge is the gap**.

Bridge (per direction):
- **Devin → Ada:** on dispatch end, write the outcome summary as MDDB doc
  `devin-session/<date>-<sid>` in the `reports` bank (meta: kind=report,
  owner=devin, ref=file path + branch). Ada can then answer "what did
  Devin do about the gate motor" via `ada_memory_search`.
- **Ada → Devin:** Devin queries the same banks via MDDB REST
  (`MDDB_BASE_URL`, port 11023) or the `mddb` MCP server when healthy;
  Ada's `session-memory.md` stays the human-readable rolling view.
- **Shared vocabulary:** canonical terms in `ssot.terminology.yml` →
  synced to MDDB → Ada primes them, Devin queries `mcp_query_ssot`.
  "Dynamic SSOT" is the first term to add.
- **Backfill:** one-shot script indexes existing `summaries/history_*.md`
  into MDDB (`kind: session-summary`, ref=file path) so the 98 past
  sessions become searchable.
- Anti-pattern to avoid: don't copy KB text into Ada banks or vice
  versa — reference by `mddb://` ref and let each reader fetch.

## E. Voice enrollment + benchmark

DONE this session: `scripts/enroll_speakers.py` (batch, offline, multi-
sample merge, contamination guard intact) + `gen_voice_fixture.py
--voice name=hz:seed` for extra synthetic fixtures.

Benchmark (new method vs old live-session enrollment):
1. Record 3× ~5 s clean clips per person
   (`arecord -f S16_LE -r 16000 -c 1 -d 5 <name>-<n>.pcm`).
2. Method A (old): live `ada_enroll_speaker` — 1 sample, needs a session.
   Method B (new): `enroll_speakers.py --profiles /tmp/eval.json`.
3. New script `scripts/identify_eval.py` (~40 lines): embed each held-out
   clip, score vs all profiles → confusion matrix of (best_score,
   margin) — thresholds: ≥0.45 score, ≥0.05 margin
   (`DEFAULT_THRESHOLD`/`MIN_MARGIN`).
4. Compare: margin 1-sample vs 3-sample voiceprints; operator wall-time
   and steps per speaker; refusal rate (contamination guard).

## F. Action plan

### Now — this worktree, no approvals needed
1. DONE: session-security gates (P1–P7), `session_items` fold, invite
   flow + `ha_person` binding, `enroll_speakers.py`, offline scenarios —
   329 tests green.
2. Commit this worktree to its branch (small conventional commits —
   auth/invites, security policy, reporting, scripts, tests) — do NOT
   merge or push.
3. L0 `ref`/`turn` fields + `ref: report:<date>-<sid>` in
   `session-memory.md` entries.
4. `scripts/report-rollup.py` — daily rollup over `reports/*.json` →
   `reports/daily/` + MDDB doc. Includes latency stats once (5) lands.
5. `scripts/identify_eval.py` — confusion matrix + margins for the
   speaker-ID benchmark.
6. Provider timing events (`turn_latency`, per-tool `dur_ms`) — the
   Ada-latency benchmark substrate; extend `latency_probe.yaml` with
   `latency_ms_max`.
7. `session_security.secondary_blocked` — tool_runner reads rendered
   config with hardcoded default fallback.
8. `scripts/recall-bench.py` + dispatch-outcome → MDDB write + one-shot
   backfill of `summaries/history_*.md` (Devin↔Ada bridge).

### Needs your approval — writes outside this worktree
9. chaba SSOT files: `ssot.apps.ada-reports.yml`,
   `ssot.policy.memory-hierarchy.yml` (P-R1–5 + collaboration pact),
   additions to `ssot.values.yml`, `ssot.terminology.yml` ("Dynamic
   SSOT"), and the `reports` bank + schema fields in
   `ssot.apps.ada-memory-banks.yml`.
10. `devin-kb/docs/work-policy.md` + one-liner in
    `~/.codeium/windsurf/memories/global_rules.md` and ada repo
    `AGENTS.md` — the "remind each other" layer.
11. Merge worktree branch to main (only after you review the diff).

### Needs the live host (Pi/HA + speechbrain + real tokens)
12. Re-issue KK's key with `ha_person=person.kk` (`PUT
    /api/auth/invites/user-kk` + re-mint), enroll real voices via
    `enroll_speakers.py`, run `identify_eval.py` benchmark (1 vs 3
    samples).
13. Live barge-in scenarios (`tests/scenarios-live/`, voice fixtures) —
    verifies P4–P6 end-to-end; update `kk_first_contact` expectations.
14. Ada `policy/memory-hierarchy` doc (`prime: true`) in a spec bank so
    every session starts with the rules.
15. Weekly timer (cron/systemd) for scenario suite + `report-rollup.py`.

### Decision pending
- Baselines: explicit `tests/baselines.yaml` (auditable, my default) vs
  trailing-median auto-derived (less maintenance, drifts silently).

## H. Continuous improvement loop — runs as first-class records

**Unify the envelope.** A voice session, a scenario run, a benchmark run,
and a dispatch task are all *runs* with the same shape — one schema feeds
the whole hierarchy:

```json
{
  "ref": "run:<kind>/<date>-<id>", "ref_v": 1,
  "kind": "session|scenario|bench|dispatch|rollup",
  "started": "…", "actor": "kk|devin|cron",
  "metrics": {"ttft_p95_ms": 2100, "steps": 9, "failures": 1},
  "events": [ …L0 items with refs… ],
  "summary": "one paragraph",
  "escalations": [{"what": "…", "why": "p95 +40% vs baseline", "ref": "…"}]
}
```

- `scenario-live.py`/`scenario-report.py` gain `--json-out runs/<date>/scenario-<name>.json` — same expectations output, wrapped in the envelope.
- `recall-bench.py`, `identify_eval.py`, dispatch runs emit the same.
- Storage: files under `runs/` + MDDB doc in the `reports` bank
  (`kind: run`) — one queryable surface for both Ada and Devin.

**What reaches the top: baselines + escalation rules.** A run rolls up
only when it trips a rule; everything else stays diggable one level down.
Baselines are canonical values (SSOT-rendered, e.g.
`tests/baselines.yaml` seeded from `ssot.values.yml`):

```yaml
escalate_when:
  - metric.failures > 0
  - metric.ttft_p95_ms > baseline * 1.25
  - metric.denials < baseline            # security regressions too
  - run.kind == "scenario" AND status != "ok"
```

Each level keeps `{summary, metrics, escalations(with refs)}` — an
escalation propagates **verbatim with its ref intact**, so the weekly
line "scenario speaker_contamination_guard FAILED" digs straight to the
failing step and its transcript.

**Close the loop with the existing action-proposal machinery** —
`_extract_actions` already writes `action-proposal` docs that get primed
into the next session. Run escalations write the same shape:

```
run fails → escalation doc (kind: action-proposal, ref: run:<…>)
        → next session prime shows "regression in tv_power — see run ref"
        → fix lands → next run green → rollup shows it closed
```

So "important information reaches the top" = baselines define important,
refs preserve the dig path, proposals make it actionable, and the next
run's metrics prove the fix. Continuous improvement becomes a measurable
loop, not a pile of logs.

**Cadence:** sessions report themselves (already); scenario+bench suite
runs on demand and via a weekly timer; rollups are regeneratable views —
rerun `report-rollup.py` anytime to re-dig.

## G. Open items

- Read API / `ada_report` tool for ref resolution — worth it?
- Weekly/monthly cadence: cron/systemd timer vs on-demand.
- Does `session_items` need `turn` back-pointers into the transcript —
  requires correlating events to turn index; easy only if we stamp turn
  numbers at add_user time.
