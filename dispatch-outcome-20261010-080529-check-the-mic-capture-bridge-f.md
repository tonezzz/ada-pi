# Dispatch outcome — ada-speech-capture-degraded

Branch `dispatch/20261010-080529-check-the-mic-capture-bridge-f` in this worktree. No commit, push, or deploy.

## Findings

**1. "0.0s captured — capture bridge may be dead" was session mis-selection, not a dead feed.**

`enroll_from_buffer()` emits that diagnostic only when `SpeakerSession._fed_bytes == 0` (`backend/speaker_id.py:1142+`). The mic feed itself is intact — `voice_socket` feeds every binary PCM16 frame into `speaker_session.feed()` gated only on TTS-bleed suppression (`pwa_server.py:1185+`). The real defects:

- `_CALLER_SPEAKER_SESSION` defaulted to `None`, and `ada_enroll_speaker` resolved the buffer via `ctx.get() or self.speaker_session`. An explicit `None` bound by a session with no voice buffer (text channel, speaker-ID off — and crucially any *fresh provider after a reconnect*, see below) silently fell back to `tool_runner.speaker_session`: whichever websocket session connected **last**. With a Telegram/LINE relay or a second tab open, enrollment read a different session's empty buffer → `0.0s` while the user was actively speaking. (`backend/tool_runner/common.py:265`, `memory.py:765`, `tools.d/speaker_profiles.py:88`)
- Text-only `?channel=` sessions created a `SpeakerSession` that could never be fed (relays send JSON text, never PCM) and **clobbered the shared `tool_runner.speaker_session`** for the entire process on every connect. (`pwa_server.py:983`)
- Provider reconnects created a fresh provider but never re-linked `provider.speaker_session` (defaults to `None`) and dropped `current_speaker`/`current_speaker_ha_person` — orphaning the bridge after every reconnect/rotation. (`pwa_server.py:1445`)

**2. Session-rotation amnesia was real: the live tail was never re-injected.**

`_context_rotate_when_idle()` clears `resumption_handle` and closes the provider (`realtime_provider.py:1857+`). The reconnect loop then created a fresh provider and called `_prime_session(new_provider)` **without** a reconnect tuple — so `session_prime_text` ran with `away_seconds=None, last_tail=""`: no resume directive, no transcript tail. `ConversationMemory` is shared across providers, but that's a Python object the new Gemini session can't see. The mid-conversation Lisa-clip context was simply never re-injected. (`pwa_server.py:1439`, `memory_ops.py:151`, `1264`)

## Changes

- `backend/tool_runner/common.py` — `_CALLER_SPEAKER_SESSION` default `None` → `_IDENTITY_UNSET` (same convention as `_CALLER_SPEAKER`): sentinel = non-ws caller → shared-field fallback OK; explicit `None` = this session has no voice buffer → never read another session's; session object = use it.
- `backend/tool_runner/memory.py` — `ada_enroll_speaker` uses sentinel-aware resolution.
- `backend/tool_runner/__init__.py` — `_is_secondary_turn` stale-label reader normalized to `.get(None)`.
- `backend/tools.d/speaker_profiles.py` — `_speaker_session` follows the same semantics; explicit `None` now returns `None` (honest "speaker ID is not active on this session").
- `pwa_server.py` —
  - skip `SpeakerSession` creation on `?channel=` text-only sessions (they can never be fed);
  - reconnect re-links `new_provider.speaker_session` and carries `current_speaker`/`current_speaker_ha_person` forward;
  - fresh-context reconnect primes with `reconnect=(0.0, conversation.recent_context(max_turns=12, max_chars=3000))` → `away_tier(0)` = RESUME: "same conversation resuming" + live tail; stale summary/headlines correctly skipped for <1h gaps;
  - `create_provider` on reconnect uses session-scoped `caller_name`/`caller_person` locals, not the shared runner fields;
  - teardown clears shared-runner bindings only when they still hold this session's values.

## Verification

- New regression tests, all green:
  - `tests/test_speaker_id.py::CaptureBridgeTest` — fed session enrolls with `duration_s > 0`; unfed session reports the bridge diagnostic (proves the diag is honest when genuinely unfed).
  - `tests/test_speaker_profiles_tool.py` — caller-bound session beats shared field; explicit `None` never reads a shared/decoy buffer; no-voice enroll returns "not active".
  - `tests/test_memory_recall_extras.py::test_rotate_reconnect_carries_live_tail` — `session_prime_text(away_seconds=0, last_tail=...)` carries the Lisa-clip tail and omits the stale session summary.
- Focused suites: `test_speaker_id`, `test_speaker_profiles_tool`, `test_memory_recall_extras`, `test_memory_banks`, `test_tool_runner`, `test_scenarios`, `test_context_awareness`, `test_summary_rollups` — **438 tests OK**.
- Full suite (951 tests): only pre-existing env failures (missing `fastapi`, `PIL`, `ai_edge_litert`) and pre-existing tool-lint debt (`voice_fx` descsize/coverage, `yt_cached_list` undeclared) — confirmed identical on the stashed baseline.
- `python3 -m py_compile pwa_server.py` — clean.

**Live verification** (enroll >0s on a real session; related questions across a rotation) could not be performed here — the worktree has no Gemini credentials/mic and no deploy was authorized. On staging (idc02): speak ~3s, invoke `ada_enroll_speaker`, expect `duration_s > 0`; run a long session past the context budget, then ask a follow-up on the pre-rotation topic. Watch `live_reconnect resumed=false` + `prime injected` log lines.

## Limitations / residual suspects

- The cited journal/transcript artifacts (b0698a4e9a, e47b155e99, 73d7713ce8) were not staged in the task dir or worktree — the exact 16:02 timeline couldn't be replayed.
- If the diagnostic still fires live on a real voice session, the remaining suspect is the TTS-suppression feed gate (`provider._response_active` / <0.6s-since-TTS at `pwa_server.py:1185+`) sticking on.
- Shared `tool_runner` fields remain racy across concurrent ws sessions at connect time (unconditional overwrite, `pwa_server.py:901-912`); contextvars are now the authoritative path — shared fields are legacy fallback only.

SSOT trail: `docs/ssot/jobs/ada/2026-10-10-speaker-capture-bridge-rotation.yml`.
