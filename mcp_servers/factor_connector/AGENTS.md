# AGENTS.md — factor_connector

Guidance for AI coding agents working on the **Ken French Data
Library** MCP server inside the [`traider`](../../AGENTS.md) hub.
Read the root AGENTS.md first — it frames how this directory fits
into the wider hub.

## What this is

`factor-connector` is a read-only bridge between an AI CLI (via MCP)
and the
[Ken French Data Library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html)
hosted by Dartmouth Tuck. It exposes:

- **Fama-French factor series** — 3-factor (Mkt-RF, SMB, HML, RF),
  5-factor (adds RMW, CMA), momentum, and short/long-term reversal,
  in monthly / weekly / daily frequencies as the library offers them.
- **Industry portfolios** — the 5/10/12/17/30/38/48/49-industry Ken
  French classifications, monthly and daily.
- A generic **escape hatch** (`get_dataset`) for any other dataset on
  the library (sort-based portfolios, international regional factors,
  etc.) by filename.

This is the canonical source for factor-model inputs: Fama / French
publish directly here, there's no intermediary, and the series run
from 1926 to the prior month-end.

## Not a market-data backend

Unlike `schwab_connector` / `yahoo_connector`, this server is
**additive** — it doesn't bind port 8765 and doesn't overlap with the
market-data tool surface.

- Default HTTP port: **8771**.
- Compose service name: `factor-connector` (profile: `factor`).

## Hard constraints

Inherits every rule in the hub AGENTS.md. Specifically:

- **Read-only.** The library is a static HTTP file server; nothing to
  write.
- **Primary source only.** URLs are all under
  `mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/`. Don't
  substitute a mirror, a pandas-datareader shim, or a cached third-
  party copy — the point of using the library is that the numbers
  come from Fama / French themselves.
- **No silent fallback to stale data.** Responses are disk-cached
  (24h TTL by default) because the source updates monthly at most
  and polite use means not re-fetching on every tool call. But when
  the cache is expired and the fetch fails, we **raise** — we never
  serve an expired snapshot and pretend it's live. Every response
  includes `from_cache`, `cache_age_seconds`, and `ttl_seconds` so
  the model can audit freshness.

## Secrets

None. The library is unauthenticated. No API key, no User-Agent
registration requirement (unlike SEC EDGAR). We still send a
descriptive UA so Dartmouth's logs can identify the traffic.

## Caching

ZIPs are cached on disk under
`$FACTOR_CACHE_DIR` (default `~/.cache/traider-factor-connector/`).
The cache key is the dataset filename; TTL is per-call (default 24 h,
override with `ttl_seconds=…` on any tool). `refresh=True` on any
tool bypasses the cache for one call without invalidating it for
other callers.

The Docker image mounts `~/.cache/traider-factor-connector` from the
host into `/cache` inside the container, so the cache persists across
container restarts. If you drop the volume, every restart refetches
the full set — the library won't mind (its files are small) but it's
wasteful.

## Layout

```
src/factor_connector/
  __init__.py       # re-exports FrenchClient + error types
  __main__.py       # entry point — loads .env, dispatches to server.main
  french_client.py  # fetch + disk cache + CSV parser
  server.py         # FastMCP server with 4 tools
pyproject.toml      # deps: mcp, httpx, python-dotenv
```

`french_client.py` is the parser that matters. Ken French CSVs embed
multiple tables in one file, separated by blank lines, with optional
multi-line titles and a `,Col1,Col2,...` header row. The parser is
block-based: split on blank lines, identify the data-header row
inside each block, treat prose lines above it as the section title,
rows below as data.

## Tool surface

- `list_datasets()` — catalog of curated datasets this server knows
  about (all factor files + all industry portfolios). Does not
  enumerate the ~300 other Ken French datasets; those are reachable
  via `get_dataset(filename)`.
- `get_factors(model, frequency, …)` — Fama-French factor time
  series. `model ∈ {3factor, 5factor, momentum, st_reversal,
  lt_reversal}`; `frequency ∈ {monthly, weekly, daily}` (with
  `5factor` + `*_reversal` not offered weekly, and `momentum` not
  offered weekly — `list_datasets` shows the valid pairs).
- `get_industry_portfolios(n_industries, frequency, weighting, …)` —
  N-industry returns. `n_industries ∈ {5, 10, 12, 17, 30, 38, 48,
  49}` (38 / 49 are monthly-only). `weighting` picks which block:
  `value` / `equal` / `value_annual` / `equal_annual` / `num_firms`
  / `avg_firm_size` (daily files only have `value` / `equal`).
- `get_dataset(dataset_filename, table=None, …)` — escape hatch.
  Pass the filename stem as it appears on the library FTP page
  (without `_CSV.zip`). With `table=None`, returns a section-index
  (titles + column lists + row counts); with `table=<substring>`,
  returns that section's rows.

All tools accept `start_date` / `end_date` (ISO) to server-side trim
the return. Format must match the file: `YYYY-MM-DD` for daily,
`YYYY-MM` for monthly/weekly, `YYYY` for annual.

## Units and sentinels

- **Returns are in percent**, not decimals. A value of `2.96` means
  +2.96%, not +296%.
- **Missing values** in the source file are `-99.99` or `-999`. The
  parser converts them to `None` so downstream math doesn't
  accidentally treat them as -100% returns.
- **RF** in the factor files is the 1-month T-bill rate for the
  corresponding period (also in percent).

## Don't start the MCP server yourself

Same rule as every server in the hub. The user runs
`factor-connector` in their own terminal (or the Compose service).
If a tool call fails because the server isn't up, tell the user;
don't spawn it.

## Running / developing

```bash
conda activate traider
pip install -e ./mcp_servers/factor_connector

factor-connector                                           # stdio
factor-connector --transport streamable-http --port 8771   # HTTP
```

## Server logs

Rotating file at `logs/server.log` (5 MB × 3). Override with
`--log-file PATH` or `FACTOR_CONNECTOR_LOG`. Captured sources:
`factor_connector`, `mcp`, `uvicorn`, `httpx`.

## Things that will bite you

- **Units trap.** The values are percents — don't multiply by 100
  "to convert" them; they already are. Mixing factor returns (pct)
  with raw equity returns (decimal) will give nonsense regressions.
- **Annual blocks lexicographically sort fine vs. monthly** but
  `"2024" > "2023-12"` under string comparison, which is what
  `filter_rows_by_date` uses. Pass bounds that match the frequency
  of the section you're reading (`YYYY` for annual, `YYYY-MM` for
  monthly, `YYYY-MM-DD` for daily). Mixing them will quietly filter
  out rows you meant to keep.
- **Industry ticker-like column names are Ken French's, not yours.**
  `Durbl` isn't a ticker, it's the "Consumer Durables" industry
  bucket. The README has the full key per industry-count.
- **Daily files are large.** The 48-industry daily file is ~9 MB
  unzipped and has ~26k rows. Filter server-side by date rather than
  pulling the whole thing if you're after a specific window.
- **Library filenames aren't always intuitive.** `F-F_Research_Data_5_Factors_2x3`
  is the 5-factor file; the `2x3` refers to the sort methodology,
  not the number of columns. `list_datasets` maps the short-form
  `model` parameter to the right filename.

## What not to do

- Don't add a pandas-datareader code path as a fallback. The URL
  pattern is stable; if it breaks, raise — that's the signal.
- Don't silently widen the cache TTL to mask a fetch failure. The
  TTL is per-call and explicit; if a caller wants fresher data, they
  pass `refresh=True`, not a longer TTL to avoid re-fetching.
- Don't reshape the column names ("Mkt-RF" → "market_minus_rf"). The
  Ken French names are canonical in the literature; changing them
  makes cross-referencing papers harder.
- Don't add a "compute alpha / factor exposure" tool here. This
  server fetches and parses. Any regression of a portfolio onto
  these factors should be a separate tool (or a client-side
  computation on top of this server plus the market-data backend).
