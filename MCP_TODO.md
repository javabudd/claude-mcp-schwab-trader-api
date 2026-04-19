# MCP_TODO.md — planned additions to the traider hub

Punch list of MCP servers to add, grouped by the gap each one fills.
All entries inherit the hub-wide rules in
[AGENTS.md](AGENTS.md): read-only, primary sources preferred, secrets
out of repo/logs, surface 429s, no silent fallbacks.

Status: `[ ]` todo · `[~]` in progress · `[x]` landed.

## Tier 1 — catalysts & fundamentals depth

- [~] **sec_edgar_connector** — filings (10-K/10-Q/8-K), Form 4
  insider transactions, 13F institutional holdings, XBRL company
  facts. Primary source: `data.sec.gov`. Unauthenticated (just a
  descriptive `User-Agent`). 10 req/sec rate limit. Planning doc in
  this file below.
- [ ] **earnings_connector** — earnings calendar, consensus
  estimates, surprises, guidance. Candidate sources: Finnhub (free
  tier has earnings calendar + estimates), Zacks RSS, Nasdaq Data
  Link. Likely needs a paid tier for quality estimates — flag trade-
  offs before picking.
- [ ] **news_connector** — headline / event feed for catalyst
  tracking. Candidates: Benzinga News API, NewsAPI, Tiingo News, or a
  curated RSS aggregator. Pick one primary source per ticker/topic;
  do not blend providers silently.

## Tier 2 — macro completion

- [ ] **bls_connector** — BLS direct (CPI, NFP, JOLTS). FRED mirrors
  these but BLS is the primary publisher and releases a few minutes
  earlier. Worth it for release-day precision.
- [ ] **bea_connector** — BEA direct (GDP components, personal
  income, trade balance). Same rationale as BLS.
- [ ] **treasury_connector** — Treasury Direct / Fiscal Data API:
  auction results, debt-to-the-penny, daily Treasury statement, yield
  curve.
- [ ] **eia_connector** — US Energy Information Administration:
  weekly petroleum status, natural gas storage, electricity. Critical
  for energy-name trades.
- [ ] **global_cb_connector** — ECB SDW, BoJ, BoE statistical
  releases. Per hub rule: land one central bank at a time, each as
  its own module with its own primary-source client.

## Tier 3 — positioning & flow

- [ ] **cboe_connector** — put/call ratios, VIX term structure, IV
  surfaces, total options volume. Fills the gap left by static option
  chains in the market-data backends.
- [ ] **finra_connector** — short interest (bi-monthly), short sale
  volume (daily), ATS volume.
- [ ] **etf_flows_connector** — ETF holdings, creations/redemptions,
  sector rotation signal. Candidates: ICI, ETF.com, issuer feeds
  (iShares, SPDR, Vanguard).
- [ ] **cftc_connector** — Commitments of Traders (futures
  positioning by trader class). Weekly release. Primary source:
  `publicreporting.cftc.gov`.

## Tier 4 — risk / factor

- [ ] **factor_connector** — Ken French data library (Fama-French
  factors, industry portfolios). Primary source:
  `mba.tuck.dartmouth.edu`. Small, slow-moving data — cache with
  explicit TTL.

## Tier 5 — alt data (lower priority)

- [ ] **trends_connector** — Google Trends interest-over-time for
  ticker- or theme-level attention signals.
- [ ] **social_connector** — Reddit (`pushshift`/`reddit.com/.json`),
  X (requires paid tier post-2023). Sentiment is downstream of the
  data fetch — keep this server to fetching, not scoring.
- [ ] **crypto_connector** — CoinGecko (unauthenticated) or Binance
  public endpoints. Only add if the user starts asking crypto
  questions.

## Port allocation

Additive servers occupy contiguous ports to keep docker-compose
simple. Current + planned assignments:

| Port | Server                     | Status  |
|-----:|----------------------------|---------|
| 8765 | schwab / yahoo (exclusive) | shipped |
| 8766 | fred                       | shipped |
| 8767 | fed-calendar               | shipped |
| 8768 | sec-edgar                  | planned |
| 8769 | earnings                   | planned |
| 8770 | news                       | planned |
| …    | …                          | …       |

Claim the next free port when starting a new server. Update this
table in the same commit.

---

# Planning: `sec_edgar_connector`

## Why

EDGAR is the primary source for every US-listed public company's
disclosures. Four capabilities the hub is missing without it:

1. **Filings** — 10-K (annual), 10-Q (quarterly), 8-K (material
   events), S-1 (IPO), proxy statements. Catalyst and fundamental
   research depends on these.
2. **Insider transactions (Form 4)** — officer/director buys and
   sells, filed within 2 business days. High-signal.
3. **Institutional holdings (13F)** — quarterly positions for
   managers >$100M AUM, 45-day lag. Useful for positioning /
   crowding analysis.
4. **XBRL company facts** — structured financials (revenue, EPS,
   cash, debt, by period) without PDF-parsing the 10-K.

## Data sources (all `data.sec.gov`, unauthenticated)

| Endpoint                                                     | Returns                                     |
|--------------------------------------------------------------|---------------------------------------------|
| `/submissions/CIK{cik}.json`                                 | All filings for one CIK, most recent first  |
| `/api/xbrl/companyfacts/CIK{cik}.json`                       | Full XBRL facts for a company               |
| `/api/xbrl/companyconcept/CIK{cik}/us-gaap/{concept}.json`   | One concept's time-series (e.g. `Revenues`) |
| `/api/xbrl/frames/us-gaap/{concept}/USD/CY{year}Q{q}.json`   | Cross-sectional snapshot of one concept     |
| `https://efts.sec.gov/LATEST/search-index?q=...`             | Full-text search over filings               |
| `https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/`       | Filing-level document index                 |
| `https://www.sec.gov/files/company_tickers.json`             | Ticker ↔ CIK mapping (refresh daily)        |

## Hard constraints specific to EDGAR

- **User-Agent is mandatory.** SEC requires a descriptive `User-Agent`
  with contact email, or requests are blocked. Format per SEC Fair
  Access policy: `"<Company or name> <email>"`. Configurable via
  `SEC_EDGAR_USER_AGENT` env var; client raises at first request if
  unset. Do **not** hardcode a default email.
- **10 requests/second per IP.** Enforce client-side with a token
  bucket or explicit `asyncio.Semaphore` — SEC will block an IP that
  sustains overages. On 429 or 403, raise `SecEdgarRateLimitError`;
  don't retry in a loop (hub rule).
- **Ticker → CIK mapping is a file fetch.** Cache
  `company_tickers.json` for the process lifetime; refresh on a
  visible TTL (e.g. 24h) and expose `fetched_at` in responses.
- **XBRL facts are not uniform across companies.** Some report
  `Revenues`, others `SalesRevenueNet`, others
  `RevenueFromContractWithCustomerExcludingAssessedTax`. Don't paper
  over this — let the tool return the raw concept, and document the
  common aliases in the docstring.
- **13F reverse lookup (who holds ticker X?) is not a single
  endpoint.** You have to pull the 13F filings for every manager and
  index them yourself, or use a derived dataset. Scope decision
  below.
- **Form 4 is XML.** Each filing's primary doc is a small XML file
  (`ownershipDocument`). Parse with `lxml` — not a huge dep, but
  worth noting.

## Secrets

None. SEC EDGAR is public and unauthenticated. The `User-Agent`
contact email is a configuration value, not a secret — it's meant to
identify the client to SEC. Still, keep it in `.env` so the user
controls what email they publish in outbound request headers.

## Proposed tool surface

Mirror the `fred_connector` philosophy: thin client, decisions live
in tool docstrings, raw SEC JSON passed through with minimal
reshaping.

**Filings**
- `search_companies(query)` — ticker or name → `{cik, ticker, name}`.
  Uses the cached `company_tickers.json`.
- `get_company_filings(ticker_or_cik, form_types=None, since=None, limit=40)`
  — recent filings for one company, optionally filtered by form type
  (`["10-K", "10-Q", "8-K"]`) and date. Returns accession numbers,
  filing dates, and primary-doc URLs.
- `get_filing(accession_number)` — metadata + links for one filing,
  including its document index.
- `search_filings(query, form_types=None, date_range=None, limit=20)`
  — full-text search via `efts.sec.gov`. Returns filing hits with
  snippets.
- `get_recent_filings(form_type, limit=40)` — firehose of recent
  filings of a given form across all filers (e.g. "last 40 8-Ks").

**Insider (Form 4)**
- `get_insider_transactions(ticker_or_cik, since=None, limit=40)` —
  parse Form 4 XMLs for one issuer into a list of insider
  transactions: `{filer, role, transaction_date, shares, price,
  acquired_or_disposed, shares_owned_after}`.

**Institutional (13F)**
- `get_institutional_portfolio(cik, quarter=None)` — what does this
  13F filer hold, for a given quarter (`"2025Q4"` style). Parses the
  informationTable XML.
- `get_institutional_holders(ticker, quarter=None)` — **v2 scope.**
  Reverse lookup (who holds X) requires a derived dataset or wide
  fan-out. Initial version can return a clear `NotImplemented`-style
  error pointing at `get_institutional_portfolio` per manager; revisit
  once the core server is landed.

**XBRL**
- `get_company_facts(ticker_or_cik)` — the full
  `/companyfacts/CIK…json` blob (large — document the size in the
  docstring).
- `get_company_concept(ticker_or_cik, concept, taxonomy="us-gaap")`
  — one concept's reported values over time.
- `get_frame(concept, period, taxonomy="us-gaap", unit="USD")` —
  cross-sectional: all filers' values for one concept for one period.

## Proposed layout

```
mcp_servers/sec_edgar_connector/
├── AGENTS.md              # per-server constraints (mirrors fred_connector)
├── README.md              # tool surface + setup
├── pyproject.toml         # deps: mcp, httpx, lxml, python-dotenv
└── src/sec_edgar_connector/
    ├── __init__.py        # re-exports SecEdgarClient / errors
    ├── __main__.py        # entry point — loads .env, dispatches to server.main
    ├── edgar_client.py    # httpx wrapper, rate limiter, UA enforcement
    ├── ticker_map.py      # cached company_tickers.json lookup
    ├── form4_parser.py    # lxml parse of Form 4 ownershipDocument XML
    ├── form13f_parser.py  # lxml parse of informationTable XML
    └── server.py          # FastMCP server
```

## Port and compose wiring

- Default HTTP port: **8768**.
- Compose service: `sec-edgar-connector`, profile `sec-edgar`
  (additive — runs alongside any market-data backend).
- Update the root README's "Choosing a market-data backend" section
  only if needed; this server doesn't overlap tool names with
  anything else, so likely just an additive row in the servers table.

## Open questions before implementing

1. **Scope of Form 4.** Issuer-scoped (v1, easy) or also
   insider-scoped ("all of CEO Jane Doe's recent trades across all
   companies")? The data supports both; insider-scoped is a bigger
   fan-out.
2. **13F reverse lookup.** Worth building an in-process index, or
   defer to "v2 use a vendor dataset"? Real-time reverse lookup
   against SEC alone is O(managers × filings) per query.
3. **Foreign private issuers** (file 20-F instead of 10-K). Include
   them in the default form-type filters or US-only? Leaning include
   — the user can filter.
4. **Caching.** `company_tickers.json` is the obvious candidate
   (24h TTL). Filing metadata and company facts change on every new
   filing, so caching those is risky — prefer no cache with visible
   `fetched_at`, per hub rule.

Resolve these with the user before writing code.
