# Dispatch outcome — ada-memory-staged-writes (attempt 3)

## What this run actually was

The prior failure was NOT a code failure: attempt 2's staged-write lane
was hand-merged to `origin/main` as `a992a40` on 2026-10-10 (visible in
card comms), but the card stayed held by the stale `merge-conflict`
request. Tony's answer ("Merge by hand") matched reality — the merge was
already done. This attempt verified the landed work against the card
spec and cleared the remaining bookkeeping. No implementation changes
were needed; the only repo diff is a `verify_attempt3` block appended to
the job file.

## Spec compliance (checked against merged code)

- Pending store: vault dir `backend/memory_pending.py`
  (`~/.local/share/ada/memory-pending`, `ADA_MEMORY_PENDING_DIR`
  override) — chosen over MDDB because chaba has no MDDB; documented as
  the `ssot.apps.ada-memory-banks.yml` delta in
  `docs/ssot-drafts/PATCHES.md` §7.
- Routing reuses `person_policies`/`control_policies` via
  `_persona_admin` — `{full: true}`/`admin` writes direct; restricted/
  unknown/guest stage; no-map instances keep direct writes; the guest
  route always stages for non-admin. `_check_memory_write_allowed`
  (per-bank `write_policy: confirmed`) still gates the tool call —
  complementary, both kept.
- Entry shape `{id, identity, bank, key, text, staged_at,
  source_session, route, status, text_sha256, args, guest, instance}`;
  `text_sha256` is the Hermes pin — approve re-hashes and refuses on
  mismatch, keeping the entry pending. Entries are single-use.
- Approver surface: `memory-staged` events.md line (ada-review digest)
  + optional board comment when `ADA_MEMORY_REVIEW_CARD` is set;
  respond via `scripts/ada/memory-pending.py list|show|approve|reject`.
  No new UI.
- Model contract `{ok: true, staged: true, id, note}` — the note
  explicitly forbids saying "saved" and forbids retry/alternate-bank
  writes.
- Scan-before-stage: `_scan_memory_write` runs ahead of routing on every
  path (bank/vocab/guest); `memory_pending.stage` re-scans defensively
  and raises on a hit.

## Verification results (origin/main @ 172f25e, repo .venv)

- `python3 -c 'import backend.memory_write_guard'` — exit 0
  (card expected_goal; also recorded by pipeline verify, 1/1 pass)
- `python3 backend/memory_write_guard.py --selftest` — SELFTEST-OK
- `pytest tests/test_memory_pending.py -q` — 23 passed
- `pytest test_tool_runner.py test_memory_banks.py
  test_memory_recall_extras.py test_memory_write_guard.py -q` — 390 passed
- `python3 scripts/tool-lint.py` — clean (the pre-existing debt noted at
  merge time was cleared by 15d0e26)
- `scripts/ada/memory-pending.py list` — "no staged memory writes pending"
- Live smoke (isolated pending dir + stub guest store): stage →
  `mp-*` pending entry; injection text refused at stage time
  (prompt_injection); approve lands verbatim via the route's real write
  path; second approve refused ("already approved — single-use");
  reject → `refused`; queue drains to 0.

## Card/board state left behind

- `merge-conflict` request: already answered (resolution recorded in
  comms; Tony's "Merge by hand" answer on file).
- `ci-benchmark-*` request: filed by the pipeline run (card `metric` has
  no command/before) — answered in-session as not-applicable (binary
  acceptance property; expected_goals is the acceptance check).
- Pipeline stage results on card: plan=pass structure=pass audit=skip
  benchmark=blocked(answered) verify=pass. `develop` stage was
  deliberately not run — this worktree has no diff (work already
  merged), so the stage could only file a false "no implementation"
  request.
- Not verified: the card's live `verify` ("On ada-dev: guest_remember as
  a guest identity -> pending ...") — requires a deployed instance;
  deploys are out of dispatch scope.

## How to verify

1. `python3 -c 'import backend.memory_write_guard'`
2. `pytest tests/test_memory_pending.py -q` (in repo .venv)
3. On ada-dev after next deploy: `ada_remember` as a guest identity →
   `{ok:true, staged:true, id}`; `scripts/ada/memory-pending.py approve
   <id>` lands it; bank write for `person.tony` still direct.

lessons:
- A card held by a stale request can look like a failed dispatch — read
  card comms before assuming the code needs work; attempt 2's merge was
  already done and only bookkeeping blocked close.
- card-pipeline.py lives in the chaba repo, not ada-pi — run it from the ada-pi worktree as `python3 ~/CascadeProjects/chaba-tony-dell/scripts/ci/card-pipeline.py <card> --api http://127.0.0.1:8787 --repo "$(pwd)"`.
- On a verify/re-merge attempt where the work is already merged, skip
  the `develop` stage (`--stages plan,structure,audit,benchmark,verify`)
  — an empty worktree diff makes it file a false "no implementation"
  request that re-blocks the card.
- Cards whose `metric:` is prose (no `benchmark.command`) get a
  `blocked` benchmark stage + open request — answer it yourself via
  `POST /respond {id, request_id, answer}` rather than leaving it open.
- ada-pi worktrees have no `.venv`; use the edit-point's
  `~/CascadeProjects/ada-pi/.venv/bin/python -m pytest` from the
  worktree cwd — tests need `google.genai`, system python lacks it.
