# PROVIDER_TODO.md — planned additions to the traider hub

Punch list of MCP providers to add, grouped by the gap each one fills.
All entries inherit the hub-wide rules in
[AGENTS.md](AGENTS.md): read-only, primary sources preferred, secrets
out of repo/logs, surface 429s, no silent fallbacks.

Status: `[ ]` todo · `[~]` in progress · `[x]` landed.

## Tier 1 — catalysts & fundamentals depth

- [x] **sec-edgar** — shipped. Filings
  (10-K/10-Q/8-K/20-F), Form 4 insider transactions per issuer, 13F
  institutional portfolios per manager, XBRL company facts + frames.
  Primary source: `data.sec.gov` / `efts.sec.gov`. Auth: descriptive
  `SEC_EDGAR_USER_AGENT` only. Rate limit: 10 req/sec enforced
  client-side. See
  [src/traider/providers/sec_edgar/README.md](src/traider/providers/sec_edgar/README.md).
  Deferred from v1: Form 4 insider-scoped queries, 13F reverse
  lookup (who holds X?), submissions-feed overflow fan-out for deep
  history.
- [x] **earnings** — shipped as a provider module under the unified
  server. Wraps Finnhub's free-tier
  ``/calendar/earnings`` (forward + backward earnings calendar with
  consensus EPS / revenue) and ``/stock/earnings`` (per-ticker
  historical actual-vs-estimate surprises). Primary source:
  `finnhub.io/api/v1`. Auth: ``FINNHUB_API_KEY`` as
  ``X-Finnhub-Token`` header. Rate limit: 60 req/min, propagates as
  ``FinnhubError``. Free-tier coverage is US-only; international
  calendar is paid and deliberately not wired. Dev notes in
  [DEVELOPING.md § earnings](DEVELOPING.md#earnings).
  Deferred: consensus guidance breakouts (paid-tier on Finnhub),
  alternative sources (Zacks RSS, Nasdaq Data Link). Analyst
  estimates / revisions / price targets have graduated to the
  `estimates` entry below — they're a top-three gap on single-name
  analysis and deserve their own lane.
- [x] **news** — shipped. Wraps Massive's
  `/v2/reference/news` (ticker-scoped headlines with publisher
  metadata and per-article sentiment insights). Primary source:
  `api.massive.com`. Auth: `MASSIVE_API_KEY` as `apiKey` query param.
  Rate limits propagate as `MassiveError` (no silent retries).
  Intentionally narrow: Massive's quote / aggregate endpoints are
  *not* wrapped — quotes stay on the market-data backends. See
  [src/traider/providers/news/README.md](src/traider/providers/news/README.md).
  Sentiment is Massive's model output; quote it with attribution.
- [~] **estimates** — Partial. Shipped as a provider module wrapping
  Finnhub's free-tier ``/stock/recommendation`` (monthly sell-side
  rating distribution: strong-buy / buy / hold / sell / strong-sell
  counts per ticker). Reuses the ``FINNHUB_API_KEY`` from the
  ``earnings`` provider — one key, both providers, shared
  60 req/min budget. See
  [src/traider/providers/estimates/README.md](src/traider/providers/estimates/README.md).

  Still gapped (all premium on Finnhub, return 403 on the free
  key): **price targets** (``/stock/price-target``),
  **upgrade/downgrade actions** (``/stock/upgrade-downgrade``),
  **consensus EPS / revenue estimates** (``/stock/eps-estimate``,
  ``/stock/revenue-estimate``). Rating-revision breadth is covered
  by the shipped endpoint; EPS-revision breadth is not. Upgrading
  the Finnhub plan is the cleanest way to close these gaps — each
  paid endpoint is a one-method extension of the existing
  ``finnhub_client.py``. Alternative sources if Finnhub never
  upgrades: Benzinga ratings API (paid), Tiingo (free tier has
  consensus estimates), Nasdaq Data Link / ZEE (paid). Zacks was
  evaluated and rejected: the public site does not expose
  per-ticker RSS feeds for estimates data — "Zacks RSS" in the
  wild is third-party scraping, not a primary source.
- [ ] **transcripts** — Earnings call transcripts (prepared remarks
  + Q&A) for management tone, guidance language, and cross-quarter
  diffs. No clean free primary source: AlphaSense and Seeking
  Alpha are paid / ToS-restricted; IR webcasts need auto-
  transcription. Defer until the user asks for it — the lift isn't
  justified without pull.

## Tier 2 — macro completion

- [ ] **bls** — BLS direct (CPI, NFP, JOLTS). FRED mirrors these but
  BLS is the primary publisher and releases a few minutes earlier.
  Worth it for release-day precision. Primary source:
  `api.bls.gov/publicAPI/v2`. Auth: optional `BLS_API_KEY` (raises
  daily quota from 25 to 500 series/day).
- [ ] **bea** — BEA direct (GDP components, personal income, trade
  balance). Same rationale as BLS. Primary source:
  `apps.bea.gov/api/data`. Auth: mandatory `BEA_API_KEY` (free,
  email-issued).
- [x] **treasury** — shipped. Treasury Fiscal Data
  (`api.fiscaldata.treasury.gov`) auction results (bid-to-cover,
  stop-out yield, primary-dealer takedown, indirect/direct bidder
  share), Daily Treasury Statement (eight tables — operating cash
  balance / TGA, deposits+withdrawals, public-debt transactions, …),
  debt-to-the-penny. No credentials. Yield curve routes to the `fred`
  provider (H.15: DGS1MO…DGS30, DFII real yields) — not duplicated
  here. See
  [src/traider/providers/treasury/README.md](src/traider/providers/treasury/README.md).
- [ ] **eia** — US Energy Information Administration: weekly
  petroleum status, natural gas storage, electricity. Critical for
  energy-name trades. Primary source: `api.eia.gov/v2`. Auth:
  mandatory `EIA_API_KEY` (free).
- [ ] **global-cb** — ECB (`data.ecb.europa.eu`, the new Data
  Portal that superseded SDW), BoJ, BoE statistical releases. Per
  hub rule: land one central bank at a time, each as its own module
  with its own primary-source client.
- [ ] **credit** — High-yield and investment-grade OAS, CDX IG /
  HY series, single-name CDS where licensing allows. Today only the
  FRED mirrors are reachable (BAMLH0A0HYM2, BAMLC0A0CM, etc.); a
  dedicated credit provider would add term structure and issuer-
  level data. Candidates: FINRA TRACE (corporate bond prints, free
  but heavy normalization), S&P Global / IHS Markit iTraxx / CDX
  (paid), ICE BofA via FRED (partial). Priority because risk-off
  regimes usually show up in credit spreads before equities.
- [ ] **commodities** — Futures prices and continuous-contract
  series (WTI / Brent, natgas, gold / silver, copper, grains),
  forward curves where published. Candidates: Yahoo `=F` tickers
  (free, approximate, 80% case), Nasdaq Data Link continuous
  contracts, CME Group streaming API (paid, precision). EIA already
  covers petroleum and natgas storage on the inventory side.
- [ ] **fx** — Spot FX pairs, forward points, carry / rate
  differentials, DXY constituent tracking beyond FRED's daily
  cadence. Candidates: ECB reference rates (free, EUR-centric,
  EOD), HistData (free, EOD), Polygon FX (paid intraday), Schwab
  FX feed if the existing account surfaces it.

## Tier 3 — positioning & flow

- [ ] **cboe** — The vol complex: put/call ratios, VIX term
  structure (VIX1D / VIX9D / VIX / VIX3M / VIX6M for
  contango-backwardation calls), VVIX, SKEW, IV surfaces, total
  options volume. MOVE (ICE BofA bond vol) is a different publisher
  but fits the same module. Fills the gap left by static option
  chains on the market-data backends.
- [ ] **options-flow** — Unusual options activity, dealer gamma
  exposure (GEX / zero-gamma level), max-pain per expiry, dark-pool
  prints. Complements static chains with positioning intelligence —
  *"what is flow telling us?"* is a standard analyst question the
  current toolset can't answer. Candidates: SpotGamma, Unusual
  Whales, CBOE DataShop, SqueezeMetrics (all paid). No viable free
  primary source — gate behind a paid key. Split from `cboe`
  because the data shape (positioning inference) and vendors are
  different.
- [ ] **finra** — short interest (bi-monthly), short sale volume
  (daily), ATS volume. Days-to-cover and utilization are load-
  bearing for squeeze framing and any short-side risk assessment.
- [ ] **etf-flows** — ETF holdings, creations/redemptions, sector
  rotation signal. Candidates: ICI weekly flow reports (free, fund-
  category level only), issuer feeds (iShares, SPDR, Vanguard —
  free daily holdings as CSV/JSON), ETF.com and ETFdb (scraping,
  ToS-restricted). No single clean source; expect per-issuer
  adapters.
- [ ] **cftc** — Commitments of Traders (futures positioning by
  trader class). Weekly release. Primary source:
  `publicreporting.cftc.gov` (Socrata API, no auth required). Free.
- [ ] **corporate-actions** — Historical splits, dividend payments
  (regular + special), spin-offs, symbol / name changes, M&A
  timelines. Required to (a) corporate-action-adjust historical
  price series for clean return math, and (b) flag upcoming
  ex-dividend drops the user shouldn't read as signal. 8-K filings
  are already reachable via `sec-edgar` but need parsing; cleaner
  primary sources include Nasdaq's dividend calendar (free RSS) and
  Polygon's corporate-actions feed (paid). Schwab's transaction
  feed partially covers owned names but not the broad market.

## Tier 4 — risk, factor, account analytics

- [x] **factor** — shipped. Fama-French 3/5-factor,
  momentum, short/long-term reversal, and 5/10/12/17/30/38/48/49-
  industry portfolios at monthly and (where published) daily
  frequencies. Primary source: `mba.tuck.dartmouth.edu/…/ken.french/ftp/`.
  No credentials. Disk-cached with a 24 h TTL (`refresh=True` to
  override per-call). See
  [src/traider/providers/factor/README.md](src/traider/providers/factor/README.md).
  `get_dataset(filename)` is the escape hatch for the ~300 datasets
  outside the curated catalog (sort-based portfolios, international
  regional factors, etc.).
- [ ] **tax-lots** — *Analytics over existing Schwab data, not a
  new external provider.* Lot-level P&L, STCG vs LTCG classification,
  30-day wash-sale detection (both pre- and post-sale windows),
  tax-aware sell sequencing (HIFO / LIFO / specific-ID simulation),
  and options exercise / assignment lineage. Uniquely load-bearing
  for the user's TOD taxable Schwab account — every sell
  recommendation has a tax shape the existing tools can't see.
  Builds on `get_transactions` history; needs careful handling of
  corporate actions (depends on `corporate-actions` above for
  clean lot bases) and options lifecycle events.

## Tier 5 — alt data (lower priority)

- [ ] **trends** — Google Trends interest-over-time for ticker- or
  theme-level attention signals. No official API; the only paths
  are unofficial wrappers (`pytrends`) or scraping, both with ToS
  friction and aggressive rate limiting. Treat as best-effort, not
  a primary source.
- [ ] **social** — Reddit (`reddit.com/.json`, heavily rate-limited
  since the 2023 API changes — pushshift is no longer publicly
  available), X (paid tier only post-2023), StockTwits (free REST,
  the cleanest free option). Sentiment is downstream of the data
  fetch — keep this provider to fetching, not scoring.
- [ ] **crypto** — CoinGecko (unauthenticated) or Binance public
  endpoints. Only add if the user starts asking crypto questions.

