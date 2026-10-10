# Dispatch outcome — ada-reads-text-chat

Card: `ada-reads-text-chat` — "Ada cannot read the text-chat channel —
unify voice+chat history per Tony ask" (ระบบรวมการคุยทั้งสองทาง).
Runner: idc02 · Branch: `dispatch/20261010-091641-ada-cannot-read-the-text-chat-`
No commit, no push, no deploy.

## Diagnosis

The text-chat card and the voice PWA both ride the same `/ws` endpoint,
but each websocket owned an isolated `ConversationMemory`. Text turns
were recorded and persisted at session end, yet a live voice session
could never see a live chat session's turns, and new sessions primed
only from general memory plus a lossy last-session tail — so Ada
correctly answered "no" when Tony asked whether she could read the
chat channel.

## What changed

**Shared channel history (`backend/conversation_memory.py`)**
- `_channel_log`: process-wide ring buffer (default 300). Every
  non-`no_persist` session appends user/assistant turns on
  `add_user`/`add_assistant` with `{session_id, owner, channel, role,
  text, ts}`.
- `channel_tail_text(owner, exclude_session, ...)`: owner-strict,
  age-capped (6h), turn-capped (8 turns / 1400 chars), channel-labelled
  (`Ada [chat]:`, `User [telegram]:`).
- `record_channel_tail` / `read_channel_tails`: per-owner+instance
  rolling tail docs (`channel-tail-<owner>-<instance>`) upserted into
  the **shared** `ada-ha-recall-summary-shared` collection — deliberately
  not instance-suffixed, so `ada-pi-pwa` chat turns are readable by
  `ada-ha-tony` voice, across backends and restarts. Ops-store routed
  (no embedding burn).
- `ConversationMemory.channel` / `.owner_identity` attrs;
  `add_channel_mirror()` writes a `[via <channel>]` transcript line
  marked `mirrored=True` so it never re-logs or re-echoes.

**Live mirroring (`pwa_server.py`)**
- `_mirror_turn()`: on every user turn (voice transcript, chat text,
  image caption) and completed assistant reply, same-owner live sibling
  sessions receive: (a) the labelled transcript line, (b) a silent
  `queue_context_note()` on their provider (`turn_complete=False` —
  context only, never triggers a reply), and (c) a `channel_activity`
  ws event.
- `_flush_channel_tail()`: upserts the shared persisted tail after each
  turn and at session end (`_mark_session_end`).
- `_channel_prime_context()` + `session_prime_text(channel_context=)`:
  session prime now injects "Your other chat channels … share one
  history" with the merged live + persisted tail. Reconnect re-prime
  passes the same `ConversationMemory`.
- Session setup pins `conversation.channel` (`voice`/`chat`/`telegram`/
  `line`) and `conversation.owner_identity` (policy-P1 owner, set at
  connect).
- `/api/chat/transcript` now returns `channel_history` (raw shared log,
  last 200).

**Provider (`backend/realtime_provider.py`)**
- `queue_context_note()`: public, silent — queues on
  `_pending_notifications`, flushes via the existing deferred
  `_send_context_note` path. Loop-safe for sync/unit-test callers.

**Clients**
- `pwa/cards/ada-chat-card.js`: ws URL gains `channel=chat` (text-only
  provider path, `[chat]` history label) and renders `channel_activity`
  as a dimmed `Ada/You [<surface>]` line.
- `pwa/app.js`: voice log renders `channel_activity` as a dim
  `[<surface>] Ada/You: …` line.

**Safety**: owner-strict matching everywhere (pinned at ws connect, no
cross-owner bleed); `no_persist` sessions neither send nor receive;
mirrors don't re-log; notes are `(system)` context-only and cannot
trigger replies.

## Tests

- `tests/test_channel_history.py` — **15 new tests, all pass**: log
  append, live mirror, labelled-not-relogged, `no_persist` exclusion,
  owner strictness, age/turn caps, channel labels, current-session
  exclusion, tail doc write/read ordering, stale + foreign-instance
  filtering, prime injection, provider note.
- Affected-module suite (`test_channel_history`, `test_text_channel`,
  `test_memory_recall_extras`, …) — 188 tests OK.
- Full 1097-test run: 5 failures + 4 errors, **all pre-existing or
  environmental**, verified against a clean baseline — missing
  `pytest`/`ai_edge_litert`, unreachable mddb, `test_tool_audit` lint
  debt (voice_fx desc length, undeclared `yt_cached_list`), and
  `test_eye_tool` tests that freeze `ts` at import and go "stale" after
  15 s in any long suite run.
- `py_compile` clean on all touched Python. `node` is not installed on
  this runner — the two JS edits (URL concat + a `case` + CSS) were
  verified by inspection only.

## How to verify live

1. Open the voice PWA and the text-chat card with the same owner
   (admin key or bound key).
2. Type in chat → the voice log shows `[chat] You: …` and Ada in a
   voice session can reference the message (mirror note + shared tail).
3. Speak to voice → the chat card shows `You [voice] / Ada [voice]`
   dimmed lines.
4. `GET /api/chat/transcript` → `channel_history` lists merged,
   channel-labelled turns.
5. Restart a backend, open a new session → the prime includes the
   persisted `ada-ha-recall-summary-shared` tail.

## Files

`backend/conversation_memory.py`, `backend/memory_ops.py`,
`backend/realtime_provider.py`, `pwa_server.py`,
`pwa/cards/ada-chat-card.js`, `pwa/app.js`, `.env.example`
(new `ADA_CHANNEL_*` tunables), `tests/test_channel_history.py`,
`docs/ssot/jobs/ada/2026-10-10-unified-voice-chat-history.yml`.
