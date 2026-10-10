# Dispatch outcome — ada-tv-action-hallucinated-input

Session: 20261010-080541-in-ada-pi-tv-action-1-refuse-c
Date: 2026-10-10

## What changed

**1. `tv_action cmd=type` placeholder/missing-text refusal**
(`backend/tool_runner/screens.py` + `ha.py`)

- `ScreensMixin._tv_type_text_problem(text)` refuses a type payload when
  the effective text (`text=` or a payload packed into cmd, e.g.
  `"type your-username"`) is empty or matches a placeholder class:
  `your-*`/`your_*`/`your <x>`, `<...>`/`{...}` bracket scaffolds,
  all-x strings (`xxx`, `XXXX`), and mask/ellipsis payloads
  (`***`, `•••`, `...`). Placeholder matching is capped at 40 chars so
  real sentences can't trip a mid-string `<x>`/brace match.
- `HaMixin.tv_action` raises `ValueError` before the owner-ACL and before
  the call leaves the runner; `execute()` converts it to the canonical
  `{ok: false, error}` result and attaches the `tool_guide.yml` usage
  hint. The refusal text tells the model to ask for the literal text or
  reach the field via click/press. Direct cause: journal ada-ha-tony
  16:21:50–16:22:03 ICT showed `type your-username`/`your-password`
  typed verbatim into a TV login form.

**2. Narration guard — media title/playing-state grounding**
(`backend/realtime_provider.py`)

- New `_media_grounding_problems(text, turn_tool_results)` runs at
  `turn_complete` next to the existing `phantom_write_claim` check. It
  detects play-state assertions (`playing`, `กำลังเล่น`, `เล่นอยู่`,
  `กำลังฉาย`, `ฉายอยู่`, negation-guarded) and named titles (quoted
  spans — lookbehind keeps `It's` from opening a fake span;
  `now playing X`; `X is playing`; Thai `เพลง/วิดีโอ/คลิป <latin>`), and
  requires each claim to literally appear in the turn's tool-result
  blob. Instructive result fields (error/usage/warn/note/hint/reminder)
  are scrubbed so our own "do not claim it is playing" admonitions are
  never evidence.
- Violations emit a `phantom_media_claim` ops event (same
  ada-ha-events audit lane as `phantom_write_claim`). Replays the
  incident exactly: "Dancing In The Street" over `cast_verify=paused`
  flags; a title reported by a `yt action='status'` result is clean.
- Prompt contract: new `TITLE/PLAYING-STATE CLAIMS` bullet in the voice
  system prompt (after DONE MEANS DONE) + the dictated-text-only rule in
  the `tv_action` `text=` param description and `backend/tool_guide.yml`.

## Files touched

- `backend/tool_runner/screens.py` — `_tv_type_text_problem` + patterns
- `backend/tool_runner/ha.py` — cmd=type guard in `tv_action`
- `backend/realtime_provider.py` — media guard, prompt bullet, param desc
- `backend/tool_guide.yml` — cmd=type contract
- `tests/test_tool_runner.py` — `TvTypeGuardTests` (6 tests)
- `tests/test_provider_events.py` — `MediaClaimGuardTests` (7 tests)
- `tests/scenarios/tv_type_placeholder.yaml` — offline regression
- `tests/scenarios-live/tv_whats_playing_grounded.yaml` — live probe:
  "what's playing" must issue a status/state read before answering
- `tests/benchmark.yml` — scenario registered in `write_allowed_in` +
  `casting` suite
- `docs/ssot/jobs/ada/2026-10-10-tv-action-hallucinated-input.yml` — job record

## Result

All green in an isolated `.venv-test` (httpx/google-genai/pyyaml/pillow/
numpy/pytest; venv removed after runs):

- `pytest tests/test_tool_runner.py -k TvTypeGuard` — 6/6 pass:
  placeholders refused (`ha_client.tv_action` never awaited), dictated
  text passes, `execute()` returns `ok:false` + usage hint.
- `pytest tests/test_provider_events.py` — 35/35 pass (incl. 7 new
  media-guard tests).
- `pytest tests/test_tool_runner.py tests/test_scenarios.py` — 249 pass,
  no regressions.
- `scripts/tool-lint.py` — 3 pre-existing violations (voice_fx
  descsize/coverage, yt_cached_list impl); none introduced here.

## How to verify

    pytest tests/test_tool_runner.py -k TvTypeGuard -q
    pytest tests/test_provider_events.py -k MediaClaimGuard -q
    pytest tests/test_scenarios.py -k tv_type_placeholder -q
    python3 scripts/scenario-live.py tests/scenarios-live/tv_whats_playing_grounded.yaml   # live, reads TV state

Live behavioral checks: `tv_action cmd=type text=your-username` returns
`ok:false "cmd=type refused"`; a "what's playing" answer with no
same-turn evidence emits a `phantom_media_claim` doc in
`ada-ha-events-<instance>` (hourly chaba ops digest).
