"""ada_market_quote — drop-in tool.

Answers "what's the price" for the market series the trade stack already
collects (card ada-market-tool, probe 2026-10-04: 11/11 stock turns
routed to web_search, 4/11 returned real numbers, and grounded search
can't do Thai-market data anyway). Backed by the trade-api on tony-dell
— GET {ADA_TRADE_API_URL}/exchange_rates/{ccy}, /dollar_index,
/commodity_prices/{name} — which reads the trade PostgreSQL the
auto_update jobs fill from Frankfurter/Yahoo/GTA.

Coverage (kind=):
- fx          USD -> currency pair, 24 quote currencies (currency= arg,
              default THB): AUD BRL CAD CHF CNY CZK DKK EUR GBP HKD HUF
              INR JPY KRW MXN MYR NOK NZD PLN SEK SGD THB TRY ZAR
- gold        Thai gold bar (GTA XAU-THB fixing, THB per baht-weight)
              AND USD spot per oz — both returned so the model can pick
              what the question meant
- dxy         US Dollar Index
- oil         WTI and Brent crude (pump diesel is NOT collected)
- silver / copper / natural_gas

Not covered: SET/equity quotes (PTT, AOT, SET index), pump diesel —
answer honestly or fall back to web_search; this tool says so in its
error path rather than guessing.

Every quote carries as_of/age_days/stale — the trade automation is a
daily job with known gaps (Thai holidays, stalled series), so the model
must always say the date, never "today" unless as_of really is today.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

import httpx

DECLARATION = {
    "name": "ada_market_quote",
    "description": (
        "Market prices from our trade database — kind='fx'|'gold'|"
        "'dxy'|'oil'|'silver'|'copper'|'natural_gas'. Use instead of "
        "web_search for gold/FX/DXY/crude; no SET stocks or diesel."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["fx", "gold", "dxy", "oil", "silver",
                         "copper", "natural_gas"],
                "description": "Which market series to quote.",
            },
            "currency": {
                "type": "string",
                "description": "3-letter quote currency for kind='fx' "
                               "(USD base, default THB).",
            },
        },
        "required": ["kind"],
    },
}

_BASE_URL_ENV = "ADA_TRADE_API_URL"
_DEFAULT_BASE_URL = "https://tony-dell.taila0626a.ts.net/apps/trade/api"

# Window for the range fetch: latest row + the one before it, so "did it
# go up" answers get a real delta. 21 days survives holiday gaps.
_LOOKBACK_DAYS = 21
_STALE_DAYS = 4            # daily series older than this is flagged
_HTTP_TIMEOUT = 8.0
_CCY_RE_LEN = 3

# kind -> (endpoint path, {series selector -> spoken label, unit})
#   path: format-string, {ccy} filled for fx
#   selector: matched against each row's symbol/commodity field
_KINDS: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {
    "fx": ("/exchange_rates/{ccy}",
           {"*": ("USD/{ccy}", "{ccy} per USD")}),
    "gold": ("/commodity_prices/GOLD",
             {"XAU-THB": ("thai_gold_bar", "THB per baht-weight"),
              "GOLD": ("gold_spot", "USD per oz")}),
    "dxy": ("/dollar_index", {"*": ("dollar_index", "index points")}),
    "oil": ("/commodity_prices/OIL",
            {"WTI": ("wti_crude", "USD per barrel"),
             "BRENT": ("brent_crude", "USD per barrel")}),
    "silver": ("/commodity_prices/SILVER",
               {"*": ("silver_spot", "USD per oz")}),
    "copper": ("/commodity_prices/COPPER",
               {"*": ("copper", "USD per ton")}),
    "natural_gas": ("/commodity_prices/NATURAL_GAS",
                    {"*": ("natural_gas", "USD per mmbtu")}),
}


def _base_url() -> str:
    return (os.environ.get(_BASE_URL_ENV)
            or _DEFAULT_BASE_URL).rstrip("/")


def _row_value(row: dict[str, Any]) -> float | None:
    """Price column name differs per endpoint (rate/value/price); fall
    back to close when the headline field is null."""
    for key in ("rate", "value", "price", "close"):
        v = row.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _row_date(row: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat(str(row.get("date") or ""))
    except ValueError:
        return None


def _pick_series(rows: list[dict[str, Any]], commodity: str
                 ) -> dict[str, list[dict[str, Any]]]:
    """Group a commodity endpoint's rows by symbol. FX/DXY rows have no
    symbol — they all land under '*'."""
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sym = str(row.get("symbol") or "*") if commodity else "*"
        out.setdefault(sym, []).append(row)
    return out


def _quote(rows: list[dict[str, Any]], label: str, unit: str
           ) -> dict[str, Any] | None:
    """Latest + previous row -> a voice-sized quote dict, or None."""
    rows = sorted((r for r in rows if _row_date(r)), key=_row_date)
    if not rows:
        return None
    latest = rows[-1]
    value = _row_value(latest)
    if value is None:
        return None
    as_of = _row_date(latest)
    assert as_of is not None
    age = (date.today() - as_of).days
    q: dict[str, Any] = {
        "label": label, "value": value, "unit": unit,
        "as_of": as_of.isoformat(), "age_days": age,
    }
    if age > _STALE_DAYS:
        q["stale"] = True
    prev = next((r for r in reversed(rows[:-1])
                 if _row_value(r) is not None), None)
    if prev is not None:
        pv = _row_value(prev)
        pd = _row_date(prev)
        assert pv is not None and pd is not None
        q["prev_value"] = pv
        q["prev_date"] = pd.isoformat()
        q["change"] = round(value - pv, 4)
        if pv:
            q["change_pct"] = round((value - pv) / pv * 100, 2)
    return q


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any]
               ) -> tuple[list[dict[str, Any]] | None, str | None]:
    """One trade-api GET -> (rows, err). The api flakes on a cold
    psycopg2 pool (empty body / dropped connection), so transport-level
    failures get a single second attempt."""
    url = f"{_base_url()}{path}"
    err = None
    for attempt in (1, 2):
        try:
            resp = await client.get(url, params=params)
        except (httpx.HTTPError, TimeoutError) as exc:
            err = (f"I couldn't reach the market database "
                   f"({exc.__class__.__name__})")
            continue
        try:
            data = resp.json()
        except Exception:
            data = None
        if resp.status_code >= 400:
            msg = (data or {}).get("error") if isinstance(data, dict) \
                else None
            return None, (str(msg) or
                          f"the market database returned HTTP "
                          f"{resp.status_code}")
        if not isinstance(data, dict):
            err = "the market database returned an unreadable response"
            continue
        rows = data.get("data")
        return (rows if isinstance(rows, list) else [data]), None
    return None, err


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    kind = str(args.get("kind") or "").strip().lower()
    spec = _KINDS.get(kind)
    if spec is None:
        return {"ok": False,
                "error": f"unknown kind {kind!r} — use fx, gold, dxy, "
                         "oil, silver, copper, or natural_gas"}
    path_t, selectors = spec
    ccy = str(args.get("currency") or "THB").strip().upper()
    if kind == "fx" and (len(ccy) != _CCY_RE_LEN or not ccy.isalpha()):
        return {"ok": False,
                "error": f"{ccy!r} is not a 3-letter currency code"}
    path = path_t.format(ccy=ccy)
    since = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    params = {"start_date": since, "limit": 500}

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        rows, err = await _get(client, path, params)
    if err:
        return {"ok": False, "error": err}
    assert rows is not None
    if not rows:
        return {"ok": False,
                "error": (f"the market database has no recent {kind} "
                          f"data — the feed may be down; say so rather "
                          f"than guessing")}

    is_commodity = path.startswith("/commodity_prices/")
    series = _pick_series(rows, is_commodity)
    quotes = []
    for sym, (label, unit) in selectors.items():
        picked = rows if sym == "*" else series.get(sym, [])
        label = label.format(ccy=ccy)
        unit = unit.format(ccy=ccy)
        q = _quote(picked, label, unit)
        if q:
            q["symbol"] = sym if sym != "*" else (
                ccy if kind == "fx" else kind.upper())
            quotes.append(q)
    if not quotes:
        return {"ok": False,
                "error": (f"the market database has no usable {kind} "
                          "rows — say the data is missing rather than "
                          "guessing")}

    out: dict[str, Any] = {"ok": True, "kind": kind, "quotes": quotes}
    stale = [q["label"] for q in quotes if q.get("stale")]
    if stale:
        out["note"] = ("data for " + ", ".join(stale) +
                       " is days old — always say the as_of date aloud")
    if kind == "gold":
        out["note"] = ((out.get("note") or "") +
                       " thai_gold_bar is the GTA baht-weight fixing; "
                       "gold_spot is USD/oz").strip()
    return out
