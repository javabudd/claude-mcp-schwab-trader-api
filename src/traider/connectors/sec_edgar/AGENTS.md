# AGENTS.md — sec_edgar_connector

Guidance for AI coding agents working on the **SEC EDGAR** MCP server
inside the [`traider`](../../AGENTS.md) hub. Read the root AGENTS.md
first — it frames how this directory fits into the wider hub.

## What this is

`sec-edgar-connector` is a read-only bridge between an AI CLI (via
MCP) and the [SEC EDGAR](https://www.sec.gov/edgar) public APIs
(`data.sec.gov`, `www.sec.gov/Archives`, `efts.sec.gov`). It exposes
the four things a trading analyst reaches EDGAR for:

- **Filings** — 10-K (annual), 10-Q (quarterly), 8-K (material events),
  S-1 (IPO), proxy statements, plus their foreign-private-issuer
  equivalents (20-F, 6-K). By company or full-text.
- **Insider transactions (Form 4)** — parsed per issuer.
- **Institutional holdings (13F)** — parsed per manager.
- **XBRL company facts** — structured financials (Revenues,
  NetIncomeLoss, Assets, …) both per-company and cross-sectionally.

EDGAR is the **primary source** for every US-listed public company's
disclosures. Aggregators (Yahoo, S&P) derive from it with lag; this
server skips the middleman.

## Not a market-data backend

Additive server — does not bind port 8765.

- Default HTTP port: **8768**.
- Compose profile: `sec-edgar` (additive to any market-data backend).

## Hard constraints

Inherits every rule in the hub AGENTS.md. Specifically:

- **Read-only.** EDGAR is a data-only surface; no write endpoints to
  misuse. If a feature request implies submitting to EDGAR (e.g. an
  assistant that drafts a Form 4), push back — that's not this hub.
- **SEC Fair Access: descriptive `User-Agent` required.** Every
  request must carry a `User-Agent` that identifies the client and a
  contact email, or SEC will IP-block the server. The client enforces
  this at construction time via `SEC_EDGAR_USER_AGENT` — do **not**
  hardcode a default email, and do not silently fall back to a
  generic UA if the env var is unset. Fail loud.
- **10 requests/second per IP.** Enforced client-side with a token
  bucket in `edgar_client.py`. On 429 or 403, raise
  `SecEdgarRateLimitError` and stop — per hub rule, no retry loops.
  If a tool needs higher throughput than the bucket allows (13F
  fan-out, large Form 4 batches), make the caller paginate.
- **No silent fallback to stale data.** If EDGAR is unreachable,
  tools raise. Do not serve a cached snapshot and pretend it's live
  — trade theses depend on knowing whether a filing is the most
  recent one.
- **`User-Agent` email out of logs.** The UA is a configuration
  value, not a secret — but it's the user's real contact email in
  outbound headers. Don't include it in tool response bodies (the
  client keeps it to the HTTP layer).

## Secrets

None. EDGAR is public and unauthenticated. The only configuration is
`SEC_EDGAR_USER_AGENT`, which lives in the root `.env`.

Example value (use the user's own name/email, not a placeholder):

```
SEC_EDGAR_USER_AGENT=traider-hub andy@servicecore.com
```

## Layout

```
src/sec_edgar_connector/
  __init__.py         # re-exports SecEdgarClient / error classes
  __main__.py         # entry point — loads .env, dispatches to server.main
  edgar_client.py     # httpx wrapper, token bucket, UA enforcement
  ticker_map.py       # company_tickers.json cache (24h TTL, visible)
  form4_parser.py     # lxml parse of Form 4 ownershipDocument XML
  form13f_parser.py   # lxml parse of 13F informationTable XML
  server.py           # FastMCP server with 10 tools
pyproject.toml        # deps: mcp, httpx, lxml, python-dotenv
```

Same philosophy as `fred_connector`: client is thin (each method =
one endpoint, passes JSON through), parsers extract only the fields a
trading analyst uses, decisions about what's useful live in the tool
docstrings in `server.py`.

## Tool surface

**Company lookup**
- `search_companies(query, limit=20)` — ticker/name search over SEC's
  canonical `company_tickers.json`.

**Filings**
- `get_company_filings(ticker_or_cik, form_types=None, since=None, limit=40)`
  — recent filings for one company, filterable by form + date.
- `get_filing(ticker_or_cik, accession_number)` — document index
  (primary doc URL + all exhibits) for one filing.
- `search_filings(query, form_types=None, date_start=None, date_end=None, limit=20)`
  — full-text search via `efts.sec.gov`.

**Insider (Form 4)**
- `get_insider_transactions(ticker_or_cik, since=None, limit=20)` —
  parses Form 4 XMLs per issuer. Issuer-only in v1.

**Institutional (13F)**
- `get_institutional_portfolio(cik, accession_number=None)` — parses
  one manager's informationTable. Reverse lookup (who holds ticker X)
  is deliberately not shipped — see "Scoping choices" below.

**XBRL**
- `get_company_facts(ticker_or_cik)` — raw `companyfacts` blob.
- `get_company_concept(ticker_or_cik, concept, taxonomy="us-gaap")`
  — one concept's time series.
- `get_frame(concept, period, taxonomy="us-gaap", unit="USD")` —
  cross-sectional snapshot across all filers.

## Scoping choices made in v1

These were the open questions from the planning doc; the answers are
baked into the current surface and should be preserved until there's
a concrete user need to change.

1. **Form 4 is issuer-scoped.** You can list a company's insider
   trades, but there's no "all of CEO Jane Doe's trades across her
   boards" tool. That would require fan-out over her reporting-owner
   CIK; add it only if asked.
2. **13F reverse lookup is not shipped.** `get_institutional_portfolio`
   reads one filer's holdings; there is no "who holds AAPL?" tool.
   Real-time reverse lookup against SEC alone is O(managers ×
   filings) per query — impractical without an in-process index.
   Defer until the user asks and decide then whether to build the
   index or wire a vendor dataset.
3. **Foreign private issuers are included.** Default form-type
   filters don't exclude `20-F` / `6-K`. Users who only want
   US-domestic filers can pass `form_types=["10-K", "10-Q", "8-K"]`.
4. **Caching: only the ticker map.** `company_tickers.json` has a
   24-hour TTL (exposed via `ticker_map_fetched_at` in every response
   that consults it). Filings, company facts, concepts, and frames
   are **not cached** — they change on every filing and stale reads
   would mislead. Visible `fetched_at` on every response.

## Don't start the MCP server yourself

Same rule as every server in the hub. The user runs
`sec-edgar-connector` in their own terminal (or the Compose service).
If a tool call fails because the server isn't up, tell the user; don't
spawn it.

## Running / developing

```bash
conda activate traider
pip install -e ./mcp_servers/sec_edgar_connector

export SEC_EDGAR_USER_AGENT="traider-hub you@example.com"
sec-edgar-connector                                           # stdio
sec-edgar-connector --transport streamable-http --port 8768   # HTTP
```

No API key, no OAuth. Just the `User-Agent` env var.

## Server logs

Rotating file at `logs/server.log` (5 MB × 3). Override with
`--log-file PATH` or `SEC_EDGAR_CONNECTOR_LOG`. Captured sources:
`sec_edgar_connector`, `mcp`, `uvicorn`, `httpx`.

## Things that will bite you

- **XBRL concept names are not uniform across filers.** Some
  companies tag revenue as `Revenues`, others as `SalesRevenueNet`,
  others as `RevenueFromContractWithCustomerExcludingAssessedTax`.
  `get_company_concept` will 404 on the wrong name. When a lookup
  fails, try `get_company_facts` to see what the filer actually tags,
  or use `get_frame` which aggregates across the concept specifically
  (not the filer's tagging).
- **Frame periods: duration vs. instantaneous.** Balance-sheet
  concepts (`Assets`, `Liabilities`) only exist as instantaneous
  values — the frame period must end in `I`
  (e.g. `CY2024Q4I`). Flow concepts (`Revenues`, `NetIncomeLoss`) are
  duration — no `I` suffix. Mixing them up returns 404.
- **13F value units changed in 2022.** SEC switched 13F `value` from
  thousands of dollars to whole dollars for periods ending on or
  after 2022-09-30. The parser infers the unit from
  `period_of_report` and tags it on the envelope. If you're building
  aggregates, check that field before summing.
- **Amendments are separate form codes.** `10-K/A`, `10-Q/A`,
  `8-K/A`, `4/A`, `13F-HR/A` are distinct filings, not silently
  merged. `form_types=["10-K"]` will *not* match `10-K/A` — include
  both if you want restated numbers.
- **Submissions feed only holds recent filings inline.** Older
  history spills into `filings.files[*]` overflow JSONs referenced
  by name. The tool layer currently only reads the inline recent
  block — for deep history, the client has `submissions_overflow(...)`
  but the MCP tool doesn't fan out yet. Add that if/when a user
  actually asks for decades-old filings.
- **Rate limit is real.** 10 req/sec is enforced by SEC at the IP
  level; the client's token bucket matches. A fan-out over many
  Form 4 filings or a 13F loop that also dereferences every
  underlying issuer *will* hit the wall. Prefer fewer, larger tool
  calls; tell the model to paginate rather than to grep.
- **The full-text search endpoint is undocumented.** `efts.sec.gov`
  powers EDGAR's public search page. Its response shape is
  Elasticsearch's; SEC can (and does) change it without notice. The
  tool passes the raw hits through — brittleness here is worth the
  signal.

## What not to do

- Don't hardcode a default `User-Agent`. SEC explicitly calls out
  "sample User-Agent strings copied from the docs" as a ban-worthy
  offense. Require the env var.
- Don't retry 429s or 403s. Raise, let the user see the throttle.
- Don't cache filing contents. Filings are immutable once filed, but
  the *set* of filings changes constantly — a cache would serve
  "missing the newest 10-Q" very easily.
- Don't reshape `companyfacts` / `companyconcept` JSON into a
  "nicer" schema. The structure is ugly but authoritative; hiding
  fields makes debugging a wrong concept name impossible.
- Don't add aggregator fallbacks (Yahoo financials, Simply Wall St).
  This server is primary-source-only. If EDGAR is down, the tool
  raises and the user decides.
