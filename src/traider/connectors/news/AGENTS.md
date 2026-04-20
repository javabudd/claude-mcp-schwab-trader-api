# AGENTS.md — news_connector

Guidance for AI coding agents working on the **Massive news** MCP
server inside the [`traider`](../../AGENTS.md) hub. Read the root
AGENTS.md first — it frames how this directory fits into the wider
hub.

## What this is

`news-connector` is a read-only bridge between an AI CLI (via MCP)
and the [Massive](https://massive.com) market-data platform. It
exposes only Massive's news endpoint (`/v2/reference/news`) — the rest
of Massive's surface (quotes, aggregates, trades) is intentionally out
of scope because the hub already has dedicated market-data backends
(`schwab_connector` / `yahoo_connector`).

Each article carries publisher metadata, the tickers it references,
and a per-ticker `insights` array with Massive's sentiment label and
reasoning. That sentiment is **Massive's model output**, not a
primary-source fact — treat it as one signal among many, not ground
truth.

## Not a market-data backend

Unlike `schwab_connector` / `yahoo_connector`, this server is
**additive** — it does not bind port 8765 and does not overlap with
the market-data tool surface.

- Default HTTP port: **8770**.
- Compose service name: `news-connector` (profile: `news`).

## Hard constraints

Inherits every rule in the hub AGENTS.md. Specifically:

- **Read-only.** Massive's news endpoint is query-only. Don't add
  anything that writes (comments, subscriptions, alerts) if they ever
  appear in the API.
- **Surface 429s / 5xx.** The client raises `MassiveError`. Massive's
  free tier is rate-limited; let throttles propagate so the user can
  back off intelligently.
- **No silent fallback to stale data.** If Massive is down or keyed
  out, the tool raises. Don't synthesize headlines from other
  providers and don't pull from training data — that's the single
  worst-case outcome for a catalyst-tracking tool.
- **Sentiment is a model output, not a fact.** When quoting
  `insights[].sentiment` / `sentiment_reasoning`, attribute it to
  Massive and don't present it as an objective reading.

## Secrets

- `MASSIVE_API_KEY` — required. Register at
  [massive.com](https://massive.com). Sent as an `apiKey` query param
  on every request. Do not log it.

## Layout

```
src/news_connector/
  __init__.py         # re-exports MassiveClient / MassiveError
  __main__.py         # entry point — loads .env, dispatches to server.main
  massive_client.py   # httpx wrapper around /v2/reference/news
  server.py           # FastMCP server with one tool (get_news)
pyproject.toml        # deps: mcp, httpx, python-dotenv
```

The client is intentionally thin: one method (`news()`) that mirrors
the endpoint's query-parameter surface 1:1. Translation of
ergonomics (`published_after` → `published_utc.gte`) lives in
`server.py`.

## Tool surface

- `get_news(ticker, published_after, published_before, limit, order,
  sort)` — recent news articles. `ticker` is case-sensitive and
  optional (omit for cross-market feed); `published_after` /
  `published_before` accept ISO date or RFC3339 timestamps. Defaults:
  `limit=10`, `order=desc`, `sort=published_utc`.

The tool returns Massive's response JSON unchanged: `status`,
`count`, `results[]`, `next_url`, `request_id`. Pagination is
exposed via `next_url` — the caller follows it if they want more
than one page.

## Don't start the MCP server yourself

Same rule as every server in the hub. The user runs
`news-connector` in their own terminal (or the Compose service). If a
tool call fails because the server isn't up, tell the user; don't
spawn it.

## Running / developing

```bash
conda activate traider
pip install -e ./mcp_servers/news_connector

news-connector                                           # stdio
news-connector --transport streamable-http --port 8770   # HTTP
```

## Server logs

Rotating file at `logs/server.log` (5 MB × 3). Override with
`--log-file PATH` or `NEWS_CONNECTOR_LOG`. Captured sources:
`news_connector`, `mcp`, `uvicorn`, `httpx`.

## Things that will bite you

- **Ticker is case-sensitive.** Massive documents `ticker` as a
  case-sensitive match. Pass `AAPL`, not `aapl`.
- **`published_utc` is RFC3339.** When filtering a specific window,
  use the full timestamp (e.g. `2026-04-18T13:30:00Z`) for minute-
  level precision. ISO dates also work.
- **Sentiment is Massive's, not yours.** `insights[].sentiment` is
  a model label ("positive" / "negative" / "neutral") with a
  `sentiment_reasoning` blurb. Quote it with attribution; don't
  aggregate or average across articles without flagging that as
  interpretation.
- **Pagination uses `next_url`.** Massive returns a pre-built cursor
  URL in the response — follow it if needed. The tool does not
  aggregate pages for you.
- **Free tier rate limits.** Massive throttles free-tier keys. A
  burst of `get_news` calls will 429; the tool surfaces it instead
  of retrying silently. Back off or batch.

## What not to do

- Don't expand into Massive's other endpoints (aggregates, trades,
  fundamentals) — those overlap with the market-data backends and
  re-introduce the "which server owns quotes?" routing ambiguity the
  hub tries to avoid.
- Don't cache responses silently. News freshness is the whole point
  of this server; a stale cache hit defeats the tool.
- Don't retry 429s internally. Let them propagate, same as every
  other server in the hub.
- Don't blend Massive's feed with another news provider inside a
  single tool call. If a second news source lands, it gets its own
  server (per AGENTS.md "don't blend providers silently").
