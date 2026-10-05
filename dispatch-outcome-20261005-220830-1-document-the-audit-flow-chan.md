# Dispatch outcome — 20261005-220830-1-document-the-audit-flow-chan

Card: `ada-observability-resilience` (Ada observability: journald
suppression + durable tool-write retry)
Branch: `dispatch/20261005-220830-1-document-the-audit-flow-chan` —
commit `02b2c15` (11 files, +1036/-46). Not pushed, not merged, not
deployed.

## What changed

**1. Audit flow documented** — `docs/session-audit.md` (new runbook):
evidence tiers strongest-first: `reports/<date>-<sid>.json` →
`session_events` (journald-independent, the only surviving call ledger
during the 2026-10-05 storm), `call-logs/*.jsonl`, `transcripts/*.md`,
then `journalctl` last — always with a `grep -i suppressed` check before
trusting a journal gap. The ada-transcript-audit runner itself lives
outside this repo; a follow-up on the card flags updating it.

**2. Durable write outbox** — `backend/write_outbox.py` (new):
`MddbClient.add_document/update_document/delete_document` gained
`durable=True`; on failure the op lands in
`~/.local/share/ada/outbox/pending.jsonl` and a background task retries
with exponential backoff (5s→60s) for `ADA_OUTBOX_WINDOW_S` (default
900s) / `ADA_OUTBOX_MAX_ATTEMPTS` (12). Update ops replay the full
read-merge-write so retries re-merge onto the freshest doc. Expired
entries move to `dead-letters.jsonl` + emit an `outbox_dead` ops event +
queue a user-facing notice. Wired into `devin_answer` (answer doc +
job-status flip — the flip is queued even when the same outage ate the
job read), `cms_publish_page`, `cms_note_update`, `cms_delete_page`, and
memory_ops `remember`/`forget`/`record_outcome`/`persona` writes. Tool
results return `status="queued"` / `queued_for_retry` with an explicit
"do NOT describe as saved yet" note — no phantom saves. Each queue event
is logged to `session_events` (`write_queued`). Pending entries resume
retrying after a process restart on the first `execute()` call. The
provider drains dead-letter notices at each turn boundary via
`notify_or_defer(urgent=True)` — a permanent failure is spoken on the
next turn.

**3. Dropped-log mitigation** — every `function_call received` /
`function_call result` line is mirrored to
`call-logs/<date>-<sid>.jsonl` (default `<ADA_TRANSCRIPT_DIR>/../call-logs/`),
session-scoped, append-as-it-happens — journald suppression can't erase
it. `ADA_CALL_LOG=0` disables, `ADA_CALL_LOG_DIR` relocates.

## Result

- `tests/test_write_outbox.py`: 9 tests — queue/deliver/backoff/
  dead-letter/update-replay/restart-resume — all pass.
- `tests.test_scenarios + test_tool_runner + test_mddb_client +
  test_ops_events + test_memory_recall_extras`: 190 tests pass.
  FakeMddb gained the new kwargs; one cms_delete_page assertion updated.
- Full suite (618 tests): only pre-existing env errors
  (ai_edge_litert/pytest missing from the scratch .venv this worktree
  needed to create — no repo .venv existed).

## Verify

- `python3 -m unittest tests.test_write_outbox` (needs a venv with
  google-genai, httpx, pyyaml — this worktree's `.venv/` has them).
- Live: stop mddb → `cms_publish_page` returns `status: queued` →
  restart mddb → `pending.jsonl` empties and the page lands.
- `docs/ssot/jobs/ada/2026-10-05-observability-resilience.yml` records
  the job trail + follow-ups (deploy to idc01, update the external
  ada-transcript-audit runner, route the HA event-recorder flush through
  the outbox).
