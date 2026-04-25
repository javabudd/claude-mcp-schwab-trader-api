# cftc provider

Read-only bridge to the
[CFTC Public Reporting portal](https://publicreporting.cftc.gov/),
which exposes Commitments of Traders (COT) data on Socrata. One of
the provider modules bundled in the unified
[`traider`](../../../../README.md) MCP server. See the root
[AGENTS.md](../../../../AGENTS.md) for hub-wide analyst rules and
[DEVELOPING.md § cftc](../../../../DEVELOPING.md#cftc) for dev
internals.

## Scope

Three curated reports plus a generic SoQL escape hatch.

- **Disaggregated** (commodities) — Producer/Merchant/Processor/User
  vs. Swap Dealers vs. Managed Money vs. Other Reportables vs.
  Non-Reportables. The most-cited COT framing for crude, natural
  gas, gold, silver, copper, ag products. Started 2009.
- **Traders in Financial Futures (TFF)** — Dealer/Intermediary vs.
  Asset Manager vs. Leveraged Funds vs. Other Reportables vs.
  Non-Reportables. The financial-markets analogue: S&P 500, Nasdaq,
  Treasuries, USD, currencies, VIX.
- **Legacy** — Commercial (hedger) vs. Non-Commercial (speculator)
  vs. Non-Reportable. The simplest framing; what financial media
  usually means by "the latest COT report." History back to the
  early 1980s.
- **Generic CFTC dataset** — `get_cftc_dataset` against any other
  CFTC Socrata 4-by-4 (Bank Participation, Supplemental Commodity
  Index Traders, Cotton On-Call, …).

Each curated report is published in two flavors: **futures-only**
and **combined** (futures plus the delta-equivalent of options).
``combined=True`` is the default; financial media typically cite
combined.

## Release cadence

COT is published **weekly, Friday 3:30 PM ET, for positions held
the prior Tuesday close** — a ~3-day reporting lag baked into how
the report is collected. On holiday weeks the release slides to
Monday. Any "current positioning" answer that uses COT is one
Tuesday old by construction.

## Tools

All tools return the Socrata JSON essentially unchanged inside a
`source` / `dataset_id` / `flavor` / `fetched_at` / `row_count` /
`data` envelope.

### `get_cot_disaggregated(...)`

Disaggregated commodities report.

- `market_contains` — case-insensitive substring match on
  `market_and_exchange_names` (e.g. `"CRUDE OIL"`, `"GOLD"`,
  `"NATURAL GAS"`).
- `contract_market_code` — exact `cftc_contract_market_code` when
  you know it.
- `commodity_subgroup` — `"GRAINS"`, `"PETROLEUM AND PRODUCTS"`,
  `"NATURAL GAS AND PRODUCTS"`, `"PRECIOUS METALS"`,
  `"BASE METALS"`, `"LIVESTOCK"`, `"SOFTS"`.
- `combined` — `True` (default) for futures-and-options combined;
  `False` for futures-only.
- `start_date` / `end_date` — ISO `YYYY-MM-DD` bounds on
  `report_date_as_yyyy_mm_dd`.
- `select` — SoQL projection (rows have ~150 columns; narrow for
  long windows).
- `limit`, `offset` — paging (max 50 000).

Field naming convention: trader class prefix (`prod_merc_`,
`swap_`, `m_money_`, `other_rept_`, `nonrept_`) +
`positions_long_all` / `positions_short_all`; `_old` / `_other`
columns split front/back contract months. `change_in_*`,
`pct_of_oi_*`, `traders_*` mirror the same shape for deltas, OI
share, and trader counts.

### `get_cot_financial_futures(...)`

Traders in Financial Futures.

- `market_contains` — e.g. `"E-MINI S&P 500"`, `"10-YEAR U.S.
  TREASURY NOTES"`, `"VIX FUTURES"`, `"EURO FX"`.
- `contract_market_code` — exact code when you know it.
- `combined`, `start_date`, `end_date`, `select`, `limit`, `offset`
  — as above.

Field naming convention: trader class prefix (`dealer_`,
`asset_mgr_`, `lev_money_`, `other_rept_`, `nonrept_`) +
`positions_long` / `positions_short` / `positions_spread`. TFF is
flatter than the commodities disaggregated — no `_old` / `_other`
splits.

### `get_cot_legacy(...)`

Legacy COT — Commercial / Non-Commercial / Non-Reportable.

Same parameter set as the others. Field naming: `comm_`,
`noncomm_`, `nonrept_` prefixes + `positions_long_all` /
`positions_short_all` / (non-commercial only)
`positions_spread_all`.

### `get_cftc_dataset(...)`

Generic SoQL escape hatch.

- `dataset_id` — Socrata 4-by-4 (e.g. `"4zgm-a668"` for
  Supplemental CIT).
- `select` — SoQL `$select` projection.
- `where` — SoQL `$where` filter. Strings single-quoted; double
  internal single quotes per SoQL escaping. Date compare:
  `report_date_as_yyyy_mm_dd >= '2025-01-01'`. Substring:
  `upper(field) like upper('%PATTERN%')`.
- `order` — SoQL `$order` (default
  `report_date_as_yyyy_mm_dd DESC`; pass explicitly for datasets
  without that column).
- `q` — Socrata `$q` full-text search.
- `limit`, `offset` — paging.

Browse the dataset catalog at
<https://publicreporting.cftc.gov/browse> for IDs.

## Setup

1. Add `cftc` to `TRAIDER_PROVIDERS`.
2. *(Optional)* Register at <https://evergreen.data.socrata.com/signup>
   for an app token if you hit per-IP rate limits, and set
   `CFTC_APP_TOKEN=...` in `.env`.
3. Start the hub as normal — no separate port. Tools are exposed on
   the shared endpoint at `http://localhost:8765/mcp`.

## Coverage and limits

- **Rate limit.** Without an app token, Socrata throttles
  per-IP. With a token, the documented limit is 1000 req/min.
  429s propagate as `CftcError`; no silent retries.
- **Page size cap is 50 000.** Large windows need pagination via
  `offset`.
- **Field-name typos are preserved verbatim.** CFTC has historic
  column-name typos like `swap__positions_spread_all` (double
  underscore, disaggregated) and `noncomm_postions_spread_all` (no
  second 'i' in "positions", legacy). The client does **not**
  rename these — quote names from a sample row, don't transcribe
  from documentation.
- **Combined vs futures-only.** Combined includes the
  delta-equivalent of options exposure and is the more common
  citation. Futures-only is "purer" but ignores any options
  hedging.
- **`market_and_exchange_names` is the right needle.**
  `contract_market_name` omits the exchange and is ambiguous when
  the same commodity trades on multiple venues. Filter on the full
  string.
- **No fallback if upstream is down.** Errors raise `CftcError`.
  This provider does not silently serve stale data or substitute a
  different source.

## Prompts that put these tools to work

- **"Where are managed-money speculators positioned in WTI right
  now?"** — `get_cot_disaggregated(market_contains="CRUDE OIL,
  LIGHT SWEET", limit=10)` and read
  `m_money_positions_long_all` / `m_money_positions_short_all` and
  the week-over-week deltas. Compare net (long − short) to history
  for the bull/bear extremity read.
- **"How are leveraged funds positioned in S&P 500 futures vs.
  three months ago?"** —
  `get_cot_financial_futures(market_contains="E-MINI S&P 500",
  start_date="<3mo ago>", limit=20)` and look at the
  `lev_money_positions_long` / `_short` series.
- **"What's the commercial-vs-speculator split in gold?"** —
  `get_cot_legacy(market_contains="GOLD")` for the simple framing,
  or pair with `get_cot_disaggregated(market_contains="GOLD")` for
  the swap-dealer vs PMPU breakdown.
- **"Pull the Treasury futures positioning across the curve."** —
  `get_cot_financial_futures(market_contains="U.S. TREASURY",
  limit=200)` returns 2Y / 5Y / 10Y / Ultra 10Y / 30Y / Ultra T-Bond
  in one shot; group by `contract_market_name`.

Pair these with `schwab` / `yahoo` for the underlying price action
the positioning is supposed to anchor, with `treasury` for auction
results around the same windows, and with `news` to anchor a
positioning shift to a specific catalyst.
