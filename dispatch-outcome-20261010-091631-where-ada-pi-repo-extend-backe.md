# Dispatch outcome — ada-memory-write-caps (attempt 2)

## What changed

Created `backend/memory_write_guard.py` — the pre-write guard the card
asks for — and wired it into every memory write path:

- **Caps** (module-header config, env-overridable,
  `BANK_CAPS` per-bank override map):
  - entry body: 2,000 chars (`ADA_MEMORY_ENTRY_CAP`)
  - key/title fields (key, subject, attribute, supersedes): 160
    (`ADA_MEMORY_FIELD_CAP`)
  - bank doc count: warn-only event at 400 docs
    (`ADA_MEMORY_BANK_DOC_WARN`), never a hard stop
- **Refusal contract**: `{ok:false, error:"memory_cap", field, size,
  cap, hint:"shorten by K chars or split into N entries"}` — the model
  can self-correct in the same turn.
- **Wiring**:
  - `memory_ops.remember()` — checked after ACL/kind validation, before
    any store read. Covers create, correct-in-place, and supersede
    (text is the final body in all three).
  - `tool_runner/memory.py` — `ada_remember kind='guest'` checks
    text+slug before chaba; `guest_remember`/`guest_remember_private`
    direct methods guarded; `_vocab_append` checks the FINAL merged
    doc, not the delta (the card's named edge case).
  - `chaba_memory.py` — entry-count warn on guest files (lazy import,
    standalone-safe).
  - `tool_guide.yml` — ada_remember blurb names the caps so the model
    can pre-comply.
- **Audit**: refusals log `memory-cap-refused`, warns log
  `memory-bank-doc-warn` to events.md — sizes/fields only, never
  payload.

## Key finding

The sibling card `ada-memory-injection-scan` is marked done, but its
`memory_write_guard.py` never reached `origin/main` — comms cite a merge
to `chaba/master` (commit 9ecae6dd unreachable from origin; origin has
no `master` branch). Per the card spec's fallback, this module was
created fresh carrying the caps stage; `check_write()` is the single
entry point and the docstring marks where the scan classes slot in
ahead of cap checks when the chaba-side merge arrives. Expect a text
conflict on that file when the scan module lands — resolve by keeping
both stages inside `check_write()`.

## Result

- `python3 backend/memory_write_guard.py --selftest` → `SELFTEST-OK`
  (17 checks: over/at/under boundary, hint math, field cap,
  merged-append, per-bank override, doc-warn throttle, event log)
- `tests/test_memory_write_guard.py` — 15 tests, all pass (wiring:
  over-cap ada_remember refuses before any store write, supersede
  refusal leaves the old doc active, guest paths inherit caps, vocab
  merged-append counts final size)
- Regression: `test_memory_banks + test_tool_runner +
  test_memory_recall_extras + test_scenarios` — 386 passed on
  `../ada-pi/.venv` python
- `grep -l memory_write_guard backend/tool_runner/memory.py` → match
  (expected_goal)
- tool-lint: 3 pre-existing violations (voice_fx ×2, yt_cached_list) —
  unrelated files, untouched
- `scripts/ci/card-pipeline.py` does not exist in this repo — the
  card's `pipeline: ci` step could not run from this worktree
- Job trail: `docs/ssot/jobs/ada/2026-10-10-memory-write-caps.yml`

## How to verify

1. `python3 backend/memory_write_guard.py --selftest` → SELFTEST-OK
2. `.venv/bin/python -m pytest tests/test_memory_write_guard.py -q`
3. On ada-dev post-deploy: `ada_remember` with a 5,000-char text →
   `{ok:false, error:"memory_cap", size:5000, cap:2000, hint:...}`;
   a 2,000-char write lands; a vocab append onto a near-full log
   refuses on merged size.

lessons:
- `python3 backend/<mod>.py --selftest` runs with backend/ (not repo
  root) on sys.path — `from backend import X` fails; lazy/try-import or
  sys.path fix needed for standalone runs.
- `expected_goals` for a prior card can be satisfied on a DIFFERENT
  repo/branch (scan card merged to chaba/master, unreachable from
  origin) — verify the artifact exists in origin/main before building
  on it.
- SCENARIO_BANKS `personal` bank is write_policy=confirmed — tool-level
  ada_remember tests need confirmed=True; memory_ops.remember called
  directly bypasses the gate.
- Guard event logging writes to ~/.local/share/ada/events.md by default
  — pin ADA_EVENTS_FILE to a temp path in tests (and mind that guard
  code auditing to events.md in unit tests leaks onto the dev host).
- No `scripts/ci/card-pipeline.py` in ada-pi — the `pipeline: ci` card
  field references tooling outside this repo.
