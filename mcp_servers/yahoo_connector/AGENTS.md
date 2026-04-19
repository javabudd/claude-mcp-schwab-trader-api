# AGENTS.md — yahoo_connector

Guidance for AI coding agents working on the **Yahoo Finance** MCP
server inside the [`traider`](../../AGENTS.md) hub. If you landed here
without reading the root `AGENTS.md`, read that first — it frames how
this directory fits into the wider collection of MCP servers.

## What this is

`yahoo-connector` is a read-only bridge between an AI CLI (via MCP)
and **Yahoo Finance**, built on the
[`yfinance`](https://pypi.org/project/yfinance/) library. It exists so
users who don't have a Schwab developer account (or haven't waited out
Schwab's app-approval cycle) can still get quotes, OHLCV history,
TA-Lib indicators, and the full set of `analyze_*` analytics.

The tool surface **intentionally matches** `schwab-connector`'s so
prompts, examples, and analytics code are portable between the two.
When Yahoo's data model can't cover a capability, the tool raises
`YahooCapabilityError` rather than inventing a response — see
"Capability gaps" below.

## How backend selection works

The hub ships two market-data backends but only one runs at a time
(both default to port 8765 and register under the same tool names).
The selection mechanism is `TRAIDER_BACKEND` in the root `.env`:

```
TRAIDER_BACKEND=yahoo    # or schwab
```

That value is mapped into `COMPOSE_PROFILES` by
`mcp_servers/docker-compose.yml`, so `docker compose up` only starts
the service whose profile matches. On the host (no Docker), just run
`yahoo-connector` *or* `schwab-connector`; don't start both. See the
root README's "Choosing a market-data backend" section.

## Hard constraints

- **Read-only scope.** No writes, no orders, no alerts — same as every
  server in this hub.
- **No silent fallbacks.** If Yahoo can't serve the request (brokerage
  data, authoritative market hours, 10-minute bars), raise. Do not
  synthesize a plausible answer from partial data.
- **Surface rate limits.** Yahoo enforces unpublished per-IP quotas
  via Cloudflare; when `yfinance` raises, let it propagate. Don't
  retry-loop.
- **Unofficial endpoint.** Yahoo has no public API. `yfinance` scrapes
  the same endpoints the web UI uses. Expect occasional breakage when
  Yahoo changes schemas; don't pin the fix with monkey-patches —
  upgrade the `yfinance` pin in `pyproject.toml`.

## Capability gaps vs. Schwab

These tools exist in the surface for parity but raise
`YahooCapabilityError` at call time:

- **`get_accounts`** — Yahoo is a data source, not a brokerage. There
  are no positions, cost basis, or account numbers. Portfolio-aware
  prompts need the Schwab backend (or a future broker connector).
- **`get_market_hours`** — Yahoo publishes no authoritative
  session-hours endpoint. Rather than hand-rolling a schedule that
  could disagree with a real exchange holiday, we raise.

These tools work but behave differently than Schwab:

- **`get_movers`** — Yahoo's screeners are US-market-wide rather than
  per-index. `sort` selects the screener; `index` is only used when
  it matches a raw Yahoo screener key. `frequency` is ignored.
- **`search_instruments`** — Yahoo's symbol search is fuzzy; all
  `symbol-*` / `desc-*` / `search` projections return the same list.
  `fundamental` pulls the `Ticker.info` block.
- **Intraday history depth** — Yahoo caps 1-minute history at ~7 days
  and all sub-hourly bars at ~60 days. Schwab goes further back.
  `get_price_history` won't warn about the cap; an empty `candles`
  list is the signal.
- **Options symbology** — Yahoo uses its own option-symbol format
  (e.g. `SPY250321C00500000`, no padding), not Schwab's 21-char OSI
  with spaces. The client does not translate between them; pass the
  Yahoo form when you're on this backend.
- **10-minute bars** — Schwab supports `frequency=10`; Yahoo does not.
  The client raises `ValueError` rather than substituting 15m.

## Layout

All paths below are relative to this directory
(`mcp_servers/yahoo_connector/`).

```
src/yahoo_connector/
  __init__.py       # re-exports YahooClient / YahooCapabilityError
  __main__.py       # entry point (no auth subcommand — yfinance needs none)
  yahoo_client.py   # yfinance wrapper that emits Schwab-shaped payloads
  ta.py             # TA-Lib indicator runner over candle lists
  analytics.py      # pure-numpy return/risk/correlation analytics
  server.py         # FastMCP server: same tool names as schwab-connector
pyproject.toml      # deps: mcp, yfinance, numpy, TA-Lib, python-dotenv
```

`ta.py` and `analytics.py` are intentional duplicates of the Schwab
versions — the hub's pattern is "each server independently installable
with its own deps," and deduplicating into a shared library would
couple the installs. If you change analytics behavior, change it in
both places (or accept the divergence explicitly in commit/PR text).

## Don't start the MCP server yourself

The user runs `yahoo-connector` in a separate terminal. You do **not**
need to spawn the server, background it, or restart it — assume it is
already running (or that the user will start it). If a tool call fails
because the server isn't up, tell the user; don't try to launch it.

## Running / developing

All Python commands for this server run inside the `traider` conda
environment, same as every other server in the hub:

```bash
conda activate traider

# from the repo root:
pip install -e ./mcp_servers/yahoo_connector

yahoo-connector                                           # MCP server on stdio
yahoo-connector --transport streamable-http --port 8765   # or over HTTP
```

No OAuth, no tokens, no secrets — yfinance is unauthenticated.

## Server logs

The server writes a rotating log to `logs/server.log` (relative to
cwd). Override with `--log-file PATH` or `YAHOO_CONNECTOR_LOG`.
Captured sources: `yahoo_connector`, `mcp`, `uvicorn`, `yfinance`.
Rotation: 5 MB × 3 backups. Same debugging pattern as the Schwab
server — **read the log file before asking the user for the
traceback**; tool handlers wrap their bodies in `logger.exception`.

## Things that will bite you

- **`yfinance` version drift.** Yahoo's endpoints change; breakage
  shows up as `yfinance` exceptions or unexpected empty responses.
  First debugging step is always `pip install -U yfinance` and see if
  upstream shipped a fix.
- **`.info` is slow and rate-limit-prone.** It does several HTTP
  requests to assemble the `Ticker.info` dict. The client only calls
  it for quotes (to get bid/ask/sizes) and for
  `projection="fundamental"`. Don't add more callers without a reason.
- **Timestamps.** yfinance returns pandas Timestamps with tz. The
  client converts to UTC epoch ms to match the Schwab candle schema.
  If you change the conversion, make sure the analytics functions
  still see strictly increasing ms values — `analytics.py` infers
  annualization from bar spacing.
- **Splits / dividends.** `history(..., auto_adjust=False)` gives raw
  OHLC; switching to adjusted prices would change the outputs of
  every `analyze_*` tool. Don't flip the default without updating the
  docstring and tests.
- **Empty candles.** Yahoo drops rows for holidays and halted sessions
  inside a date range. The client skips NaN rows rather than emitting
  zero-filled bars; an empty `candles` list means the request window
  had no data (common for 1m bars older than ~7 days).
- **Screeners.** `yf.screen(...)` occasionally returns a dict with an
  empty `quotes` list when Yahoo throttles; don't mistake it for "no
  movers today." Log the full response if you see this in the wild.

## What not to do

- Don't add an `auth` subcommand — yfinance is unauthenticated. If a
  future feature needs Yahoo login (e.g. premium data), add it as a
  separate code path rather than faking an OAuth flow.
- Don't translate between Yahoo and Schwab option-symbol formats
  inside this client. Symbology translation belongs above both
  connectors (in prompt engineering or a future normalization layer),
  not in a per-backend client.
- Don't paper over Yahoo's 429/Cloudflare responses with retry loops.
  Same rule as Schwab: surface the throttle.
- Don't copy-adjust prices silently. If you want adjusted returns,
  expose a flag on the tool and document it.
