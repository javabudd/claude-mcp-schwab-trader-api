# estimates provider

Read-only [Finnhub](https://finnhub.io/docs/api) analyst-recommendation
bridge. One of the provider modules bundled in the unified
[`traider`](../../../../README.md) MCP server. See the root
[AGENTS.md](../../../../AGENTS.md) for hub-wide analyst rules and
[DEVELOPING.md § estimates](../../../../DEVELOPING.md#estimates) for
dev internals.

## Scope

Wraps exactly one Finnhub endpoint, on the free tier:

- `/stock/recommendation` — monthly distribution of sell-side
  analyst ratings (strong-buy / buy / hold / sell / strong-sell) per
  ticker.

The rest of Finnhub's estimates surface — **price targets**,
**upgrade/downgrade actions**, **consensus EPS and revenue
estimates** — is premium-only and returns 403 with the free key.
Those gaps are real and deliberate: the provider ships one honest
endpoint rather than silently pretend to cover more.

Earnings-related data (calendar + historical surprises) lives in the
sibling `earnings` provider; quotes on `schwab` / `yahoo`; filings
on `sec-edgar`; news on `news`.

## Tools

### `get_recommendation_trends(symbol)`

Monthly analyst recommendation distribution for one ticker.

- `symbol` — ticker (e.g. `AAPL`). Required. Pass the canonical
  exchange symbol.

Returns a `source` / `fetched_at` / `symbol` envelope plus a
`trends` list, newest-first. Each entry carries `period`
(month-start `YYYY-MM-DD`), `strongBuy`, `buy`, `hold`, `sell`,
`strongSell`, and an echoed `symbol`.

An unknown or unfollowed ticker returns `trends: []` rather than
raising.

## Setup

`estimates` shares the `FINNHUB_API_KEY` env var with the `earnings`
provider — one key covers both.

1. Register at [finnhub.io](https://finnhub.io) and copy the API key
   (if you haven't already for `earnings`).
2. In `.env`: `FINNHUB_API_KEY=...`
3. Add `estimates` to `TRAIDER_PROVIDERS`.
4. Start the hub as normal — no separate port. Tools are exposed on
   the shared endpoint at `http://localhost:8765/mcp`.

## Coverage and limits

- **Free tier: recommendation trends only.** Price targets,
  upgrade/downgrade actions, and EPS/revenue estimates all require a
  paid Finnhub plan. Surface the gap to the user — do not
  reconstruct any of those from training data.
- **US issuers with sell-side coverage.** Unknown or unfollowed
  tickers return an empty list.
- **Rate limit: 60 requests/minute** (shared across *all* Finnhub
  endpoints — counts toward the `earnings` budget too). 429s
  propagate as `FinnhubError`; no silent retries.
- **Counts are Finnhub's aggregation** of sell-side ratings. Quote
  with attribution — they are not a primary source.
- **Ratings vs. EPS revisions.** Month-over-month movement in the
  bucket counts is *rating*-revision breadth, not *EPS*-revision
  breadth. They usually correlate but don't equate — and this
  provider only covers the former on the free tier.

## Prompts that put this tool to work

- **"Is the Street getting more bullish on NVDA?"** —
  `get_recommendation_trends(symbol="NVDA")` and diff `strongBuy +
  buy` vs. `sell + strongSell` across the returned months.
- **"How does the analyst distribution on AAPL look right now?"** —
  `get_recommendation_trends(symbol="AAPL")` and read the first row
  (current month). Flag when a large `hold` bucket is doing the
  heavy lifting — that's a crowded fence-sitter setup, not a
  consensus buy.
- **"Has coverage on X been falling?"** — sum the five buckets per
  month; a declining total means analysts dropping coverage, which
  is its own signal.
