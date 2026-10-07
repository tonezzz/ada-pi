# Dispatch outcome — report-loop-ada-opinion

Card: `report-loop-ada-opinion` (chaba kanban). Repo: ada-pi.
Design: chaba `docs/design/report-session-loop.md` §3b; convention:
chaba `docs/ssot/infrastructure/ssot.ada-participation.yml`
(`t1_comment .opinion_convention`).

## What changed

- `backend/realtime_provider.py` — appended a REPORT OPINION stanza to
  `CMS_INSTRUCTIONS` (~L207): on an opinion request, `cms_read
  action='get'` the page first (never opine unread) → `ada_board_write
  action='read' report=<slug>` to find the linked kanban card →
  `ada_board_write action='comment'` with text starting `[opinion]`
  citing slug + the updated timestamp read → also answer aloud (the
  comment is the durable record). No linked card → answer aloud and
  offer `action='create'`; ambiguity → say which card got the comment.
- `backend/tools.d/ada_board_write.py` —
  - `_compact_card` now passes the card `report` field through in the
    compact read payload (server `/cards` already emits it — verified
    live: `scenario-report-opinion` carries `report: dev-kanban`).
  - New `_match_report_card(cards, slug)` helper: open (non-`done`)
    cards outrank done; within each tier most recently `updated` wins;
    slug compare is case/space-insensitive.
  - `action=read report=<slug>` → `_report_lookup`: returns
    `{card, open_matches, done_matches, also[], note}` — deterministic
    matching instead of asking the model to scan the listing.
    done-only matches surface the done card with a comment-or-file
    note; no match → `card: null` + offer-to-file note.
  - `DECLARATION` gains the `report` param.
- `backend/tool_guide.yml` — `ada_board_write` entry documents the
  `report=` read filter and the `[opinion]` comment prefix.
- `tests/test_board_write.py` — new `ReportLookupTest` (7 tests):
  helper semantics, slug normalization, read `report=` paths
  (open/ambiguous/done-only/none), compact-payload field passthrough.
- `tests/scenarios-live/report_opinion_loop.yaml` — live scenario;
  `[opinion]` comms land on the `scenario-report-opinion` fixture card,
  the designed comms sink.
- `docs/ssot/jobs/ada/2026-10-07-report-opinion-loop.yml` — SSOT trail.

Deliberately NOT built: a separate `kanban_opinion` tool — design §6
says `action=comment` + `[opinion]` prefix is the designed path.

## Result / verify

- `../ada-pi/.venv/bin/python -m unittest tests.test_board_write` —
  21/21 green (7 new). tools_loader + provider_events + board_write =
  61/61 green under the project venv (system python lacks
  `google.genai`; use the venv).
- `python3 scripts/tool-lint.py` — only 2 pre-existing violations
  (`ada_track_device` descsize, `ada_device_acl` coverage; identical on
  stashed base).
- Live sanity: `_report_lookup` against `127.0.0.1:8787/cards` resolves
  `dev-kanban` → `scenario-report-opinion`, `no-such-slug` → card:null
  + offer-to-file note.
- Card pipeline: `scripts/ci/` does not exist in this worktree and the
  card YAML lives on the chaba board — skipped.

## Deploy

NOT deployed — per ada-pi AGENTS.md, deploy to idc03 needs Tony's
approval. Merge + `~/.local/bin/deploy-ada.sh` when approved; the
REPORT OPINION stanza takes effect on the next ada-ha restart.
