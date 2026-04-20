# fed-calendar-connector

Read-only Federal Reserve **FOMC meeting calendar** exposed as an MCP
server. One of the MCP servers bundled in the
[`traider`](../../README.md) hub (see the root
[AGENTS.md](../../AGENTS.md)). See [AGENTS.md](AGENTS.md) in this
directory for per-server constraints and gotchas.

Primary source: the Fed's own calendar page at
<https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm>.
The server scrapes the HTML (there is no JSON/ICS feed) and returns
structured records.

This connector is **additive** — it runs on port 8767 alongside
whichever market-data backend you picked (`schwab_connector` or
`yahoo_connector`).

## What this MCP server can do

All tools are read-only. Narrow by design: dates and flags only.

### `get_fomc_meetings(year=None, upcoming_only=False)`

Every FOMC meeting listed on the Fed's calendar page. Each meeting:

```json
{
  "year": 2026,
  "month": "January",
  "day_range": "27-28",
  "start_date": "2026-01-27",
  "end_date": "2026-01-28",
  "is_sep": true,
  "has_press_conference": true,
  "note": null,
  "statement_url": "https://www.federalreserve.gov/newsevents/...",
  "minutes_url": "https://www.federalreserve.gov/monetarypolicy/...",
  "press_conference_url": "https://www.federalreserve.gov/monetarypolicy/fomcpressconf20260128.htm"
}
```

- `is_sep` — Summary of Economic Projections (dot plot) released with
  this meeting.
- `has_press_conference` / `press_conference_url` — set when the Fed
  has posted the press-conference permalink (happens a few days before
  the meeting). Absence on a future meeting doesn't mean no presser —
  every FOMC meeting since 2019 has had one; the flag just tracks when
  the URL lands on the page.
- `note` — parenthetical on the date cell, if any (e.g.
  `"notation vote"`, `"unscheduled"`, `"conference call"`).
- `upcoming_only=True` drops meetings whose `end_date` is in the past.

### `get_next_fomc_meeting()`

Convenience wrapper — returns the first meeting on or after today
(UTC), with `days_until_start`. `meeting` is `null` if the Fed page
doesn't list a future meeting yet.

## Setup

### 1. Install

```bash
conda activate traider
pip install -e ./mcp_servers/fed_calendar_connector
```

No API key, no auth.

### 2. Run the server

```bash
fed-calendar-connector                                           # stdio
fed-calendar-connector --transport streamable-http --port 8767   # HTTP
```

Or via Docker (alongside whichever market-data backend is active),
from the repo root:

```bash
docker compose --profile fed-calendar up -d
```

## Connect your AI CLI

Same recipes as the rest of the hub; the
[hub README](../../README.md#connect-your-ai-cli) has the full
Claude Code / OpenCode / Gemini CLI examples. The HTTP endpoint is
`http://localhost:8767/mcp`.

## Prompts that put this tool to work

- **"When's the next FOMC meeting and is it a dot-plot meeting?"** —
  `get_next_fomc_meeting()`, check `is_sep`.
- **"List all 2026 FOMC meetings."** — `get_fomc_meetings(year=2026)`.
- **"Anything happening in the next two weeks on the Fed side?"** —
  `get_fomc_meetings(upcoming_only=True)` plus filter by
  `start_date`.

Pair with `fred_connector` for *content* (statement text timing via
release `101`, rate-decision series like `FEDFUNDS`) and with
`schwab_connector` / `yahoo_connector` for market reaction.

## Things worth knowing

- **Primary source, HTML scrape.** The Fed does not publish an ICS or
  JSON feed for this calendar. When federalreserve.gov changes markup
  (rare but possible), this tool fails loudly rather than returning a
  guess — update the scraper.
- **Two-month meetings.** Meetings sometimes span April/May or
  October/November. The server anchors `start_date` to the first
  month and `end_date` to the second.
- **Unscheduled / notation-vote rows** are surfaced with a non-null
  `note`. Rows that don't carry a date at all (rare, purely
  parenthetical "unscheduled" placeholders) are skipped — they don't
  have a date to anchor to.
- **Timezone.** "Today" and `days_until_start` are computed in UTC.
  FOMC decisions are released in ET; on the edges the two can differ
  by a few hours. The response includes `fetched_at` in UTC so the
  model can reason about it explicitly.
- **No silent cache.** Every tool call refetches the page. If that
  becomes a rate-limit concern, the server will grow an explicit TTL
  field in the response — but it won't lie about freshness.
