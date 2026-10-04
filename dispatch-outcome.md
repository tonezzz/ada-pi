status: done

# Orchestration benchmark harness — implemented per CMS 'multi-model-orchestration'

## What changed

- **`tests/bench/orch-bench.py`** — rewritten from the partial stub into
  the full harness the CMS plan specified. Same case set through every
  structure, one curve per structure:
  - **5 structures** (`--structure`): `student` baseline, `cascade`
    (student →4b inside the gray band), `parallel` (weighted vote),
    `router` (feature router — code-switch / embedded-affirm /
    imperative-shaped turns escalate at zero extra model calls;
    documented stand-in for a trained router head), `verifier` (4b
    audits student YES decisions and tool choices; veto rejects or
    re-routes).
  - **3 case sets** (`--sets`): `golden` confirm + `tool` routing are
    AST-loaded from `jev-bench.py` so the benches can't drift; `hard`
    loads the new `tests/bench/orch-hard-cases.jsonl` — 35 rows tagged
    by slice (imperative-lookalike ×12, code-switch TH/EN ×13,
    ok-nonconfirm ×7, hard-positive ×3).
  - **Metrics**: accuracy overall + per set, p50/p95 latency,
    cpu-seconds/call (server `usage.elapsed_s` — the $/call proxy),
    heavy-call fraction, 10-bin ECE per decision.
  - **Retrain hook**: `--corpus-out PATH` appends confirm-kind misses as
    jev-corpus rows (`src=orch-bench:<structure>`) for
    `scripts/jev/merge-corpus.py`.
  - **Reporting**: `--mddb/--report-cms` unchanged in shape — writes the
    kind:benchmark doc to `ada-ha-scenario-reports` and refreshes the
    `bench-orch` CMS page.
- **`tests/bench/orch-hard-cases.jsonl`** — new hard case set.
- **CMS page `multi-model-orchestration`** — harness section rewritten,
  "What to measure" corrected (10 confirm + 8 tool, not 13+8), first
  follow-up item checked off.
- **`docs/ssot/jobs/ada/2026-10-04-orch-bench-harness.yml`** — job trail.

## Verification

- `python3 -m py_compile` clean.
- End-to-end run against a local stub `/v1/systemone` (student + heavy
  doubles on localhost): all 5 structures executed, 53 cases, metrics
  table, miss report, `--corpus-out` rows and `--json-out` verified.

## Not done / next

- **No live run** — the sandbox could not reach the tailnet endpoints
  (100.74.146.0:8778 student, 100.123.163.11:8777 4b). Run on idc01:
  `.venv/bin/python tests/bench/orch-bench.py --mddb
  http://100.74.146.0:11023/v1 --report-cms --corpus-out
  ~/.local/share/ada/jev-corpus.jsonl`
- Remaining follow-ups on the CMS page (run cascade, wire the winner
  into tool_runner/confirm-gate, retrain timer) are unchanged.
