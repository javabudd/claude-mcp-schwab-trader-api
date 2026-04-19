# fred-connector

Read-only [FRED](https://fred.stlouisfed.org) bridge exposed as an
MCP server. One of the MCP servers bundled in the
[`traider`](../../README.md) hub (see the root
[AGENTS.md](../../AGENTS.md) for how the hub is organized). See
[AGENTS.md](AGENTS.md) in this directory for the per-server
constraints and gotchas.

Unlike `schwab_connector` / `yahoo_connector`, this server is
**additive**: it exposes macro / economic-release data rather than
equity quotes, and it runs on a different port (8766) so it can sit
alongside whichever market-data backend you picked.

## What this MCP server can do

All tools are **read-only**. Every response is FRED's JSON
essentially unchanged — the model can introspect raw fields rather
than second-guessing a translation.

### `get_release_schedule(...)`

The "what's coming out this week" view — the economic-release calendar
across *every* FRED release (CPI, PPI, NFP, GDP, PCE, Retail Sales,
JOLTS, …).

- `realtime_start` / `realtime_end` — ISO `YYYY-MM-DD`. Pass today to
  filter to upcoming releases.
- `include_empty=True` includes scheduled future dates that don't
  have data attached yet (this is how you find upcoming prints).
- `order_by`, `sort_order`, `limit` — standard FRED knobs.

### `get_release_dates(release_id, ...)`

Past *and* scheduled publication dates for one release. Use
`list_releases` first to find the `release_id`. Key IDs:

| Release                      | `release_id` |
|------------------------------|-------------:|
| Consumer Price Index (CPI)   |           10 |
| Employment Situation (NFP)   |           50 |
| GDP                          |           53 |
| PCE (Personal Income)        |           21 |
| Retail Sales                 |           32 |
| JOLTS                        |          192 |
| FOMC Meeting                 |          101 |

### `list_releases(limit=200)`

All FRED releases. Use this to discover `release_id` values.

### `get_release_info(release_id)`

Metadata for one release — name, press-release URL, notes.

### `get_release_series(release_id, ...)`

Series that live under a release (e.g. CPI headline, core CPI, and
every component).

### `search_series(search_text, ...)`

Fuzzy search over series IDs and titles. Examples:

- `"core CPI"` → `CPILFESL`
- `"10-year treasury"` → `DGS10`
- `"fed funds"` → `FEDFUNDS` / `DFF`
- `"unemployment rate"` → `UNRATE`

### `get_series_info(series_id)`

Metadata for one series: units, frequency, seasonal adjustment,
last-updated timestamp.

### `get_series(series_id, ...)`

The actual time-series observations. `units` can do server-side
transforms:

- `lin` (default) — levels
- `chg` — change, `ch1` — year-over-year change
- `pch` — % change, `pc1` — YoY % change, `pca` — % change annualized
- `log` — natural log

`frequency` + `aggregation_method` resample on the server (`m`, `q`,
`a` with `avg` / `sum` / `eop`).

## Setup

### 1. Get a FRED API key (free)

Register at <https://fredaccount.stlouisfed.org/apikeys>. No rate
tier shopping — the free key is the only tier.

### 2. Put the key in `.env`

At the repo root:

```
FRED_API_KEY=your-key-here
```

### 3. Install

```bash
conda activate traider
pip install -e ./mcp_servers/fred_connector
```

### 4. Run the server

```bash
fred-connector                                           # stdio
fred-connector --transport streamable-http --port 8766   # HTTP
```

Or via Docker (together with whichever backend is active):

```bash
cd mcp_servers
docker compose --env-file ../.env --profile fred up -d
```

## Connect your AI CLI

Same recipes as the rest of the hub; the
[hub README](../../README.md#connect-your-ai-cli) has the full
Claude Code / OpenCode / Gemini CLI examples. The HTTP endpoint is
`http://localhost:8766/mcp`.

## Prompts that put these tools to work

- **"What US macro releases are scheduled for the next 10 days?"** —
  `get_release_schedule(realtime_start=<today>, include_empty=True)`
- **"When's the next CPI print?"** — `get_release_dates(release_id=10,
  realtime_start=<today>)`
- **"Has core PCE been trending up or down YoY?"** —
  `get_series("PCEPILFE", units="pc1")`
- **"Plot the unemployment rate for the last five years."** —
  `get_series("UNRATE", observation_start=<5y ago>)`
- **"What's the 10Y–2Y yield spread doing?"** —
  `get_series("T10Y2Y")` (it's precomputed by FRED)

Pair these with `schwab_connector` / `yahoo_connector` prompts to
condition equity decisions on the macro calendar — e.g. *"run
analyze_returns on SPY then show me what CPI prints overlap that
window."*

## Things worth knowing

- **Rate limit.** 120 requests per 60s per key. The tool surfaces
  FRED's 429 as a `FredError`; back off, don't retry-loop.
- **Realtime vs. observation.** The release calendar is *realtime*-
  dated (when was the value published); series observations are
  *observation*-dated (which period they describe). The tool
  parameters separate the two.
- **Empty release dates.** `include_empty=True` is what surfaces
  *future* scheduled dates that don't have values yet — that's the
  point. Filtering them out would hide the calendar.
- **Free-tier humility.** FRED is maintained by one Fed reserve bank
  on public funding. Uptime is very good but not nine-nines; if a
  tool call fails, the server propagates the error rather than
  serving stale data.
