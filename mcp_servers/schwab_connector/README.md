# schwab-connector

Read-only Schwab Trader API bridge exposed as an MCP server. One of the
MCP servers bundled in the [`traider`](../../README.md) hub (see the
root [AGENTS.md](../../AGENTS.md) for how the hub is organized).
See [AGENTS.md](AGENTS.md) in this directory for how this server's
code is organized and what to watch out for.

## What this MCP server can do

Once the server is running and Claude is connected, Claude gets the
tools below. All are **read-only** — no orders, no alerts, no writes.

Market-data tools (quotes, candles, TA, movers, instruments, hours)
hit `/marketdata/v1/*`. Account tools hit `/trader/v1/accounts/*`.
The `analyze_*` tools fetch candles and then run pure-numpy analytics
on them locally — no extra API calls per metric.

### `get_quote(symbol, field="LAST")`

A single snapshot field for one symbol. Handy for a quick "what's SPY
trading at right now."

- `symbol` — any symbol Schwab accepts: equities (`AAPL`), ETFs
  (`SPY`), futures (`/ES`), indices (`$SPX`), or 21-char OSI options
  (`SPY   250321C00500000`).
- `field` — either a friendly alias or a native Schwab key. Aliases:
  `LAST`, `BID`, `ASK`, `VOLUME`, `MARK`, `OPEN`, `HIGH`, `LOW`,
  `CLOSE`, `NET_CHANGE`, `PERCENT_CHANGE`, `BID_SIZE`, `ASK_SIZE`.
  Anything else is passed straight through to the Schwab quote object
  (e.g. `lastPrice`, `quoteTime`, `52WeekHigh`).

Returns a string (empty if the field isn't present).

### `get_quotes(symbols, fields=None)`

Batched version of `get_quote`. Use this whenever Claude needs more
than one symbol or more than one field — one HTTP call instead of N.

- `symbols` — list of tickers.
- `fields` — list of aliases or native keys. If omitted, each symbol's
  entry is the full Schwab `quote` object (useful when Claude wants
  to browse what's available).

Returns `{symbol: {field: value}}`.

### `get_price_history(symbol, period_type="year", period=1, frequency_type="daily", frequency=1, ...)`

OHLCV candles for charting or lookback analysis. Defaults give **one
year of daily bars** (the "yearly chart, daily candles" case).

Response shape is Schwab's native format:

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

Valid `period_type` / `period` / `frequency_type` / `frequency`
combinations — Schwab rejects the rest with a 400:

| `period_type` | `period`              | `frequency_type`          | `frequency`       |
|---------------|-----------------------|---------------------------|-------------------|
| `day`         | 1, 2, 3, 4, 5, 10     | `minute`                  | 1, 5, 10, 15, 30  |
| `month`       | 1, 2, 3, 6            | `daily`, `weekly`         | 1                 |
| `year`        | 1, 2, 3, 5, 10, 15, 20| `daily`, `weekly`, `monthly` | 1              |
| `ytd`         | 1                     | `daily`, `weekly`         | 1                 |

You can also pass `start_date` / `end_date` as epoch milliseconds —
they override `period` when set. `need_extended_hours_data=True`
includes pre/post-market candles; `need_previous_close=True` adds the
prior session's close to the response.

### `run_technical_analysis(symbol, indicators, ...)`

Runs one or more [TA-Lib](https://ta-lib.org/) indicators over the
OHLCV candles for a symbol. Price-history parameters (`period_type`,
`period`, `frequency_type`, `frequency`, `start_date`, `end_date`,
`need_extended_hours_data`) behave exactly like `get_price_history`,
so the same valid-combination matrix applies.

- `indicators` — list of spec dicts. Each dict **must** have `name`
  (a TA-Lib function name; case-insensitive). Any other keys are
  forwarded to TA-Lib as keyword arguments. Use `label` to rename
  the output entry if you want the same indicator with different
  params (e.g. SMA_20 *and* SMA_50 in one call).
- `tail` — optional int. Trim each returned series (and the matching
  `datetime` entries) to the last N points. Leave unset for the full
  aligned history.

```json
{
  "symbol": "SPY",
  "indicators": [
    {"name": "SMA", "label": "SMA_20", "timeperiod": 20},
    {"name": "SMA", "label": "SMA_50", "timeperiod": 50},
    {"name": "RSI", "timeperiod": 14},
    {"name": "MACD", "fastperiod": 12, "slowperiod": 26, "signalperiod": 9},
    {"name": "BBANDS", "timeperiod": 20, "nbdevup": 2, "nbdevdn": 2}
  ],
  "tail": 5
}
```

Response shape:

```json
{
  "symbol": "SPY",
  "datetime": [1712275200000, 1712361600000, "..."],
  "indicators": {
    "SMA_20": [517.2, 517.8, "..."],
    "SMA_50": [510.4, 510.9, "..."],
    "RSI": [61.2, 58.7, "..."],
    "MACD": {"macd": [...], "macdsignal": [...], "macdhist": [...]},
    "BBANDS": {"upperband": [...], "middleband": [...], "lowerband": [...]}
  }
}
```

Warm-up slots at the start of a series are `null` (TA-Lib NaN).
Multi-output indicators (MACD, BBANDS, STOCH, …) come back as a dict
keyed by TA-Lib's output names. Any TA-Lib function works — common
picks: `SMA`, `EMA`, `WMA`, `RSI`, `MACD`, `BBANDS`, `ATR`, `ADX`,
`STOCH`, `STOCHRSI`, `OBV`, `CCI`, `MFI`, `AROON`.

### `get_option_chain(symbol, contract_type="ALL", strike_count=None, ...)`

Full option chain for an underlying, straight from
`/marketdata/v1/chains`. Native Schwab shape:

```json
{
  "symbol": "SPY",
  "status": "SUCCESS",
  "underlying": { "...": "underlying quote" },
  "strategy": "SINGLE",
  "callExpDateMap": {
    "2025-06-20:47": {
      "510.0": [
        {"putCall": "CALL", "symbol": "SPY   250620C00510000",
         "bid": 12.35, "ask": 12.45, "last": 12.40, "mark": 12.40,
         "totalVolume": 4821, "openInterest": 18234,
         "volatility": 15.82,
         "delta": 0.58, "gamma": 0.021, "theta": -0.084,
         "vega": 0.63, "rho": 0.18,
         "intrinsicValue": 4.1, "timeValue": 8.3,
         "strikePrice": 510.0, "daysToExpiration": 47}
      ]
    }
  },
  "putExpDateMap": { "...": "same shape, puts" }
}
```

Useful filters:

- `contract_type` — `CALL`, `PUT`, or `ALL`.
- `strike_count` — strikes above **and** below the at-the-money
  strike. Use this to keep responses small.
- `strategy` — defaults to `SINGLE`. Set `ANALYTICAL` to theoretical-
  price the chain at overridden `volatility` / `underlying_price` /
  `interest_rate` / `days_to_expiration`. Other values
  (`VERTICAL`, `CALENDAR`, `STRANGLE`, `STRADDLE`, `BUTTERFLY`,
  `CONDOR`, `DIAGONAL`, `COLLAR`, `ROLL`, `COVERED`) make Schwab
  return pre-built multi-leg strategy previews.
- `from_date` / `to_date` — `YYYY-MM-DD` bounds on expiration.
- `range_` — `ITM`, `NTM`, `OTM`, `SAK`, `SBK`, `SNK`, or `ALL`.
- `strike` — exact strike filter.
- `exp_month` — `JAN`..`DEC` or `ALL`.
- `option_type` — `S` (standard), `NS` (non-standard), or `ALL`.
- `include_underlying_quote` — defaults to `true`; set `false` to
  skip the underlying block and shrink the payload.

### `get_option_expirations(symbol)`

List of available expiration series for an underlying, from
`/marketdata/v1/expirationchain`. Use this before calling
`get_option_chain` when you need to know which dates exist (weekly
vs standard vs quarterly, settlement type, option root symbols).

```json
{
  "status": "SUCCESS",
  "expirationList": [
    {"expirationDate": "2025-06-20", "daysToExpiration": 47,
     "expirationType": "M", "settlementType": "P",
     "optionRoots": "SPY", "standard": true}
  ]
}
```

### `get_movers(index, sort=None, frequency=None)`

Top movers for an index. Handy for screeners.

- `index` — `$DJI`, `$COMPX`, `$SPX`, `NYSE`, `NASDAQ`, `OTCBB`,
  `INDEX_ALL`, `EQUITY_ALL`, `OPTION_ALL`, `OPTION_PUT`,
  `OPTION_CALL`.
- `sort` — `VOLUME`, `TRADES`, `PERCENT_CHANGE_UP`, `PERCENT_CHANGE_DOWN`.
- `frequency` — minutes of activity required: `0`, `1`, `5`, `10`,
  `30`, `60`.

### `search_instruments(symbol, projection="symbol-search")`

Instrument lookup / fundamentals. Pass `projection="fundamental"` to
get the fundamentals block (P/E, EPS, dividend yield, 52-week range,
market cap). Other projections (`symbol-regex`, `desc-search`,
`desc-regex`, `search`) search by pattern or description.

### `get_market_hours(markets, date=None)`

Session hours for one or more markets. `markets` is a list of any of
`equity`, `option`, `bond`, `future`, `forex`. `date` is `YYYY-MM-DD`
and defaults to today.

### `get_accounts(include_positions=False)`

All authorized accounts. With `include_positions=True`, each account
includes its `positions` array — quantity, cost basis, market value,
and unrealized P&L per holding. Read-only; no order data.

### `analyze_returns(symbol, ...)`

Return/risk summary for one instrument. Fetches candles with the
same params as `get_price_history`, then returns:

- `total_return`, `ann_return`, `ann_volatility`
- `sharpe`, `sortino`, `calmar`
- `max_drawdown`
- `skew`, `excess_kurtosis`

`risk_free_rate` is annualized (e.g. `0.05` for 5%).
`annualization` overrides the periods-per-year inferred from bar
spacing — set it for intraday bars if the inference looks off.

### `analyze_correlation(symbols, ...)`

Pearson correlation matrix of log returns across `symbols`. Fetches
each symbol's candles, inner-joins on timestamps, then computes the
matrix. Returns `{"symbols": [...], "matrix": [[...], ...],
"n_bars": N, "first_datetime": ms, "last_datetime": ms}`.

### `analyze_beta(symbol, benchmark="SPY", ...)`

Beta, annualized alpha, R², and correlation of `symbol` vs
`benchmark` on log returns over the shared window.

### `analyze_volatility_regime(symbol, short_window=20, lookback=252, ...)`

Classifies current realized vol against its trailing distribution.
Takes a rolling `short_window`-bar close-to-close vol, z-scores and
percentile-ranks the latest reading against the last `lookback`
values, and labels the regime `low` / `normal` / `elevated` /
`extreme` (thresholds: z < −1 / ±1 / +1…+2 / ≥+2). Defaults fetch
two years of daily bars so the lookback window is filled.

### `analyze_zscore(symbol, window=20, source="close", ...)`

Rolling z-score series. `source="close"` for mean-reversion of price,
`source="log_return"` for return anomalies. `tail` trims the
returned series to the last N points.

### `analyze_pair_spread(symbol_a, symbol_b, hedge_ratio=None, zscore_window=60, ...)`

Log-price spread between two instruments with a rolling z-score and
an AR(1) half-life in bars. If `hedge_ratio` is omitted, it's
estimated by OLS of `log(A)` on `log(B)` over the shared window.
`|zscore| > ~2` on a mean-reverting pair (finite `half_life_bars`)
is the classic stat-arb entry signal. `tail` trims returned series.

> **Install note.** TA-Lib is a C library with a Python wrapper. On
> conda: `conda install -c conda-forge ta-lib`. On other systems,
> install the C library first (Homebrew: `brew install ta-lib`; Debian:
> `apt install libta-lib0 libta-lib-dev`) and then `pip install
> TA-Lib` picks it up.

## Prompts that put these tools to work

These are example prompts you can type at Claude once the MCP server
is connected. Keep them as simple as shown — Claude will pick the
right tools and chain them. The power comes from combining tools, so
most of these examples intentionally pull from several at once.

### Quick single-tool prompts

- "What's SPY trading at right now?"
- "Top 10 Nasdaq gainers today."
- "Give me the P/E, EPS, and 52-week range for KO."
- "Is the equity market open tomorrow?"
- "Show me my positions sorted by unrealized P&L."
- "One year of daily bars on TSLA, then RSI(14) and MACD on the last 20 days."
- "Annualized Sharpe, Sortino, and max drawdown for NVDA over the last year."
- "Is SPY realized volatility in an elevated regime right now?"

### Screeners that combine tools

- "Find today's top 20 Nasdaq gainers and give me the 14-day RSI for
  each — flag anything above 70."
- "Of the Dow 30 movers by volume, which ones have a 20-day close
  z-score above 2?"
- "Pull the top 10 percent-change-up names on NYSE and show me their
  P/E ratios and dividend yields next to the move."

### Portfolio-aware analysis

- "For every position I hold, compute beta to SPY over the last year
  and rank from highest to lowest."
- "Across my holdings, annualized Sharpe and max drawdown over 1
  year. Which positions are dragging the portfolio?"
- "Correlation matrix across my top 10 positions — where is my risk
  actually concentrated?"
- "For each position, check whether the underlying's realized-vol
  regime is elevated or extreme — I want to know where risk has
  picked up."

### Macro / cross-asset

- "Correlation matrix for XLK, XLF, XLE, XLV, XLY, XLP, XLU, XLI,
  XLB, XLRE, and SPY over the last 90 days of daily bars."
- "Compare one-year Sharpe and max drawdown across the Mag 7
  (AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA) — rank them."
- "Beta of QQQ, IWM, and DIA vs SPY over the last year."
- "Is the equity market open today? If so, pull 5-minute bars on /ES
  and show me the 20-bar rolling z-score of log returns."

### Mean-reversion / stat-arb

- "Check KO vs PEP for mean-reversion: estimate the hedge ratio,
  current spread z-score, and half-life in days. Is it a fadeable
  setup?"
- "For GLD/SLV, give me the current pair spread z-score and the
  rolling z-score over the last 60 days."
- "Scan XOM/CVX, KO/PEP, and MA/V as pairs — which one has the
  biggest current z-score and a half-life under 30 days?"

### Regime-aware drill-downs

- "Is SPY in an elevated or extreme vol regime? If yes, give me the
  names in my portfolio with the lowest beta to SPY."
- "For the top 20 movers on $COMPX right now, compute a realized-vol
  regime for each — are these moves happening in already-quiet or
  already-hot names?"
- "If TSLA's 20-day realized vol is in the top decile of its 1-year
  distribution, also give me its 14-day RSI and MACD histogram."

### Fundamentals + technicals together

- "For AAPL: current price, P/E, 52-week range, and 14-day RSI — is
  it expensive and overbought?"
- "For each of the Mag 7, give me P/E, EPS, dividend yield, plus
  1-year Sharpe and max drawdown. Which ones look best on
  risk-adjusted returns *and* valuation?"

### Things worth knowing

- **Freshness.** Quotes are real-time during RTH (or as close as the
  Schwab API gets). Outside RTH, `lastPrice` may be stale — pre/post
  fields live under different keys in the quote JSON, so ask for them
  explicitly.
- **Options.** Use the 21-character OSI format
  (`SPY   250321C00500000`, with padded spaces), not dotted TOS
  notation.
- **Rate limits.** Schwab enforces per-endpoint quotas. If you hit
  one, the tool raises (HTTP 429) — the server won't silently retry.
- **Token expiry.** Access tokens auto-refresh. Refresh tokens die
  after ~7 days of inactivity — if you see `SchwabAuthError`, re-run
  `schwab-connector auth`.

## Setup

### 1. Install conda

If you don't already have it, install **Miniforge** (community conda
distribution, permissive license, fast solver):

- macOS / Linux / WSL:
  ```bash
  curl -L -O "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
  bash Miniforge3-$(uname)-$(uname -m).sh
  ```
- Windows: download the installer from
  <https://github.com/conda-forge/miniforge/releases/latest> and run it.

Miniconda works fine too if you already have it —
<https://docs.conda.io/en/latest/miniconda.html>.

Restart your shell (or `source ~/.bashrc` / `source ~/.zshrc`) so
`conda` is on your PATH.

### 2. Create the `traider` environment

The project always uses an env named `traider`, pinned to Python 3.13:

```bash
conda create -n traider python=3.13
conda activate traider
```

Every subsequent command in this repo (including `pip install`,
`schwab-connector`, any test runner) assumes this env is active.

### 3. Install the package

From the `traider` repo root:

```bash
conda activate traider
pip install -e ./mcp_servers/schwab_connector
```

(Or `cd` into `mcp_servers/schwab_connector` first and run
`pip install -e .` — same result.)

### 4. Register a Schwab developer app

The connector authenticates as an OAuth app you own on the Schwab
developer portal. You need to create that app once before anything
else works.

1. **Create a developer account** at <https://developer.schwab.com>
   and sign in. Your Schwab brokerage login works here.
2. **Create a new app** from the Dashboard. You'll be asked for:
   - **App name** and **description** — free text, shown only to you.
   - **API products** — select **Accounts and Trading Production**
     and **Market Data Production**. (The connector is read-only, but
     the Trader product is what exposes `/marketdata/v1/quotes`.)
   - **Callback URL** — must be HTTPS and must match
     `SCHWAB_CALLBACK_URL` exactly, including trailing slash.
     `https://127.0.0.1` is the simplest choice and is what the auth
     flow assumes by default.
3. **Submit for approval.** New apps start in `Approved - Pending`
   and have to flip to `Ready For Use` before the keys work. This
   usually takes a few minutes to a couple of days; you can't
   shortcut it. If `schwab-connector auth` returns `invalid_client`,
   the app is still pending.
4. **Copy the App Key and Secret** from the app's detail page once it
   is `Ready For Use`. The key is public-ish (it's the OAuth
   `client_id`); the secret must be kept private — don't paste it
   into chat, logs, or anything committed to git.
5. **Rotating credentials.** If you regenerate the secret in the
   portal, existing tokens are invalidated — you'll need to re-run
   `schwab-connector auth`.

### 5. Configure Schwab credentials

Either export the vars directly:

```bash
export SCHWAB_APP_KEY=...
export SCHWAB_APP_SECRET=...
export SCHWAB_CALLBACK_URL=https://127.0.0.1   # must match the app reg
```

…or drop them in a `.env` file at the repo root (gitignored, loaded
automatically on startup):

```
SCHWAB_APP_KEY=...
SCHWAB_APP_SECRET=...
SCHWAB_CALLBACK_URL=https://127.0.0.1
```

### 6. Authorize once, then run the server

```bash
schwab-connector auth             # browser flow, paste redirected URL
schwab-connector                  # start the MCP server on stdio
```

Or expose it over HTTP for remote MCP clients:

```bash
schwab-connector --transport streamable-http --port 8765
```

### `schwab-connector auth` vs `schwab-connector` — when to run which

- **`schwab-connector auth`** is the interactive OAuth bootstrap. It
  opens your browser, you log into Schwab, and you paste the redirect
  URL back into the terminal. It writes `schwab-token.json` (access +
  refresh token) and exits. Run it:
  - the **first time** you set up the repo;
  - any time **`schwab-connector` prints `SchwabAuthError`** (the
    refresh token is dead — happens after ~7 days of no use, or if
    you revoke the app);
  - after **rotating** `SCHWAB_APP_KEY` / `SCHWAB_APP_SECRET`, since
    tokens are bound to the app registration.
- **`schwab-connector`** (no subcommand) starts the MCP server. It reuses
  the token file written by `auth` and refreshes the access token on
  its own as needed. This is the one Claude actually talks to — leave
  it running in a terminal while you use the connector. You do **not**
  need to re-run `auth` each session; only when the refresh token
  itself has expired.

Tokens are persisted to `~/.schwab-connector/schwab-token.json`
(overridable via `SCHWAB_TOKEN_FILE`). Access tokens auto-refresh;
refresh tokens expire ~7 days and require re-running
`schwab-connector auth`.
