# factor-connector

Read-only
[Ken French Data Library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html)
bridge exposed as an MCP server. One of the MCP servers bundled in the
[`traider`](../../README.md) hub (see the root
[AGENTS.md](../../AGENTS.md) for how the hub is organized). See
[AGENTS.md](AGENTS.md) in this directory for the per-server
constraints and gotchas.

Unlike `schwab_connector` / `yahoo_connector`, this server is
**additive**: it exposes factor-model inputs (Fama-French factors,
momentum, industry portfolios) rather than equity quotes, and it runs
on a different port (8771) so it can sit alongside whichever market-
data backend you picked.

## What this MCP server can do

All tools are **read-only**. Responses include source URL, fetched-at
timestamp, and cache-hit flag so the model can audit freshness.

### `list_datasets()`

Catalog of the curated datasets this server knows about — every
`(model, frequency)` combination for the factor files, and every
`(n_industries, frequency)` combination for the industry portfolios.
Use this to pick the right inputs for `get_factors` /
`get_industry_portfolios`, or to find the filename for `get_dataset`.

### `get_factors(model, frequency, ...)`

Fama-French factor time series.

- `model` — one of:
  - `3factor` — Mkt-RF, SMB, HML, RF (Fama-French 1992).
  - `5factor` — Mkt-RF, SMB, HML, RMW, CMA, RF (Fama-French 2015).
  - `momentum` — Mom (UMD). Pair with 3factor or 5factor for Carhart.
  - `st_reversal` — short-term reversal factor.
  - `lt_reversal` — long-term reversal factor.
- `frequency` — `monthly`, `weekly` (3factor only), or `daily`.
- `start_date` / `end_date` — ISO bounds (`YYYY-MM` for monthly,
  `YYYY-MM-DD` for daily, `YYYY` for annual).
- `annual=True` — return the annual January-December block instead
  of the periodic block.
- `refresh=True` / `ttl_seconds=N` — cache knobs.

Values are **percent returns**. RF is a 1-month T-bill rate.

### `get_industry_portfolios(n_industries, frequency, weighting, ...)`

N-industry portfolio returns under Ken French's classification.

- `n_industries` — 5, 10, 12, 17, 30, 38, 48, or 49. Daily files
  exist only for 5/10/12/17/30/48; 38 and 49 are monthly-only.
- `frequency` — `monthly` or `daily`.
- `weighting` — which block inside the multi-section file:
  - `value` (default) — value-weighted returns.
  - `equal` — equal-weighted returns.
  - `value_annual` / `equal_annual` — annual Jan-Dec returns (monthly
    files only).
  - `num_firms` — firm count per portfolio (monthly only).
  - `avg_firm_size` — mean market cap, millions USD (monthly only).
- `start_date` / `end_date` — ISO bounds, format must match the
  frequency.
- `refresh=True` / `ttl_seconds=N` — cache knobs.

The 12-industry names are: `NoDur, Durbl, Manuf, Enrgy, Chems, BusEq,
Telcm, Utils, Shops, Hlth, Money, Other`. Other N-industry splits use
their own short names; `columns` in the response lists them.

### `get_dataset(dataset_filename, table=None, ...)`

Escape hatch for any Ken French file outside the curated list (sort-
based portfolios, international factors, …).

- `dataset_filename` — filename stem as it appears at
  `https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/`,
  **without** the trailing `_CSV.zip`. Examples:
  - `Portfolios_Formed_on_BE-ME`
  - `25_Portfolios_5x5`
  - `Developed_3_Factors`
  - `Emerging_5_Factors`
- `table` — optional case-insensitive substring match against
  section titles. With `table=None`, the response lists every
  section (title + columns + row count) so you can discover what's
  there; with `table=<string>`, returns the matched section's rows.
- `start_date` / `end_date` — applied only when `table` is set.

## Setup

### 1. No credentials required

The Ken French library is unauthenticated. Nothing to configure.

### 2. Install

```bash
conda activate traider
pip install -e ./mcp_servers/factor_connector
```

### 3. Run the server

```bash
factor-connector                                           # stdio
factor-connector --transport streamable-http --port 8771   # HTTP
```

Or via Docker (together with whichever backend is active):

```bash
cd mcp_servers
docker compose --env-file ../.env --profile factor up -d
```

Add `factor` to `COMPOSE_PROFILES` in `.env` to run it as part of
the hub's default `docker compose up -d`.

## Connect your AI CLI

Same recipes as the rest of the hub; the
[hub README](../../README.md#connect-your-ai-cli) has the full
Claude Code / OpenCode / Gemini CLI examples. The HTTP endpoint is
`http://localhost:8771/mcp`.

## Prompts that put these tools to work

- **"Plot the Fama-French 3 factors for the last 10 years."** —
  `get_factors(model="3factor", frequency="monthly", start_date="<10y ago>")`
- **"Which industry has the best Sharpe ratio since 2020?"** —
  `get_industry_portfolios(n_industries=12, weighting="value", start_date="2020-01")`,
  then compute Sharpe on each column.
- **"Run a Carhart 4-factor regression on my portfolio."** —
  `get_factors("3factor", "monthly")` + `get_factors("momentum", "monthly")`,
  align on date, regress portfolio excess return onto the four
  factors.
- **"How does the 5-factor model explain last year's returns?"** —
  `get_factors(model="5factor", start_date="2024-01", end_date="2024-12")`.
- **"What's the value premium look like across emerging vs developed
  markets?"** — `get_dataset("Developed_5_Factors")` +
  `get_dataset("Emerging_5_Factors")`, compare HML columns.

Pair these with `schwab_connector` / `yahoo_connector` equity tools
when you want to attribute a position's performance to factor
exposures.

## Things worth knowing

- **Returns are in percent.** 2.96 means +2.96%, not +296%. Don't
  multiply by 100.
- **Source updates monthly.** The factor and monthly-portfolio files
  refresh a few days after month-end, once CRSP is in. The daily
  files update less frequently than true daily — the library
  publishes daily series on a batch schedule, not intraday.
- **24-hour cache by default.** Responses show `from_cache`,
  `cache_age_seconds`, and `ttl_seconds`. Pass `refresh=True` on any
  tool to force a re-fetch. Cache bytes live at
  `~/.cache/traider-factor-connector/` (override with
  `FACTOR_CACHE_DIR`).
- **Missing values are `None`.** The raw sentinels (`-99.99`, `-999`)
  get converted at parse time — don't filter them yourself.
- **No fallback if upstream is down.** An expired cache + failed
  fetch raises `FrenchFetchError`. The server does not silently
  serve stale data.
