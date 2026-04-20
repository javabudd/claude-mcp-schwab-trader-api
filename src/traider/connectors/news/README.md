# news-connector

Read-only [Massive](https://massive.com) news bridge exposed as an
MCP server. One of the MCP servers bundled in the
[`traider`](../../README.md) hub (see the root
[AGENTS.md](../../AGENTS.md) for how the hub is organized). See
[AGENTS.md](AGENTS.md) in this directory for the per-server
constraints and gotchas.

Unlike `schwab_connector` / `yahoo_connector`, this server is
**additive**: it exposes ticker-scoped news headlines with publisher
metadata and per-article sentiment, rather than quotes or history,
and it runs on a different port (8770) so it can sit alongside
whichever market-data backend you picked.

## Scope

Wraps exactly one Massive endpoint — `/v2/reference/news`. The rest
of Massive's surface (quotes, aggregates, trades, fundamentals) is
intentionally out of scope because the hub already has dedicated
market-data backends. Pull quotes and history from
`schwab_connector` / `yahoo_connector`; use this server for the
catalyst / narrative layer.

## What this MCP server can do

All tools are **read-only**. The response is Massive's JSON
essentially unchanged — the model can introspect raw fields
(`insights[].sentiment`, `publisher.name`, `tickers`,
`published_utc`) rather than trust a translation layer.

### `get_news(...)`

Recent news articles.

- `ticker` — case-sensitive ticker filter (e.g. `AAPL`). Omit for the
  cross-market feed.
- `published_after` / `published_before` — ISO date or RFC3339
  timestamp window. Maps to Massive's `published_utc.gte` /
  `published_utc.lte`.
- `limit` — 1–1000, default 10.
- `order` — `asc` or `desc`, default `desc` (newest first).
- `sort` — field to sort by, default `published_utc`.

Returns Massive's response JSON: `status`, `count`, `results[]`,
`next_url` (pagination cursor when more rows exist), `request_id`.
Each result carries `title`, `description`, `author`, `publisher`
(`name`, `logo_url`, `homepage_url`, `favicon_url`), `article_url`,
`image_url`, `amp_url`, `tickers`, `keywords`, `published_utc`, and
an `insights` array with per-ticker `sentiment` +
`sentiment_reasoning`.

**Sentiment is Massive's model output**, not a primary-source fact —
quote it with attribution, don't present it as objective.

## Setup

### 1. Get a Massive API key

Free tier available — register at [massive.com](https://massive.com).
Rate limits apply; the server surfaces 429s rather than retrying.

### 2. Put it in `.env`

```
MASSIVE_API_KEY=...
```

### 3. Install

```bash
conda activate traider
pip install -e ./mcp_servers/news_connector
```

### 4. Run the server

```bash
news-connector                                           # stdio
news-connector --transport streamable-http --port 8770   # HTTP
```

Or via Docker (together with whichever backend is active), from the
repo root:

```bash
docker compose --profile news up -d
```

Add `news` to `COMPOSE_PROFILES` in `.env` to run it as part of the
hub's default `docker compose up -d`.

## Connect your AI CLI

Same recipes as the rest of the hub; the
[hub README](../../README.md#connect-your-ai-cli) has the full
Claude Code / OpenCode / Gemini CLI examples. The HTTP endpoint is
`http://localhost:8770/mcp`.

## Prompts that put this tool to work

- **"Why did NVDA move after the open today?"** —
  `get_news(ticker="NVDA", published_after="<today 13:30Z>")`,
  then cross-check the headline timestamps against the price action
  (`get_price_history` on the market-data backend).
- **"What's the latest on TSLA?"** —
  `get_news(ticker="TSLA", limit=20)`.
- **"Anything notable in the cross-market tape this morning?"** —
  `get_news(published_after="<today 13:30Z>", limit=50)`.
- **"Walk me through the last week of news for AAPL."** —
  `get_news(ticker="AAPL", published_after="<7d ago>", limit=100)`.

## Things worth knowing

- **Ticker is case-sensitive.** Massive matches `ticker` exactly —
  use `AAPL`, not `aapl`.
- **Timestamps are RFC3339.** `published_utc` fields and filter
  inputs use full timestamps with timezone (e.g.
  `2026-04-18T13:30:00Z`). ISO dates also work for bounds.
- **Sentiment is a model output.** `insights[].sentiment` and
  `sentiment_reasoning` are Massive's labels, not ground truth.
  Attribute them and weigh them against primary-source reading of
  the article.
- **Pagination via `next_url`.** The response carries a pre-built
  cursor URL when more rows are available. The tool does not
  aggregate pages — follow the cursor in a second call if needed.
- **No fallback if upstream is down.** Massive errors raise
  `MassiveError`. The server does not silently serve stale news or
  fabricate headlines.
