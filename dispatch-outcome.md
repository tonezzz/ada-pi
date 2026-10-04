# Dispatch outcome — tools-ci-audit (20261004-180803)

Made the tool-consolidation program self-auditing. Branch
`dispatch/20261004-180803-make-the-tool-consolidation-wo`, ada-pi.

## What changed

- `docs/ssot/ssot.tool-surface.yml` — **new**: the machine-readable
  contract. `count_cap: 106` (= today's census, ratchet — growth needs a
  deliberate bump), `target_count: 38`, six merge families bound to
  `tools-merge-*` cards + their regression scenarios + absorbed-name
  lists, and `coverage_debt` (43 census-day tools with no scenario
  reference — grandfathered; new uncovered tools fail).
- `docs/assessments/tool-consolidation-spec-2026-10-04.md` — **new**:
  the spec the design card referenced but never committed; reconstructed
  from card notes (merge groups, `action=` pattern, alias plan, gates
  preserved, audit machinery).
- `backend/tool_runner.py` — `_ALIASES` + `_ALIAS_ARG_DEFAULTS` tables
  and `execute()` resolution: retired names route to the canonical tool
  with implied `action=` args; alias hits logged for the census.
- `scripts/tool-lint.py` — **new**: static audit (AST+yaml, no backend
  import). Fails on: surface > cap, dup declarations, alias violations
  (key still declared / target undeclared / chain / stray), absorbed
  name retired without an alias row, gate-set or benchmark `write_tools`
  member resolving to nothing, declared tool with no scenario reference
  and no debt entry, started-merge family without its scenario file.
- `scripts/tool-usage-report.py` — **new**: journal scan of
  `tool X args=` / `tool alias` / `denied` / phonetic-normalize lines →
  markdown census → CMS page `report/tool-usage` (ada-cms-pages); falls
  back to static census when `/api/tools` is unreachable.
- `scripts/ada-tool-usage.{service,timer}` — **new**: systemd user units
  (daily 06:20, idc01 `~/CascadeProjects/ada-pi` layout).
- `scripts/tools-merge-gate.py` — **new**: a `tools-merge-*` card in
  done/closed without a pass|flaky latest report for its family scenario
  is a violation; `--card-id` is the close-time check; MDDB-unreachable
  is distinguished from absent evidence (gate can't be satisfied by a
  dead report store).
- `tests/test_tool_audit.py` — **new**: 19 tests — real-repo lint is the
  in-suite audit stage; fixture repos pin every violation class; gate
  logic covered for done/review/backlog/single-card paths.
- `tests/benchmark.yml` — removed phantom `ada_remove_speaker` from
  `write_tools` (the lint's first real catch: a policy entry guarding a
  tool that doesn't exist).
- `docs/ada-tool-dev.md` — checklist rule 8: surface budget + lint.

## Result

`tool-lint` baseline is clean (106 declared = cap, 63 scenario-covered,
43 acknowledged debt, 11 drift warnings). The gate correctly blocks all
six merge cards today — none has a family scenario yet. The usage report
renders and publishes; the timer needs install on idc01.

## Follow-ups (out of repo scope — declared in the SSOT job)

- chaba `ssot-validate-all.mjs` / board-api `/action close`: call
  `tools-merge-gate.py --card-id <id>` for tools-merge-* cards
  (enforcement point lives in the chaba repo — unreachable from this
  worktree).
- idc01: `systemctl --user enable --now ada-tool-usage.timer` after
  copying units to `~/.config/systemd/user/`.

## Verify

    python3 scripts/tool-lint.py                        # exit 0
    python3 -m unittest tests.test_tool_audit -v        # 19 tests
    python3 scripts/tools-merge-gate.py                 # 0 violations (all backlog)
    python3 scripts/tools-merge-gate.py --card-id tools-merge-camera  # exit 1
    python3 scripts/tool-usage-report.py --dry-run --journal-file <f>
