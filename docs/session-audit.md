# Session audit — source-of-truth order

Status: **current** (2026-10-05). Companion to
`docs/memory-hierarchy-plan.md` (report hierarchy) and
`docs/session-security-policy.md` (event semantics).

## Why this doc exists

2026-10-05 incident (session `0d9530dbf7`): during an mddb `vector_search`
error storm, **journald rate-limiting suppressed 37+ messages and every
`function_call` line for a live 10-minute session vanished from
`journalctl`**. Transcript audits that read only the journal saw a session
that apparently called no tools — the only surviving evidence was
`session_events` inside the session report JSON.

**Rule: audit `reports/*.json` BEFORE journals. The journal is the
weakest evidence tier, not the primary one.**

## Evidence tiers, strongest first

1. **`reports/<date>-<sid>.json` → `session_events`** — in-process
   session-mechanics log (connect identity, speaker matches, tool
   calls + `dur_ms`/`ok`, denials, barge-ins, `write_queued`,
   `turn_latency`). Written at session end by `ConversationMemory`'s
   `_fold_session_items` — no journald involvement, cannot be
   suppressed. `turn` field links each event back to transcript turn n.
   Path: `~/.local/share/ada/reports/` on the voice host (or
   `ADA_TRANSCRIPT_DIR/../reports/`).

2. **`call-logs/<date>-<sid>.jsonl`** — session-scoped function_call
   mirror (NEW this job). Every `function_call received`/`result` line
   is appended to a plain file as it happens, so the call trace exists
   even when journald is suppressing. One JSON object per line:
   `{ts, session_id, event, id, name, args|result}`. Disable with
   `ADA_CALL_LOG=0`; `ADA_CALL_LOG_DIR` relocates.

3. **`transcripts/<date>-<sid>.md`** — raw user/assistant turns.
   Speech only — tool calls never appear here; pair with tier 1/2.

4. **journald (`journalctl -u ada-ha-*`)** — richest detail (full args,
   stack traces) but **lossy under burst**: `Suppressed N messages`
   lines mark dropped intervals. Always check for suppression before
   trusting a gap:
   `journalctl -u ada-ha-tony --since <t0> --until <t1> | grep -i suppressed`.
   A missing `function_call` line means nothing unless suppression was
   confirmed absent.

## Auditing a session (ada-transcript-audit flow)

```
1. Load reports/<date>-<sid>.json
   → session_events[]  : authoritative call/deny/barge timeline
   → documents[]       : doc-archive/print timeline
   → memory_block      : one-line session digest (owner, speakers, counts)
2. For each session_events entry kind=tool_call:
   → corroborate args/result in call-logs/<date>-<sid>.jsonl (same call id)
3. journalctl -u <unit> --since/--until  — deep detail + a suppression check.
   Treat the journal as annotation, never as the tool-call ledger.
4. transcripts/<date>-<sid>.md         — spoken side, when wording matters
```

`scripts/report-dig.py report:<date>-<sid>` resolves the report and lists
child refs to dig next; `transcript:<date>-<sid>#t<n>` jumps to a turn.

## Write durability: the outbox

A write tool (`devin_answer`, `cms_publish_page`, `cms_edit`,
`ada_remember`, `ada_forget`, `ada_ops` outcome, `ada_persona`) that hits
an mddb outage now passes `durable=True` through `MddbClient`, which parks
the payload in `~/.local/share/ada/outbox/pending.jsonl`
(`ADA_OUTBOX_DIR` overrides). A background loop retries with exponential
backoff (5s→60s) for a bounded window (`ADA_OUTBOX_WINDOW_S`, default
15 min, `ADA_OUTBOX_MAX_ATTEMPTS` 12).

- Tool result says `status: "queued"` / `queued_for_retry: true` — Ada is
  told explicitly NOT to describe it as saved yet.
- The enqueue is recorded as `write_queued` in `session_events`.
- On window exhaustion the entry moves to `dead-letters.jsonl`, an
  `outbox_dead` ops event is attempted, and the provider drains the
  notice at the next turn boundary — the user hears "the save failed"
  instead of silence.
- Pending entries survive restarts: the retry loop resumes on the first
  tool call of the new process.

Inspecting/repairing:

```bash
cat ~/.local/share/ada/outbox/pending.jsonl      # in-flight retries
cat ~/.local/share/ada/outbox/dead-letters.jsonl # permanent failures
# Re-submit a dead letter by hand (payload fields are op/collection/key/
# lang/content_md/meta — replay via POST /v1/add or /v1/delete):
jq -r 'select(.op=="add") | {collection,key,lang,contentMd:.content_md,meta}' \
  dead-letters.jsonl | curl -s http://127.0.0.1:11023/v1/add -d @-
```

## Known limits

- `session_events` is written at session *end* — a crash loses in-flight
  events. The call-log mirror (tier 2) is append-as-it-happens, so it is
  the call trace of last resort mid-session.
- The outbox guards mddb writes, not side-effect tools (calendar writes,
  casts, followup injection). Those still fail at call time and report
  the error to the model.
- Ops events (`_emit_ops_event`, `outbox_dead`) are best-effort mddb
  writes — during the outage that caused the dead letter they usually
  fail too; the dead-letters file is the durable record.
