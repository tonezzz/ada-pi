# Dispatch outcome — ada-tools-desc-slim (20261005-215023)

## What changed

- **`backend/realtime_provider.py`** — all 37 declaration descriptions
  rewritten to ≤4 source lines / ≤300 chars each (2-line routing blurbs:
  what the tool is, its `action=` values, absorbed legacy names, gate
  flags). Total description text 32,887 → 7,092 chars (**21.6%** of
  baseline, under the card's <30% metric). `gev_command` 47 lines /
  3,622 chars → 3 lines / 196 chars; `cast_to_screen` 44→3, `yt` 28→4,
  `cctv_wall` 24→4. **Parameter schemas byte-identical to HEAD**
  (AST-dump comparison — zero schema or routing changes).
- **`backend/tool_guide.yml`** (new) — 37 terse per-tool entries
  distilled from the removed prose; recovery-focused (the common
  mistake + exact fix), keyed by canonical tool name.
- **`backend/tool_runner.py`** — `_tool_guide()` lazy loader; new
  `_usage_hint()` attaches a `usage` key to dict results carrying
  `error`/`needs_confirm`/`ok:false` (after `_denial_breaker`, before
  `_capture_reminder`); raised exceptions (gate denials, arg errors)
  get `\nusage: <hint>` appended to `exc.args` so the provider's
  `{"error": ...}` wrapper carries it. Same needs_confirm/verify_warn
  pattern — the detailed contract costs session context only on
  failure.
- **`backend/tools.d/ada_board_write.py`** — DECLARATION description
  slimmed (283 → 83 chars).
- **`scripts/tool-lint.py`** — new `descsize` check: every declaration
  (builtin + manifest-declared tools.d names) must stay under
  `desc_max_lines`/`desc_max_chars` from the SSOT; refactor into
  `decl_details()` shared by the census.
- **`docs/ssot/ssot.tool-surface.yml`** — `desc_max_lines: 4`,
  `desc_max_chars: 300` thresholds documented as the contract.
- **`docs/ada-tool-dev.md`** — rule 9 "Slim description, fat error
  path" added to the authoring checklist.
- **`tests/test_tool_audit.py`** — `test_fat_description_fails` +
  `test_slim_description_passes` fixture tests.
- **`docs/ssot/jobs/ada/2026-10-05-tool-desc-slim.yml`** — job record
  for the new mechanism and thresholds.

## Result

Card requirements met: descriptions ≤2 short lines (enforced by lint
caps at 4 AST content lines / 300 chars — content lines exclude the
paren wrapper), domain guidance moved to error payloads, lint green,
routing intact. Live scenarios (`tests/scenarios-live/*.yaml`) need a
running Gemini session and can't execute headless; the offline
scenario engine exercises the same `ToolRunner.execute` dispatch,
aliases, and gates.

## How to verify

```bash
python3 scripts/tool-lint.py                 # clean: 38 declared, 90 aliases
PYTHONPATH=<ada-pi>/.teststubs ADA_INSTANCE_ID=test \
  python3 -m unittest tests.test_tool_audit tests.test_tool_runner tests.test_scenarios
# 21 + 155 + 7 tests pass; full discover suite 610 tests, 2F/21E
# identical to HEAD baseline (pre-existing env gaps: document_check
# intake asserts, provider_events genai-stub AttributeErrors)
```

Spot-check: `gev_command` description at
`backend/realtime_provider.py:~2624` is 3 lines; `sed -n` the same for
any decl — all are ≤4 lines now.

## Summary

Slimmed all 38 shipped tool declarations to ≤4-line/≤300-char routing
blurbs — description text 32.9k → 7.1k chars (21.6%, beating the card's
<30% metric; gev_command 47 lines/3.6k chars → 3 lines/196 chars) with
parameter schemas byte-identical to HEAD — and moved the removed prose
into a new backend/tool_guide.yml (37 entries) that ToolRunner attaches
to failed/denied calls as a `usage` key or appended exception text,
extending the proven needs_confirm/verify_warn error-path pattern.
Enforcement landed via a `descsize` check in tool-lint.py (thresholds
in ssot.tool-surface.yml), rule 9 in docs/ada-tool-dev.md, two fixture
tests, and an SSOT job record. Verified: tool-lint clean, 21/21 audit
tests, 155/155 tool_runner tests, 7/7 offline scenarios routing tools
and aliases; the full suite's 2F/21E are identical pre-existing
environment failures on HEAD. No commit or push per dispatch rules.
