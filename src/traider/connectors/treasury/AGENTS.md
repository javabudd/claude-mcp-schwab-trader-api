# AGENTS.md — treasury_connector

Guidance for AI coding agents working on the **Treasury Fiscal Data**
MCP server inside the [`traider`](../../AGENTS.md) hub. Read the root
AGENTS.md first — it frames how this directory fits into the wider
hub.

## What this is

`treasury-connector` is a read-only bridge between an AI CLI (via
MCP) and the [Treasury Fiscal Data API](https://fiscaldata.treasury.gov)
published by the US Department of the Treasury's Bureau of the Fiscal
Service. It exposes three primary-source datasets:

- **Securities auction results** — bid-to-cover, stop-out yield/rate,
  primary-dealer takedown, direct/indirect bidder share.
- **Daily Treasury Statement (DTS)** — operating cash balance (TGA),
  deposits/withdrawals, public-debt transactions, and the other DTS
  tables.
- **Debt to the Penny** — daily total public debt outstanding
  (public-held + intragov).

## What this server does NOT cover

**Yield curve queries go to `fred_connector`.** FRED mirrors the
Treasury H.15 Daily Treasury Yield Curve in full (`DGS1MO`, `DGS3MO`,
`DGS6MO`, `DGS1`, `DGS2`, `DGS3`, `DGS5`, `DGS7`, `DGS10`, `DGS20`,
`DGS30`, plus `DFII*` for TIPS real yields). Duplicating that surface
here would force the model to guess which connector owns rates. If a
user asks for "the 10-year," route to `fred_connector.get_series`.

This server is specifically for the Treasury datasets FRED does not
carry at useful granularity: auction mechanics, the full DTS, and
daily total debt.

## Not a market-data backend

Unlike `schwab_connector` / `yahoo_connector`, this server is
**additive** — it does not bind port 8765 and does not overlap with
the market-data tool surface.

- Default HTTP port: **8772**.
- Compose service name: `treasury-connector` (profile: `treasury`).

## Hard constraints

Inherits every rule in the hub AGENTS.md. Specifically:

- **Read-only.** Fiscal Data is a query-only API — there's nothing to
  write. Don't add submission-like endpoints if they ever appear.
- **Surface 429s / 5xx.** The client raises `TreasuryError`. Fiscal
  Data is generally generous but can throttle; let errors propagate
  so the user can back off intelligently.
- **No silent fallback to stale data.** If Fiscal Data is down, the
  tool raises. The downstream analysis (e.g. "is the TGA refilling?")
  depends on the latest record_date being current — do not serve an
  expired snapshot and pretend it's live.

## Secrets

**None.** Fiscal Data is unauthenticated. No API key, no OAuth, no
User-Agent registration requirement (unlike SEC EDGAR). We still send
a descriptive UA so Treasury's logs can identify the traffic.

## Layout

```
src/treasury_connector/
  __init__.py       # re-exports TreasuryClient / TreasuryError
  __main__.py       # entry point — loads .env, dispatches to server.main
  treasury_client.py  # httpx wrapper around Fiscal Data
  server.py         # FastMCP server with 3 tools
pyproject.toml      # deps: mcp, httpx, python-dotenv
```

The client is intentionally thin: each method maps to one Fiscal Data
dataset and returns the JSON essentially unchanged. Translation
(e.g. "bill vs. note vs. bond", DTS table names) lives in
`server.py`, not in the client.

## Tool surface

- `get_auction_results(security_type, security_term, cusip, start_date,
  end_date, fields, limit, page, sort)` — Treasury-securities auction
  results. Default projection covers bid-to-cover, dealer takedown,
  stop-out yield; pass `fields=[...]` to pull any column from the
  auctions_query dataset.
- `get_daily_treasury_statement(table, start_date, end_date, fields,
  limit, page, sort)` — DTS. `table` picks which of the eight DTS
  tables to read (default `operating_cash_balance` for TGA).
- `get_debt_to_the_penny(start_date, end_date, fields, limit, page,
  sort)` — daily total public debt outstanding.

All date filters operate on `auction_date` (for auctions) or
`record_date` (for DTS and debt-to-the-penny). Fiscal Data's filter
dialect is `field:op:value` joined with commas — the client
assembles it internally.

### Fiscal Data query dialect

All three tools funnel through one `TreasuryClient.query` method that
understands Fiscal Data's common params:

- `filter` — `field:op:value,field:op:value,...` where op is `eq`,
  `gte`, `gt`, `lte`, `lt`, `in`.
- `fields` — comma-separated projection.
- `sort` — field name, `-` prefix for desc.
- `page[size]` / `page[number]` — page size max 10 000, 1-indexed.

If a user asks for something outside the curated tool surface (a
different dataset on the Fiscal Data catalog), the simplest path is
to expose it as a new tool — don't try to repurpose `query` as a raw
passthrough. The value is in the projection and the defaults.

## Don't start the MCP server yourself

Same rule as every server in the hub. The user runs
`treasury-connector` in their own terminal (or the Compose service).
If a tool call fails because the server isn't up, tell the user;
don't spawn it.

## Running / developing

```bash
conda activate traider
pip install -e ./mcp_servers/treasury_connector

treasury-connector                                           # stdio
treasury-connector --transport streamable-http --port 8772   # HTTP
```

## Server logs

Rotating file at `logs/server.log` (5 MB × 3). Override with
`--log-file PATH` or `TREASURY_CONNECTOR_LOG`. Captured sources:
`treasury_connector`, `mcp`, `uvicorn`, `httpx`.

## Things that will bite you

- **The DTS changed format in 2022.** The legacy PDF format and the
  new JSON-native tables are different shapes. This server only
  talks to the new Fiscal Data tables (`/v1/accounting/dts/...`). If
  someone asks for data before Oct 2022, the DTS endpoints return
  the new-format columns only — they will not reconstruct the old
  table structure.
- **Amounts are strings.** Fiscal Data returns monetary fields as
  strings (e.g. `"847182563921.43"`) to preserve precision. Don't
  assume numeric JSON; the client returns JSON verbatim.
- **`record_date` vs. reporting window.** DTS `record_date` is the
  date the statement covers (usually T-1 settlement). Auction
  `record_date` is the date the record was published; use
  `auction_date` for filtering when you care about auction timing.
- **Paging matters for long windows.** Default page size is 100. A
  full year of daily TGA balances is ~260 rows — still under one
  page — but a full year of auctions can exceed 1 000 rows. Bump
  `limit` or iterate `page`.
- **Yield curve is elsewhere.** If a user asks for the 2Y / 10Y /
  30Y yield as a time series, the answer is
  `fred_connector.get_series("DGS10")` — not `get_auction_results`.
  Auction high yields are stop-out yields for a specific sale, not
  a secondary-market curve.

## What not to do

- Don't add a yield-curve tool. Route to FRED.
- Don't add a "secondary-market rate" tool — that's also FRED (H.15
  again).
- Don't reshape Fiscal Data's column names. Treasury's naming is
  canonical in the primary-dealer / fixed-income literature;
  translating them makes cross-referencing harder.
- Don't cache responses silently. If caching becomes necessary
  (Fiscal Data is generous but not free of latency), expose the TTL
  and signal stale-hit in the response — don't hide it.
- Don't introduce a retry loop around 429s. Let them propagate, same
  as every other server in the hub.
