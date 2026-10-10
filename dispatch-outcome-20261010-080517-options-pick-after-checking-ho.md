# Dispatch outcome — ada-gemini-free-tier-quota (2026-10-10)

## Result: done

**Wiring audit (the card's precondition):** exactly **one** active
`GEMINI_API_KEY` is wired — the same key value (sha 1c8c745605…) in
`ada-ha-tony.env`, `ada-ha-michael.env`, `ada-dev.env`, `ada-pi-pwa.env`
and `mddb-gemini.env`. `_attic-20261007/mddb-gemini.env.bak` holds one
retired key. `ada-ha-tony-keys.json` contains only Ada device keys (no
Gemini material); `ADA_VISION_API_KEY` is ollama. So **A** (billing
project) and **B** (N projects) both need Tony to provision new GCP
resources — not implementable from the repo. Picked **C + D**, plus a
key pool that makes A/B zero-code-change the moment a second key (extra
project or billing-enabled) is dropped into the env.

## What changed

- `backend/gemini_pool.py` (new) — shared authority for side-tool
  Gemini calls:
  - `configured_keys()` — `GEMINI_API_KEY`, `GEMINI_API_KEYS` (comma
    list), `GEMINI_API_KEY_2..9`, `GOOGLE_API_KEY`, deduped.
  - `next_key(model)` — round-robin over keys still holding quota for
    that model; `mark_exhausted()` parks a (key, model) pair until the
    free-tier reset (midnight US/Pacific); `pool_exhausted()` lets
    callers skip doomed requests; `ADA_GEMINI_SIMULATE_EXHAUSTED=1`
    forces the drained state for the forced-exhaust test.
  - `emit_ops_event()` — the `web_search_quota` event generalized:
    throttled per tool (`ADA_QUOTA_EVENT_MIN_S`, default 300s) into
    `ada-ha-events-<instance>` so quota burn lands in the hourly digest.
  - `is_quota_error()`, `QuotaExhaustedError` (quota-shaped, says
    "tell the user plainly"), TTL `cache_get/cache_put`, `generate()`
    failover helper, `status()` for health, `reset()` test hook.
- Wired sites: `tool_runner/web.py` (pool + 15-min answer cache +
  skip-when-drained), `document_check.py` (pool rotate, sha256 classify
  cache 24h, loud quota summary, ops event), `tool_runner/cms.py`
  `_cms_llm`, `conversation_memory.py` (all 5 REST calls + quota events
  from `_report_failure`), `summary_rollups.py`, `decision_check.py`
  (mark+rotate inside its 429 retry; loud `QuotaExhaustedError` at the
  end), `devteam.py`, `posture_verifier.py`, `tool_runner/screens.py`
  `_vcast_tts` (mark+emit on HTTP 429; client-TTS fallback stays).
- `conversation_health()` gains a `gemini` block — keys configured,
  exhausted pairs, reset time, last quota error per tool (no key
  material).
- `.env.example` documents the new vars.
- Not touched by design: `realtime_provider` (live session — different
  quota class) and `ada_render_video` (Veo, paid-only).
- `tests/test_gemini_pool.py` — 21 tests. `test_web_search.py` /
  `test_document_check.py` gained `gemini_pool.reset()` in setUp
  (module-global cache/throttle isolation).
- `docs/ssot/jobs/ada/2026-10-10-gemini-free-tier-quota-pool.yml` — job
  trail.

## Verification

- `pytest tests/test_gemini_pool.py` — 21 passed
- Related suites (web_search, document_check, decision_check, devteam,
  posture_verifier, summary_rollups, context_awareness, ops_events,
  tool_runner, provider_events, tools_loader, cms_regen, secret_drop,
  mddb_client, memory_banks) — 499 passed total
- `scripts/tool-lint.py` — 3 PRE-EXISTING violations on HEAD
  (voice_fx descsize/coverage, yt_cached_list impl) — unrelated, same
  before and after my diff
- `.venv` (gitignored) recreated in the worktree for tests — dispatch
  host lacks google-genai/PIL/numpy

## Not verified live (no ada service/MDDB on the dispatch host)

On idc03 after deploy:

- `ADA_GEMINI_SIMULATE_EXHAUSTED=1` → forced-exhaust test: `web_search`
  auto still answers via DDG flagged `quota_exhausted`/`degraded` and
  `provider=gemini` raises a loud quota error — card VERIFY clause.
- 48h journal check: quota burn should appear as `*_quota` ops events
  in `ada-ha-events-tony` (hourly digest), and once a key is marked,
  later calls skip the API — no repeated silent 429s.
- Dropping a second/billing key into `GEMINI_API_KEYS` (or
  `GEMINI_API_KEY_2`) in `ada-ha-tony.env` activates options A/B with
  no code change — each key gets its own per-model daily quota.
