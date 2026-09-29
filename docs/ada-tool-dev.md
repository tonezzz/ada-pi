# Ada Tool Development — authoring contract for tools.d

How a new tool gets built and shipped. This doc is the standard a
dispatched Devin session follows when Ada asks for a capability she
doesn't have; it's also what a human reviewer checks against.

## Anatomy

A tool is **one file + one manifest line** — no edits to
`tool_runner.py` or `realtime_provider.py`:

```
backend/tools.d/
  manifest.yml          # tool name -> module + policy
  ada_mddb_health.py    # the tool
```

`realtime_provider` offers the tool's `DECLARATION` to the model at
session start (respecting `ADA_EXCLUDED_TOOLS`); `ToolRunner.execute`
dispatches it with the manifest's policy enforced.

## Module contract

```python
DECLARATION = {
    "name": "ada_myservice_thing",       # must match the manifest key
    "description": "...",                # what it does + when to call it
    "parameters": {"type": "object",     # JSON Schema; keep args few
                   "properties": {...},
                   "additionalProperties": False},
}

async def run(runner, **args):           # MUST be async
    ...
    return {"ok": True, ...}             # plain dict — JSON-serializable
```

`runner` is the shared `ToolRunner`. Available:

- `runner.mddb` — `MddbClient` (add/search/vector_search/get/update/delete)
- `runner.banks` — `MemoryBankRegistry` (`bank_allowed`, `policy_for`, …)
- `runner.context.ha_client` — Home Assistant client
- `runner._memory_identity()` — the caller's resolved identity
  (ContextVar-safe; never cache it at module level)

## Rules (the checklist)

1. **Never raise into the model.** Catch exceptions at the tool boundary,
   return `{"ok": False, "error": "<plain sentence>"}`. The error string
   may be spoken aloud — make it honest and human, not a traceback.
2. **Policy is declared, not coded.** Pick the manifest policy:
   - `read` — any identified session, no confirmation (read-only tools)
   - `confirmed` — mutating; requires `confirmed=true` after a spoken yes
   - `owner_only` — only identities with `full` person_policies (Tony)
   When unsure, pick the stricter one. `secondary_allowed` defaults to
   `false` — non-owner speakers (KK, Testo, guest) can't call it unless
   the manifest says so.
3. **Bounded.** `timeout_s` in the manifest (default 20) is enforced by
   the runner. Subprocess/ssh work needs `BatchMode=yes` and its own
   timeout inside that budget.
4. **Voice-sized output.** Results stay in the Gemini session — cap at
   ~8 lines of substance (see `devin_status`). Never dump raw API JSON.
5. **Honesty.** Describe only what actually happened. If a dependency is
   down, say "I couldn't reach X" — never narrate success on failure
   (see `ada_camera_snapshot` for the pattern).
6. **Identity from the call, not module state.** The runner is shared
   across concurrent sessions — a module-level identity cache will
   cross-leak. `runner._memory_identity()` per call.
7. **Tests.** Add a unittest in `tests/` mirroring `test_tools_loader.py`
   patterns: happy path, error path, policy gate. Live behavior goes in
   `tests/scenarios-live/<name>.yaml`.

## Anti-patterns (seen in production)

- **Retry storms** — the model retries a refused call verbatim. If your
  tool can return `needs_confirm`, expect retries; the denial breaker
  exists but don't rely on it for correctness.
- **Phantom success** — returning a dict that *sounds* like success when
  the action failed. Gates now prefix denials with "NOT EXECUTED"; your
  tool's errors should be equally blunt.
- **Spoken tool names** — the model sometimes reads `tool_name{` aloud.
  Keep names pronounceable anyway.
- **Dispatch dedup bait** — a dispatched session building a tool should
  write ONE file; a spec that produces two near-identical tools gets
  deduplicated badly.

## Ship flow

1. Spec drafted into the `devin-handoff` bank (`spec/<slug>`).
2. `devin_dispatch` with the `build-tool` playbook — or hand-write it.
3. Session adds `tools.d/<name>.py` + manifest entry + tests; CI/tests pass.
4. Merge to main (auto-merge to staging while the pipeline is new);
   ada-ha-tony picks it up on next deploy/restart.
5. Add a live scenario yaml asserting the tool call + honest response.
