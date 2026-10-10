# Dispatch outcome — ada-memory-staged-writes (attempt 2)

## Result: DONE — staged memory writes implemented and verified; not committed/pushed/deployed.

## What happened

Attempt 1 almost certainly failed its goal check (`import backend.memory_write_guard`) because
the dependency card `ada-memory-injection-scan` was marked done but its module never reached any
branch — it lived uncommitted on the edit-point checkout. This run recreated the guard from the
card's published spec, then built the staged-write lane on top.

## Files changed (uncommitted, in this worktree)

- `backend/memory_write_guard.py` (new) — recreated dependency. Data-driven `_RULES` table;
  classes prompt_injection / credential_shape / invisible_unicode / token_flood / homoglyph_mix.
  `scan(text, *fields)` → `None` clean, else `{ok:false, error:memory_scan_refused, matched_class,
  reason}` with no payload. `--selftest` prints SELFTEST-OK.
- `backend/memory_pending.py` (new) — pending store. Choice: **vault pending/ dir**
  (`~/.local/share/ada/memory-pending`, override `ADA_MEMORY_PENDING_DIR`), NOT MDDB — the chaba
  guest instance has no MDDB but still stages guest writes, and it mirrors ChabaMemory's existing
  `pending/` convention. Entry: `{id, identity, bank, key, text, staged_at, source_session, route,
  status, text_sha256, args, guest, instance}`; flock + atomic write; queue cap 200;
  pin = sha256 of staged text — approve re-hashes and refuses on mismatch (keeps pending);
  single-use (second approve refused). Re-scan at approve time too.
- `backend/tool_runner/memory.py` — every `ada_remember` write path (bank, kind=vocab, kind=guest,
  guest private) now runs scan → stage-or-direct. Retired names `guest_remember`,
  `guest_remember_private`, `vocab_note` delegate through `ada_remember` so aliases can't bypass.
  Staged result is `{ok:true, staged:true, id}` + a note instructing "keep it for Tony to approve",
  never "saved".
- `backend/memory_ops.py` — `vocab_append()` extracted so the approver replays vocab writes
  through the same code path.
- `backend/tool_runner/common.py` — imports for the two new modules.
- `scripts/ada/memory-pending.py` (new) — approver CLI: `list|show|approve|reject`. Approve
  replays the pinned route (bank → memory_ops.remember, vocab → vocab_append, guest → chaba
  store re-keyed onto the staged guest identity).
- `tests/test_memory_pending.py` (new) — 23 tests: guard classes, store CRUD, pin mismatch,
  double-apply, routing (full direct / restricted+anonymous+guest staged / no-map direct),
  scan-before-stage, guest+vocab approval replay, board-notify failure non-fatal.
- `tests/test_tool_runner.py` — two guest-alias tests now pass `owner="admin"` so they keep
  asserting direct-path routing (staging is covered by the new file).
- `docs/ssot-drafts/PATCHES.md` — ssot.apps.ada-memory-banks.yml delta: `write_guard` +
  `staged_writes` blocks (store choice, routing rule, pinning, approver surface, out-of-scope).
- `docs/ssot/jobs/ada/2026-10-10-memory-staged-writes.yml` (new) — job trail.

## Routing rule (reuses the existing access map — no new ACL)

- `policy_identity()` → `_persona_admin` (`{full:true}` in person_policies/control_policies or
  `admin`) → direct write, zero added latency for Tony.
- restricted/unknown identities → staged when any policy map exists.
- guest-store writes → always stage for non-admin (untrusted by definition).
- instance with NO policy map → direct (nothing to route by; pre-change behavior kept).
- Per-bank `write_policy: confirmed` unchanged — it gates the tool call upstream;
  staging gates the content. Complementary.

## Approver surface (no new UI)

- `memory-staged` line in the ada events digest (events.md → ada-review).
- Optional board comment when `ADA_MEMORY_REVIEW_CARD` names a standing card.
- Respond via `scripts/ada/memory-pending.py`.

## Verification

- `python3 backend/memory_write_guard.py --selftest` → SELFTEST-OK
- `pytest tests/test_memory_pending.py` → 23 passed
- `pytest tests/test_tool_runner.py tests/test_memory_banks.py
  tests/test_memory_recall_extras.py tests/test_memory_pending.py tests/test_scenarios.py`
  → 394 passed
- Full suite → 1106 passed; only pre-existing failures:
  - `tests/test_eye_tool.py` — `FRESH` bakes `time.time()` at module import; the ~100s suite
    outlives the staleness window. Flake, unrelated (passes in isolation).
  - `tests/test_tool_audit.py` lint — violations in `voice_fx.py`/`screens.py` exist at HEAD.
  - `tests/test_web_search.py` quota tests — pre-existing (deselected; unrelated to memory).
- All runs used the repo venv (`~/CascadeProjects/ada-pi/.venv/bin/python3`); system python3
  lacks `google.genai`.

## Caveats

- `pipeline: ci` declared on the card but `scripts/ci/card-pipeline.py` does not exist in this
  repo — the runner is dispatcher-side infra; noted on the card.
- `ADA_MEMORY_REVIEW_CARD` is unset by default (opt-in); the events.md digest line always emits.
- `ada_forget` and `ada_ops` outcomes are intentionally out of scope (reversible / telemetry).

lessons:
  - A "done" dependency card can leave its module uncommitted on the edit-point checkout — check the worktree for the actual artifact (here backend/memory_write_guard.py) before assuming the dep exists; recreate from the card spec if absent.
  - test_eye_tool.py FRESH bakes time.time() at import — any suite run longer than the staleness window flakes the stale assertions; passes in isolation.
  - Tests that mock chaba with SimpleNamespace need .identity now that guest writes stage; passing owner="admin" to execute() keeps legacy tests on the direct-write path.
  - scripts/ci/card-pipeline.py is not in the ada-pi repo — the pipeline: ci runner lives in dispatch infra, not the target worktree.
