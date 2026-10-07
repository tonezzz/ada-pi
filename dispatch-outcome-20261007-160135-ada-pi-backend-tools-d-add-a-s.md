# Dispatch outcome — speaker-profile-tool

**Card:** speaker-profile-tool · **Task:** 20261007-160135-ada-pi-backend-tools-d-add-a-s
**Result:** implemented, unit-tested, lint-clean (for the new surface); live scenario authored, host run pending.

## What changed

**New tool** — `backend/tools.d/speaker_profiles.py` + `backend/tools.d/manifest.yml`
entry. Actions:

- `list` — enrolled profiles with ha_person, display_name, samples, media flag, aliases.
- `match` — scores the session's buffered voice against every profile via
  `SpeakerSession.match_buffer()`; returns best candidate + score **below**
  threshold too, plus a `say` hint ("you're enrolled as X — merge?").
- `remove` — deletes a profile; `name=` accepts key, slug spelling, alias,
  display_name, or `person.x`.
- `re-enroll` — force-replaces the resolved canonical profile's prints with the
  current buffer (stale/poisoned-print recovery).
- `alias` — binds a profile to an HA person (`person='kk'` or `person.kk`),
  auto-seeding aliases (friendly name + entity slug) so `person.kk` ↔ `พรศิริ`
  resolve to one identity; `rename_to=` renames the key and keeps the old name
  resolving as an alias.

**Mutation gate** (in-module `_may_mutate`, since manifest policy is flat):
full-access identity (admin or `{full: true}` person/control policy), or a
non-owner may only touch the profile bound to their own `person.*`; secondary
turns never mutate.

**`backend/speaker_id.py`** — added `EnrollConflict(ValueError)` carrying
`matched`/`score`; `resolve_name()` (key → slug → ha_person → display_name →
aliases → canonical, None when ambiguous); `rename()` (moves prints/metadata,
records old key as alias); `score_audio()` (ranked per-profile scores);
`aliases` persisted in profile metadata JSON (load/save round-trip);
`SpeakerSession.match_buffer()` + `_buffer_pcm()` refactor shared with
identify/enroll. Fixed while touching: `enroll()` now resolves the incoming
name to its canonical key before the contamination guard (enrolling under an
alias merges into the canonical profile instead of self-conflicting), and
`remove()` uses `resolve_name()` (alias/person deletes work).

**`backend/tool_runner/memory.py`** — `ada_enroll_speaker` refusal now returns
`{error, matched_profile, match_score, suggest}` so the model names the
existing profile and offers a merge instead of looping. Also fixed a
pre-existing bug: the success path built `out` but never returned it.

**`backend/memory_ops.py`** — `ada_forget` rejects speaker-profile-shaped keys
(`speaker*/voiceprint*/voice*` prefixes) with a redirect message; when a key
has no memory doc but resolves to an enrolled profile, it errors pointing at
`speaker_profiles action='remove'` instead of "no such document". Real memory
docs always win.

**Stretch** — `CONFIRM_TOKEN_TTL_S` default 120 → **300** in
`backend/tool_runner/common.py` (env override kept; handoff flows where the
session owner differs from the speaker expired mid-exchange).

**Guidance/SSOT** — `backend/tool_guide.yml`: `speaker_profiles` playbook +
`ada_enroll_speaker` "don't retry — call match, offer merge" note.
`docs/ssot/ssot.tool-surface.yml`: `count_cap` 106 → 107. Job trail:
`docs/ssot/jobs/ada/2026-10-07-speaker-profile-tool.yml`.

**Tests** — `tests/test_speaker_profiles_tool.py` (28 tests: loading, all
actions, mutation gates, secondary ban, alias/rename/roundtrip, alias-enroll
canonical merge, forget redirect). `tests/scenarios-live/
speaker_profiles_merge.yaml` encodes the card's kk-wrong-name flow: enroll
synthetic voice → re-enroll under second name → refusal names profile +
`speaker_profiles` engaged (max_calls 4 anti-loop assert) → merge resolves.

## Verify

- `python3 -m pytest tests/test_speaker_profiles_tool.py tests/test_speaker_id.py tests/test_memory_banks.py -q` → **136 passed**
- `python3 scripts/tool-lint.py` → 41 declared (37+4 tools.d), cap 107,
  scenario-covered ✓. Two FAILs (`ada_track_device` descsize,
  `ada_device_acl` coverage) are **pre-existing on base** (verified via stash).
- Live run pending a host with the backend + mic path:
  `python3 scripts/scenario-live.py tests/scenarios-live/speaker_profiles_merge.yaml --url ws://<host>:8003/ws --api-key $ADA_API_KEY -v`

## Notes / limitations

- The worktree's system Python lacks `google-genai` (in
  `backend/requirements.txt`); `backend.tool_runner` import chain fails
  without it — pre-existing env gap affecting several existing test files too.
  The new test file stubs the `google.genai` module chain when absent so it
  runs dep-less.
- `match` returns a sub-threshold best candidate as a hint, not identity —
  the `say` field tells the model to treat it as such.
- True embedding-level merge of two profiles isn't implemented — alias/rename
  covers the wrong-name case the card targets.
