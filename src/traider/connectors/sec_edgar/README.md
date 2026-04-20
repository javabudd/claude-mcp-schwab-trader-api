# sec-edgar-connector

Read-only [SEC EDGAR](https://www.sec.gov/edgar) bridge exposed as an
MCP server. One of the MCP servers bundled in the
[`traider`](../../README.md) hub (see the root
[AGENTS.md](../../AGENTS.md) for how the hub is organized). See
[AGENTS.md](AGENTS.md) in this directory for the per-server
constraints and gotchas.

Unlike `schwab_connector` / `yahoo_connector`, this server is
**additive**: it exposes company filings, insider transactions,
institutional holdings, and XBRL financials rather than market
quotes, and it runs on a different port (8768) so it can sit
alongside whichever market-data backend you picked.

## What this MCP server can do

All tools are **read-only**. Every response includes a `source` URL
and `fetched_at` timestamp so the model (and you) can see exactly
where the data came from and when. Raw SEC shapes are passed through
with minimal reshaping — the model can introspect the fields rather
than second-guessing a translation.

### `search_companies(query, limit=20)`

Ticker / name substring search over SEC's canonical
`company_tickers.json`. Use this when you have a name or partial
ticker and need a CIK. Every other tool accepts a ticker or CIK via
`ticker_or_cik`; `search_companies` is the entry point.

### `get_company_filings(ticker_or_cik, form_types=None, since=None, limit=40)`

Recent filings for one company, newest first.

- `form_types` — list like `["10-K", "10-Q", "8-K"]`. Amendments
  (`10-K/A`, `10-Q/A`, `8-K/A`) are separate codes — include them if
  you want restated reports.
- `since` — ISO `YYYY-MM-DD`; only filings on or after.
- Each row: `accession_number`, `filing_date`, `report_date`, `form`,
  `primary_doc` (URL), `primary_doc_description`.

Foreign private issuers file `20-F` (annual) and `6-K` (interim)
instead of `10-K` / `10-Q`. Include those form codes explicitly.

### `get_filing(ticker_or_cik, accession_number)`

Document index for a single filing: primary doc plus every exhibit,
with direct URLs. Accepts accession numbers in either dashed
(`0000320193-24-000123`) or dashless (`000032019324000123`) form.

### `search_filings(query, form_types=None, date_start=None, date_end=None, limit=20)`

Full-text search over all EDGAR filings (via `efts.sec.gov`). Use
this when the user cares about *what a filing says* rather than which
filings a known company has made. Returns snippets plus accession
numbers — follow up with `get_filing` to pull a document list for any
hit.

### `get_insider_transactions(ticker_or_cik, since=None, limit=20)`

Parsed Form 4 (insider-transaction) filings for one **issuer**.
Fetches the issuer's recent Form 4s, pulls each primary XML, and
parses into structured transaction records.

Transaction codes worth knowing: `P` open-market purchase, `S`
open-market sale, `A` grant/award, `M` derivative exercise, `F` tax
withholding, `G` gift. Open-market `P`/`S` are the high-signal ones;
`A`/`F` are compensation plumbing.

### `get_institutional_portfolio(cik, accession_number=None)`

Parsed 13F `informationTable` for one institutional **filer** (the
manager, not the issuer of the stocks). Pass the manager's CIK
(e.g. `0001067983` = Berkshire Hathaway). If `accession_number` is
omitted, the most recent 13F-HR is used.

SEC requires 13F within 45 days of quarter-end, so the most recent
filing reflects a quarter that's at least 45 days old. `value_usd`
is in thousands of dollars for periods before 2022-09-30 and whole
dollars after — the response's `information_table.unit` field reports
which.

**Reverse lookup** (who holds ticker X?) is not shipped in v1 — it
would require an in-process index over every manager's filings.

### `get_company_facts(ticker_or_cik)`

Full XBRL `companyfacts` blob for one company. Large payload (several
MB for mega-caps) — prefer `get_company_concept` when you only need
one line item.

### `get_company_concept(ticker_or_cik, concept, taxonomy="us-gaap")`

One XBRL concept's reported values over time. Examples:

- `concept="Revenues"` — top-line revenue as tagged.
- `concept="NetIncomeLoss"` — net income.
- `concept="Assets"` — total assets.
- `concept="CashAndCashEquivalentsAtCarryingValue"` — cash.

**Concept names are not uniform across filers.** Some tag revenue as
`Revenues`, others as `SalesRevenueNet`, others as
`RevenueFromContractWithCustomerExcludingAssessedTax`. If a concept
isn't reported, EDGAR returns 404 and the tool raises. Fall back to
`get_company_facts` to see the filer's actual tags, or use `get_frame`
for cross-sectional queries.

### `get_frame(concept, period, taxonomy="us-gaap", unit="USD")`

Cross-sectional snapshot: one concept across all filers for one
period.

- Duration periods: `"CY2024Q4"` (Q4 '24), `"CY2024"` (full year).
- Instantaneous periods: append `I` — `"CY2024Q4I"` for
  balance-sheet concepts like `Assets`.
- `taxonomy`: `us-gaap` (default), `ifrs-full`, `dei`.

Useful for peer comps ("rank every filer by Revenues this quarter")
and macro aggregates. Not every concept/period combination is
populated.

## Setup

### 1. Pick a descriptive `User-Agent`

SEC Fair Access requires every request to carry a `User-Agent` with
your name/project and a contact email. No email = IP block. Use your
own details; do **not** copy a sample from SEC docs.

```
SEC_EDGAR_USER_AGENT=traider-hub you@example.com
```

### 2. Put it in `.env`

At the repo root:

```
SEC_EDGAR_USER_AGENT=traider-hub you@example.com
```

### 3. Install

```bash
conda activate traider
pip install -e ./mcp_servers/sec_edgar_connector
```

### 4. Run the server

```bash
sec-edgar-connector                                           # stdio
sec-edgar-connector --transport streamable-http --port 8768   # HTTP
```

Or via Docker (together with whichever backend is active), from the
repo root:

```bash
docker compose --profile sec-edgar up -d
```

## Connect your AI CLI

Same recipes as the rest of the hub; the
[hub README](../../README.md#connect-your-ai-cli) has the full
Claude Code / OpenCode / Gemini CLI examples. The HTTP endpoint is
`http://localhost:8768/mcp`.

## Prompts that put these tools to work

- **"What did AAPL say in their latest 10-Q?"** —
  `get_company_filings("AAPL", form_types=["10-Q"], limit=1)`, then
  `get_filing("AAPL", <accession>)` to pull the primary doc URL.
- **"Any insider selling at TSLA in the last 90 days?"** —
  `get_insider_transactions("TSLA", since=<90d ago>)`, filter the
  response for `transaction_code="S"`.
- **"What does Berkshire own as of their latest 13F?"** —
  `get_institutional_portfolio("0001067983")`.
- **"Show Apple's revenue trend from XBRL."** —
  `get_company_concept("AAPL", "Revenues")` — if 404, try
  `"SalesRevenueNet"` or `"RevenueFromContractWithCustomerExcludingAssessedTax"`.
- **"Rank S&P 500 names by Q4 '24 revenue."** —
  `get_frame("Revenues", "CY2024Q4")` (then filter to your universe).
- **"Any 8-Ks mentioning 'going concern' this month?"** —
  `search_filings('"going concern"', form_types=["8-K"], date_start=<30d ago>)`.

Pair these with `schwab_connector` / `yahoo_connector` prompts to
condition equity decisions on fundamental outliers — e.g. *"find
companies with a recent Form 4 buy > $1M, then pull their 1-year
`analyze_returns`."*

## Things worth knowing

- **10 req/sec rate limit.** Enforced client-side by a token bucket.
  SEC will IP-block at the network layer if you sustain overages. On
  429/403 the tool surfaces a `SecEdgarRateLimitError` — back off,
  don't retry-loop.
- **User-Agent is mandatory.** The client refuses to start if
  `SEC_EDGAR_USER_AGENT` is unset or missing an `@`. This is SEC
  policy, not an arbitrary choice.
- **Ticker map caches for 24h.** Every response that consults the
  ticker map includes `ticker_map_fetched_at` so you can see how
  fresh the lookup was.
- **Filings and company facts are not cached.** Always a fresh fetch
  — a stale filing cache is a trap. The `fetched_at` on every
  response reflects the actual network call.
- **Concept tagging varies.** When `get_company_concept` 404s, don't
  give up — try a close alias or pull `get_company_facts` to see the
  filer's actual `us-gaap` tags. This is an EDGAR data-quality
  artifact, not a bug in the server.
