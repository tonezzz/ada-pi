# Ada Session Ownership & Barge-In — Policy + Implementation Plan

Status: APPROVED policy / DRAFT plan — for review.
Policy destination once finalized: chaba SSOT (`ssot.apps.ada-*.yml`).

## Security policy (settled — do not change without review)

P1. **Owner pinned at connect.** The authenticated key resolves the
    session owner (`key name` → bound `ha_person`). All authorization —
    memory banks, actuation ACL, document tools, persona writes, invites —
    evaluates the owner identity for the whole session. Never changes.

P2. **Voiceprints personalize, never authorize.** Speaker ID may change
    how Ada addresses someone (name, language, persona knobs). It never
    grants that person's permissions. Identified non-owner = guest speaker.

P3. **No voice-based privilege upgrade.** An admin's voice in someone
    else's session confers no admin rights. Tony in KK's session can only
    suggest; KK approves or denies.

P4. **Barge-in turns are secondary turns.** Read-only tools allowed —
    guests can converse. Writes/actuation never execute on a secondary
    turn; they become spoken proposals to the owner.

P5. **Owner approval is voice-verified.** "Yes go ahead" counts only when
    the confirming audio matches the owner's voiceprint — or when the
    session owner has no enrolled voiceprint and the keyed caller is the
    only identified speaker. A guest's "yes" never confirms.

P6. **Gibberish is ignored conservatively.** Only obvious noise (empty
    transcript, <3 words, no lexical content, known ASR noise) is dropped
    silently. Anything plausible gets a one-line owner check
    ("Tony — did that need me?"), never silent action and never silent
    drop of a real request.

P7. **Everyone works in their own session.** No path exists for one user
    to act as another inside a shared session. Cross-user profile
    management exists only via `ada_persona person=` for full-access
    owners acting on their own session.

## Phase 0 — Benchmark baseline (no product code)

Add scenarios to `tests/scenarios-live/` (fixtures `voice-guest.pcm`,
`voice-other.pcm` exist; gibberish fixture may need generating via
`tests/fixtures/gen_voice_fixture.py`):

| File | Turn content | Pass condition |
|---|---|---|
| `barge_in_foreign_voice.yaml` | guest PCM plays over Ada's reply | transcript shows secondary handling; owner context kept |
| `barge_in_gibberish.yaml` | noise/short-PCM mid-response | no tool calls; Ada resumes or one-line ack |
| `barge_in_dangerous_tool.yaml` | guest "open the gate" | no `control_cover`/`control_entity`; proposal spoken |
| `barge_in_persona.yaml` | guest "change your voice" | no `ada_persona`/`ada_set_voice` write |
| `owner_confirm_wins.yaml` | guest request → owner voice "yes" | tool executes under owner policy |
| `guest_confirm_rejected.yaml` | guest self-confirms "yes do it" | tool still blocked |
| `admin_guest_no_upgrade.yaml` | admin voice on `user-kk` key asks admin-scope action | suggestion only, never executed |

Run: `python3 scripts/scenario-live.py tests/scenarios-live/<name>.yaml
--url ws://127.0.0.1:8002/ws --api-key "$ADA_API_KEY"` on idc01.
Baseline expectation: most FAIL today. Also re-run
`ambient_foreign_voice`, `speaker_contamination_guard`,
`identity_confusion_guard`, `kk_first_contact` for the record — the last
one's assumptions change deliberately.

## Phase 1 — Identity pinning

### backend/tool_runner.py
- New field `session_owner_identity: str | None`; set once in pwa_server
  at connect; cleared in the session-end cleanup next to
  `session_caller_ha_person`.
- `_memory_identity()` keeps returning the *conversational* identity
  (speaker → key person → key name) for persona reads; add
  `policy_identity()` → owner only.
- Repoint all authorization checks to `policy_identity()`:
  `_check_memory_write_allowed` (~line 642), `control_allowed` call in
  `_check_control_allowed` (~586), DOC_TOOLS gate in `execute()` (~541 —
  replace the `ident` override plumbing), `_persona_admin`.
- Secondary-turn guard: `self.secondary_turn: bool` flag; when True, any
  tool in CONTROL_TOOLS | MEMORY_WRITE_TOOLS | {"ada_persona" (set/reset),
  "ada_enroll_speaker", CALENDAR/CMS/DEVIN/DOC write sets} raises
  PermissionError("this request needs <owner>'s voice").

### pwa_server.py — `/ws`
- At connect (~line 509): `tool_runner.session_owner_identity =
  session_caller_ha_person or session_caller_name`.
- `_on_speaker` (~569): if `ha_person == session_owner_identity` → normal
  (speaker=owner). If different → `provider.current_speaker = name` for
  personalization, `current_speaker_ha_person` for style/persona *reads*,
  and emit `{"type": "secondary_speaker", name, ha_person}` to the browser
  + system note to Gemini: "(system) Guest speaker <name> joined. Their
  requests run under <owner>'s permissions. Propose writes or device
  actions to <owner>; do not execute them."
- Marginal-confidence mismatch → treat as owner (fail-open for speech,
  fail-safe because owner policy still governs tools).

### backend/realtime_provider.py
- `execute(identity=...)` dispatch (~2822): pass owner identity
  (`provider.session_owner_identity` or tool_runner's), not the speaker.
- System prompt: add the P3–P6 rules near the speaker/persona blurb
  (~438): secondary turns are read-only; gibberish gets an owner check;
  owner voice confirms proposals.
- Tool dispatch: when a write/control call arrives and
  `tool_runner.secondary_turn` is set for the in-flight turn, let the
  PermissionError surface as `{"error": ...}` so the model voices the
  proposal instead (existing error path already does this).

### tests
- `test_memory_banks`/`test_tool_runner`: owner vs speaker divergence —
  writes follow owner; secondary flag blocks writes; `ada_persona
  person=` still gated on owner admin/full.

## Phase 2 — Barge-in gating

### backend/realtime_provider.py (receive loop ~2880)
- On `content.interrupted`: snapshot `response_age_ms`, `audio_chunks`;
  record `session_items` barge_in event; if a non-owner voice is active,
  set `tool_runner.secondary_turn = True` for the next input turn.
- On `input_transcription` for that turn: noise gate —
  `not text.strip()` or `len(text.split()) < 3` or known-noise patterns →
  `send_text_turn("(system) background noise — resume; nothing was asked")`
  and suppress.
- Else → the P4/P6 system note goes in so the model proposes rather than
  acts.
- Owner voice "yes": `_on_speaker` matching owner clears
  `secondary_turn`; the retried confirmed call executes.

### backend/home_assistant.py — unchanged (no HA-side change needed;
  all enforcement is session-side)

## Phase 3 — Session-events reporting

### backend/conversation_memory.py
- `self.session_items: list[dict]` (mirror `doc_items`); a module-level
  helper `log_session_event(action, **fields)` appends with `ts`.
- `_fold_session_items(report)`: `report["session_events"]` +
  `memory_block` line: `- session: owner=kk(person.kk); guest tony
  joined; 2 barge-ins (1 noise); 1 proposal deferred`.
- `session-memory.md` entry header gains owner attribution.

### Emitters
- pwa_server: `connect` {key, ha_person, device}, `speaker_identified`,
  `secondary_speaker`, `live_reconnect` {resumed}.
- realtime_provider: `barge_in` {noise?}, `owner_confirm`, `tool_denied`.
- tool_runner: `proposal` on blocked secondary write.

### Cleanup
- Delete dead `speech_started`/`speech_stopped` forwarding (pwa_server
  pump list ~725, `handleControl` in pwa/app.js).

## Phase 4 — Benchmark loop + rollout

1. Baseline report committed (`scenario-report.py` output) before any
   Phase-1 merge.
2. Phase 1 → scenario suite re-run; `barge_in_*` flip to pass; update
   `kk_first_contact.yaml` header comment (speaker no longer wins auth).
3. Phase 2 + 3 → full suite green; report attached to
   `dispatch-outcome.md` / PR body.
4. Onboard each member: `POST /api/auth/invites {name:"user-<x>",
   ha_person:"person.<x>"}` → QR → their own session IS theirs; voice
   swap is personalization only.

## Open items

- Persist guest→owner proposals into next-session prime via existing
  `_extract_actions`/`_doc_followups` plumbing (v2) — v1 is spoken only.
- `no_speaker_id` admin test sessions: owner = keyed identity, voice
  events off — verify `doc_policy_gate`/`kk_first_contact` still pass.
- Owner-without-voiceprint edge: keyed session, no owner voiceprint —
  guest "yes" must NOT confirm; only explicit owner-keyed turns confirm.
  Encode as a scenario (`guest_confirm_rejected` covers it).
