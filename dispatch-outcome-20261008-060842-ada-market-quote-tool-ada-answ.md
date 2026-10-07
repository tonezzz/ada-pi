# Dispatch outcome — ada-market-tool (2026-10-08)

Task: build `ada_market_quote` so Ada answers gold/FX/DXY from the
trade PostgreSQL instead of web_search (probe 2026-10-04: 11/11 turns
→ web_search, 4/11 real numbers, Thai-market data impossible for
grounded search).

## What landed (commit `fa0b4a6`, branch `dispatch/20261008-060842-…`, not pushed)

- **`backend/tools.d/ada_market_quote.py`** — drop-in tool,
  `kind= fx|gold|dxy|oil|silver|copper|natural_gas`, `currency=` for fx
  (default THB). One trade-api range fetch per call → latest + previous
  row → `change`/`change_pct`. `gold` returns BOTH `thai_gold_bar`
  (GTA XAU-THB baht-weight fixing — what ทอง questions mean) and
  `gold_spot` (USD/oz). Every quote carries `as_of`/`age_days`/`stale`
  (>4d) because the feed lags — the model is told to say the date.
  Honest `ok:false` on unreachable API, empty data, bad currency.
  SET/equity + pump diesel are out of scope (not in the DB).
- **manifest.yml** — `policy: read`, `secondary_allowed: true` (gold/FX
  asks come from any speaker), `timeout_s: 20`.
- **`tool_guide.yml`** — error-path usage entry.
- **`ssot.tool-surface.yml`** — `count_cap` 107 → 108 (deliberate bump).
- **`.env.example`** — `ADA_TRADE_API_URL` (default
  `https://tony-dell.taila0626a.ts.net/apps/trade/api`).
- **`tests/test_market_quote.py`** — 12 tests, faked httpx (all pass).
- **`tests/scenarios-live/market_quote.yaml`** — new routing scenario:
  5 turns assert the tool is picked, 2 honesty turns (PTT, diesel).
- **`tests/scenarios-live/stock_query_probe.yaml`** — `ada_market_quote`
  added to `calls_any` on the gold / USDTHB / diesel turns + baseline
  note (2026-10-04: 11/11 web_search, 4/11 real numbers) for the
  benchmark delta.
- **`docs/ssot/jobs/ada/2026-10-08-ada-market-quote-tool.yml`** — trail.

## Verified

- 12/12 unit tests pass; tools_loader registers the tool from the real
  manifest; `tool-lint` — only 3 PRE-EXISTING baseline FAILs remain
  (`ada_memory_search`/`ada_track_device` descsize, `ada_device_acl`
  coverage — verified identical on stash baseline).
- **Live API sanity** (worktree host → `localhost:9002`): gold →
  thai_gold_bar 66,050 THB/baht (as_of 2026-09-28, −2.51% d/d) +
  gold_spot $4,144.55/oz; USD/THB 33.595 (10-02); USD/JPY 157.83;
  DXY 101.21; WTI $95.01 / Brent $99.78 — all with stale flags.

## Watch / next steps

- **Benchmark**: post-deploy, rerun
  `tests/scenarios-live/stock_query_probe.yaml` on ada-dev and compare
  per-turn routing vs the 2026-10-04 baseline — that delta is the
  tool's benchmark per the card. Also run `market_quote.yaml`.
- **Feed staleness**: the trade automation is behind — THB latest
  2026-10-02 (−6d), DXY/GTA gold/OIL 2026-09-28 (−10d), some currencies
  (CZK/HUF/PLN/TRY) stuck at 2026-08-03. The tool reports it honestly,
  but the trade stack's auto_update likely needs a kick — separate
  trade-repo issue, worth a card.
- trade-api flakes occasionally on a cold psycopg2 pool (empty
  response); the tool retries transport errors once and reports
  honestly after that.
