# ada-output-hygiene-tags — Ada speaks internal tags aloud + drifts to English mid-Thai session

## Symptoms (from the card)

- Session 67b02417a8: Ada literally spoke "(ha_safety: caution)" and
  "(kanban list review)" — parenthesized internal tool/safety
  annotations reached speech.
- Session e9837ae922: slipped to English 3x mid-Thai-conversation
  ("ทำไมเปลี่ยนภาษา").

## What changed

Three layers, matching the Live architecture (the model synthesizes its
own audio, so voice output can only be prevented upstream — the
transcript strip keeps it out of memory/audit and covers the real
pre-TTS path, `vcast_say` → Gemini TTS):

### backend/speech_sanitize.py

- `_INTERNAL_TAG_RE` strips parenthesized internal annotations: ≥2
  all-lowercase `[a-z0-9_]` tokens joined by space/colon/comma inside
  parens — "(ha_safety: caution)", "(kanban list review)" match;
  "(draft)", "(I think)", "(โอเค)", "(system)" survive.
- `_INTERNAL_TAG_OPEN_RE` drops the same shape unclosed at end-of-text.
- `ARTIFACT_TAIL_RE` now holds a trailing `(...` fragment so a tag split
  across deltas is never emitted half-complete.

### backend/realtime_provider.py

- `DEFAULT_ADA_INSTRUCTIONS`:
  - SPEECH IS PLAIN TEXT now also bans parenthesized internal notes,
    tool names, safety levels, and stage directions from being voiced.
  - LANGUAGE FIDELITY gains a mid-reply lock: once a reply starts in a
    language it stays in it to the end of the turn; a lone English term
    inside a Thai sentence is fine, a whole English clause is a slip.
- Language-drift guard (runtime):
  - `_count_scripts` / `_assistant_drifted_english` — Latin-dominant
    reply (≥30 latin letters, latin > thai) while the user is
    Thai-locked = drift. The floor keeps Thai sentences with English
    terms from tripping it.
  - `self._last_user_thai` tracks the last clearly-dominant user script
    (≥3 Thai letters wins Thai; ≥8 Latin letters at >2× wins English;
    filler like "ok" doesn't flip the lock).
  - On drift: `logger.warning`, `language_drift` ops event for the
    transcript audit, and a **silent** language-lock note via
    `_send_context_note` (turn_complete=False — lands in context without
    prompting a reply). Capped at 2 per session — a stubborn model needs
    the ada-context-budget session rotate, not more notes.
- `_ARTIFACT_HINT_RE` gains `(` so paren-tag strips log like other
  artifact strips.

### tests/test_output_hygiene.py (new, 15 tests)

- Verbatim-leak strips, natural-parenthetical survival, unclosed tail,
  paren-fragment holdback, tag split across deltas.
- Drift detector thresholds, end-to-end events-loop test: Thai input +
  English reply → `language_drift` ops event + one silent
  turn_complete=False lock note; Thai reply → none; cap honored; filler
  "ok" turn doesn't unlock Thai.

## Verification

- `python -m unittest tests.test_output_hygiene` — 15/15 OK.
- `python -m unittest tests.test_speech_sanitize tests.test_transcript_leak
  tests.test_dead_turn tests.test_context_budget tests.test_provider_events
  tests.test_text_channel` — 93/93 OK.
- Full discover (1096 tests): 5 failures, all **pre-existing** —
  verified `tests.test_tool_audit` fails identically on the stashed
  baseline (voice_fx descsize/coverage, yt_cached_list undeclared;
  test_eye_tool.LiveTest ×3 need live hardware).

Note: for Live voice sessions the sanitizer cleans the *transcript* —
the audio was already spoken; prevention is the prompt clause plus the
annotation never being re-parroted from stored context. The true
pre-TTS strip applies to `vcast_say`/backend Gemini TTS.
