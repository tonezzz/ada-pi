# dispatch outcome — unified ACL resolver + 'who can do what' explain

Card `ada-acl-explain`, attempt 2 (attempt 1 left no traceable branch or
detail in card comms — rebuilt from scratch). Delivered phase 1 exactly
as scoped: **read-only unification** — one resolver consumed by all
gates, plus the explain path. No stores merged (phase 2 is a separate
card).

## What landed (branch dispatch/20261010-…, commit c0ceba9, not pushed)

- `backend/memory_banks.py` — `MemoryBankRegistry.acl_decision(
  identity, *, entity_id=, bank=, tool=, tool_aliases=)`: the single
  resolver. Walks layers in evaluation order, returns
  `{allowed, decided_by, verdict, reason, trace[]}`.
  - control subject: policy_lookup (identity|default|unknown|unlisted)
    → full → deny_entities → deny_domains → device_grants overlay →
    allow_entities → allow_domains → default allow.
  - bank subject: registry membership → person_scope swap →
    person_policies allow/deny/full; `tool=` adds the write layers
    (bank.writable → bank.allowed_tools with `_ALIASES` expansion →
    bank.person_scope foreign-write → write_policy annotated as
    `requires_confirm` / verdict `needs_confirmation`).
  - `control_allowed()` and `bank_allowed()` are now bool views over
    the resolver — every existing consumer (gates, memory_ops,
    ada_device_acl, pwa_server power endpoint) inherits it.
- `backend/tool_runner/__init__.py` — `_check_control_allowed` calls
  the resolver (denial log now names `decided_by`);
  `_check_memory_write_allowed` replaces its three inline checks with
  one `acl_decision` call, keeping per-layer PermissionError messages.
  `bank.person_scope` denials are deferred to
  `memory_ops._check_person_scope_write` so the denial keeps its
  `{ok: False}` tool-result shape (a gate raise would change the
  contract — verified by test_person_scope_forget_denied_for_other_identity).
- `backend/tool_runner/meta.py` — `ada_ops action='acl_explain'`
  (`identity=`, `entity_id=`, `bank=`, `tool=`; identity defaults to
  the session's policy identity). Annotates call-time gates the stores
  don't own: ADA_READ_ONLY, chaba guest deny-pattern, dangerous safety
  map (or "unloaded"), rate-limit counters — folded into `verdict`.
- `backend/realtime_provider.py` + `tool_guide.yml` — ada_ops schema
  gains `acl_explain` + the three params; guide documents it.
- `docs/ssot/jobs/ada/2026-10-10-acl-resolver.yml` — design trail.
- Tests: `AclDecisionTests` (8 cases incl. grant-loses-to-deny
  "ambiguous" trace and a full verdict-parity sweep vs
  control_allowed/banks_for_person) and `AclExplainTests` (6 cases).

## Verify (card: granted + denied + ambiguous, deciding layer named)

- `pytest tests/test_memory_banks.py tests/test_tool_runner.py` —
  320 pass.
- `scripts/tool-lint.py` — no new violations (3 pre-existing on main:
  voice_fx descsize/coverage, yt_cached_list impl — untouched).
- Real-registry smoke (`/home/tony/.config/ada/memory-banks.json`):
  `person.kk + media_player.living_room_tv` → allowed,
  decided_by `control_policies` (allow_domains covers media_player);
  `person.kk + cover.gate` → denied, `control_policies.allow_domains`;
  `person.kk + general + ada_remember` → `needs_confirmation`,
  decided_by `person_policies` + write_policy note. The grant-vs-deny
  case (grant exists, deny_entities wins) is covered in tests.
- acl_explain stays owner-tier on secondary turns (not in the ops_read
  carve-out) — ACL internals are admin surface.

## Notes for phase 2 (store convergence — separate card)

The trace layers are the seam: `control_policies`, `device_grants`,
`person_policies`, `bank.*` — converging the stores behind
acl_decision won't touch the gates.

lessons:
- `tests/test_*.py` need the repo venv (`../ada-pi/.venv/bin/python
  -m pytest`); system python3 lacks google-genai and conftest import
  fails — also ADA_INSTANCE_ID is required outside pytest (conftest
  sets it).
- Denial-shape parity matters: layers enforced inside tool bodies
  (e.g. `_check_person_scope_write`) return {ok:False} — moving them
  into a dispatch gate turns them into raises; defer enforcement,
  keep the layer in the resolver trace.
- tool-lint measures only tool-level `description` (literal, ≤300
  chars) — param descriptions are free-form.
- bank `allowed_tools` may name absorbed tools — pass the runner's
  `_ALIASES` into acl_decision via tool_aliases so canonical names
  inherit.
