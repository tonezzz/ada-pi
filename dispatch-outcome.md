status: done

# Investigation: why Ada cannot view HA users / manage KK's persona

## Root cause (two distinct gaps)

1. **No user/person listing surface.** `HomeAssistantClient` only exposes
   `/api/states`, `/api/services/*`, `/api/history`, `/api/logbook` (REST)
   and `lovelace/config`, `person/create`, `config/auth/list` (WS). The
   only *user* list — `config/auth/list` — is `@require_admin` in HA core;
   a non-admin token gets `{"error": "unauthorized"}` → `RuntimeError`
   (used server-side only, pwa_server.py `_ha_guest_user_id`). No tool
   exposed person listing to the model (`DISCOVERABLE_DOMAINS` excludes
   `person`), so Ada could not even verify `person.kk` exists.

2. **`ada_persona` was current-speaker-only.** Identity came from
   `current_speaker_ha_person` or the key name (`session_caller_name`).
   There was no way to target another user's profile, and no way for a
   key (e.g. `user-kk`) to carry its HA person — unenrolled speakers fell
   back to the default `personal` bank (the KK leak noted in
   `tests/scenarios-live/kk_first_contact.yaml`).

## Changes

### backend/home_assistant.py
- `persons()` — lists `person.*` entities via `/api/states` (works with
  ANY valid token; no admin needed).
- `resolve_person(name_or_entity)` — 'KK' / 'person.kk' → person entity.

### backend/tool_runner.py
- `ada_persona` gains `action="list"` (HA people + persona bank + custom
  knobs) and `person` param for show/set/reset of another person's
  profile, gated by `_persona_admin` (name `admin` or `{full: true}` in
  person/control policies) or same-bank self-resolution.
- `session_caller_ha_person` field; `_memory_identity()` is now
  voiceprint → key-bound ha_person → key name.
- `ada_enroll_speaker` uses `resolve_person` (get_state fallback kept).

### backend/realtime_provider.py
- `ada_persona` schema: `list` action + `person` param; instructions updated.
- `caller_person` kwarg threads the key's bound person into tool dispatch.

### backend/auth.py — invite rewrite
- Key entries carry optional `ha_person`; `create_key(name, ha_person=)`,
  `ha_person_for_key(name)`, `set_key_ha_person(name, hp)`,
  `issued_key_details()`; `bind_device` now preserves extra fields.

### pwa_server.py — invite endpoints
- `POST /api/auth/invites` — create per-user key + one-time redeem URL/QR
  in one call (`{name, ha_person?, path?, redirect?, qr?, origin?}`).
- `GET /api/auth/invites` — list users (issued, device bound, ha_person).
- `POST /api/auth/invites/{name}` — re-mint re-pair link (resets binding).
- `PUT /api/auth/invites/{name}` — bind/clear ha_person on a key.
- Legacy `/api/auth/keys*` + `/api/auth/redeem-token` kept as aliases;
  `GET /redeem/{token}` unchanged (burn-once → cookie + key handoff).
- `/ws` connect resolves key→ha_person into `session_caller_ha_person`,
  auto-resolving bare key names to `person.<slug>`; invite create/update
  responses report `ha_person_exists` (warn-only).
- Keys-file writes are serialized with a process lock (`_KEYS_LOCK`).

## Session-security policy (docs/session-security-policy.md) — implemented

Per the reviewed plan, sessions now pin authorization to the connecting
key; voice identity only personalizes:

- `ToolRunner.session_owner_identity` + `policy_identity()` — owner set
  once at `/ws` connect; control/bank/doc/persona gates all evaluate the
  owner. `session_prime_text` uses the owner too.
- `SECONDARY_BLOCKED_TOOLS` + `_is_secondary_turn()` — a positively
  identified non-owner voice (person != owner, modulo `person.<slug>`
  alias of the key name) turns the turn "secondary": writes, actuation,
  doc/memory search, enrollment, persona set/reset raise PermissionError
  telling the model to propose to the owner aloud. Persona `show`/`list`
  and ambient reads still work.
- `_on_speaker` classifies owner vs guest: emits
  `secondary_speaker`/`speaker_identified` events, sends `{secondary:
  true}` to the browser, and injects a guest-rules system note for
  non-owners.
- Provider-side tools that bypass `execute()` (`ada_session_recall`,
  `ada_decision_check`, `ada_set_voice`) enforce the same gate; tool
  dispatch passes the owner identity.
- Barge-in noise gate (P6): `content.interrupted` logs a `barge_in`
  event; if the next turn's transcript is obvious noise
  (`_looks_like_noise` — empty/punct/filler only), it's kept out of the
  transcript, logged `barge_noise`, and Ada is told to resume. Plausible
  speech always gets through.
- Cross-person persona writes fail closed: `ada_persona set/reset` with
  `person=` refuses when the target has no person-scoped bank instead of
  silently filing under `personal`.
- `ConversationMemory.session_items` + `log_event()` record connect
  identity, speaker matches, barge-ins, denials, reconnects; folded into
  the L1 report (`report["session_events"]` + one-line summary in
  `memory_block`). Dead `speech_started`/`speech_stopped` forwarding
  removed.

## Verify

- Full offline suite in a venv (`/tmp/ada-venv`, google-genai etc.
  installed): `python -m unittest discover -s tests` → **329 tests, 3
  errors** — all `ai_edge_litert` ModuleNotFoundError (pose/hailo vision
  env gap, pre-existing, unrelated).
- New offline scenarios encode the policy:
  `tests/scenarios/session-owner-policy.yaml` (guest voice → every
  write/control/doc/search denied "session owner"; owner voice restores)
  and `tests/scenarios/persona-cross-target.yaml` (admin + scoped bank ok,
  no-bank fail-closed, non-admin cross-target denied).
- `tests.test_memory_banks`: 7 new unit tests for pinning/secondary/alias/
  fail-closed semantics.

## Report hierarchy + benchmarks (memory-hierarchy-plan.md §F, items 3–8)

- **L0 drill-down refs**: every `session_items` event now carries `turn`
  (index into the raw transcript); transcript files get a
  `<!-- ref: transcript:<date>-<sid> -->` header; `session-memory.md`
  entries get `- ref: report:<date>-<sid>`. Top→bottom navigation is
  live: rollup → report → event → transcript turn.
- **Latency instrumentation**: provider logs `tool_call` events with
  `dur_ms` per call and `turn_latency` events (`ttft_ms` = last
  input-transcription chunk → first model output, `dur_ms`,
  `audio_bytes`, `tools`) per completed turn — lands in every L1 report
  automatically.
- **`scripts/report-rollup.py`** — daily L2 rollup over session reports +
  run records; aggregates owners, event-kind counts, ttft p50/p95, and
  escalates {tool_denied, secondary_speaker, speaker_unrecognized,
  barge_noise, reconnect} + propagated run escalations with refs intact.
  Writes `runs/daily/<date>.json`, optional `--mddb` write to
  `ada-ha-reports-<instance>`. Smoke-tested end-to-end.
- **`scripts/identify_eval.py`** — speaker-ID benchmark: held-out clips
  (`<speaker>-<n>.pcm`) → score/margin/confusion vs enrolled profiles;
  emits a run record. Needs speechbrain (runs on the live host).
- **`scripts/recall-bench.py`** — Devin recall benchmark: probes YAML
  times the local-summaries scan vs MDDB vector_search, p50 + hit_rate,
  emits a run record. Verified offline (MDDB path needs the service up).
- **`scripts/devin-memory-bridge.py`** — publish dispatch-outcome.md and
  run records into the `devin` bank (`ada-ha-bank-devin-<instance>`) with
  `ref: file:<path>` meta. Continuation-summary backfill was dropped —
  it already exists: `chaba/scripts/ada/sync-devin-summaries.py` runs
  hourly via `devin-summaries-sync.timer` into the same bank.
- **MDDB topology discovered**: tony-dell runs a **read-only follower**
  bound to Tailscale `100.68.142.13:11023` (not loopback — the default
  `MDDB_BASE_URL=127.0.0.1:11023` misses it). Follower answers
  `/v1/search` but has **no embedding provider** → vector-search 400.
  Writes and semantic search need the **idc01 primary**
  (`100.74.146.0:11023`), which was unreachable (connect timeout) —
  that's also why `devin-summaries-sync.service` is in failed state.
  recall-bench verified the local read path (summaries p50=114ms,
  hit=1.0; mddb path times out cleanly and is reported per-path).
- **`session_security.secondary_blocked`** — rendered config override:
  `memory-banks.json` gains a `session_security` block; tool_runner
  resolves group tokens (control/memory_write/calendar_write/cms_write/
  devin_confirmed/doc/persona_write) + literal names; absent/malformed
  fails closed to the built-in default. SSOT tightening no longer needs
  a deploy.
- **Baselines + L3**: `tests/baselines.yaml` holds audited thresholds
  (latency p95, speaker-ID accuracy/refusal, scenario failures, recall
  hit-rate); `report-rollup.py` checks day aggregates against them
  (`ttft_p95 > baseline×1.25`, tool p95, accuracy/refusal, recall) and
  emits rule-based escalations with refs. `--level weekly` rolls daily
  L2 files into `runs/weekly/<iso-week>.json` with `daily_refs` — the
  L3→L2→L1→L0 chain is complete. Smoke-tested: rules fire correctly.
- **Bridge proven live**: `devin-memory-bridge.py dispatch-outcome.md`
  published to `mddb://ada-ha-bank-devin-tony/devin/report/
  2026-09-26-mddb-identity-policy` (write path to idc01 works).
- **SSOT drafts staged** in `docs/ssot-drafts/` (not yet applied to
  chaba): `ssot.apps.ada-reports.yml`, `ssot.policy.memory-hierarchy.yml`
  (P-R1–R5 + collaboration pact), `PATCHES.md` with the values/
  terminology/memory-banks deltas and the rules one-liner. Approval-gated
  — outside this worktree.

## idc01 provider-reboot prep (RAM upgrade)

- Verified boot-persistent: `Linger=yes`, all 8 quadlets have
  `WantedBy=default.target` → auto-start after provider reboot.
- Post-reboot verify (one command):
  `ssh idc01 'systemctl --user is-active mddb ollama gemini-ollama-proxy
  mddb-panel && systemctl --user show mddb -p NRestarts'` then
  `curl http://100.74.146.0:11023/v1/health`.
- Expected: mddb ~5 min silent init before binding; follower streams
  only deltas now (LSN is current after the reseed) so no OOM storm.
- With the new RAM, consider raising `mem.conf` (5G/6.5G cap) — that cap
  was today's OOM trigger; also lets the vector index load faster.
- Failed oneshots to ignore/recheck after: `ada-scenario-full`,
  `ada-scenario-smoke` (failed during the outage window).
- Store design decision noted: keep L0/events as files + MDDB; Postgres
  would add a hard idc01 dependency to every write — today's outage
  would have blocked all reporting.

## Follow-ups

- Deployed `~/.config/ada/memory-banks.json` should either give
  `personal-kk` `key_scope: ["user-kk"]` OR rely on the new invite
  `ha_person` binding (re-issue KK's key with ha_person=person.kk).
- If Ada's HA token is non-admin, `config/auth/list` stays unusable —
  `persons()` via `/api/states` is the supported path now.
- Live behavior (VAD noise classification, speaker-ID confidence on
  owner-vs-guest) needs on-device scenario runs — `tests/scenarios-live/`
  has the harness; add barge-in cases with the voice fixtures.
- An *unrecognized* voice still carries owner rights by design (can't
  lock out unenrolled owners); only positively-identified foreign voices
  become secondary. Documented as the accepted v1 edge.
- `scripts/enroll_speakers.py` — bulk pre-enrollment from .pcm/.wav
  files (~1 KB per voiceprint; real enroll needs speechbrain on the
  host). `tests/fixtures/gen_voice_fixture.py --voice name=hz:seed`
  mints extra synthetic voices for multi-guest scenario fixtures.
- MDDB incident (resolved mid-session): tony-dell's MDDB is a read-only
  follower bound to Tailscale `100.68.142.13:11023` (loopback doesn't
  work — fix `MDDB_BASE_URL` consumers). The idc01 primary
  (`100.74.146.0:11023`) was in an OOM restart loop: each startup the
  follower re-streamed ~18M binlog LSNs, ballooned memory past the cgroup
  cap (`MemoryHigh=5G/Max=6.5G`, host has only 7G), died with
  `219/CGROUP`, repeat. Fix applied: stopped the follower on tony-dell +
  restarted mddb on idc01 → primary healthy (`/v1/search` 200 in 1.7s).
  `devin-summaries-sync.service` then ran green: 35 synced / 66 unchanged
  / 0 failed — this thread's own summary is now in
  `ada-ha-bank-devin-tony`. Vector-search still warming (503 "index
  loading") at handoff.
- **Reseed done**: copied idc01's `mddb.db` (2.3G, LSN ~18.3M) over the
  follower's stale file (backup kept as `mddb.db.stale-20260926` in
  `~/.config/containers/mddb/data/`; delete when verified). Follower no
  longer requests the 18M-LSN backlog — the OOM loop is broken (primary
  NRestarts=0 since, `search` 200 in ~200ms, `get`/`metrics` healthy;
  vector-search 503 while its index warms — self-recovering).
  Follower itself is still initializing — tony-dell disk was at ~96%
  util under unrelated load; expect it to bind within minutes once I/O
  frees. Local reads at 100.68.142.13 return when it does.
- Yesterday's session (`dispatch-wt-20260925-215808`) already repointed
  all stale MDDB defaults to idc01 (chaba `72d9bc3`) — merged; the
  leftover worktree is clean to remove.
- Live HA + speechbrain need the Pi/host for `identify_eval` and real
  enrollment.
- Apply `docs/ssot-drafts/` to chaba + write `devin-kb/docs/
  work-policy.md` when approved; add the one-liner to
  `ssot.windsurf.common.md`, ada `AGENTS.md`, and `global_rules.md`.
- Open decision: explicit `tests/baselines.yaml` (recommended, seeded
  from ssot.values) vs trailing-median baselines for `escalate_when`.

## Task board (meaningful titles)

Done this session: `ada-auth-person-keys`, `ada-sec-session-owner`,
`ada-sec-secondary-gate`, `ada-latency-instrumentation`,
`reports-l0-to-l3-pipeline`, `reports-drilldown-read-path`,
`reports-baseline-escalations`, `devin-mddb-bridge-live`,
`mddb-follower-reseed`, `mddb-primary-recovery`.

Pending (who/what unblocks):

- `verify-mddb-follower-bind` — curl `100.68.142.13:11023/v1/health`;
  then delete `mddb.db.stale-20260926`.
- `post-reboot-ram-verify` — after the RAM upgrade: 8 quadlets active,
  vector index warm, raise `mem.conf` past 6.5G, recheck failed
  `ada-scenario-*` oneshots.
- `enroll-kk-live-voiceprint` — live host: `enroll_speakers.py` with real
  samples, then `identify_eval.py` benchmark vs `tests/baselines.yaml`.
- `live-bargein-scenarios` — owner/secondary policy scenarios with voice
  fixtures; update `kk_first_contact`.
- `reissue-kk-key-person` — key with `ha_person=person.kk`; verify
  `personal-kk` bank scope.
- `apply-ssot-drafts-to-chaba` — needs approval; copies
  `docs/ssot-drafts/` + values/terminology/rules deltas from PATCHES.md.
- `devin-kb-work-policy` — needs approval; `docs/work-policy.md` +
  rules one-liners.
- `triage-focus-inbox` — tony-dell IP-drift alerts + handoff-* items
  sitting untriaged in chaba `focus-inbox/`.
- `cleanup-old-worktree` — remove `dispatch-wt-20260925-215808-*`
  (commits already on chaba HEAD).
- `ai-edge-litert-dep` — 3 test errors are this missing dep only.
- `ada-report-dig-tool` — optional: expose the drill chain to Ada
  (owner-gated) and/or `GET /api/reports/<ref>` in pwa_server.
- `weekly-rollup-timer` — schedule `report-rollup.py --level weekly`
  alongside scenario timers on the host.
