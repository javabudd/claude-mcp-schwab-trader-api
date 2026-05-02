# DEVELOPING.md — traider

Developer overlay for `traider`. For runtime / analyst
guidance — the docs that get loaded into an AI CLI's context — see
[AGENTS.md](AGENTS.md). This file is for humans (and coding agents)
touching the code.

---

## Table of contents

- [Environment setup](#environment-setup)
- [Package layout](#package-layout)
- [How the provider system works](#how-the-provider-system-works)
- [Running locally](#running-locally)
- [Logging](#logging)
- [Adding a new provider](#adding-a-new-provider)
- [Shared modules](#shared-modules)
  - [ohlcv (TA + analytics)](#ohlcv-ta--analytics)
  - [options (chain summary)](#options-chain-summary)
- Per-provider dev notes
  - [schwab](#schwab)
  - [yahoo](#yahoo)
  - [fred](#fred)
  - [fed-calendar](#fed-calendar)
  - [sec-edgar](#sec-edgar)
  - [factor](#factor)
  - [treasury](#treasury)
  - [news](#news)
  - [earnings](#earnings)
  - [estimates](#estimates)
  - [eia](#eia)
  - [cftc](#cftc)
  - [intent](#intent)

---

## Environment setup

**All Python commands in this repo run inside a conda env named
`traider`.** TA-Lib has a C dependency; conda-forge is the supported
install path and the Docker image uses the same recipe.

```bash
conda create -n traider -c conda-forge -y python=3.13 ta-lib
conda activate traider
pip install -e .
```

`pyproject.toml` declares every provider's deps at the top level —
`yfinance`, `lxml`, `beautifulsoup4`, `numpy`, `TA-Lib`, `mcp`, `httpx`,
`python-dotenv`. They're installed unconditionally; which of them
actually get *imported* is decided at runtime by the provider loader.

## Package layout

```
src/traider/
  __init__.py              # version, module docstring
  __main__.py              # entry: traider [auth schwab] | server
  server.py                # FastMCP setup, PROVIDERS map, lazy loader
  settings.py              # TraiderSettings (TRAIDER_PROVIDERS, log_dir)
  logging_utils.py         # attach_provider_logger(logger_name, path)
  ohlcv/
    __init__.py            # re-exports analytics + ta
    ta.py                  # TA-Lib indicator runner (provider-agnostic)
    analytics.py           # pure-numpy return/risk/regime/structure
  options/
    __init__.py            # re-exports summary
    summary.py             # bounded-size analyst view of an option chain
  providers/
    __init__.py            # (empty — providers are imported lazily)
    schwab/
      __init__.py
      tools.py             # def register(mcp, settings) — the MCP surface
      schwab_client.py     # OAuth-authenticated httpx client
      auth.py              # interactive authorization-code flow
    yahoo/
      __init__.py
      tools.py
      yahoo_client.py      # yfinance wrapper, Schwab-shaped payloads
    fred/
      __init__.py
      tools.py
      fred_client.py       # httpx wrapper
      analytics.py         # pure-numpy macro analytics over FRED observation series
    fed_calendar/
      __init__.py
      tools.py
      fomc_scraper.py      # httpx + BeautifulSoup
    sec_edgar/
      __init__.py
      tools.py
      edgar_client.py      # httpx + token bucket + UA enforcement
      ticker_map.py        # company_tickers.json cache (24h TTL)
      form4_parser.py      # lxml
      form13f_parser.py    # lxml
    factor/
      __init__.py
      tools.py
      french_client.py     # fetch + disk cache + CSV block parser
    treasury/
      __init__.py
      tools.py
      treasury_client.py   # httpx wrapper around Fiscal Data
    news/
      __init__.py
      tools.py
      massive_client.py    # httpx wrapper around /v2/reference/news
    earnings/
      __init__.py
      tools.py
      finnhub_client.py    # httpx wrapper around Finnhub calendar/earnings
    estimates/
      __init__.py
      tools.py
      finnhub_client.py    # httpx wrapper around Finnhub /stock/recommendation
    eia/
      __init__.py
      tools.py
      eia_client.py        # httpx wrapper around api.eia.gov/v2
    cftc/
      __init__.py
      tools.py
      cftc_client.py       # httpx wrapper around publicreporting.cftc.gov (Socrata)
    intent/
      __init__.py
      tools.py
      store.py             # SQLite-backed local trade-intent journal (no external writes)
      account_profile.py   # YAML loader for user-authored account framing (age, role, risk capacity)
      rules.py             # YAML rules loader (seed rules/ + per-user rules.local/ overlay)
```

Each provider is **self-contained under its directory** — imports
inside a provider are relative (`from .client import X`, `from ..
logging_utils import …`). Providers don't import from each other, but
they may import from package-level shared modules (`logging_utils`,
`ohlcv`, `options`, `settings`). The `ohlcv` package holds
provider-agnostic TA + quant analytics that operate on the generic
candle shape every market-data backend emits; both `schwab/tools.py`
and `yahoo/tools.py` consume it via `from ...ohlcv import analytics`
and `from ...ohlcv.ta import run_indicators`. See
[Shared modules → ohlcv](#ohlcv-ta--analytics) for the contract and
the rationale for what does and does not belong there. The `options`
package is the analogous home for option-chain-shape helpers — it
currently exposes `summary.summarize_chain` (a bounded analyst view of
the canonical option-chain payload) consumed by both providers via
`from ...options.summary import summarize_chain`.

## How the provider system works

One server, many providers. `TRAIDER_PROVIDERS` (comma-separated in
`.env` or the process env) lists the providers to load:

```
TRAIDER_PROVIDERS=schwab,fred,sec-edgar,factor,treasury,news
```

Startup flow, in `src/traider/server.py`:

1. `load_settings()` reads `TRAIDER_PROVIDERS` and `TRAIDER_LOG_DIR`
   into a frozen `TraiderSettings`.
2. `_validate_providers(...)` rejects unknown names and enforces the
   `schwab` / `yahoo` mutex.
3. `_build_mcp()` creates one `FastMCP("traider", …)` instance.
4. `load_providers(mcp, settings)` walks the provider list and calls
   `importlib.import_module(PROVIDERS[name])` — this is where lazy
   loading happens. `yfinance`, `TA-Lib`, `lxml`, etc. only get
   imported for providers that are enabled.
5. For each loaded module, `module.register(mcp, settings)` is called
   to hang `@mcp.tool()` functions on the shared FastMCP.
6. `mcp.run(transport=…)` starts the transport loop.

`schwab` and `yahoo` are listed in `MARKET_DATA_PROVIDERS`; enabling
both is a `SystemExit` at startup rather than a race on the shared
tool names.

### The register contract

Every provider's `tools.py` exposes:

```python
def register(mcp: FastMCP, settings: TraiderSettings) -> None:
    attach_provider_logger("traider.<name>", settings.log_file("<name>"))

    @mcp.tool()
    def some_tool(...): ...
    @mcp.tool()
    def other_tool(...): ...
```

Module-level state (client singletons, caches) lives at module scope;
`register()` is called once at startup. Tool functions close over the
module's `_get_client()` / config and capture the `mcp` instance
through the decorator.

## Running locally

```bash
conda activate traider
pip install -e .

export TRAIDER_PROVIDERS=schwab,fred,sec-edgar
export SCHWAB_APP_KEY=...          # if TRAIDER_PROVIDERS includes schwab
export SCHWAB_APP_SECRET=...
export SCHWAB_CALLBACK_URL=https://127.0.0.1
export FRED_API_KEY=...            # if TRAIDER_PROVIDERS includes fred
export SEC_EDGAR_USER_AGENT="your-name you@example.com"   # if sec-edgar

# Schwab-only: one-time interactive OAuth. Writes to
# ~/.schwab-connector/schwab-token.json (or $SCHWAB_TOKEN_FILE).
traider auth schwab

# Then the server:
traider                                          # streamable-http on 8765
traider --transport stdio                        # for direct CLI wiring
traider --transport streamable-http --port 9000  # custom port
```

`python -m traider` is equivalent to `traider`.

### Docker

`Dockerfile` + `docker-compose.yml` at the repo root build a single
image that carries every dep (conda ta-lib + all pip deps). The
service entry reads `TRAIDER_PROVIDERS` from `.env` and exposes 8765.

```bash
docker compose build
docker compose run --rm traider auth schwab   # schwab only
docker compose up -d
```

Volume mounts:

- `${HOME}/.schwab-connector:/tokens` — Schwab OAuth token file.
- `./logs:/app/logs` — per-provider log files (host-visible).
- `${HOME}/.cache/traider-factor:/cache` — Ken French ZIP cache.

## Logging

Two layers:

- **Root aggregate log**: `<TRAIDER_LOG_DIR>/traider.log` (default
  `./logs/traider.log`). Captures the `traider`, `mcp`, `uvicorn*`,
  and `httpx` loggers.
- **Per-provider logs**: `<TRAIDER_LOG_DIR>/<name>.log` (e.g.
  `logs/schwab.log`, `logs/fred.log`). Each provider's `register()`
  calls `attach_provider_logger("traider.<name>", settings.log_file("<name>"))`.

Rotation: 5 MB × 3 backups on every handler. Tool handlers wrap their
bodies in `logger.exception(...)`, so the full traceback for a failed
tool call lands in the relevant per-provider log. **When a tool
call fails, read the log file before asking the user for the
traceback** — MCP transports often hide stdout/stderr from the agent
calling tools.

## Adding a new provider

Mirror an existing small one (e.g. `news/` or `treasury/`):

```
src/traider/providers/<name>/
├── __init__.py          # one-line module docstring
├── tools.py             # def register(mcp, settings)
└── <client>.py          # thin httpx wrapper (or scraper, or parser)
```

Then:

1. **Add the provider** to `PROVIDERS` in `src/traider/server.py`.
2. **Add any new deps** to the top-level `pyproject.toml`
   (`dependencies = [...]`) — but keep the heavy imports inside
   `tools.py` so unused providers don't pay the load cost.
3. **Add env-var docs** to `.env.dist`.
4. **Link from root docs**: row in the provider table in `README.md`
   and `AGENTS.md`, plus a section in this file's
   [per-provider dev notes](#per-provider-dev-notes).

Conventions shared across providers:

- Clients are thin (one method per upstream endpoint). JSON comes
  back essentially unchanged so the model sees the raw shape.
- Tool docstrings are where the guidance for the model lives — pick
  of parameters, period formats, gotchas. Keep them detailed; the
  model reads them through MCP.
- Raise on 429 / upstream errors. No silent retry-loop fallbacks.
- Every response that hits an upstream should include a `source` URL
  and `fetched_at` timestamp if the upstream doesn't already provide
  one, so the analyst can cite it.

---

## Shared modules

### ohlcv (TA + analytics)

`src/traider/ohlcv/` holds the TA-Lib indicator runner (`ta.py`) and
the pure-numpy quant analytics (`analytics.py`). Both operate on the
generic candle-list shape every market-data backend emits:
`[{open, high, low, close, volume, datetime}, ...]` with `datetime`
in epoch ms UTC. Nothing in here imports from a specific provider —
this is the boundary where market-data backends agree on a schema.

**What lives here.** Stateless, pure-numpy (or pure-TA-Lib) functions
that take the candle shape (or a dict of candle lists, for cross-asset
analytics) and return a JSON-safe dict. No HTTP, no caching, no
provider-specific symbology. If a function needs anything beyond the
candle dict to work, it takes that as an argument.

**What does NOT live here.** Anything that knows about a specific
upstream API, OAuth, ticker conventions (Schwab's `$VIX` vs Yahoo's
`^VIX`), or response wrappers. Those are provider concerns; the tool
wrapper in `providers/<name>/tools.py` is the layer that adapts the
provider's payload to the candle shape and adds `source` /
`fetched_at` to the response.

**Public surface (current).**

- `ta.run_indicators(candles, indicators, tail=None)` — TA-Lib
  pass-through.
- `analytics.returns_metrics(candles, ...)` — Sharpe / Sortino /
  Calmar / max drawdown / skew / kurtosis. Set
  `include_drawdown_series=True` to also return the per-bar equity
  curve and drawdown series.
- `analytics.realized_volatility(candles, method=...)` — close-to-close,
  Parkinson, Garman-Klass, Rogers-Satchell.
- `analytics.correlation_matrix(candles_by_symbol)` — Pearson on log
  returns, inner-joined on timestamp.
- `analytics.rolling_correlation(a, b, window)` — rolling Pearson on
  two assets.
- `analytics.beta(asset, benchmark, ...)` — beta / alpha / R².
- `analytics.volatility_regime(candles, ...)` — regime label from
  rolling vol z-score against trailing distribution.
- `analytics.rolling_zscore(candles, window, source)` — rolling z of
  close or log returns.
- `analytics.pair_spread(a, b, ...)` — OLS hedge ratio + spread
  z-score + AR(1) half-life.
- `analytics.session_ranges(candles, ...)` — Asia/London/NY session
  buckets with tight-Asia and London-sweep flags.
- `analytics.support_resistance(candles, ...)` — fractal swing
  highs/lows + classic / Fibonacci / Camarilla pivot points (pivots
  require caller-supplied prior H/L/C).
- `analytics.anchored_vwap(candles, anchor=...)` — VWAP from a point
  in the series (epoch ms, ISO date/datetime, or `None`).
- `analytics.donchian_channels(candles, period)` — rolling
  highest-high / lowest-low / midline.
- `analytics.mean_reversion_score(candles, ...)` — Hurst exponent +
  Lo-MacKinlay variance ratio combined into a regime label.
- `analytics.atr_stop_levels(candles, entry, side, ...)` — Wilder
  ATR + stop / target / R:R for a hypothetical entry.

**Things that will bite you.**

- **Annualization is inferred from bar spacing.** Most analytics that
  return annualized stats accept an `annualization` override; for
  irregular bars or non-RTH intraday data, pass it explicitly
  (daily=252, weekly=52, monthly=12, 1-min RTH≈98280). The inference
  uses median bar spacing — it works for clean daily/weekly/monthly
  series but is noisy on extended-hours data.
- **JSON has no NaN.** `_jsonify` walks the result tree and converts
  NaN/inf/non-finite numpy floats to `None`. Don't return raw
  `np.nan` from a new function — always pass through the helper.
- **Hurst / variance ratio are estimators, not tests.** They don't
  reject the random-walk null at any p-value. The regime label is a
  bias for strategy choice, not a trade signal.
- **Pivot points require caller-supplied prior session H/L/C.** The
  `support_resistance` function does not infer the daily session
  boundary from intraday bars. If `prior_high` / `prior_low` /
  `prior_close` are omitted, pivots are skipped (swings still
  returned).
- **TA-Lib still loads lazily.** `ta.py` imports `talib.abstract`
  only on the first call to `run_indicators`, so the MCP server can
  still start for non-TA tools when TA-Lib isn't installed.
  Preserve that pattern if you add new TA-Lib-dependent functions
  here.
- **No pandas, no scipy.** Pure numpy keeps the dep surface narrow
  and the analytics composable. Don't reach for either; rolling
  stats are a few lines of numpy.

**What not to do.**

- Don't add functions that call into a specific provider's client.
  Take the candle shape as input.
- Don't add caching. These are pure functions; a stale cache hit
  defeats the freshness contract callers rely on.
- Don't reshape the candle dict. New analytics should accept the
  same `[{open, high, low, close, volume, datetime}, ...]` shape so
  callers can pipe `get_price_history` output through without
  munging.
- Don't introduce per-provider variants. If two backends emit
  candles in different shapes, the right fix is to normalize at the
  provider layer, not to fork the analytics.

### options (chain summary)

`src/traider/options/` holds provider-agnostic helpers that operate on
the canonical option-chain payload both `schwab` and `yahoo` emit:
top-level `underlyingPrice` / `symbol` / `isDelayed` plus
`callExpDateMap` and `putExpDateMap` keyed by `"YYYY-MM-DD:dte"` →
strike (string) → list of contract dicts. Same boundary rule as
`ohlcv`: nothing here imports from a specific provider — this is
where market-data backends agree on a schema.

**Public surface (current).**

- `summary.summarize_chain(chain, wings=5, top_n=5)` — bounded
  analyst view of a chain. Per expiration: ATM straddle / implied
  move / implied range, IV skew across ±`wings` strikes around ATM,
  top `top_n` strikes by open interest and volume on each side.
  Pass-throughs `dataQualityWarning` when the source provider
  emitted one. Raises `ValueError` if `underlyingPrice` is missing
  (no anchor for ATM).

**Things that will bite you.**

- **The shape, not the source.** `summarize_chain` keys off
  `callExpDateMap` / `putExpDateMap` and the contract fields
  (`mark`, `bid`, `ask`, `volatility`, `delta`, `openInterest`,
  `totalVolume`, `inTheMoney`). If a future market-data backend
  emits a chain in a different shape, normalize it at the provider
  layer (Yahoo already does this in `yahoo_client.py`) — don't fork
  this module.
- **`mark` is mid-of-bid-ask, not a fill.** The summary surfaces
  `mark` straight through; OPTIONS.md has the analyst-side caveat,
  but the function itself does not "improve" the price.

**What not to do.**

- Don't add caching or HTTP. Pure functions only.
- Don't add provider-shaped pass-throughs (e.g. Schwab-specific
  `underlying.*` blocks). The summary is intentionally narrow to the
  fields both backends carry.
- Don't introduce per-provider variants. If a backend's chain shape
  drifts, normalize at the provider boundary.

---

## Per-provider dev notes

Each subsection captures the dev-oriented content from the provider's
original per-server AGENTS.md: internals, secrets, gotchas, and "what
not to do." Runtime analyst guidance lives in the root
[AGENTS.md](AGENTS.md).

### schwab

**What it is.** Read-only bridge to the Schwab Trader API. Quotes,
OHLCV history, TA-Lib indicators, option chains (with Greeks), movers,
market hours, accounts, and pure-numpy analytics. Pure Python over
HTTP — no COM, no desktop app.

**Secrets.** `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`,
`SCHWAB_CALLBACK_URL` (must match the app registration in Schwab's
developer portal). Tokens persist to the file at `SCHWAB_TOKEN_FILE`
(default `~/.schwab-connector/schwab-token.json`, mode 0600).
`SchwabClient` auto-refreshes the access token; if the refresh token
itself is dead, it raises `SchwabAuthError` and the user must re-run
`traider auth schwab`.

**Why not the TOS RTD path anymore.** The repo originally tried to
reach TOS Desktop's RTD COM server (`Tos.RTD`, the interface behind
Excel's `=RTD("tos.rtd",…)` formulas). That path is abandoned. The
blocking issue: `IRTDUpdateEvent` is a dual COM interface, and
pywin32 can only synthesize a real vtable for a dual interface from a
registered type library (Office's `MSO.DLL` ships it; plain Windows
does not). Without that TLB, every call path into our Python callback
eventually hits undefined vtable memory and `Py_FatalError`s the
process. Investigating that took a lot of time — don't re-open it
without (a) Office installed and (b) a plan to use
`win32com.universal.RegisterInterfaces`.

**Things that will bite you.**

- **Token expiry.** Schwab access tokens expire in ~30 minutes and
  refresh tokens in ~7 days. After a lapse, the user has to re-run
  `traider auth schwab`. Don't silently swallow "invalid refresh
  token" — surface it.
- **`currentDayProfitLoss` is not day P&L on same-day opens.** The
  field in `get_accounts` positions equals `marketValue` for any
  position opened today (detectable via
  `previousSessionLongQuantity` / `previousSessionShortQuantity` of
  `0`, or non-zero `currentDayCost`). For same-day opens the
  correct open P&L is in `longOpenProfitLoss` / `shortOpenProfitLoss`.
  The `get_accounts` tool docstring carries the full warning — it
  is load-bearing guidance for any consuming LLM, not just a dev
  note. If you refactor the tool, preserve it.
- **Options symbology.** Schwab expects the 21-character OSI format
  (e.g. `SPY   250321C00500000`), not dotted TOS notation. Equities
  and futures (`/ES`) work as-is.
- **Index symbology on `/quotes` — `$PREFIX`, no `.X`, no `^`.**
  Indices and CBOE yield products need a `$` prefix: `$VIX`, `$SPX`,
  `$NDX`, `$DJI`, `$RUT`, `$COMPX`, `$XSP`, `$VVIX`, `$VXN`, `$TNX`
  / `$TYX` / `$IRX` / `$FVX`. The `.X` suffix and `^` prefix (ToS /
  Yahoo conventions) both return empty strings; so does the bare
  root (`VIX`). Non-CBOE indices (ICE `$DXY`, index-option roots
  like `$SPXW`) don't resolve. Miss mode is a silent empty
  `result`, not an HTTP error — callers can't distinguish wrong
  symbol, wrong convention, and closed market. The full rule (with
  the 10× quirk for CBOE yield products) lives in the `get_quote`
  docstring; keep that authoritative if you refactor.
- **Option chains are nested maps, not lists.** `get_option_chain`
  returns Schwab's native `callExpDateMap` / `putExpDateMap` keyed by
  `"YYYY-MM-DD:dte"` → strike → **list** of contracts (Schwab allows
  multiple strategy legs per strike). When flattening for analysis,
  iterate the list; don't assume length 1 even for `strategy=SINGLE`.
- **`strategy` overrides are load-bearing.** `ANALYTICAL` re-prices
  the whole chain against caller-supplied `volatility` /
  `underlying_price` / `interest_rate` / `days_to_expiration`. If any
  of those are off, every Greek and theoretical value in the response
  is off too. Don't set them just to round-trip — leave them `None`
  unless the user is explicitly asking for a re-priced chain.
- **Market hours — REST `/quotes` is RTH-anchored by design.**
  Per the Schwab Streamer docs, `LEVELONE_EQUITIES` splits
  last-price into Field 3 `Last Price` (all trades) and Field 29
  `Regular Market Last Price` ("Only records regular trade").
  The REST `/marketdata/v1/quotes` response mirrors Field-29
  semantics: `lastPrice`, `netChange`, `mark`, `postMarketChange`,
  `tradeTime`, `quoteTime` all pin at the 4PM regular-session
  close and don't advance during pre/post/overnight. Not a cache
  bug — the endpoint's spec. **For live extended-hours data use
  `/pricehistory` with `needExtendedHoursData=true`**: it returns
  real minute bars with volume from 07:00 ET through the 20:00 ET
  post-market close. The 20:00–07:00 ET **overnight 24/5 session
  is not covered by any Schwab REST endpoint** — passing a start
  date inside that window returns the same standard-session
  dataset. The Streamer API docs don't commit to overnight
  coverage for `LEVELONE_EQUITIES` either (it's the only
  LEVELONE service without explicit "Update Regular / AM-PM"
  columns, and "overnight" / "24/5" appears nowhere in the
  Streamer spec). Overnight visibility for QQQ/SPY/etc. is
  currently a ToS-only surface. The `fields` whitelist on
  `get_quotes` is strict — a narrow list silently drops the
  AH-delta keys, so the `get_quote` / `get_quotes` docstrings
  name them explicitly; preserve that when refactoring.
- **Sandbox vs production.** If you set `SCHWAB_BASE_URL`, make sure
  it points where you intend.
- **Price history parameter combos.** Schwab's `/pricehistory`
  endpoint rejects most `periodType` / `frequencyType` / `period` /
  `frequency` combinations with a terse 400. The valid matrix is in
  the tool docstring. If tempted to add client-side validation, don't
  — the response is specific enough, and the matrix is subject to
  change.
- **Candle timestamps are epoch ms UTC.** Convert to
  `America/New_York` when formatting for display or computing session
  boundaries.
- **Transactions endpoint takes ISO-8601 with milliseconds.**
  `startDate` / `endDate` on `/trader/v1/accounts/{hash}/transactions`
  require the full `YYYY-MM-DDTHH:MM:SS.000Z` shape — a bare
  `YYYY-MM-DD` 400s. `_normalize_iso_datetime()` in `schwab_client.py`
  expands pure dates (start→`00:00:00.000Z`, end→`23:59:59.999Z`) so
  the tool can accept either shape from the model. Don't reach into
  that helper to drop the ms — Schwab rejects `...:00Z`.
- **Transaction records nest the per-leg details in `transferItems`.**
  A single `TRADE` record carries an array: one entry per leg
  (each with `price`, `amount` as signed quantity, `cost`, and an
  `instrument` block) plus separate entries for commissions and
  fees marked with `feeType`. Don't read `transaction.price` as the
  fill price — that field is a summary / may not be set. Iterate
  `transferItems` and filter on the instrument to reconstruct leg
  fills, commissions, and realized P&L.
- **Account number vs. hash.** `/trader/v1/accounts` returns the
  plaintext `accountNumber`; every other `/trader/v1/accounts/{X}`
  endpoint expects the `hashValue` from
  `/trader/v1/accounts/accountNumbers`. `get_account_numbers` is the
  discovery tool. The `get_transactions` / `get_orders` tools
  auto-resolve when exactly one account is authorized, so
  single-account users can skip the lookup; multi-account users must
  pass `account_hash` explicitly.
- **Orders endpoint lookback is ~60 days.** Unlike transactions
  (~1 year), `/trader/v1/accounts/{hash}/orders` rejects a
  `fromEnteredTime` older than 60 days. The `get_orders` tool
  defaults both date params to "last 60 days" on purpose — don't
  widen that default without also handling the upstream 400. Same
  ISO-8601-with-milliseconds format as the transactions endpoint;
  `_normalize_iso_datetime()` is reused.
- **Multi-leg / conditional orders nest under `childOrderStrategies`.**
  A single order record's top-level `orderLegCollection` holds the
  primary legs, but OCO groups and trigger (first-triggers-next)
  orders hang the other legs under `childOrderStrategies` as full
  order sub-objects (each with its own `orderId`, `status`,
  `orderLegCollection`). Flattening to "legs on this order" has to
  walk that array recursively — don't read `orderLegCollection`
  alone.
- **TA-Lib is a C dep.** Use conda-forge (`ta-lib`) or the distro
  package; the pip wrapper needs the native library present first. If
  the wrapper imports but returns garbage, check the C lib version
  matches what the wheel was built against. Don't silently fall back
  to a pure-Python reimplementation — indicator outputs won't match
  what users expect from TA-Lib.
- **TA-Lib warmup NaNs.** Indicators need history before producing a
  value (SMA(20) returns NaN for the first 19 points). The shared
  `ohlcv/ta.py` converts those to JSON `null`; don't strip them —
  the positions have to stay aligned with `datetime`.
- **Series size.** `run_technical_analysis` returns one value per
  candle per indicator by default. A year of 1-minute bars × several
  indicators can blow up the response. Push callers toward `tail` or
  a coarser frequency when they don't need the full history.

**What not to do.**

- Don't store OAuth tokens in the repo, in env files committed to git,
  or in log output.
- Don't introduce an ORM, a database, or a queue. Thin HTTP client
  plus MCP surface — keep it thin.
- Don't add write operations. Schwab's API supports them; this
  provider does not, by policy.
- Don't paper over rate limits with exponential-retry loops. One retry
  for a transient 5xx is fine; 429s should propagate.
- Don't re-attempt the RTD COM path without Office's MSO TLB and a
  concrete plan for `win32com.universal`.

### yahoo

**What it is.** Read-only bridge to Yahoo Finance via
[`yfinance`](https://pypi.org/project/yfinance/). Exists for users
without a Schwab developer account. Tool surface intentionally
matches `schwab` so prompts are portable. When Yahoo can't cover a
capability, the tool raises `YahooCapabilityError` rather than
inventing a response.

**Secrets.** None. yfinance is unauthenticated.

**Capability gaps vs. schwab.**

- **`get_accounts`** — raises. Yahoo is a data source, not a brokerage.
- **`get_market_hours`** — raises. Yahoo has no authoritative
  session-hours endpoint.
- **`get_option_chain`** — works but: no Greeks
  (`delta`/`gamma`/`theta`/`vega`/`rho` are `null`), delayed ~15 min,
  illiquid strikes can show stale or zero bid/ask, bid/ask sizes
  emitted as `0`. Response carries a top-level `dataQualityWarning`
  key. Only `strategy="SINGLE"`; strategies and the `ANALYTICAL`-only
  numeric overrides raise.
- **`get_option_expirations`** — returns date list only;
  `expirationType` / `settlementType` / `optionRoots` / `standard`
  are `null`.
- **`get_movers`** — US-market-wide Yahoo screeners, not per-index.
  `sort` selects the screener; `index` is only used when it matches a
  raw Yahoo screener key; `frequency` is ignored.
- **`search_instruments`** — Yahoo's symbol search is fuzzy; all
  `symbol-*` / `desc-*` / `search` projections return the same list.
- **Intraday history depth** — 1m history caps at ~7 days, all
  sub-hourly bars at ~60 days.
- **Option symbology** — Yahoo's own format (e.g.
  `SPY250321C00500000`, no padding). The client does not translate
  to/from Schwab's OSI.
- **10-minute bars** — Yahoo doesn't support them; `frequency=10`
  raises rather than substituting 15m.

**Things that will bite you.**

- **`yfinance` version drift.** Yahoo's endpoints change; breakage
  shows up as `yfinance` exceptions or unexpected empty responses.
  First debugging step is always `pip install -U yfinance` and see if
  upstream shipped a fix.
- **`.info` is slow and rate-limit-prone.** Assembles via several
  HTTP requests. Only called for quotes (bid/ask/sizes) and for
  `projection="fundamental"`. Don't add more callers without reason.
- **Timestamps.** yfinance returns pandas Timestamps with tz. The
  client converts to UTC epoch ms to match the Schwab candle schema.
  If you change the conversion, make sure `ohlcv/analytics.py` still
  sees strictly increasing ms values — it infers annualization from
  bar spacing.
- **Splits / dividends.** `history(..., auto_adjust=False)` gives raw
  OHLC; switching to adjusted prices would change the outputs of
  every `analyze_*` tool. Don't flip the default without updating
  docstrings and tests.
- **Empty candles.** Yahoo drops rows for holidays and halted
  sessions. The client skips NaN rows rather than emitting
  zero-filled bars; an empty `candles` list means the request window
  had no data (common for 1m bars older than ~7 days).
- **Screeners.** `yf.screen(...)` occasionally returns a dict with
  an empty `quotes` list when Yahoo throttles; don't mistake it for
  "no movers today." Log the full response if you see this.
- **Option IV units.** yfinance reports IV as decimal (`0.42` = 42%);
  Schwab reports it as percent (`42.0`). `get_option_chain`
  normalizes to Schwab's convention on the way out — don't
  double-multiply if you touch that path.
- **`Ticker.option_chain(date)` is one HTTP per expiration.** A full
  SPY request is dozens of round-trips. Encourage callers to narrow
  with `from_date` / `to_date` / `exp_month`.

**What not to do.**

- Don't add an `auth` subcommand — yfinance is unauthenticated.
- Don't translate option-symbol formats inside this client.
- Don't paper over 429/Cloudflare responses with retry loops.
- Don't copy-adjust prices silently. Expose a flag and document it.

### fred

**What it is.** Read-only bridge to the
[FRED API](https://fred.stlouisfed.org/docs/api/fred/). Economic-
release calendar (CPI, PPI, NFP, GDP, PCE, retail sales, JOLTS),
release/series metadata, and observation time-series for any of FRED's
~800 000 series.

**Secrets.** `FRED_API_KEY` (free, from
<https://fredaccount.stlouisfed.org/apikeys>).

**Why the heavy lifting lives server-side.** FRED's `/releases/dates`
is a firehose of low-signal releases:

1. **"FOMC Press Release" (release 101) spam** when
   `include_release_dates_with_no_data=true`: fires on every day of
   a meeting window, so a two-week window returns ~14 copies. The
   curated `get_high_impact_calendar` excludes release 101; its
   docstring points callers at the `fed-calendar` provider's
   `get_fomc_meetings` for FOMC dates.
2. **No knob to filter by release** on `/releases/dates` itself.
   `get_release_schedule(release_ids=[...])` and
   `get_high_impact_calendar` fan out per-release and merge the rows.

**FRED release-id cheatsheet for common trading-relevant prints**
(these change rarely; verify with `list_releases` if in doubt):

| Release                          | `release_id` | Common series IDs                                    |
|----------------------------------|-------------:|------------------------------------------------------|
| Consumer Price Index             |           10 | `CPIAUCSL` (headline), `CPILFESL` (core)             |
| Producer Price Index             |           46 | `PPIACO`, `PPIFIS`                                    |
| Employment Situation (NFP)       |           50 | `PAYEMS`, `UNRATE`, `AHETPI`                         |
| Personal Income and Outlays (PCE)|           21 | `PCEPI`, `PCEPILFE` (core)                           |
| Gross Domestic Product           |           53 | `GDP`, `GDPC1` (real)                                 |
| Retail Sales                     |           32 | `RSAFS`, `RSXFS` (ex auto)                           |
| JOLTS                            |          192 | `JTSJOL`, `JTSQUR`                                    |
| FOMC Meeting / Statement         |          101 | — (no series; use `fed-calendar`)                    |

**Things that will bite you.**

- **Realtime vs. observation dates.** FRED distinguishes *realtime*
  (the vintage — when was this value visible to market participants?)
  from *observation* (which period does the value describe?). The
  release-calendar endpoints are realtime-scoped; series observations
  are observation-scoped. Mixing them up puts the wrong dates in
  front of the user.
- **Empty release dates.** When `include_empty=True`, future scheduled
  dates appear with no data yet. That's the signal for "upcoming
  release" — don't filter them out.
- **Series-level units.** Passing `units="pch"` computes a percent
  change server-side. Computing it client-side *too* will
  double-transform.
- **Rate limit.** 120 requests per 60s per key. A tight loop over
  `get_release_series(...)` for many releases will trip it; batch
  with higher `limit` instead.

**What not to do.**

- Don't add an OAuth flow — FRED uses a static API key.
- Don't cache responses silently. If caching is useful, expose the
  TTL and signal stale-hit in the response.
- Don't reshape the FRED JSON into a "nicer" schema. The model can
  read the raw shape; hiding fields makes debugging harder.

### fed-calendar

**What it is.** Read-only scrape of
<https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm>.
Dates and flags only (meeting date range, SEP flag, press-conference
URL, parenthetical notes). No JSON/ICS/RSS feed exists for this
calendar; HTML scrape is the primary source.

**Secrets.** None. federalreserve.gov is public.

**HTML structure we depend on.** Documented so the next person
touching `fomc_scraper.py` knows exactly what will break it. Source:
federalreserve.gov as of 2026.

- Each year is a `div.panel.panel-default` under the article body. The
  heading text contains the year (e.g. `"2026 FOMC Meetings"`).
- Each meeting is a `div.row.fomc-meeting` inside that panel.
- Month label: `div.fomc-meeting__month > strong` — full month names,
  or `"April/May"` style for meetings that straddle two months.
- Date range: `div.fomc-meeting__date` — `"27-28"`, `"8-9*"` (SEP),
  `"22 (notation vote)"`, or parenthetical-only for unscheduled items.
- SEP footnote: trailing literal `*` on the date cell.
- Press conference: `<a>` with `href` matching regex
  `fomcpres{1,2}conf` (Fed has historically used both spellings).
- Minutes / statement URLs: label or href keyword match; not
  semantically tagged.

The panel-footer holds the SEP legend. We don't parse it — the `*`
flag is self-documenting in the tool docstring.

**Things that will bite you.**

- **Fed layout changes.** If every tool call starts failing with "no
  FOMC year panels found", inspect the HTML in a browser and update
  the selectors in `fomc_scraper.py` to match. Don't paper over with
  fuzzy fallbacks.
- **Two-month meetings.** Occasionally a meeting spans April/May or
  October/November. The month cell reads e.g. `"April/May"` and the
  date cell gives two days in different months. The scraper anchors
  `start_date` to the first month, `end_date` to the second. If the
  Fed ever publishes a three-day straddle (unprecedented), logic
  needs updating.
- **Notation votes / unscheduled items.** Purely parenthetical rows
  (e.g. `"(notation vote)"`) with no days are skipped — no date to
  anchor to.
- **Timezone.** `utc_today()` uses UTC. A FOMC decision is an ET
  event, so "today" / "tomorrow" can disagree by a few hours on the
  edges. Document the UTC choice in the tool response rather than
  faking ET.
- **Rate limiting.** federalreserve.gov isn't aggressive, but each
  tool call sends one request. If this grows, add an explicit TTL
  cache — don't let the agent accidentally loop.

**What not to do.**

- Don't add aggregator fallbacks. Fed page is the only source.
- Don't extend to every central bank in one commit. If ECB/BoE/BoJ
  coverage is worth adding, land each as its own module with its own
  primary-source scraper.
- Don't silently cache. If caching helps, expose a visible TTL in
  the response.
- Don't "enhance" meeting records with computed fields that aren't on
  the Fed page (rate-decision probability from fed funds futures,
  etc.). That belongs in analysis code, not the primary-source
  scraper.

### sec-edgar

**What it is.** Read-only bridge to SEC EDGAR (`data.sec.gov`,
`www.sec.gov/Archives`, `efts.sec.gov`). Filings (10-K/10-Q/8-K/S-1/
proxy + FPI equivalents 20-F/6-K), Form 4 insider transactions, 13F
institutional holdings, and XBRL company facts per-company and
cross-sectionally.

**Secrets.** None — EDGAR is public and unauthenticated. The only
configuration is `SEC_EDGAR_USER_AGENT`, which lives in `.env`.
Example value (use a real name/email, not a placeholder):
`SEC_EDGAR_USER_AGENT=traider-hub you@example.com`.

**SEC Fair Access requirements.**

- **Descriptive `User-Agent` required.** Every request must carry a
  UA that identifies the client and includes a contact email, or SEC
  will IP-block. The client enforces this at construction via
  `SEC_EDGAR_USER_AGENT` — **do not** hardcode a default email or
  silently fall back to a generic UA. Fail loud.
- **10 requests/second per IP.** Enforced client-side with a token
  bucket in `edgar_client.py`. On 429 or 403, the client raises
  `SecEdgarRateLimitError` and stops — no retry loops. If a tool
  needs higher throughput than the bucket allows (13F fan-out, large
  Form 4 batches), push pagination to the caller.
- **UA email out of logs.** The UA is a config value, not a secret,
  but it's the user's real contact email in outbound headers. Don't
  include it in tool response bodies — it stays at the HTTP layer.

**Scoping choices made in v1.** These answers should be preserved
until there's a concrete user need to change:

1. **Form 4 is issuer-scoped.** You can list a company's insider
   trades, but there's no "all of CEO Jane Doe's trades across her
   boards" tool. Would need fan-out over her reporting-owner CIK;
   add it only if asked.
2. **13F reverse lookup is not shipped.** `get_institutional_portfolio`
   reads one filer's holdings; no "who holds AAPL?" tool. Real-time
   reverse lookup against SEC alone is O(managers × filings) per
   query — impractical without an in-process index. Defer until
   asked, then decide between building the index and wiring a vendor
   dataset.
3. **Foreign private issuers are included.** Default form-type
   filters don't exclude `20-F` / `6-K`. Users who only want US
   filers can pass `form_types=["10-K", "10-Q", "8-K"]`.
4. **Caching: only the ticker map.** `company_tickers.json` has a
   24-hour TTL (exposed via `ticker_map_fetched_at` in every response
   that consults it). Filings, company facts, concepts, and frames
   are **not cached** — they change on every filing and stale reads
   would mislead. Visible `fetched_at` on every response.

**Things that will bite you.**

- **XBRL concept names are not uniform across filers.** Some
  companies tag revenue as `Revenues`, others as `SalesRevenueNet`,
  others as `RevenueFromContractWithCustomerExcludingAssessedTax`.
  `get_company_concept` 404s on the wrong name. When a lookup
  fails, try `get_company_facts` to see what the filer actually tags,
  or use `get_frame` which aggregates across the concept.
- **Frame periods: duration vs. instantaneous.** Balance-sheet
  concepts (`Assets`, `Liabilities`) only exist as instantaneous
  values — the frame period must end in `I` (e.g. `CY2024Q4I`). Flow
  concepts (`Revenues`, `NetIncomeLoss`) are duration — no `I`
  suffix. Mixing returns 404.
- **13F value units changed in 2022.** SEC switched 13F `value` from
  thousands of dollars to whole dollars for periods ending on or
  after 2022-09-30. The parser infers the unit from `period_of_report`
  and tags it on the envelope. If aggregating, check that field
  before summing.
- **Amendments are separate form codes.** `10-K/A`, `10-Q/A`,
  `8-K/A`, `4/A`, `13F-HR/A` are distinct filings, not silently
  merged. `form_types=["10-K"]` will *not* match `10-K/A`.
- **Submissions feed only holds recent filings inline.** Older
  history spills into `filings.files[*]` overflow JSONs referenced
  by name. The tool layer reads only the inline recent block; for
  deep history, the client has `submissions_overflow(...)` but it's
  not fanned out at the MCP layer yet. Add only when asked.
- **Rate limit is real.** 10 req/sec is enforced by SEC at the IP
  level; the client's token bucket matches. Fan-outs over many
  Form 4 filings *will* hit the wall — prefer fewer, larger tool
  calls and tell the model to paginate.
- **Full-text search endpoint is undocumented.** `efts.sec.gov` powers
  EDGAR's public search page; its response shape is Elasticsearch's
  and SEC can change it without notice. The tool passes raw hits
  through — brittleness is worth the signal.

**What not to do.**

- Don't hardcode a default `User-Agent`. SEC bans "sample UA strings
  copied from the docs." Require the env var.
- Don't retry 429s or 403s. Raise, let the user see the throttle.
- Don't cache filing contents. Filings are immutable once filed, but
  the *set* of filings changes constantly.
- Don't reshape `companyfacts` / `companyconcept` JSON into a "nicer"
  schema.
- Don't add aggregator fallbacks (Yahoo financials, Simply Wall St).
  Primary-source-only.

### factor

**What it is.** Read-only bridge to the
[Ken French Data Library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html).
Fama-French 3/5-factor, momentum, short/long-term reversal, and
N-industry portfolios at monthly / weekly / daily frequencies. Plus an
escape hatch for any other file on the library.

**Secrets.** None. The library is unauthenticated.

**Caching.** ZIPs are disk-cached under `$FACTOR_CACHE_DIR` (default
`~/.cache/traider-factor/`, or `/cache` in the Docker image, mounted
from the host). Cache key is the dataset filename; TTL is per-call
(default 24 h, override with `ttl_seconds=…` on any tool).
`refresh=True` bypasses the cache for one call without invalidating
it for other callers.

The Docker volume mount (`${HOME}/.cache/traider-factor:/cache`)
persists the cache across container restarts. Dropping it causes
every restart to re-fetch — harmless (files are small) but wasteful.

**CSV parser internals.** `french_client.py` is the parser that
matters. Ken French CSVs embed multiple tables in one file, separated
by blank lines, with optional multi-line titles and a
`,Col1,Col2,...` header row. The parser is block-based: split on
blank lines, identify the data-header row inside each block, treat
prose lines above it as the section title, rows below as data.

**Units and sentinels.**

- **Returns are in percent**, not decimals. A value of `2.96` means
  +2.96%, not +296%.
- **Missing values** in the source file are `-99.99` or `-999`. The
  parser converts them to `None` so downstream math doesn't treat
  them as -100% returns.
- **RF** in the factor files is the 1-month T-bill rate for the
  corresponding period (also in percent).

**Things that will bite you.**

- **Units trap.** Values are percents — don't multiply by 100 "to
  convert" them; they already are. Mixing factor returns (pct) with
  raw equity returns (decimal) gives nonsense regressions.
- **Annual blocks** lexicographically sort fine vs. monthly but
  `"2024" > "2023-12"` under string comparison, which is what
  `filter_rows_by_date` uses. Pass bounds matching the frequency of
  the section you're reading (`YYYY` for annual, `YYYY-MM` for
  monthly, `YYYY-MM-DD` for daily). Mixing them quietly filters out
  rows you meant to keep.
- **Industry column names are Ken French's, not yours.** `Durbl`
  isn't a ticker — it's the "Consumer Durables" bucket. See
  `list_datasets`.
- **Daily files are large.** The 48-industry daily file is ~9 MB
  unzipped with ~26k rows. Filter server-side by date rather than
  pulling the whole thing.
- **Filenames aren't intuitive.** `F-F_Research_Data_5_Factors_2x3`
  is the 5-factor file; `2x3` is the sort methodology, not the column
  count. `list_datasets` maps the short-form `model` parameter to
  the right filename.

**What not to do.**

- Don't add a pandas-datareader fallback. The URL pattern is stable;
  if it breaks, raise.
- Don't silently widen the cache TTL to mask a fetch failure. TTL is
  per-call and explicit.
- Don't reshape column names ("Mkt-RF" → "market_minus_rf"). Ken
  French names are canonical in the literature.
- Don't add a "compute alpha / factor exposure" tool here. This
  provider fetches and parses; any regression belongs in a separate
  tool or client-side over this + a market-data backend.

### treasury

**What it is.** Read-only bridge to the
[Treasury Fiscal Data API](https://fiscaldata.treasury.gov). Three
primary-source datasets:

- **Securities auction results** — bid-to-cover, stop-out yield/rate,
  primary-dealer takedown, direct/indirect bidder share.
- **Daily Treasury Statement (DTS)** — operating cash balance (TGA),
  deposits/withdrawals, public-debt transactions, etc.
- **Debt to the Penny** — daily total public debt outstanding.

**Yield curve is deliberately not here.** FRED mirrors Treasury's
H.15 in full (`DGS1MO` … `DGS30`, `DFII*` for TIPS). This provider
exists for the Treasury datasets FRED does not carry at useful
granularity.

**Secrets.** None. Fiscal Data is unauthenticated. We still send a
descriptive UA so Treasury's logs can identify the traffic.

**Fiscal Data query dialect.** All three tools funnel through one
`TreasuryClient.query` method:

- `filter` — `field:op:value,field:op:value,...` where op is `eq`,
  `gte`, `gt`, `lte`, `lt`, `in`.
- `fields` — comma-separated projection.
- `sort` — field name, `-` prefix for desc.
- `page[size]` / `page[number]` — page size max 10 000, 1-indexed.

If a user asks for something outside the curated tool surface
(a different dataset in the Fiscal Data catalog), expose it as a new
tool rather than repurposing `query` as a raw passthrough. Value lives
in the projection and defaults.

**Things that will bite you.**

- **The DTS changed format in 2022.** The legacy PDF format and the
  new JSON-native tables differ. This provider only talks to the new
  Fiscal Data tables (`/v1/accounting/dts/...`). Data before Oct 2022
  returns new-format columns only — the endpoints will not
  reconstruct the old table structure.
- **Amounts are strings.** Fiscal Data returns monetary fields as
  strings (e.g. `"847182563921.43"`) to preserve precision. Don't
  assume numeric JSON; the client returns JSON verbatim.
- **`record_date` vs. reporting window.** DTS `record_date` is the
  date the statement covers (usually T-1 settlement). Auction
  `record_date` is the date the record was published; use
  `auction_date` for filtering when you care about auction timing.
- **Paging matters for long windows.** Default page size 100. A full
  year of daily TGA balances is ~260 rows (under one page); a full
  year of auctions can exceed 1 000 rows. Bump `limit` or iterate
  `page`.
- **Yield curve is elsewhere.** 2Y / 10Y / 30Y time series →
  `fred` provider's `get_series("DGS10")` — not `get_auction_results`.
  Auction high yields are stop-out yields for a specific sale, not a
  secondary-market curve.

**What not to do.**

- Don't add a yield-curve tool here. Route to FRED.
- Don't add a "secondary-market rate" tool — also FRED (H.15).
- Don't reshape Fiscal Data's column names.
- Don't cache responses silently. If caching becomes necessary, expose
  the TTL and signal stale-hit in the response.
- Don't retry 429s. Let them propagate.

### news

**What it is.** Read-only bridge to Massive's
`/v2/reference/news` endpoint. The rest of Massive's surface (quotes,
aggregates, trades) is intentionally out of scope — traider already
has dedicated market-data backends.

Articles carry publisher metadata, the tickers they reference, and a
per-ticker `insights` array with Massive's sentiment label and
reasoning. **That sentiment is Massive's model output, not a primary-
source fact** — treat it as one signal among many, not ground truth.

**Secrets.** `MASSIVE_API_KEY` (register at
<https://massive.com>). Sent as an `apiKey` query param on every
request. Do not log it.

**Things that will bite you.**

- **Ticker is case-sensitive.** Massive documents `ticker` as
  case-sensitive. Pass `AAPL`, not `aapl`.
- **`published_utc` is RFC3339.** For minute-level precision, use the
  full timestamp (e.g. `2026-04-18T13:30:00Z`). ISO dates also work.
- **Sentiment is Massive's, not yours.** `insights[].sentiment` is a
  model label ("positive" / "negative" / "neutral") with a
  `sentiment_reasoning` blurb. Quote with attribution; don't
  aggregate or average across articles without flagging that as
  interpretation.
- **Pagination uses `next_url`.** Massive returns a pre-built cursor
  URL in the response — follow it if needed. The tool does not
  aggregate pages for you.
- **Free tier rate limits.** Massive throttles free-tier keys. A burst
  of `get_news` calls will 429; the tool surfaces it instead of
  retrying silently. Back off or batch.

**What not to do.**

- Don't expand into Massive's other endpoints (aggregates, trades,
  fundamentals) — re-introduces the "which provider owns quotes?"
  routing ambiguity traider avoids.
- Don't cache responses silently. News freshness is the whole point;
  a stale cache hit defeats the tool.
- Don't retry 429s internally.
- Don't blend Massive's feed with another news provider in one tool
  call. A second news source gets its own provider.

### earnings

**What it is.** Read-only bridge to
[Finnhub](https://finnhub.io/docs/api). Two free-tier endpoints:

- ``/calendar/earnings`` — forward- and backward-looking earnings
  calendar with consensus EPS / revenue.
- ``/stock/earnings`` — per-ticker history of EPS actual vs.
  estimate (surprise%).

Everything else on Finnhub's surface (quotes, fundamentals,
sentiment, recommendation trends, ...) is intentionally out of
scope — quotes stay on the market-data backend, filings on
``sec-edgar``, news on ``news``.

**Secrets.** ``FINNHUB_API_KEY`` (register at
<https://finnhub.io>). Sent as the ``X-Finnhub-Token`` header on
every request. Do not log it and do not fall through to a header-
less request — free tier returns 401 without the token.

**Things that will bite you.**

- **Free-tier coverage is US issuers.** Finnhub's ``international``
  flag on the calendar endpoint requires a paid plan; this client
  does not expose it. If a ticker looks right but the calendar
  returns empty, it's likely a non-US listing.
- **Rate limit is 60 req/min.** Enforced upstream; 429s propagate
  as ``FinnhubError`` with no retry loop. A fan-out over many
  tickers will trip it — prefer one cross-market calendar call
  over per-ticker loops when possible.
- **Consensus is Finnhub's aggregation.** ``epsEstimate`` /
  ``revenueEstimate`` are Finnhub's sell-side consensus, not a
  primary-source fact. Quote with attribution. Actuals (``epsActual``
  / ``revenueActual``) *are* primary — they come from the 8-K /
  earnings release.
- **``hour`` field semantics.** ``"bmo"`` = before market open,
  ``"amc"`` = after market close, ``"dmh"`` = during market hours,
  ``""`` = not specified. The empty string is common for smaller
  names — surface it, don't guess.
- **``stock/earnings`` returns a list, not a dict.** The tool layer
  wraps it in a dict so the response is citable (``source`` +
  ``fetched_at`` envelope). Don't flatten that envelope without a
  replacement for the provenance fields.
- **Date format.** The calendar endpoint takes ``YYYY-MM-DD`` for
  both ``from`` and ``to`` and requires both — there's no
  "one day" shortcut. The tool defaults ``to_date`` to
  ``from_date + 14`` days so the common "upcoming two weeks" query
  is a zero-arg call.
- **EPS units.** Reported in USD per share, regardless of the
  issuer's primary currency. Revenue is in USD (absolute dollars,
  not thousands).

**What not to do.**

- Don't add an OAuth flow — Finnhub is a static API key.
- Don't widen the tool surface to quotes / fundamentals /
  sentiment. Re-introduces the "which provider owns quotes?"
  ambiguity traider was consolidated to avoid.
- Don't retry 429s internally — surface them so the user can back
  off.
- Don't cache responses silently. Earnings dates and consensus
  shift as analysts revise; a stale cache hit would feed wrong
  numbers into a recommendation.
- Don't reshape the Finnhub JSON beyond adding the ``source`` /
  ``fetched_at`` envelope. The model reads raw fields
  (``hour``, ``epsEstimate``, ``surprisePercent``) directly.

### estimates

**What it is.** Read-only bridge to Finnhub's analyst-recommendation
endpoint. One free-tier endpoint:

- ``/stock/recommendation`` — monthly sell-side rating distribution
  per ticker (strong-buy / buy / hold / sell / strong-sell counts).

Finnhub's other estimates endpoints (``/stock/price-target``,
``/stock/upgrade-downgrade``, ``/stock/eps-estimate``,
``/stock/revenue-estimate``) all require a paid plan and return 403
with a free key — they are deliberately not wired. Upgrading the key
later is a one-method extension of ``finnhub_client.py``.

The client is a near-duplicate of ``earnings/finnhub_client.py`` —
same auth header, same error handling, same rate-limit story. They
are intentionally separate so either provider can load without the
other (the hub rule: providers don't import from each other).

**Secrets.** ``FINNHUB_API_KEY``, shared with the ``earnings``
provider. One key, both providers. Do not log it.

**Things that will bite you.**

- **Shared rate-limit budget.** 60 req/min is enforced per key
  across *all* Finnhub endpoints, so enabling both ``earnings`` and
  ``estimates`` means their calls compete for the same budget. A
  fan-out in one will 429 the other. Propagate, don't retry.
- **Free-tier gap is load-bearing.** Users will ask for price
  targets, upgrade/downgrade actions, and consensus EPS. None of
  those are reachable. The tool docstring and the provider README
  call this out — do not paper over by reconstructing from training
  data or another provider. Surface the gap, let the user decide
  whether to pay for the tier.
- **Unknown tickers return ``200 []``, not a 404.** Don't treat an
  empty list as an error — surface it as "no coverage" to the user.
  Conversely, don't retry a ticker that returned ``[]`` in hopes of
  different data; the list is authoritative for the moment.
- **Period is month-start, not month-end.** ``period: "2026-04-01"``
  is the April 2026 snapshot. Sort newest-first by that date; don't
  assume the upstream already sorted.
- **Rating counts aren't EPS revisions.** Month-over-month deltas in
  the five buckets are *rating*-revision breadth. The lay reading
  ("analysts are revising up") conflates this with EPS-estimate
  revisions, which this endpoint does **not** cover. Keep the two
  labels distinct in any analyst output.

**What not to do.**

- Don't add an OAuth flow — Finnhub is a static API key.
- Don't silently fall back to the Yahoo / Schwab analyst fields or
  to training data when the paid endpoints 403. Raise / surface the
  403.
- Don't cache responses silently. Rating trends shift
  month-to-month; a stale cache hit feeds wrong distributions into
  recommendations.
- Don't reshape the Finnhub JSON beyond adding the ``source`` /
  ``fetched_at`` / ``symbol`` / ``trends`` envelope.
- Don't merge this provider into ``earnings``. Separate providers
  keep the load costs and failure modes independent — a 403 on one
  endpoint shouldn't poison the other.

### eia

**What it is.** Read-only bridge to the
[US Energy Information Administration v2 API](https://www.eia.gov/opendata/).
Three curated routes — Weekly Petroleum Status Report ending stocks,
EIA-912 weekly natural-gas storage, monthly Electric Power
Operational Data — plus a generic ``get_eia_series`` escape hatch
for any other route on the EIA Open Data Browser.

**Secrets.** ``EIA_API_KEY`` (register at
<https://www.eia.gov/opendata/register.php>). Sent as the
``api_key`` query param on every request. Do not log it and do not
fall through to a key-less request — EIA returns 403 without it.

**EIA v2 query dialect.** All routes share the same shape:

- ``frequency`` — ``hourly`` / ``daily`` / ``weekly`` / ``monthly`` /
  ``quarterly`` / ``annual``. Each route advertises which it supports
  on its metadata page (URL without ``/data/``).
- ``data[]`` — column projection. Most routes expose ``value`` as the
  numeric series; some use named columns (``generation``,
  ``consumption``).
- ``facets[<name>][]`` — categorical filters. Available facets vary
  per route. The metadata endpoint lists them.
- ``start`` / ``end`` — ISO bounds on ``period``. Granularity must
  match the route's frequency (``YYYY-MM-DD`` for weekly,
  ``YYYY-MM`` for monthly, ``YYYY`` for annual).
- ``sort[0][column]`` / ``sort[0][direction]`` — sort key.
- ``offset`` / ``length`` — paging. Max ``length`` is 5000.

The client serializes facets and sort fields into the bracketed
query-string format EIA expects (``facets[series][]=WCESTUS1``,
``sort[0][column]=period``); httpx handles the list-value expansion
naturally.

**Things that will bite you.**

- **Rate limit is real.** EIA documents 5,000 req/hour per key. A
  fan-out across many series with small ``length`` will trip it
  faster than expected — prefer one larger query over many small ones.
  ``EiaError`` propagates 429s; do not retry in a loop.
- **Series IDs are stable but typo-sensitive.** WPSR series like
  ``WCESTUS1`` (US ex-SPR crude) and storage series like
  ``NW2_EPG0_SWO_R48_BCF`` (Lower 48 working gas) have rigid naming
  conventions. EIA returns 200 with empty ``response.data`` for an
  unknown series, not a 404 — surface "no rows" without retrying.
- **Units vary by route.** Petroleum stocks come back in thousand
  barrels (``MBBL``); gas storage in billion cubic feet (``BCF``);
  electricity generation in thousand megawatt-hours. Always read the
  ``units`` field on each row before quoting a number.
- **`period` granularity matches frequency.** A monthly route with
  ``start=2026-04-15`` is rejected; pass ``start=2026-04`` for
  monthly, ``start=2026`` for annual.
- **`response` envelope is real.** EIA wraps results under
  ``{"response": {"data": [...], "total": "...", ...}, "request":
  {...}, "apiVersion": "..."}``. Don't strip it on the way out —
  ``response.total`` is what callers need for paging decisions.
- **The Open Data Browser is the source of truth for routes and
  facet schemas.** When users ask for something the curated tools
  don't cover (retail gas prices, hourly grid demand, crude
  imports), point ``get_eia_series`` at the right route — but verify
  facet names against
  <https://www.eia.gov/opendata/browser/> first. Guessing facet
  names returns 200 with empty data, not an error.

**What not to do.**

- Don't add an OAuth flow — EIA is a static API key.
- Don't retry 429s or 403s internally. Surface them so the user
  sees the throttle / auth issue.
- Don't cache responses silently. Inventory and storage prints are
  the whole point of the tool; a stale cache hit feeds the wrong
  number into a recommendation.
- Don't reshape EIA's JSON. The model reads ``response.data[]``
  directly; flattening to a "nicer" schema hides the ``total`` /
  ``frequency`` / ``description`` paging metadata.
- Don't expose a ``query`` raw passthrough as an MCP tool. The
  ``get_eia_series`` tool already takes a ``route`` parameter and
  enforces the bracketed-param serialization; a second passthrough
  with no validation just invites invalid requests.

### cftc

**What it is.** Read-only bridge to the
[CFTC Public Reporting portal](https://publicreporting.cftc.gov/),
which exposes the Commitments of Traders (COT) datasets on the
Socrata Open Data platform. Three curated reports plus a generic
SoQL escape hatch:

- **Disaggregated** (commodities) — ``72hh-3qpy`` futures-only,
  ``kh3c-gbw2`` combined.
- **Traders in Financial Futures** — ``gpe5-46if`` futures-only,
  ``yw9f-hn96`` combined.
- **Legacy** (commercial / non-commercial) — ``6dca-aqww``
  futures-only, ``jun7-fc8e`` combined.

**Secrets.** None. The Socrata endpoint is open. An optional
``CFTC_APP_TOKEN`` raises the per-IP rate limit when set; the client
sends it as the ``X-App-Token`` header. Don't log it.

**Release cadence is structural.** COT is published **weekly,
Friday 3:30 PM ET, for positions held the prior Tuesday close** —
a ~3-day reporting lag baked into how the report is collected. On
holiday weeks the release slides to Monday. Surface this lag in any
"current positioning" answer; recommendations that imply real-time
COT data are wrong by construction.

**SoQL query dialect.** Every CFTC dataset on Socrata accepts the
same parameters:

- ``$select`` — comma-separated projection.
- ``$where`` — SoQL filter. Strings single-quoted, internal single
  quotes doubled to escape. ISO dates compared as floating
  timestamps: ``report_date_as_yyyy_mm_dd >= '2025-01-01'``.
  Substring match: ``upper(market_and_exchange_names) like
  upper('%CRUDE%')``.
- ``$order`` — column with optional ``DESC``.
- ``$limit`` — page size, max 50 000.
- ``$offset`` — 0-indexed paging offset.
- ``$q`` — full-text search across all columns.

The curated tools build ``$where`` from a small set of common
filters (market substring, contract market code, commodity
subgroup, date range). For anything outside that set, callers reach
for ``get_cftc_dataset`` with raw SoQL.

**Things that will bite you.**

- **Field-name typos are intentional, preserved verbatim.** CFTC
  has shipped column names with typos that are now load-bearing
  (anyone joining historical data on column position would break).
  Examples: ``swap__positions_spread_all`` (double underscore) in
  the disaggregated report, ``noncomm_postions_spread_all`` (no
  second 'i' in "positions") in the legacy report. The client and
  tools do **not** rename these — pass through verbatim. Quote
  field names from a sample row, don't transcribe from
  documentation.
- **Combined vs futures-only is a real choice.** "COT" in financial
  media usually means combined (futures-and-options), but
  futures-only is sometimes preferred for purity (options can be
  hedges that distort the signal). Keep ``combined=True`` as the
  default but expose the toggle and document the difference.
- **`market_and_exchange_names` is the right needle.** The
  ``contract_market_name`` column omits the exchange suffix
  ("WHEAT-SRW" vs "WHEAT-SRW - CHICAGO BOARD OF TRADE") and is
  ambiguous when the same commodity trades on multiple exchanges
  (e.g. gold on COMEX vs NYMEX mini, wheat on CBOT vs KCBT vs
  MGEX). Always filter on the full market-and-exchange string.
- **Old / Other contract decomposition only on commodities.** The
  disaggregated and legacy reports split positions across
  ``_all`` / ``_old`` / ``_other`` columns to track front-month vs
  back-month exposure. The TFF report does **not** carry that
  split — financial-futures positioning is reported only as ``_all``.
  Don't emit confused "no `_old` column?" errors when working with
  TFF.
- **Bank Participation isn't on this Socrata catalog under a
  Commitments-of-Traders 4-by-4.** It's a separate report category
  on the portal; if the user asks for BPR, look up the current
  dataset ID on https://publicreporting.cftc.gov/ before passing it
  to ``get_cftc_dataset``.
- **Socrata returns ``200 []``, not 404, for empty filters.** A
  ``where`` clause that matches no rows comes back as an empty
  list. Surface "no rows" to the user — don't loop over variations
  of the filter trying to coerce a hit.
- **Rate limits.** Without an app token, Socrata documents the
  endpoint as "throttled" without publishing exact numbers; with a
  token, the documented limit is generous (1000 req/min). 429s
  propagate as ``CftcError``; don't retry.

**What not to do.**

- Don't add an OAuth flow — Socrata uses a static app token, and
  it's optional.
- Don't retry 429s. Surface them so the user can decide whether to
  back off or get an app token.
- Don't cache responses silently. The weekly cadence makes a
  stale-by-a-week cache hit feed obviously wrong positioning into a
  recommendation.
- Don't reshape the Socrata JSON. The model reads the verbatim
  field names directly — including the historic typos.
- Don't try to "merge" combined + futures-only flavors into one
  unified row. They're separate snapshots and the user needs to
  know which they're seeing; the ``flavor`` field on the response
  envelope is load-bearing.

### intent

**What it is.** A local journal of trade *intents* — symbol, side,
quantity, target price, fill price, stop / target levels, the
thesis, catalysts, tags, and a running notes field — keyed by a
generated UUID. The model uses it to capture the *why* behind a
position when the trade is being designed (or just after fill) and
to recall that reasoning in future sessions when looking at the
current book.

**Storage.** SQLite, single table ``trade_intents``, one file at
``~/.traider/intents.db`` by default (override with
``TRAIDER_INTENT_DB``). Lives outside the repo so it can't be
accidentally committed. ``stdlib`` only — no extra dependency
beyond ``sqlite3``.

**Tools.**

- ``record_trade_intent`` — create. Required: ``symbol``, ``side``,
  ``quantity``, ``thesis``. Everything else (levels, catalysts,
  option details, tags, parent_intent_id) is optional but encouraged.
- ``update_trade_intent`` — amend. Used post-fill to record actual
  ``fill_price``, on stop trails / target raises, on status changes,
  and via ``append_note`` to journal updates without overwriting the
  original thesis.
- ``get_trade_intent`` / ``list_trade_intents`` — read. ``list``
  filters on symbol, status, account_id, instrument_type, and
  created_at range; returns newest first.
- ``delete_trade_intent`` — explicit hard delete. Requires
  ``confirm=True`` and is documented as "almost always the wrong
  move; prefer ``status='canceled'``" so the historical thesis
  survives.

**The read-only constraint.** This is the *only* provider in
traider that performs writes, and it's important to be precise
about why that's still consistent with the AGENTS.md rule:

- The rule is **no writes to external systems** — no order entry,
  no alert creation, no broker pushes. ``intent`` writes only to a
  local file the user already owns.
- Tool docstrings are explicit that recording an intent does NOT
  place an order. The model is supposed to reach for this when the
  user is *planning* a trade or *journaling* one they just placed
  themselves; it is not a brokerage write path.
- If you ever extend this provider, that constraint is the line.
  Syncing to a brokerage, posting to a webhook, writing to a shared
  service — none of those are extensions, they're a different
  provider that violates the read-only invariant.

**Schema notes.**

- ``tags`` and ``option_details`` are stored as JSON-encoded TEXT
  columns. Don't promote them to their own tables — the schema
  needs to stay loose so unusual structures (custom multi-leg
  spreads, ratios, calendars) can be captured without a migration
  every time the user trades a new shape.
- ``parent_intent_id`` is a soft self-reference (no FK constraint)
  used to link adds, scales, rolls, and option legs to a parent
  intent. Soft so legacy or partial records don't fail to insert.
- ``account_id`` is whatever string the user wants — typically the
  Schwab ``hashValue``, but it's intentionally untyped so other
  brokerages or paper accounts work too.
- ``status`` is enumerated in ``store.VALID_STATUSES``: ``planned``
  / ``open`` / ``partially_filled`` / ``closed`` / ``canceled``.
  Add to that frozenset and ``update_trade_intent`` validates the
  new value automatically.
- ``notes`` is append-only via ``append_note`` (timestamps each
  line). Direct overwrites are still possible by passing ``notes``
  to ``update``, but the docstring nudges callers to ``append_note``
  so the journal grows over time.

**What not to do.**

- Don't add a ``submit_to_broker`` tool, a webhook, or any sync to
  an external service. The local-only invariant is what keeps this
  provider compatible with the read-only rule.
- Don't pre-compute P&L from intent records — pull live quotes via
  the market-data backend at the point of use. Stale snapshot
  numbers in the journal would invite the same trust-breaking
  citation errors the analyst frame is trying to prevent.
- Don't import from ``schwab`` or any other provider here. Intent
  is provider-agnostic; correlation with positions happens at the
  analyst layer (cross-tool reasoning), not in the schema.
- Don't ship a destructive migration. SQLite ``ALTER TABLE`` is
  fine for adding columns; if a breaking change is ever needed,
  ship a new column and write a migration that reads the old one.

#### Account profile (sub-feature of the intent provider)

**What it is.** A user-authored YAML file capturing framing about
each account they trade out of: age, role in total wealth
(``trading-sleeve`` / ``primary-wealth`` / ``retirement`` / …),
risk capacity, and analyst-facing notes. Surfaced through MCP
tools so the analyst doesn't re-ask the same clarifying
questions every session.

**Storage.** Plain YAML at ``~/.traider/account-profiles.yaml``
(override with ``TRAIDER_ACCOUNT_PROFILES``). Cloners get a
documented template at the repo root
(``account-profiles.example.yaml``); copying it is optional —
missing file is handled gracefully. No DB, no migrations: this
is once-a-year metadata that's friendlier to edit in a text
editor than through a setter tool.

**Tools.**

- ``get_account_profile(account_id=None)`` — returns the empty
  baseline merged with ``defaults:``, then merged with the
  per-account block if ``account_id`` matches a key in
  ``accounts:``. Always returns a dict (never errors); carries
  ``_has_file`` so callers can tell whether anything is
  configured at all.
- ``list_account_profiles`` — full dump for discovery
  (``defaults`` + every configured account).
- ``reload_account_profiles`` — re-read from disk after the user
  edits the file mid-session.

``get_position_context(symbol)`` also includes the profile
bundle: ``defaults`` always present plus a ``by_account`` map
keyed by every unique ``account_id`` that appeared on the open
intents for the symbol, so multi-account users see the right
framing per intent.

**Soft-fail by design.** Missing file, malformed YAML, wrong
top-level shape, or an empty document — all log a warning and
return an empty index. Profile data is *framing*, not data the
analysis depends on for correctness; a broken profile YAML must
not break ``get_position_context``. The model is told via
``AGENTS.md`` to ask the user when fields are blank rather than
guessing.

**Schema is intentionally loose.** Documented fields are
``user_age``, ``total_wealth_context``, ``role``,
``risk_capacity``, ``description``, ``notes_to_analyst``.
Unknown keys pass through verbatim with an info log — users can
add their own structured fields without a code change. Don't
tighten this into a strict schema unless a real downstream
consumer needs the guarantee.

**What not to do.**

- Don't add a setter tool. Static metadata wants a deliberate
  ``vim`` edit; a tool-mediated write invites the model to
  silently mutate framing mid-session and creates a validation
  surface that doesn't earn its complexity.
- Don't move profiles into ``rules/``. Rules describe *what to
  do* per position class; profiles describe *facts about the
  account*. Different cardinality, different lifecycle — keep
  them separate.
- Don't import ``schwab`` here to auto-populate from
  ``hashValue``. Profile keys are user-chosen on purpose so the
  same alias survives a re-auth that rotates the hash.
