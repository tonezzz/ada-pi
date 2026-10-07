# Dispatch outcome — ada-pi realtime_provider three guards

Card: `ada-dead-turn-guard` (session 56d2e4d167 — bare "response:" dead
turns twice over a memory search that already held the plate 5ขว 6249;
tool-call text leaked into transcript; hits injecting whole 30-50KB docs).

## What changed

**`backend/realtime_provider.py`**

1. **Dead-turn detector** — `_dead_turn_text()` flags turns whose
   accumulated output is empty or degenerate: bare scaffold prefixes
   (`response:`/`answer:`/`output:`/`assistant:`/`text:`/`transcript:` +
   Thai คำตอบ/ตอบ/ผลลัพธ์), lone structural tags, punctuation-only.
   At `turn_complete`, when the turn was owed an answer (user speech,
   open turn, tool calls, or response activity) and the text is
   degenerate — or empty with zero audio bytes — the scaffold is NOT
   written to the transcript. Instead a `dead_turn` ops event fires and
   the provider deterministically retries ONCE: a `[system]` nudge via
   `send_client_content` restates the question and hands the model a
   digest of that turn's tool results (the plate is literally in the
   nudge). If the retry also lands dead, `_dead_turn_fallback()`
   synthesizes the answer from the results — `"พบแล้วค่ะ <top hit first
   line>"` for memory search, degraded searches keep announcing
   degraded, failed results say so, and result-less dead turns get an
   honest retry-ask. The dead bubble is closed with
   `response_completed` before the retry so the real answer renders as
   its own reply.
2. **Tool-call text sanitizer** — `_strip_tool_leak` now ORs the
   declared-name regex with `_GENERIC_TOOL_LEAK_RE`
   (`identifier{key:value` / `key=value`, any identifier — hallucinated
   tool names no longer slip past the declaration list). Ops event
   renamed `transcript_tool_leak` → `leak_detected` per the card.
3. **Memory-hit truncation** — `MEMORY_HIT_CONTENT_MAX` (env
   `ADA_MEMORY_HIT_MAX_CHARS`, default 2048) in
   `backend/tool_runner/memory.py`: `_cap_hit_content()` bounds every
   hit's `content` in `ada_memory_search` at the runner boundary —
   banks/sessions/guest scopes all covered regardless of upstream caps.
   key/subject/score/meta survive; `content_truncated` marker added;
   `degraded` flag preserved.

Confirm-gate (`confirmed=true`/`pending_confirm`) untouched.

## Regression test

`tests/test_dead_turn.py` — 13 tests. Replays the plate-question turn
shape: stubbed Live session delivers user transcript →
`ada_memory_search` returning hits containing `5ขว 6249` → model output
`"response:"`. Asserts the user-facing answer ends with the plate, the
literal scaffold is never persisted, the retry nudge carries the digest,
a second dead turn triggers the synthesized fallback, and a degraded
result still announces degraded.

## Result

- `unittest discover -s tests -k dead -v`: **17 tests OK**
- `pytest tests/ -q` (borrowed `~/CascadeProjects/ada-pi/.venv`):
  **799 passed, 2 failed — both pre-existing tool-lint violations**
  (`ada_track_device` descsize, `ada_device_acl` coverage) confirmed
  failing on the clean tree before this diff.
- Card checks: `leak_detected` present in realtime_provider.py;
  `2048`/`truncated` present in backend/tool_runner/memory.py.

## How to verify

- `python3 -m unittest discover -s tests -k dead -v`
- Post-deploy on idc03: grep session call-logs/transcripts for
  `dead_turn` ops events; a replayed "response:" turn should produce one
  retry then the spoken answer.
- SSOT trail: `docs/ssot/jobs/ada/2026-10-07-dead-turn-guard.yml`
