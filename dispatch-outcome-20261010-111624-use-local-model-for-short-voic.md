# Dispatch outcome — use-local-model-for-short-voice-commands

## What changed

A new **local-model command lane** (`backend/command_lane.py`) now handles short
command-shaped turns through the local `/v1/systemone` fleet instead of always
spending a Gemini Live turn.

- **Candidate gate**: word cap (`ADA_COMMAND_LANE_MAX_WORDS`, default 12) plus a
  character cap so unsegmented Thai commands pass but long single-token text is
  rejected.
- **Deterministic replies**: time/date questions (EN + TH — "what time is it",
  "what day is today", "กี่โมง", "วันอะไร") are answered with zero model calls.
- **Two-hop model routing** (same shape as the bench `tool` domain):
  1. route question → `remember|recall|control|answer|search|pass`
  2. for `control`, entity pick over the real HA controllable set (token-overlap
     prefilter caps criteria at 30; a pick outside the offered list is treated
     as a hallucination and falls through).
- **Deterministic direction verbs**: state-checks first ("is the fan on" is a
  read, not an actuation), then on/off, then media/cover verbs. Verbs translate
  per picked domain — `on`/`off` for switchables, `action=` for
  media_player/cover/button; unmappable directions never actuate.
- **All actuation goes through `tool_runner.execute`** — confirmation gates,
  owner/speaker identity, secondary-speaker restrictions, storm breaker, and
  ADA_READ_ONLY apply unchanged. Gate denials and runner failures deliberately
  fall through to the normal provider turn rather than a flat machine reply.
- **Corpus**: every probe/handle appends a row to
  `~/.local/share/ada/command-lane-corpus.jsonl` (`ADA_COMMAND_LANE_CORPUS`) —
  input, surface, route, entity pick, confidence, direction, handled/fell
  through, tool result, session, latency. These rows are the promotion evidence.

## Modes (`ADA_COMMAND_LANE`)

- `off` — disabled.
- `shadow` (default) — classify fire-and-forget on voice + text, record corpus
  rows, emit `command_lane` ops events when the lane *would* have served.
- `enforce` — text turns: a confident servable route executes through the
  runner and replies without a Gemini turn; a silent context note is queued to
  the live session so Ada knows device state may have changed. **Voice stays
  advisory in every mode** — audio already streams to Gemini Live before the
  transcript exists, so enforcement on voice needs a local STT stage upstream
  (voice-rainy-day card).

## Wiring

- `backend/realtime_provider.py` — after `user_transcript` at turn_complete,
  `command_lane.probe(transcript, surface="voice", tools=<this turn's tool
  names>, emit=_emit_ops_event)`.
- `pwa_server.py` — ws chat text turns: `enforced()` → `try_handle` with the
  session owner/caller identity (same `(owner or speaker)` convention as the
  provider's tool calls); `armed()` → `probe(surface="text")`.

## Config (.env.example documented)

`ADA_COMMAND_LLM_URL` (falls back to `ADA_JEV_URL` — note a noul-only jev
student answers choice questions with conf 0, i.e. the lane is inert on those
endpoints), `ADA_COMMAND_LANE`, `ADA_COMMAND_LANE_MAX_WORDS`,
`ADA_COMMAND_LANE_MIN_CONF` (0.65), `ADA_COMMAND_LANE_TIMEOUT_S` (8),
`ADA_COMMAND_LANE_CORPUS`.

## Verification

- `python3 -m unittest tests.test_command_lane` — **32 tests, OK** (unittest
  runner used; dispatch host lacks pytest in system python).
- `ADA_INSTANCE_ID=tony python3 -m unittest tests.test_tool_runner` — **283
  tests, OK**.
- `python3 -m py_compile backend/command_lane.py backend/realtime_provider.py
  pwa_server.py tests/test_command_lane.py` — clean.
- `python3 scripts/tool-lint.py` — same **3 pre-existing violations as HEAD**
  (voice_fx descsize/coverage, yt_cached_list undeclared impl); unrelated.
- **Live systemone smoke** against `http://100.123.163.11:8777` (gemma-3-4b,
  open-jev, tailnet): route → `control` conf 0.9999 on "turn off the bedroom
  fan"; entity pick → entity conf 0.854. **Latency caveat**: 4b-CPU took
  27–45s/call — point `ADA_COMMAND_LLM_URL` at a gemma-3-1b lane
  (idc03:8779 / tony-omen:8778 / 100.75.102.88:8778, ~1 cpu-s/call) or raise
  `ADA_COMMAND_LANE_TIMEOUT_S` to ~60. A timed-out classify just falls
  through, so the default is safe everywhere.

## Files

- `backend/command_lane.py` (new, ~550 lines)
- `backend/realtime_provider.py` (import + advisory probe)
- `pwa_server.py` (import + text-turn lane integration)
- `.env.example` (documented lane env vars + latency guidance)
- `tests/test_command_lane.py` (new, 32 tests)
- `docs/ssot/jobs/ada/2026-10-10-command-lane.yml` (new — durable trail)

## Notes / limits

- No commit, push, deploy, or PR — worktree only, as instructed.
- Voice promotion to enforce needs local STT upstream (voice-rainy-day) and
  corpus evidence; text enforce is safe to try now on a 1b endpoint.
- Entity-pick accuracy on the real home inventory is unproven — the corpus is
  the measurement instrument; shadow mode on a live endpoint will fill it.

## One-paragraph summary

Implemented the local-model short-command lane: `backend/command_lane.py`
classifies short turns through the existing `/v1/systemone` fleet (route +
entity pick, deterministic direction/time-date handling) and executes only
through `tool_runner`'s existing gates; voice turns get a fire-and-forget
advisory probe in `realtime_provider.py` (audio still goes to Gemini —
advisory in every mode), ws text turns in `pwa_server.py` can be locally
answered in `enforce` mode, every decision lands in a promotion-evidence
corpus JSONL, and all env knobs are documented in `.env.example` — verified
with 32 new unit tests (all pass), the 283-test tool-runner suite (all pass
with `ADA_INSTANCE_ID=tony`), a live smoke against the gemma-3-4b endpoint
(correct route+entity, but 27–45s/call on CPU so a 1b lane is recommended),
and `tool-lint.py` showing only the 3 pre-existing HEAD violations; SSOT
trail at `docs/ssot/jobs/ada/2026-10-10-command-lane.yml`, no commit/push/
deploy performed.
