# MCP_TODO.md — planned additions to the traider hub

Punch list of MCP servers to add, grouped by the gap each one fills.
All entries inherit the hub-wide rules in
[AGENTS.md](AGENTS.md): read-only, primary sources preferred, secrets
out of repo/logs, surface 429s, no silent fallbacks.

Status: `[ ]` todo · `[~]` in progress · `[x]` landed.

## Tier 1 — catalysts & fundamentals depth

- [x] **sec_edgar_connector** — shipped on port 8768. Filings
  (10-K/10-Q/8-K/20-F), Form 4 insider transactions per issuer, 13F
  institutional portfolios per manager, XBRL company facts + frames.
  Primary source: `data.sec.gov` / `efts.sec.gov`. Auth: descriptive
  `SEC_EDGAR_USER_AGENT` only. Rate limit: 10 req/sec enforced
  client-side. See
  [mcp_servers/sec_edgar_connector/AGENTS.md](mcp_servers/sec_edgar_connector/AGENTS.md).
  Deferred from v1: Form 4 insider-scoped queries, 13F reverse
  lookup (who holds X?), submissions-feed overflow fan-out for deep
  history.
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
- [x] **treasury_connector** — shipped on port 8772. Treasury Fiscal
  Data (`api.fiscaldata.treasury.gov`) auction results
  (bid-to-cover, stop-out yield, primary-dealer takedown,
  indirect/direct bidder share), Daily Treasury Statement (eight
  tables — operating cash balance / TGA, deposits+withdrawals,
  public-debt transactions, …), debt-to-the-penny. No credentials.
  Yield curve routes to `fred_connector` (H.15: DGS1MO…DGS30, DFII
  real yields) — not duplicated here. See
  [mcp_servers/treasury_connector/AGENTS.md](mcp_servers/treasury_connector/AGENTS.md).
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

- [x] **factor_connector** — shipped on port 8771. Fama-French 3/5-
  factor, momentum, short/long-term reversal, and 5/10/12/17/30/38/48/
  49-industry portfolios at monthly and (where published) daily
  frequencies. Primary source: `mba.tuck.dartmouth.edu/…/ken.french/ftp/`.
  No credentials. Disk-cached with a 24 h TTL (`refresh=True` to
  override per-call). See
  [mcp_servers/factor_connector/AGENTS.md](mcp_servers/factor_connector/AGENTS.md).
  `get_dataset(filename)` is the escape hatch for the ~300 datasets
  outside the curated catalog (sort-based portfolios, international
  regional factors, etc.).

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
| 8768 | sec-edgar                  | shipped |
| 8769 | earnings                   | planned |
| 8770 | news                       | planned |
| 8771 | factor                     | shipped |
| 8772 | treasury                   | shipped |
| …    | …                          | …       |

Claim the next free port when starting a new server. Update this
table in the same commit.
