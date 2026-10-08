# dispatch outcome — implement-ada-kanban-access-pe (resume)

Second dispatch on card `ada-kanban-access`. The first dispatch
(20261008-055425) built the full implementation on a dispatch branch;
this session found it already hand-merged to `origin/main` (f2a42b9,
conflict resolution logged on the card 11:05) and ran the verification
the card still owed.

## State found

- `backend/tools.d/kanban.py` — consolidated board tool
  (`list`/`read`/`comment`/`move`/`ask`/`file`/`respond`) implementing
  the design §3 authority matrix: free triage moves
  (`backlog→doing` under `doing_limit`, `doing→backlog`,
  `doing→review`), `review→done` only with `evidence=`, Tony-decides
  classes (priority:high, review_kind:decide, prod/security tags,
  reopens, off-matrix transitions) routed through the runner
  confirm-gate (`_require_confirmation`, single-use `confirm_token`,
  `needs_confirm` denials) or `action='ask'` for the async path.
  `respond` owner-gated; secondary voices get list/read only.
- `ada_board_write` retired to a `tool_runner._ALIASES` row; manifest,
  instructions stanza (`DEFAULT_ADA_INSTRUCTIONS` KANBAN block),
  tool_guide, ssot.tool-surface, benchmark.yml all wired.
- `tests/test_kanban.py` — 51 tests; `tests/scenarios-live/kanban_review.yaml`
  live scenario.

## Verification this session

- `pytest tests/test_kanban.py` — 51/51 pass. `tool-lint.py` clean.
- ada-dev on idc03 (:8005) confirmed running the merged code
  (checkout cabbc82 ⊇ f2a42b9, service restarted 12:56).
- Live via `/api/tools/call` on ada-dev:
  - `list` — real board (251 cards), `read` — full card incl. comms.
  - `comment` on ada-kanban-access — landed `from: ada` (verified
    on the card via board-api).
  - `move review→done` w/o evidence on `lab-parakeet-batch-asr` —
    hard refusal, correct guidance.
  - `move review→done` on `dispatch-merge-sweep` (priority:high)
    — `needs_confirm` + bound token, NOT EXECUTED.
  - `ask` — request `kanban-tool-verified-live-on-ada-dev-lis-550bf3`
    raised on the card (open, from ada).
  - alias: `ada_board_write` still callable → routed to `kanban`.
- `kanban_review.yaml` live scenario replayed against ada-dev ws —
  **PASS, 0 failed expectations**. In-conversation behavior matched the
  matrix: doing-cap over-claim → confirm-or-ask offer (not a forced
  move); `backlog→review` refused as off-matrix; evidence-less close
  refused honestly; `idc01-return-role` (done, high, decide) reported
  truthfully with no silent move; `ask` left Tony a board question on
  the probe card.

## Notes

- `BOARD_API_TOKEN` (design §2) is moot: writes authenticate via the
  tailnet identity tailscale-serve injects (`via=tonezzzz@gmail.com`);
  board_client sends `Tailscale-User-Login: ada` for comms labeling.
- Board-side quirks observed (chaba, not ada-pi): write latency
  15–75s through Caddy/flock; earlier run saw kanban-commit race a
  write; `/comment` on a dispatch card whose `action.task_id` is stale
  logs "session delivery skipped (bad task_id)" — comment still saves.
- Nothing new committed/pushed/deployed this session — the code was
  already on origin/main and live on ada-dev.

## Open request for Tony

`kanban-tool-verified-live-on-ada-dev-lis-550bf3` on this card: OK for
Ada to move the card review→done herself when it reaches review?
