# AGENTS.md — fed_calendar_connector

Guidance for AI coding agents working on the **Federal Reserve
meeting-calendar** MCP server inside the [`traider`](../../AGENTS.md)
hub. Read the root AGENTS.md first.

## What this is

`fed-calendar-connector` is a read-only bridge between an AI CLI (via
MCP) and the FOMC meeting calendar published at
<https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm>.

It's intentionally narrow: **dates and flags only** (meeting date
range, SEP/dot-plot flag, press-conference flag, statement/minutes
URLs). The page is the primary source — there is no JSON/ICS/RSS feed
for the FOMC calendar itself, so this server scrapes the HTML.

For *data* driven by the releases and the broader economic-release
calendar, use [`fred_connector`](../fred_connector/AGENTS.md). For
equity quotes / history / TA, use `schwab_connector` or
`yahoo_connector`.

## Not a market-data backend

Additive server — does not bind port 8765.

- Default HTTP port: **8767**.
- Compose profile: `fed-calendar` (additive to any market-data
  backend).

## Hard constraints

Inherits every rule in the hub AGENTS.md. Specifically:

- **Read-only.** Scrape only — no POSTs, no form submits.
- **Primary source.** The *only* source is federalreserve.gov. Don't
  add aggregator fallbacks (Bloomberg, investing.com, …); a disagreement
  with the Fed's own page is always the Fed's page being right.
- **No silent fallback to a stale snapshot.** If the fetch or parse
  fails, raise :class:`FomcScrapeError`. A cache with an explicit TTL
  is fine to add later, but stale-on-failure is not.
- **Surface layout drift.** Fed HTML markup is fairly stable but not
  guaranteed. The scraper raises a clear error ("no FOMC year panels
  found") when the top-level structure disappears — don't paper over
  that with fuzzy fallbacks; update the selectors instead.

## HTML structure we depend on

Documented here so the next person touching the scraper knows exactly
what will break it. Source: federalreserve.gov as of 2026.

- Each year is a `div.panel.panel-default` under the article body.
  The heading text contains the year (e.g. `"2026 FOMC Meetings"`).
- Each meeting is a `div.row.fomc-meeting` inside that panel.
- Month label: `div.fomc-meeting__month > strong` — full month names,
  or `"April/May"` style for meetings that straddle two months.
- Date range: `div.fomc-meeting__date` — `"27-28"`, `"8-9*"` (SEP),
  `"22 (notation vote)"`, or parenthetical-only for unscheduled items.
- SEP footnote: trailing literal `*` on the date cell.
- Press conference: `<a>` with an `href` matching the regex
  `fomcpres{1,2}conf` (Fed has historically used both spellings).
- Minutes / statement URLs: label or href keyword match; not
  semantically tagged.

The panel-footer holds the legend ("* Meeting associated with a
Summary of Economic Projections"). We don't parse it — the `*` flag
is self-documenting in the tool docstring.

## Secrets

None. federalreserve.gov is public and unauthenticated.

## Layout

```
src/fed_calendar_connector/
  __init__.py          # re-exports FomcScraper / FomcScrapeError
  __main__.py          # entry point — loads .env, dispatches to server.main
  fomc_scraper.py      # httpx + BeautifulSoup scrape of the calendar page
  server.py            # FastMCP server: 2 tools
pyproject.toml         # deps: mcp, httpx, beautifulsoup4, python-dotenv
```

## Tool surface

- `get_fomc_meetings(year=None, upcoming_only=False)` — full parsed
  list from the calendar page.
- `get_next_fomc_meeting()` — convenience: first meeting on or after
  today (UTC), with `days_until_start`.

Both tools return:

```json
{
  "source": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
  "fetched_at": "2026-04-19T15:12:00+00:00",
  ...
}
```

so the model (and the user) can see exactly what was fetched and
when.

## Don't start the MCP server yourself

Same rule as the rest of the hub. The user runs
`fed-calendar-connector` themselves.

## Running / developing

```bash
conda activate traider
pip install -e ./mcp_servers/fed_calendar_connector

fed-calendar-connector                                           # stdio
fed-calendar-connector --transport streamable-http --port 8767   # HTTP
```

No API key, no auth.

## Server logs

Rotating file at `logs/server.log` (5 MB × 3). Override with
`--log-file PATH` or `FED_CALENDAR_CONNECTOR_LOG`. Captured sources:
`fed_calendar_connector`, `mcp`, `uvicorn`, `httpx`.

## Things that will bite you

- **Fed layout changes.** The calendar page has been stable for years
  but is not versioned. If every tool call starts failing with "no
  FOMC year panels found", inspect the HTML in a browser and update
  the selectors in `fomc_scraper.py` to match.
- **Two-month meetings.** Occasionally a meeting spans April/May or
  October/November. The month cell reads e.g. `"April/May"` and the
  date cell gives two days that belong to different months. The
  scraper anchors `start_date` to the first month and `end_date` to
  the second. If the Fed ever publishes a three-day straddle
  (unprecedented, but possible), the logic will need updating.
- **Notation votes / unscheduled items.** Some rows are purely
  parenthetical (e.g. `"(notation vote)"`) with no days. Those are
  skipped — they don't have a date to anchor to.
- **Timezone.** `utc_today()` uses UTC. A FOMC decision is an ET
  event, so "today" and "tomorrow" can disagree by a few hours on the
  edges. Document the UTC choice in the tool response rather than
  faking ET.
- **Rate limiting.** federalreserve.gov is not aggressive about
  throttling, but the scraper still sends one request per tool call.
  If this grows, add an explicit TTL cache — don't let Claude
  accidentally loop.

## What not to do

- Don't add aggregator fallbacks. Fed page is the only source.
- Don't extend to every central bank in one commit. If ECB/BoE/BoJ
  coverage is worth adding, land each one as its own module + tool
  with its own primary-source scraper — one commit per bank, each
  testable in isolation.
- Don't silently cache. If caching helps, expose a visible TTL field
  in the response.
- Don't "enhance" the meeting records with computed fields that
  aren't on the Fed page (rate-decision probability from fed funds
  futures, etc.). That belongs in analysis code over `fred_connector`
  or a dedicated derivatives-data server, not in the primary-source
  scraper.
