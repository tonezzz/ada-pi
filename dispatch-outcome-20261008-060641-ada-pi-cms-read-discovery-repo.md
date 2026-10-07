# dispatch-outcome — ada-cms-discovery-fixes

Card: `ada-cms-discovery-fixes` (kanban). Dispatch
`20261008-060641-ada-pi-cms-read-discovery-repo`, branch
`dispatch/20261008-060641-ada-pi-cms-read-discovery-repo`.

## What changed

Three ada-pi-side fixes from the yt-dub-liam-e2e pass-1 diagnosis
(2026-10-07, transcript ece770ccb0) — all committed on the dispatch
branch, no push, no deploy.

**1. cms_read discovery** — `backend/tool_runner/cms.py`
- `action='list'` now fetches a bounded window
  (`CMS_FETCH_WINDOW=600`, env `ADA_CMS_FETCH_WINDOW`) instead of
  `limit`, dedupes/drops dead pages locally, and returns the N
  **newest** pages by `meta.updated` — `limit=10` can no longer return
  an arbitrary cam-*-only slice and trick the model into "page does
  not exist". Return shape unchanged (bare list).
- New `action='search'` with `query=` (falls back to `key`/`slug` for
  misrouted args): token match over slug/title/summary/domain across
  every live page. Returns
  `{query, count, total_pages, pages[{key,title,domain,updated,match}]}`
  — `total_pages` signals the search is exhaustive.
- Provider surface updated: `realtime_provider.py` schema gains
  `query=` + `search` in the action enum and steers existence-questions
  toward search; `CMS_INSTRUCTIONS`, `tool_guide.yml`,
  `ssot.tool-surface.yml` updated.

**2. reports-index flood** — same file
- Module-level `cms_index_rows()` = the ada-pi port of chaba's
  `index_rows()`: en/th variants dedupe to one row per slug (en
  title/summary, freshest `updated` wins), dead-status pages drop, and
  `CMS_INDEX_DOMAIN_CAP=10` (env `ADA_CMS_INDEX_DOMAIN_CAP`) bounds
  each domain. `_cms_reports_index` renders the capped rows (window
  200→600) plus a `_Not shown: +N <domain>` footer when capping hides
  rows, pointing at `action='search'`.

**3. memory-search pollution** — `backend/memory_ops.py`
- `bank='all'` merge: hits whose key starts with a bulk-archive prefix
  (`ADA_MEMORY_DEMOTE_PREFIXES`, default `devin/`) are tagged
  `archive: true` and sort below non-archive hits regardless of score.
  Explicit `bank='devin'`/`'reports'` searches are unfiltered —
  dispatch forensics stays reachable, it just can't crowd out memory.
- `ada_memory_search` `bank=` description and `tool_guide.yml` entry
  document the behavior.

## Tests

- `tests/test_tool_runner.py` — `CmsDiscoveryTests` (9 tests): ordering
  across the wide window, `reports-index` exclusion, search incl.
  `key`/`slug` fallback + empty query, index dedupe/cap/dead-drop,
  rendered overflow note.
- `tests/test_memory_recall_extras.py` — `AllMergeDemotionTests`:
  demotion keeps a lower-scoring real memory on top + named-bank escape.

## Result

- `test_tool_runner.py`: **214 passed** (incl. 9 new, 7 subtests)
- `test_memory_recall_extras.py`: **17 passed** (incl. 2 new)
- `scripts/tool-lint.py`: no new findings — only the two pre-existing
  errors (`ada_track_device` descsize, `ada_device_acl` coverage),
  verified identical on the clean tree.
- Full suite: the two lint errors above are the only failures; an
  unrelated `ModuleNotFoundError: ai_edge_litert` skips hardware tests.

## Verify

- `python3 -m pytest tests/test_tool_runner.py tests/test_memory_recall_extras.py -q`
- Post-deploy live: `cms_read action='search' query='liam'` →
  `voice-dub-demos`; `reports-index` shows ≤10 cam rows +
  `cached-videos-report` and a `_Not shown: +N cam` footer; an
  `bank='all'` memory search for a dub topic ranks curated memory above
  devin dumps.

Trail: `docs/ssot/jobs/ada/2026-10-08-cms-read-discovery.yml`.
