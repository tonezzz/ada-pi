# Dispatch outcome — report-loop-bench (report_opinion_loop scenario + corpus miner)

Card: `report-loop-bench` (chaba kanban; umbrella `report-session-loop`,
design `docs/design/report-session-loop.md` §4). Repo: ada-pi.

## What changed

- `tests/scenarios-live/report_opinion_loop.yaml` (new) — installed from
  the staged copy at chaba
  `stacks/services/ada-scenario-runner/scenarios-staging/report_opinion_loop.yaml`,
  adapted to ada-pi conventions:
  - flat `calls_any`/`response_nonempty` moved under `expect:` (the
    driver only reads `turn["expect"]` — flat fields are ignored);
  - `url:` dropped (default `ws://127.0.0.1:8002/ws` IS ada-ha-tony);
  - `key_name: user-tony` dropped — that key is NOT in
    `ada-ha-tony-keys.json`, so every batch run would have SKIPped the
    scenario. Default `ADA_API_KEY` path is the convention;
  - `cleanup: []` removed (driver treats cleanup as a dict; a comment
    documents the no-cleanup rationale);
  - **hardened beyond staging**: turn 2 gained
    `call_args_contain: {ada_board_write: [scenario-report-opinion]}` —
    the first live run passed a bare `calls_any` while Ada posted the
    opinion to `report-loop-ada-opinion` (title match, wrong card). A
    write to the wrong card is a broken loop, not a pass.
  - `tier: smoke` → hourly batch picks it up automatically
    (`benchmark.yml` `smoke: '@smoke'`). `ada_board_write` is not in
    `write_tools`, so no `write_allowed_in` entry was needed.

- `tests/bench/report-opinion-mine.py` (new) — stdlib-only seed miner
  for the `report-opinion` bench domain: GET board `/cards` →
  report-linked cards → `from=ada` comms matching `^\[opinion\]` →
  appends `tests/bench/orch-report-opinion-cases.jsonl` rows
  (`kind: report-opinion`; `proven_right`/`category` null until a scorer
  exists). `--snapshot` fetches the current `ada-cms-pages` doc via MDDB
  `/get`; dedupes on `(card, at)`. Verified against the live board: 0
  `[opinion]` comms today — the tag convention ships with §3b, so the
  corpus accrues live from here.

- `docs/ssot/jobs/ada/2026-10-07-report-opinion-loop-scenario.yml` —
  decision/job record.

## Live verification vs ada-ha-tony (idc03, ws://127.0.0.1:8002/ws)

Run via `scripts/scenario-live.py` on idc03 (key from
`~/.config/secrets/ada-ha-tony.env`):

- Run 1 (as-staged assertions): **T1 FAIL** — Ada answered from
  `ada_memory_search` without reading the page (the read-before-opine
  failure the scenario exists to catch); **T2 pass-but-wrong-card** —
  `ada_board_write` fired but the comment landed on
  `report-loop-ada-opinion`, not the probe card.
- Run 2 (installed file): **T1 PASS** (`cms_get_page` fired), **T2
  FAIL** — she flailed into `home_search` looking for the card because
  the `ada_board_write` `report=<slug>` lookup isn't deployed yet.

Both failures are the undeployed §3b surface (`report-loop-ada-opinion`
card — a parallel session implemented the stanza + lookup this hour and
is in review), not scenario bugs. Expect hourly smoke to show this
scenario red until that card deploys; that's the intended tripwire.

## Decisions recorded

- orch-bench/nest topologies for the opinion-forming step: **deferred**
  until the corpus exists (card text + dead-code rule) — recorded in
  card comms.
- Deploy NOT done — scenario lands via the normal ada-pi deploy, which
  needs Tony's approval.

## How to verify

```bash
# on idc03 after deploy:
cd ~/CascadeProjects/ada-pi
ADA_API_KEY=$(grep -m1 '^ADA_API_KEY' ~/.config/secrets/ada-ha-tony.env | cut -d= -f2-) \
  .venv/bin/python scripts/scenario-live.py \
  tests/scenarios-live/report_opinion_loop.yaml --url ws://127.0.0.1:8002/ws
# expect: green once report-loop-ada-opinion deploys

# miner (anywhere with board access):
python3 tests/bench/report-opinion-mine.py --dry-run
python3 tests/bench/report-opinion-mine.py --snapshot
```

Committed on `dispatch/20261007-202820-design-docs-design-report-sess`;
not pushed/deployed per dispatch rules.
