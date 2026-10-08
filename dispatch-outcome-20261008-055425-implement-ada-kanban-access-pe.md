# dispatch outcome — implement-ada-kanban-access-pe

## What changed (ada-pi worktree, uncommitted per rails)

New canonical board tool `kanban` replacing `ada_board_write`:

- `backend/tools.d/kanban.py` — actions `list`/`read`/`comment`/`move`/`ask`/`file`/`respond` over `backend/board_client` (board-api on tony-dell via Caddy). Authority matrix per `docs/design/ada-kanban-access.md` §3:
  - always allowed: list, read, comment, file (backlog), ask
  - free moves: `backlog→doing` (under `doing_limit`), `doing→backlog`, `doing→review`
  - `review→done` requires non-empty `evidence=` (a fact verified with a tool); the audit comm lands as `verified: <evidence>` from `ada`
  - Tony-decides (confirm-gated via `runner._require_confirmation`, single-use `confirm_token`): high-priority, `review_kind: decide`, prod/security-tagged cards, reopens (`review/done→doing/backlog`), over-cap doing claims, and every transition outside the triage set. Denials return `ok:false, needs_confirm:true` with "NOT EXECUTED" wording — the denial-breaker catches self-asserted `confirmed=true` retries.
  - `respond` stays owner_only (board logs it under tony).
  - Loose-id resolution: exact → unique id-prefix → slugified title (the model shortens `kanban-review-probe-0be154` to `kanban-review-probe`; blind writes retry once after a server `no card`).
- `backend/tools.d/ada_board_write.py` deleted; `tool_runner._ALIASES["ada_board_write"]="kanban"` keeps the old name callable.
- `backend/tools.d/manifest.yml` — kanban `policy: read` (per-action gating lives in-module), `secondary_allowed: true`, `timeout_s: 180` (Caddy writes take 15-75s).
- `backend/realtime_provider.py` — instructions: report opinions + request capture now use `kanban`; authority stanza (free moves, evidence rule, Tony-only classes, ask path, no claiming writes that returned errors).
- `backend/tool_guide.yml` — kanban + ada_board_write-retired entries; added ada_track_device/ada_device_acl entries.
- `scripts/scenario-live.py` — legacy alias `ada_board_write → kanban`.
- `tests/test_kanban.py` — 51 tests: authority matrix, confirm gate, evidence, owner-gate, resolver, alias/manifest wiring. (Deleted `tests/test_board_write.py`.)
- `tests/scenarios-live/kanban_review.yaml` — new full-tier scenario; `ada_board_write.yaml` + `report_opinion_loop.yaml` migrated to `kanban`; `tests/benchmark.yml` write_tools updated.
- `docs/ssot/ssot.tool-surface.yml` — kanban family; `ada_device_acl` added to coverage_debt.
- `backend/tools.d/ada_track_device.py` — description compressed to ≤4 lines (pre-existing lint failure fixed).

## Verification

- `pytest tests/test_kanban.py` — 51 passed (repo venv); broader suite 333 passed earlier; `tool-lint.py` clean.
- Live on ada-dev equivalent: scratch instance on idc03 (`~/ada-kanban-scratch`, port 8015, `ada-dev.env`, `ADA_INSTANCE_ID=dev-scratch`) — no deploy of the real service.
- `kanban_review.yaml` via `scenario-live.py` → PASS, real card `kanban-review-probe-0be154`:
  - file dedup ("already exists"), doing-capacity refusal minting `confirm_token`, backlog→review and review→doing confirm-gated, honest "already in done" on `idc01-return-role`, `ask` landed request `can-the-probe-card-be-deleted-now-f13223` (open, from ada).
  - `review→done` + evidence verified via `/api/tools/call`: card durably `column: done`, comms show `ada: moved review -> done — verified: …`.

## Board-side findings (worth a chaba look)

1. Write latency through Caddy/board-api flock is 15-75s+ — timeouts abort the client while the server still completes the write (a `file` POST that timed out created the card anyway; server-side dedup saved us). Idempotent ops matter; the 180s manifest cap + 75s httpx timeout cover observed latency.
2. `kanban-commit`/git-safe-pull raced an api write: a `review→done` move at 07:33 was clobbered by the committer restoring a just-committed pre-move file (visible in comms: ada `verified:` line but no tony move line). Retry landed cleanly. Not a tool bug — a card-write durability gap on the chaba side.

## Not done / limits

- Not committed, pushed, or deployed (rails). ada-dev/ada-ha run the old `ada_board_write` until merge+deploy.
- Tony-only gates exercised via needs_confirm tokens on ungated-path-adjacent moves; a live `priority:high`/`decide` close attempt wasn't exercised end-to-end (idc01-return-role was already done) — covered by unit tests.
- `$TASK_DIR/answers.jsonl` — empty, no outstanding requests.
- Scratch dir left at `idc03:~/ada-kanban-scratch` (instance stopped; delete at leisure).

## How to verify

```
~/CascadeProjects/ada-pi/.venv/bin/python3 -m pytest tests/test_kanban.py
~/CascadeProjects/ada-pi/.venv/bin/python3 scripts/tool-lint.py
# live (after deploy): python3 scripts/scenario-live.py tests/scenarios-live/kanban_review.yaml \
#   --url ws://127.0.0.1:8005/ws --api-key "$ADA_API_KEY"
```
