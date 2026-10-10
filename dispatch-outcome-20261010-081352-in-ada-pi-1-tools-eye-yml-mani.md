# Dispatch outcome — ada_look tool (card ada-look-tool)

## What changed

- **`backend/tools.d/eye.py`** (new) — `ada_look`, no params. Reads
  `ada-ha-scenario-reports` / `eye/latest` via `runner.mddb`,
  parses the ```json block. Contract per card spec:
  - doc absent / no json block → `ok:true live:false` "No eye page is
    publishing right now." (never an error)
  - `age_s > 3*pub_s` → `stale:true` + `age_s`, summary phrased as
    "last update N ago"
  - fresh → per-class `objects` counts + `count` + speakable `summary`
    ("I see 2 people and a dog") + `src`/`model`/`age_s`/`pub_s`
  - malformed json → honest `ok:false`; missing `ts` → counts with
    `freshness: unknown`; `runner.mddb` None (chaba mode) → `ok:false`
  - defensive parsing: ts epoch or ISO-8601, `pub_s` or `pub`,
    `cls` or `label`, bare-JSON doc tolerated
- **`backend/tools.d/manifest.yml`** — `ada_look: module eye,
  policy read, secondary_allowed true, timeout_s 10` (parity with the
  ungated camera-describe path; "what do you see" is a household
  question).
- **`backend/tool_guide.yml`** — `ada_look` error-path entry.
- **`docs/ssot/ssot.tool-surface.yml`** — `count_cap` 110→111 (8th
  tools.d tool), same-change ratchet bump.
- **Scenarios** — the card's eye-look-live + eye-look-idle exist in
  both regimes:
  - `tests/scenarios/eye_look_{live,idle,stale}.yaml` — deterministic,
    run in CI via test_scenarios.py (FakeMddb seed; stale uses
    `__NOW_M_600__`)
  - `tests/scenarios-live/eye_look_{live,idle}.yaml` — voice-level
    acceptance; live asserts `calls_any: ada_look` + object-flavored
    speech, idle asserts honest-empty phrasing. Live pair is gated by
    the pending chaba /apps/eye/ + eye-mddb deploy (comments say so).
- **`tests/test_eye_tool.py`** — 13 unit tests (faked mddb): all
  branches + manifest wiring.
- **`docs/ssot/jobs/ada/2026-10-10-ada-look-tool.yml`** — job trail.

## Result / verification

- `python3 -m unittest tests.test_eye_tool` — 13 pass
- `python3 -m unittest tests.test_scenarios` eye_look_* — 3 pass
- `python3 scripts/tool-lint.py` — no ada_look violations; the 3
  remaining FAILs (voice_fx descsize + coverage, yt_cached_list impl)
  are identical on the base checkout — pre-existing debt, not this
  change.
- test_scenarios/test_tool_runner have unrelated pre-existing failures
  in this env (`ADA_INSTANCE_ID` unset — same failures reproduce on the
  base `../ada-pi` checkout).

## Caveats

- The chaba job doc `docs/ssot/jobs/eye/2026-10-07-nest-edge-vision.yml`
  `handoff_ada_look` (the "ready-to-apply snippet") is outside this
  worktree and unreachable from the dispatch sandbox — implemented from
  the card spec; field-name variants are tolerated defensively.
- `eye/latest` returns "not found" on mddb today — expected: the doc
  only exists while an /apps/eye/ page publishes AND the chaba edge
  route is live. The idle path is therefore the current production
  answer.
- No commit/push per dispatch rails.
