# treasury-connector

Read-only [Treasury Fiscal Data](https://fiscaldata.treasury.gov)
bridge exposed as an MCP server. One of the MCP servers bundled in
the [`traider`](../../README.md) hub (see the root
[AGENTS.md](../../AGENTS.md) for how the hub is organized). See
[AGENTS.md](AGENTS.md) in this directory for the per-server
constraints and gotchas.

Unlike `schwab_connector` / `yahoo_connector`, this server is
**additive**: it exposes Treasury auction mechanics, the Daily
Treasury Statement, and daily total debt outstanding rather than
equity quotes, and it runs on a different port (8772) so it can sit
alongside whichever market-data backend you picked.

## Scope — and what FRED covers instead

This server is the primary source for the pieces of Treasury data
that FRED does **not** carry:

- Treasury-securities auction results (bid-to-cover, bidder mix,
  stop-out yield).
- Daily Treasury Statement (operating cash balance / TGA, deposits
  and withdrawals, public-debt transactions, etc.).
- Debt to the penny (daily total public debt outstanding).

For **yield-curve queries** — the 2Y, 10Y, 30Y, TIPS real yields,
etc. — use [`fred_connector`](../fred_connector/README.md).
FRED mirrors Treasury's H.15 Daily Treasury Yield Curve in full
(`DGS1MO` … `DGS30`, `DFII*`). Routing those through FRED keeps the
mental model simple: macro time-series via FRED, Treasury-primary-
source data (auctions, DTS, debt) via this server.

## What this MCP server can do

All tools are **read-only**. Every response is Fiscal Data's JSON
essentially unchanged — the model can introspect raw fields rather
than trust a translation layer.

### `get_auction_results(...)`

Treasury-securities auction results. Returns the subset of fields a
trader actually reads when sizing demand for a refunding: bid-to-
cover, stop-out yield/rate, primary-dealer takedown, direct vs
indirect bidder share.

- `security_type` — `Bill`, `Note`, `Bond`, `CMB`, `TIPS`, or `FRN`.
  Omit for all.
- `security_term` — exact match, e.g. `4-Week`, `13-Week`, `2-Year`,
  `10-Year`, `30-Year`.
- `cusip` — exact CUSIP match.
- `start_date` / `end_date` — ISO `YYYY-MM-DD` bounds on
  `auction_date`. Default start is 90 days ago.
- `fields` — column projection. Omit for the curated default; pass
  your own list to pull any of the ~90 auctions_query columns
  (see Fiscal Data's
  [securities auctions dataset](https://fiscaldata.treasury.gov/datasets/securities-auctions-data)
  for the full schema).
- `limit`, `page`, `sort` — standard Fiscal Data paging / ordering.

### `get_daily_treasury_statement(...)`

Daily Treasury Statement. The DTS is broken into eight tables; pick
one via `table`:

| `table`                                             | What it is                                            |
|-----------------------------------------------------|-------------------------------------------------------|
| `operating_cash_balance` *(default)*                | TGA opening / closing balance, running totals         |
| `deposits_withdrawals_operating_cash`               | Line-item daily cash flows in/out of the TGA          |
| `public_debt_transactions`                          | Gross issuance / redemption by security class         |
| `adjustment_public_debt_transactions_cash_basis`    | Cash-basis adjustments to debt transactions           |
| `federal_tax_deposits`                              | Withheld taxes by category                            |
| `short_term_cash_investments`                       | Short-term investments (typically zero)               |
| `income_tax_refunds_issued`                         | Daily refund totals by refund type                    |
| `inter_agency_tax_transfers`                        | Trust-fund tax transfers                              |

Other params:

- `start_date` / `end_date` — ISO bounds on `record_date`. Default
  start is 30 days ago.
- `fields`, `limit`, `page`, `sort` — as above.

The DTS changed format in October 2022. This server only talks to
the new-format JSON tables; queries spanning the old PDF format will
return only new-format rows.

### `get_debt_to_the_penny(...)`

Daily total public debt outstanding.

- `start_date` / `end_date` — ISO bounds on `record_date`. Default
  start is 60 days ago.
- `fields`, `limit`, `page`, `sort` — as above.

Response columns: `debt_held_public_amt` (market-held),
`intragov_hold_amt` (Social Security, Medicare trust funds, etc.),
`tot_pub_debt_out_amt` (headline total).

## Setup

### 1. No credentials required

Fiscal Data is unauthenticated. Nothing to configure.

### 2. Install

```bash
conda activate traider
pip install -e ./mcp_servers/treasury_connector
```

### 3. Run the server

```bash
treasury-connector                                           # stdio
treasury-connector --transport streamable-http --port 8772   # HTTP
```

Or via Docker (together with whichever backend is active):

```bash
cd mcp_servers
docker compose --env-file ../.env --profile treasury up -d
```

Add `treasury` to `COMPOSE_PROFILES` in `.env` to run it as part of
the hub's default `docker compose up -d`.

## Connect your AI CLI

Same recipes as the rest of the hub; the
[hub README](../../README.md#connect-your-ai-cli) has the full
Claude Code / OpenCode / Gemini CLI examples. The HTTP endpoint is
`http://localhost:8772/mcp`.

## Prompts that put these tools to work

- **"How was demand at yesterday's 10-year auction?"** —
  `get_auction_results(security_type="Note", security_term="10-Year",
  limit=1)` — check `bid_to_cover_ratio` and the dealer/indirect
  split.
- **"Is the TGA refilling after debt-ceiling resolution?"** —
  `get_daily_treasury_statement(table="operating_cash_balance",
  start_date="<event-date>")` — look at the balance trajectory.
- **"What's today's total public debt?"** —
  `get_debt_to_the_penny(limit=1)`.
- **"How has bill demand held up over the last quarter?"** —
  `get_auction_results(security_type="Bill",
  start_date="<90d ago>", limit=500)`, then aggregate
  `bid_to_cover_ratio` by `security_term`.
- **"What's the 10-year yield?"** — **not this server.** Use
  `fred_connector.get_series("DGS10")`.

Pair these with `fred_connector` to condition on macro-release
timing, and with `schwab_connector` / `yahoo_connector` when you're
sizing equity or option trades around a Treasury refunding week.

## Things worth knowing

- **Unauthenticated, but still be polite.** Fiscal Data is rate-
  tolerant but not infinite. The tool surface paginates by default
  (limit=100); bump `limit` up to 10 000 if you're pulling a long
  window.
- **Amounts are strings.** Monetary fields come back as strings
  (e.g. `"847182563921.43"`) so precision isn't lost to float. Parse
  on the consumer side if you need to do arithmetic.
- **`auction_date` vs `record_date`.** Auctions are filtered on
  `auction_date` (when the sale happened). DTS and debt-to-the-penny
  are filtered on `record_date` (the date the statement covers).
- **DTS new-format only.** The 2022 format change broke the old
  tables' schema; this server is wired to the new JSON tables. If
  you need pre-2022 DTS data, go to the PDF archive directly.
- **No fallback if upstream is down.** Fiscal Data errors raise
  `TreasuryError`. The server does not silently serve stale data.
