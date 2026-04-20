# yahoo-connector

Read-only Yahoo Finance bridge exposed as an MCP server. One of the
MCP servers bundled in the [`traider`](../../README.md) hub (see the
root [AGENTS.md](../../AGENTS.md) for how the hub is organized).
See [AGENTS.md](AGENTS.md) in this directory for how this server's
code is organized, the Yahoo-specific constraints, and the places its
behavior diverges from `schwab-connector`.

This connector exists as the **no-account-required alternative** to
`schwab-connector`. The tool surface intentionally matches so prompts
and analytics work on either. Only one backend runs at a time —
pick with the `TRAIDER_BACKEND` environment variable (see
[Choosing a backend](../../README.md#choosing-a-market-data-backend)
in the hub README).

## What this MCP server can do

All tools are **read-only**. Market data is pulled via
[`yfinance`](https://pypi.org/project/yfinance/), which uses
Yahoo's unpublished endpoints. The `analyze_*` tools fetch candles
and run pure-numpy analytics locally — no extra HTTP per metric.

### `get_quote(symbol, field="LAST")`

A single snapshot field for one symbol.

- `symbol` — equities (`AAPL`), ETFs (`SPY`), Yahoo-style indices
  (`^GSPC`, `^IXIC`, `^DJI`). Schwab-style `$SPX` / `$DJI` / `$COMPX`
  are translated automatically.
- `field` — same friendly aliases as the Schwab connector (`LAST`,
  `BID`, `ASK`, `VOLUME`, `MARK`, `OPEN`, `HIGH`, `LOW`, `CLOSE`,
  `NET_CHANGE`, `PERCENT_CHANGE`, `BID_SIZE`, `ASK_SIZE`), or a native
  payload key (`lastPrice`, `marketState`, `exchange`, `currency`, …).

Returns a string; empty when the field isn't present (e.g. `BID` /
`ASK` may be missing for indices).

### `get_quotes(symbols, fields=None)`

Batched lookup. One yfinance call per symbol (yfinance doesn't expose
a true multi-symbol quote endpoint), so this is a convenience shape
rather than a latency win. Returns `{symbol: {field: value}}`.

### `get_price_history(symbol, period_type="year", period=1, frequency_type="daily", frequency=1, ...)`

OHLCV candles. Defaults give one year of daily bars. Response shape
matches the Schwab connector's:

```json
{
  "symbol": "SPY",
  "empty": false,
  "candles": [
    {"open": 512.3, "high": 513.9, "low": 511.0,
     "close": 513.2, "volume": 78234100, "datetime": 1708992000000}
  ]
}
```

`datetime` is epoch milliseconds (UTC).

Valid parameter combinations on the Yahoo backend:

| `period_type` | `period`              | `frequency_type`          | `frequency`    |
|---------------|-----------------------|---------------------------|----------------|
| `day`         | 1..~60                | `minute`                  | 1, 5, 15, 30   |
| `month`       | 1, 2, 3, 6            | `daily`, `weekly`         | 1              |
| `year`        | 1, 2, 3, 5, 10, 15, 20| `daily`, `weekly`, `monthly` | 1           |
| `ytd`         | 1                     | `daily`, `weekly`         | 1              |

Yahoo limits intraday history — 1-minute bars go back ~7 days,
sub-hourly back ~60 days. Windows outside those bounds return empty
`candles`. **10-minute bars are Schwab-only**; asking for them on this
backend raises `ValueError`. 15-/20-year daily windows work because
the client passes an explicit `start` date rather than yfinance's
capped `period` string.

`start_date` / `end_date` (epoch ms) override `period`.
`need_extended_hours_data=True` includes pre/post-market bars.

### `run_technical_analysis(symbol, indicators, ...)`

Runs TA-Lib indicators over the OHLCV candles. Spec-dict grammar is
identical to the Schwab connector — see its README for the full
examples. Common picks: `SMA`, `EMA`, `RSI`, `MACD`, `BBANDS`, `ATR`,
`ADX`, `STOCH`, `OBV`.

### `get_option_chain(symbol, contract_type="ALL", strike_count=None, ...)`

Schwab-shaped option chain built from `yfinance`'s `Ticker.option_chain`.
Response mirrors the Schwab connector's `callExpDateMap` /
`putExpDateMap` structure so downstream code is portable.

**Important divergences from Schwab — every call carries a
`"dataQualityWarning"` field flagging these:**

- **No Greeks.** yfinance does not publish delta/gamma/theta/vega/rho;
  they are emitted as `null`. Implied volatility *is* available
  (normalized to Schwab's percent convention).
- **Delayed ~15min.** Same lag as the rest of Yahoo.
- **Thin strikes may be stale.** Low-volume strikes often show zero
  or stale bid/ask. yfinance doesn't expose bid/ask sizes either;
  they come back as `0`.
- **`strategy="SINGLE"` only.** `ANALYTICAL`, `VERTICAL`, `STRADDLE`,
  etc. raise `YahooCapabilityError` rather than silently returning
  single-leg data.
- **`option_type="NS"` is unsupported** — Yahoo does not tag
  non-standard contracts.
- **The `ANALYTICAL`-only numeric overrides** (`interval`,
  `volatility`, `underlying_price`, `interest_rate`,
  `days_to_expiration`) also raise if passed, since they aren't
  meaningful without the `ANALYTICAL` strategy.

Supported filters: `contract_type` (CALL/PUT/ALL), `strike_count`
(symmetric band around ATM), `strike` (exact), `range_`
(ITM/NTM/OTM/ALL), `from_date` / `to_date` (YYYY-MM-DD),
`exp_month` (JAN..DEC or ALL), `include_underlying_quote`.

For a strategy-aware chain with Greeks, switch to the Schwab backend.

### `get_option_expirations(symbol)`

Schwab-shaped expiration list from `Ticker.options`. Only the
`expirationDate` and computed `daysToExpiration` are populated —
`expirationType`, `settlementType`, `optionRoots`, and `standard`
are `null` because Yahoo does not expose them. Switch to Schwab
when you need weekly/standard/AM-vs-PM tagging.

### `get_movers(index, sort=None, frequency=None)`

Top movers via a Yahoo predefined screener.

**Important divergence from Schwab:** Yahoo's screeners are US-market-
wide rather than scoped to one index. `sort` picks the screener:

- `PERCENT_CHANGE_UP` → `day_gainers`
- `PERCENT_CHANGE_DOWN` → `day_losers`
- `VOLUME` / `TRADES` → `most_actives`

`index` is accepted for signature parity but only used if it matches a
raw Yahoo screener key (e.g. pass `index="growth_technology_stocks"`).
`frequency` is ignored.

### `search_instruments(symbol, projection="symbol-search")`

- `projection="fundamental"` hydrates the `Ticker.info` block —
  trailing/forward P/E, EPS, dividend yield, 52-week high/low, market
  cap, beta, book value, profit margin, ROE, …
- Any other projection (`symbol-search`, `symbol-regex`, `desc-search`,
  `desc-regex`, `search`) delegates to `yf.Search` — Yahoo's symbol
  search is fuzzy by nature, so all of them return the same list.

### `get_market_hours(markets, date=None)`

**Not supported on the Yahoo backend.** Yahoo has no authoritative
market-hours endpoint (exchange-aware, holiday-aware). The tool raises
`YahooCapabilityError`. For authoritative session hours, switch to
`TRAIDER_BACKEND=schwab`.

### `get_accounts(include_positions=False)`

**Not supported on the Yahoo backend.** Yahoo is a market-data source,
not a brokerage. The tool raises `YahooCapabilityError`. Portfolio-
aware prompts need the Schwab backend (or another broker connector).

### `analyze_returns(symbol, ...)`

Return/risk summary: `total_return`, `ann_return`, `ann_volatility`,
`sharpe`, `sortino`, `calmar`, `max_drawdown`, `skew`,
`excess_kurtosis`. `risk_free_rate` is annualized (e.g. `0.05`).

### `analyze_correlation(symbols, ...)`

Pearson correlation matrix of log returns across `symbols`. Candles
are fetched per symbol then inner-joined on timestamps.

### `analyze_beta(symbol, benchmark="SPY", ...)`

Beta, annualized alpha, R², and correlation of `symbol` vs `benchmark`.

### `analyze_volatility_regime(symbol, short_window=20, lookback=252, ...)`

Classifies current realized vol against its trailing distribution.
Label thresholds: `z < −1` low, `±1` normal, `+1..+2` elevated, `≥+2`
extreme. Defaults pull two years of daily bars.

### `analyze_zscore(symbol, window=20, source="close", ...)`

Rolling z-score series. `source="close"` for price mean-reversion,
`source="log_return"` for return anomalies. `tail` trims the series.

### `analyze_pair_spread(symbol_a, symbol_b, hedge_ratio=None, zscore_window=60, ...)`

Log-price spread with rolling z-score and AR(1) half-life in bars.
`|zscore| > ~2` on a mean-reverting pair (finite `half_life_bars`)
is the classic stat-arb entry signal.

> **Install note.** TA-Lib is a C library with a Python wrapper. On
> conda: `conda install -c conda-forge ta-lib`. On other systems,
> install the C library first (Homebrew: `brew install ta-lib`;
> Debian: `apt install libta-lib0 libta-lib-dev`) and then
> `pip install TA-Lib` picks it up.

## Setup

### 1. Create the `traider` conda environment

Same env every server in this hub uses (Python 3.13):

```bash
conda create -n traider python=3.13
conda activate traider
```

### 2. Install the package

From the repo root:

```bash
conda activate traider
pip install -e ./mcp_servers/yahoo_connector
```

### 3. Run the server

```bash
yahoo-connector                                           # stdio
yahoo-connector --transport streamable-http --port 8765   # HTTP
```

No OAuth, no API key, no tokens — yfinance is unauthenticated.
Register the server with your AI CLI the same way you would the
Schwab connector — the
[hub README](../../README.md#connect-your-ai-cli) has the recipes for
Claude Code, OpenCode, and Gemini CLI.

## Prompts that put these tools to work

Since the tool surface matches, every prompt in the
[Schwab README](../schwab_connector/README.md#prompts-that-put-these-tools-to-work)
works here too, with two caveats:

- **Skip portfolio prompts.** Anything that calls `get_accounts` will
  raise on the Yahoo backend. "Correlation matrix across my top 10
  positions" needs Schwab.
- **Skip market-hours prompts.** "Is the equity market open today?"
  raises on Yahoo.

Everything else — quotes, price history, TA indicators, screeners,
fundamentals, returns/vol/beta/correlation/regime/pair-spread analytics
— works unchanged.

## Things worth knowing

- **Freshness.** Yahoo quotes are delayed ~15 minutes on most US
  exchanges unless your browser session has entitled real-time. The
  connector has no way to promote those.
- **Rate limits.** Yahoo throttles via Cloudflare. When it kicks in,
  `yfinance` raises and the tool fails — the server does not retry.
  Back off and try again later.
- **Options symbology.** Use Yahoo's format (e.g.
  `SPY250321C00500000`), not Schwab's 21-char OSI. The client does
  not translate between them.
- **Unofficial endpoint.** Yahoo can change response schemas without
  notice. If a tool starts returning empty or nonsense, upgrade
  `yfinance` first.
