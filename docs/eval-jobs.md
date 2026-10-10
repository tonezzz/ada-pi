# Ada Evals — job-report dispatch design (DRAFT for review)

## Problem

Scenario runs today are `systemctl start ada-scenario-{smoke,full}` —
fire-and-forget: no handle to check progress, no structured spec, no metrics
for before/after comparison, no A/B lever for config like session prime.

## Proposal

Job docs in MDDB `ada-eval-jobs`. A requester (Devin session, Ada tool, or
human) writes the job **before** dispatch — the doc is simultaneously the
spec, the live status, and the result record. A small dispatcher service on
idc01 polls for queued jobs, runs them in the existing
`ada-scenario-runner` container, and updates the doc along the way.

## Lifecycle

```
requester writes:  status=queued, spec={...}
dispatcher claims: status=running, claimed_by, heartbeat_at
per-scenario:      progress={done,total,current}, heartbeat_at refresh
finish:            status=done|failed, result={verdicts, metrics},
                   links to per-scenario report docs in ada-ha-scenario-reports
stale heartbeat:   sweeper marks running→failed (stale) after ~10 min
cancel:            requester writes status=cancel → dispatcher kills between scenarios
```

Every update is an MDDB revision — the job's timeline is free.

## Job doc schema (`ada-eval-jobs/<slug>-<ts>`)

```yaml
meta:
  kind: eval-job
  status: queued|running|done|failed|cancel
  requested_by: devin-session|ada-voice|manual
  reason: free text (e.g. "prime A/B for tv_power")
  spec_tier: smoke|full|bench
  spec_scenarios: [tv_power, doc_recall]   # empty = whole tier
  spec_repeat: 3                          # runs per arm (LLM variance)
  spec_arms: [prime-on, prime-off]        # maps to ws ?no_prime=0/1
  progress_done / progress_total / progress_current
  heartbeat_at
  result_verdicts, result_metrics         # summary; detail in report docs
```

## Dispatcher — `ada-evald.service` (idc01, user unit)

- ~100-line python loop: `search_documents(ada-eval-jobs, filter_meta={status:queued})` every 10–15 s.
- Claim = write `status:running` + `claimed_by`; single-worker by design —
  scenarios actuate the shared TV/plug, parallelism would fight hardware.
- Runs `podman run ada-scenario-runner --job <key>`; the runner reads its
  spec back from the doc (decoupled: no env plumbing).
- Whitelist-only spec fields; never interpolate doc content into shell.
- Rejects malformed specs → `status:failed, error=...` immediately.

## Runner additions (`scenario-live.py` / `scenario-report.py`)

- `--job <key>`: per-scenario-boundary job doc updates (progress + heartbeat).
- `--repeat N`: repeat each scenario, verdict = majority, metrics = mean/min/max.
- Per-turn metrics into report docs: `tool_calls[]`, `calls_count`,
  `turn_latency_s`, `in_tokens`, `out_tokens` (already in ws `usage` events).
- Bench arms → `spec_arms` maps each arm to ws params (e.g. `no_prime`).

## Backend addition

- `?no_prime=1` ws query param honored in `pwa_server._prime_session` —
  mirrors the existing `simulate_unknown_speaker` test hook. Enables
  prime on/off A/B on one live backend without restarts.

## Completion surface

- On `done`: `chaba_event` log → `/chaba-admin/events` ("eval job tv_power:
  pass; prime-off arm +1.4 tool calls/turn").
- Optional phase 2: `ada_eval_run` tool so voice can dispatch + report
  ("Ada, run the smoke evals" → she announces the verdict when done).

## Phases

1. `no_prime` param + metrics capture in scenario-live.py + `ada-eval-jobs`
   collection — manually-written job docs, run locally for first data.
2. `ada-evald` dispatcher + stale sweeper + chaba event on done.
3. `ada_eval_run` voice tool + cancel support.
4. Nightly timer (`ada-eval-smoke.timer`) once the suite is green.

## Open questions

- Repeat default: 3 per arm? (cost: each turn = real Gemini Live tokens)
- Should `bench` tier scenarios get a `budget` cap on tool calls?
- Job doc TTL — 30d in `ada-eval-jobs`, permanent summary in
  `ada-ha-scenario-reports`?
