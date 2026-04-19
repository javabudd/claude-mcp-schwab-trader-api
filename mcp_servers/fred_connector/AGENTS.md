# AGENTS.md — fred_connector

Guidance for AI coding agents working on the **FRED** MCP server
inside the [`traider`](../../AGENTS.md) hub. Read the root AGENTS.md
first — it frames how this directory fits into the wider hub.

## What this is

`fred-connector` is a read-only bridge between an AI CLI (via MCP)
and the [FRED API](https://fred.stlouisfed.org/docs/api/fred/)
published by the St. Louis Fed. It exposes:

- The **economic-release calendar** — when CPI, PPI, Employment
  Situation (NFP), GDP, PCE, Retail Sales, JOLTS, etc. are being
  published (and when the historical releases were).
- **Release / series metadata** — units, frequency, last-updated
  timestamps, which series belong to which release.
- **Time-series observations** — the actual numbers, for any of the
  ~800 000 series FRED tracks.

FRED is the authoritative primary aggregator for US macro data. For
the narrower "FOMC meeting dates" calendar, see
[`fed_calendar_connector`](../fed_calendar_connector/AGENTS.md) — that
server scrapes federalreserve.gov directly for just the meeting
schedule.

## Not a market-data backend

Unlike `schwab_connector` / `yahoo_connector`, this server is
**additive** — it does not bind port 8765 and it does not overlap
with the market-data tool surface. Run it alongside either backend.

- Default HTTP port: **8766**.
- Compose service name: `fred-connector` (no profile — always
  available when the `fred` profile is active).

## Hard constraints

Inherits every rule in the hub AGENTS.md. Specifically:

- **Read-only.** FRED is a data-only API; there's nothing to write
  anyway, but don't add write-like endpoints (e.g. series uploads to
  FRED's ALFRED realtime views) if they ever appear.
- **Surface 429s.** FRED enforces a per-key quota (120 req / 60s on
  the free tier). When the API returns an error, let it propagate as
  :class:`FredError`; don't retry in a loop.
- **API key out of logs.** The key is low-risk (read-only, easy to
  rotate) but still treat it as a secret: never log it, never include
  it in MCP tool responses, never commit it.
- **No silent fallback to cached values.** If FRED is down, the tool
  raises. Don't serve a stale snapshot and pretend it's live — the
  downstream trade thesis may depend on the most recent print.

## Secrets

`FRED_API_KEY` (free, from
https://fredaccount.stlouisfed.org/apikeys). Lives in the root
`.env`, which Compose mounts via `env_file`. The client raises a
clear error at first request if it's missing.

## Layout

```
src/fred_connector/
  __init__.py       # re-exports FredClient / FredError
  __main__.py       # entry point — loads .env, dispatches to server.main
  fred_client.py    # httpx wrapper around the FRED REST API
  server.py         # FastMCP server with 8 tools
pyproject.toml      # deps: mcp, httpx, python-dotenv
```

The client is intentionally thin: each method maps to one FRED
endpoint and returns the JSON essentially unchanged. Decisions about
what's useful live in the tool docstrings in `server.py`, not in the
client.

## Tool surface

- `get_release_schedule(...)` — calendar across releases, with
  server-side filtering (`release_ids` fan-out, `name_contains`
  substring filter, `dedupe`). Forward-looking by default
  (`realtime_start=today`).
- `get_high_impact_calendar(...)` — curated fan-out for the releases
  a trader actually cares about (CPI, PCE, PPI, NFP, JOLTS, GDP,
  Retail Sales), bucketed by `category`.
- `get_release_dates(release_id, ...)` — one release's schedule
- `list_releases(...)` — directory for finding `release_id`s
- `get_release_info(release_id)` — metadata for one release
- `get_release_series(release_id, ...)` — series in a release
- `search_series(search_text, ...)` — find series IDs by keyword
- `get_series_info(series_id)` — metadata for one series
- `get_series(series_id, ...)` — time-series observations

### Why the heavy lifting lives server-side

FRED's `/releases/dates` is a firehose (hundreds of low-signal
releases). Two specific pain points the server now filters for you:

1. **"FOMC Press Release" (release 101) spam** when
   `include_release_dates_with_no_data=true`: fires on every day of
   a meeting window, so a two-week window returns ~14 copies. The
   curated tool excludes release 101 entirely; the docstring points
   callers at `fed_calendar_connector.get_fomc_meetings` for FOMC
   dates.
2. **No knob to filter by release** on `/releases/dates` itself — if
   a caller just wants CPI + NFP + GDP, they either pull everything
   and filter client-side, or make three `/release/dates` calls.
   `get_release_schedule(release_ids=[...])` and
   `get_high_impact_calendar` fan out for them.

FRED release-id cheatsheet for common trading-relevant prints (these
change rarely; verify with `list_releases` if in doubt):

| Release                          | `release_id` | Common series IDs                                    |
|----------------------------------|-------------:|------------------------------------------------------|
| Consumer Price Index             |           10 | `CPIAUCSL` (headline), `CPILFESL` (core)             |
| Producer Price Index             |           46 | `PPIACO`, `PPIFIS`                                    |
| Employment Situation (NFP)       |           50 | `PAYEMS`, `UNRATE`, `AHETPI`                         |
| Personal Income and Outlays (PCE)|           21 | `PCEPI`, `PCEPILFE` (core)                           |
| Gross Domestic Product           |           53 | `GDP`, `GDPC1` (real)                                 |
| Retail Sales                     |           32 | `RSAFS`, `RSXFS` (ex auto)                           |
| JOLTS                            |          192 | `JTSJOL`, `JTSQUR`                                    |
| ISM Manufacturing PMI            |          258 | not on FRED directly — see ISM                       |
| FOMC Meeting / Statement         |          101 | — (no series; use `fed_calendar_connector`)          |

## Don't start the MCP server yourself

Same rule as every server in the hub. The user runs
`fred-connector` in their own terminal (or the Compose service). If
a tool call fails because the server isn't up, tell the user; don't
spawn it.

## Running / developing

```bash
conda activate traider
pip install -e ./mcp_servers/fred_connector

export FRED_API_KEY=...        # or put in .env
fred-connector                                           # stdio
fred-connector --transport streamable-http --port 8766   # HTTP
```

## Server logs

Rotating file at `logs/server.log` (5 MB × 3). Override with
`--log-file PATH` or `FRED_CONNECTOR_LOG`. Captured sources:
`fred_connector`, `mcp`, `uvicorn`, `httpx`.

## Things that will bite you

- **Realtime vs. observation dates.** FRED distinguishes *realtime*
  (the vintage — when was this value visible to market participants?)
  from *observation* (which period does the value describe?). The
  release-calendar endpoints are realtime-scoped; series observations
  are observation-scoped. Mixing them up will put the wrong dates in
  front of the user.
- **Empty release dates.** When `include_empty=True`, future scheduled
  dates appear with no data yet. That's the signal for "upcoming
  release" — don't filter them out.
- **Series-level units.** Passing `units="pch"` computes a percent
  change server-side. If you then also compute a percent change
  client-side, you'll double-transform. Pick one.
- **Rate limit.** 120 requests per 60s per key. A tight loop over
  `get_release_series(...)` for many releases will trip it; batch
  with higher `limit` instead.

## What not to do

- Don't add an OAuth flow — FRED uses a static API key.
- Don't cache responses silently. If caching is useful, expose the
  TTL and signal stale-hit in the response.
- Don't reshape the FRED JSON into a "nicer" schema. The model can
  read the raw shape, and hiding fields behind a translation layer
  makes debugging harder.
