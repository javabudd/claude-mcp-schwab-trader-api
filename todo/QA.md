# todo/QA.md — outstanding QA findings for the traider hub

Static-only QA pass on the repo (no runtime). Findings carry file:line
refs and severity. Every entry inherits the hub-wide rules in
[AGENTS.md](../AGENTS.md): read-only, primary sources preferred,
secrets out of repo/logs, surface 429s, no silent fallbacks, no
fabricated numbers.

Status: `[ ]` todo · `[~]` in progress · `[x]` resolved.

## Triage order

1. ~~#1 envelope rollout across `schwab` / `yahoo` / `news` / `fred`~~
   (resolved). ~~#2, #3, #4 silent fallbacks~~ (resolved).
   ~~#5 13F unit boundary~~ (resolved). ~~#6 fed-calendar layout
   drift~~ (resolved). All silent-fallback findings are now closed.
2. Security tightening: #7, #8.
3. Land `tests/` + CI; start with parser fixtures (Form 4 XML, 13F
   XML, FOMC HTML snapshot, Ken French CSV block) since those are
   most prone to silent breakage.

## CRITICAL

- [x] **#1 — `source` / `fetched_at` envelope is inconsistently
  applied.** DEVELOPING.md:255-257 makes it a hub convention;
  `factor`, `sec_edgar`, `earnings`, `estimates` honor it; most of
  `schwab`, `yahoo`, `news`, and `fred` don't. A `get_quote` or
  `get_news` response can't be cited per the AGENTS.md "cite tool +
  timestamp for every number" rule.
  - `schwab/tools.py`: `get_quote` (~68), `get_quotes` (~139),
    `get_price_history` (~189), `get_option_chain` (~344),
    `get_option_expirations` (~475), `get_movers` (~492),
    `search_instruments` (~517), and all account / transaction /
    order tools — none stamp source/fetched_at.
  - `yahoo/tools.py`: same set (lines ~69, 91, 109, 213, 335, 353,
    376).
  - `news/tools.py:83` — returns Massive's raw envelope only.
  - `fred/tools.py` — none of the @mcp.tool functions stamp it.
  - **Resolved:** every `@mcp.tool` in `schwab`, `yahoo`, `news`,
    and `fred` now stamps `source` (upstream URL) and `fetched_at`
    (ISO-8601 UTC) at the top level of its response. Scalar /
    list-returning tools (`get_quote`, `get_quotes`, `get_accounts`,
    `get_account_numbers`, `get_transactions`, `get_orders`) were
    rewrapped in dicts; the per-tool docstrings call out the new
    shape. The `source` key in `analytics.rolling_zscore` output was
    renamed to `series_source` to avoid colliding with the envelope
    URL.

- [x] **#2 — Yahoo silent-fallback on `.info` fetch failure.**
  `yahoo/yahoo_client.py:488-490`. Exception during `_quote_payload`
  is caught and the dict left empty, so `get_quote` returns `null`
  fields instead of raising. Direct violation of AGENTS.md "no
  silent fallbacks that change the numbers" / "no fabricated
  numbers."
  - **Resolved:** `_quote_payload` now re-raises as a new
    `YahooDataError` when `ticker.info` fails, with the original
    exception chained. The tool layer (`yahoo/tools.py:99-101`,
    `126-128`) already re-raises, so the failure surfaces to the
    caller instead of being papered over with a `None`-valued
    quote. `get_quotes` aborts the batch on the first bad symbol
    rather than silently dropping it — same "say so and stop"
    posture AGENTS.md takes on rate limits.

- [x] **#3 — Yahoo silent-drop of failed option expirations.**
  `yahoo/yahoo_client.py:336-338`. When `ticker.option_chain(d)`
  raises mid-loop, the bad expiration is logged-and-`continue`'d.
  Caller gets a partial chain with no `dataQualityWarning` field
  flagging the dropped slices.
  - **Resolved:** the per-expiration loop in `get_option_chain` now
    raises `YahooDataError` on the first failed expiration with the
    original exception chained, matching the posture taken for #2's
    `_quote_payload` / `get_quotes` fix. The tool layer
    (`yahoo/tools.py:307-309`) already re-raises, so the failure
    surfaces to the caller instead of being papered over with a
    partial `callExpDateMap` / `putExpDateMap` and a misleading
    `numberOfContracts`.

- [x] **#4 — SEC Form 4 silent-skip on parse failure.**
  `sec_edgar/tools.py:395-409`. `parse()` failures are logged and
  the filing is dropped from the response. The summary count
  silently shrinks; caller can't tell whether a CEO had no recent
  trades or whether 3/20 Form 4s failed to parse.
  - **Resolved:** the per-row loop in `get_insider_transactions`
    now raises `SecEdgarError` on the first failed Form 4 fetch /
    parse with the original exception chained, matching the
    posture taken for #2 and #3. The error message names the CIK,
    accession number, and XML document so the caller can pinpoint
    the offending filing. Callers no longer get a misleadingly
    small `count` that conflates "no recent insider activity"
    with "some filings didn't parse."

- [x] **#5 — 13F value-unit boundary is off-by-month.**
  `sec_edgar/form13f_parser.py:119`. The check is
  `(year, month) >= (2022, 9)` for the "≥ 2022-09-30" cutoff. A
  filing with `period_of_report=2022-09-15` (still thousands per
  SEC) is mis-tagged as `dollars`, blowing up summed positions by
  1000×. Only finding that materially distorts numbers.
  - **Resolved:** `_infer_unit` now parses `period_of_report` with
    `date.fromisoformat` and compares against a named
    `_DOLLARS_CUTOFF = date(2022, 9, 30)` constant, so any period
    strictly before 2022-09-30 (including 2022-09-15) returns
    `thousands_of_dollars`. Malformed dates still degrade to
    `dollars_or_thousands_unknown`. Only caller
    (`get_institutional_portfolio` in `tools.py:496`) feeds the
    EDGAR `reportDate` field, which is ISO `YYYY-MM-DD`.

- [x] **#6 — fed-calendar swallows layout drift.**
  `fed_calendar/fomc_scraper.py:137-152`. If federalreserve.gov
  reshapes the panels, the scraper logs a warning and returns
  `{"count": 0, "meetings": []}`. DEVELOPING.md is explicit: "Don't
  paper over with fuzzy fallbacks." Empty result needs to be a
  raise, not a warn.
  - **Resolved:** every layout-drift path in `parse` / `_parse_row`
    now raises `FomcScrapeError` with the original exception
    chained where applicable. A panel whose heading no longer
    yields a year, a meeting row missing the `__month` or `__date`
    cell, and a malformed day/month combo (the previous
    log-warn-and-return-`None` path) all surface as raises instead
    of silent skips. Final defense: `parse` raises if the page
    parsed cleanly but produced zero meetings, since the FOMC
    calendar is never genuinely meeting-less. The unused module
    logger was removed. The legitimate "(unscheduled)" /
    parenthetical-only rows still skip — those are data, not
    layout drift, and `_parse_row` distinguishes them via the
    `_DATE_RE` "no `days` group" branch.

## HIGH

- [x] **#7 — OAuth token file has a permission TOCTOU window on
  first write.** `schwab/auth.py:95-101` writes the file at default
  umask, then chmods to 0600. Tokens are world-readable for the
  milliseconds in between. `schwab_client.py:528-538` does this
  correctly via `tmp + os.replace + chmod`; `auth.py` does not. Use
  the same pattern.
  - **Resolved:** `run_auth_flow` now writes to a sibling `.tmp`
    file and `os.replace`s it onto the canonical token path before
    chmod, matching the `_save_tokens` pattern in `schwab_client.py`.
    The canonical token path is no longer briefly world-readable on
    the first write — the well-known path only ever sees the
    finished file via the atomic rename, and the chmod follow-up
    keeps parity with the rest of the client (including the
    Windows-filesystem fallback comment).

- [x] **#8 — MCP server defaults: `0.0.0.0` + DNS-rebinding
  protection disabled.** `server.py:55,115-116`. The combination
  exposes the unauthenticated tool surface to any host on the
  LAN/Wi-Fi and disables Host-header validation that mitigates
  DNS-rebinding from a malicious page in the user's browser. README
  does not warn about this. Either default `--host 127.0.0.1` (and
  document `0.0.0.0` for Docker), or re-enable rebinding protection,
  or both.
  - **Resolved:** both. `--host` now defaults to `127.0.0.1`
    (loopback only), and the explicit
    `enable_dns_rebinding_protection=False` in `_build_mcp` is gone
    — replaced by `_build_transport_security(...)`, which builds a
    `TransportSecuritySettings` with protection on and `localhost`
    / `127.0.0.1` / `[::1]` at the configured `--port` allowlisted
    by default. `--allow-host` and `--allow-origin` (both
    repeatable) extend the allowlists for LAN / reverse-proxy
    deployments. The Dockerfile's `CMD` keeps `--host 0.0.0.0`
    (it has to, to be reachable through the port forward), and the
    same `localhost:8765` Host header still works because the
    rebinding allowlist is parametrized on port, not bind address.
    README's *On the host* section now documents both the new
    default and the `--allow-host` / `--allow-origin` escape hatch
    for non-localhost hostnames; without the extra flag the
    middleware 421s every non-loopback Host header — the rebinding
    defense behaving as intended.

- [x] **#9 — Schwab `get_price_history` builds an invalid request
  when only one of `start_date` / `end_date` is set.**
  `schwab/schwab_client.py:156-162`. The `else: params["period"] =
  period` is in the else branch of `if start_date or end_date`, so
  passing just `start_date=...` sends `startDate` with no `endDate`
  and no `period`. Schwab rejects that combination with a terse 400.
  Either require both dates together or fall back to `period`.
  - **Resolved:** the date-window branch now defaults `endDate` to
    `now` when only `start_date` is given (matching the tool-layer
    docstring contract at `tools.py:261-262` — *"Defaults to now
    when only start_date is given"* — which the client previously
    didn't honor) and raises `ValueError` when only `end_date` is
    given (an undocumented combination Schwab rejects). Both-dates
    and neither-date paths are unchanged.

- [ ] **#10 — CIK is passed `int(cik)` (zero-padding stripped) when
  building Archive URLs.** `sec_edgar/edgar_client.py:275,286`;
  `tools.py:78,263`. Inconsistent with the 10-digit normalization
  the rest of the client enforces. Works today because EDGAR's
  archive routing tolerates it; will break the day SEC tightens.

- [x] **#11 — Finnhub clients don't distinguish 403 from generic
  4xx.** `earnings/finnhub_client.py:72-76`,
  `estimates/finnhub_client.py:67-71`. Premium endpoints return
  403; a future maintainer wiring one would see a generic
  `FinnhubError` instead of a clear "premium plan required"
  surface. Estimates' README/docstring is very firm about this —
  code should match.
  - **Resolved:** both clients now define
    `FinnhubPremiumRequiredError(FinnhubError)` and the `_get`
    helper checks `status_code == 403` *before* the generic
    `>= 400` branch, so a 403 surfaces with an explicit "premium
    plan required for this endpoint" message naming the path.
    Subclassing `FinnhubError` keeps existing `except FinnhubError`
    handlers (and the tool-layer re-raises in `earnings/tools.py`
    / `estimates/tools.py`) catching the new type unchanged. Module
    docstrings updated on both sides to reference the new subclass;
    the estimates docstring also calls out that this matches the
    README's firm stance on the premium-only gap.

- [ ] **#12 — Zero tests, zero CI.** No `tests/`, no
  `.github/workflows/`, no lint or typecheck config. For a tool
  whose purpose is "the model trusts these numbers," the parsers
  (`form4_parser`, `form13f_parser`, `fomc_scraper`,
  `french_client` CSV blocks) are the highest-value place to land
  regression tests, and currently none exist.

## MEDIUM

- [x] **#13 — `schwab/options_summary.py` and
  `yahoo/options_summary.py` are byte-for-byte identical** (`cmp`
  confirmed). DEVELOPING.md:113-115 calls out
  `ta.py` / `analytics.py` duplication as intentional, but
  `options_summary.py` is a third duplicated file the doc doesn't
  mention, and unlike the others it isn't even tweaked at the
  docstring level. Either cite it explicitly in the duplication
  note or extract to `traider.providers._shared.options_summary`
  (no cross-provider import — both still import from `_shared`).
  - **Resolved:** lifted to a top-level
    `src/traider/options/summary.py`, mirroring the
    `src/traider/ohlcv/` precedent (commit 8a9a1d5 — "single source
    of truth" for cross-provider helpers, not
    `providers/_shared/`). Both providers now import via
    `from ...options.summary import summarize_chain`; the per-
    provider `options_summary.py` files are deleted. Module
    docstring is now provider-agnostic ("canonical chain payload
    every market-data backend emits") to match its new home.
    DEVELOPING.md updates: package-layout block adds the
    `options/` entry, the import contract paragraph names
    `options` alongside `ohlcv` / `logging_utils` / `settings`,
    and the *Shared modules* section gains an
    [options (chain summary)](../DEVELOPING.md#options-chain-summary)
    subsection covering the shape contract and the same "don't
    fork per provider, normalize at the boundary" rule the
    `ohlcv` section already enforces.

- [x] **#14 — DEVELOPING.md package-layout block (~lines 51-108) is
  missing files** that exist on disk: `schwab/options_summary.py`,
  `yahoo/options_summary.py`, `fred/analytics.py`. Stale.
  - **Resolved:** the two `options_summary.py` paths went away with
    #13 (lifted to `src/traider/options/summary.py`, per-provider
    copies deleted), so they no longer need a layout entry.
    `fred/analytics.py` is now listed under the `fred/` block as
    *"pure-numpy macro analytics over FRED observation series"*.
    Two additional stale entries surfaced during the audit and were
    fixed in the same pass: `intent/account_profile.py` (YAML loader
    for user-authored account framing — age, role, risk capacity)
    and `intent/rules.py` (YAML rules loader, seed `rules/` +
    per-user `rules.local/` overlay), both of which landed after the
    QA pass was written and were missing from the `intent/` block.

- [ ] **#15 — Form 4 `_bool` / `_float` silent coercion.**
  `sec_edgar/form4_parser.py:173-186`. Malformed booleans become
  `False`; malformed floats become `None`. Per AGENTS.md "no
  fabricated numbers," coercion failure should raise (or at minimum
  surface a per-row parse-warning array on the envelope).

- [x] **#16 — `_pick_information_table` swallows 404 from
  `filing_index`.** `sec_edgar/tools.py:132-133`. The caller
  (`get_institutional_portfolio`) then reports a parser error
  instead of the real 404 / network failure.
  - **Resolved:** the `try / except SecEdgarError: return None`
    around `filing_index(...)` is gone — 404, network failure, and
    rate-limit errors now propagate to `get_institutional_portfolio`,
    whose existing `try / except Exception` block logs and re-raises,
    so callers see the real cause. The fall-through `return None`
    after scanning `directory.item` is preserved (and now the only
    `None` path), so the caller's "no informationTable XML in
    {accession}" raise stays correct for the case it actually
    describes — an index that was fetched cleanly but contains no
    matching XML name. Docstring updated to spell out the new
    contract.

- [x] **#17 — fred analytics output mixes derived classifications
  with raw fields.** `fred/tools.py:161-297` and `analytics.py`.
  AGENTS.md "distinguish tool output from your inference" applies on
  the model side, but provider responses also blend derived
  `regime`, `curve_shape`, `alignment` with upstream-shaped keys.
  Either nest under `derived: {...}` or stamp the keys.
  - **Resolved:** all interpretive labels are now nested under a
    `derived` key — top-level on `analyze_yield_curve` (`curve_shape`),
    `analyze_credit_spreads` (`regime`), and `analyze_macro_regime`
    (`regime`, `component_labels`); per-series on `analyze_breakevens`
    (`alignment`, `deviation_from_target`) and
    `analyze_financial_conditions` (`regime`). The
    `analyze_macro_regime` internal lookups (`curve["curve_shape"]`,
    `credit["regime"]`, `breakevens["series"][...]["alignment"]`,
    `fin_cond["series"][...]["regime"]`) were moved to the new
    `derived.*` paths in lockstep. Per-slope `inverted` booleans on
    `analyze_yield_curve` slopes stay where they are — single sign
    check, not a thresholded label. Tool docstrings and
    `fred/README.md` field-path callouts updated to match.

- [ ] **#18 — `auth.py` HTTP timeout 10s vs `schwab_client.py`
  30s.** `schwab/auth.py:70`. Slow-network users hit a confusing
  timeout on the most fragile path (one-shot OAuth). Align to 30.

- [ ] **#19 — Treasury monetary fields are strings — not enforced
  anywhere.** DEVELOPING.md is explicit, `treasury_client.py`
  doesn't cast, but the tool-layer doesn't assert it either. If a
  future refactor adds `float(...)` to "tidy up" the response, the
  precision-preservation guarantee is silently lost. A guard or a
  comment at the boundary would help.

- [ ] **#20 — `server.py` startup order.** `_validate_providers`
  (line 131) runs *after* `_configure_root_logging` (120) and after
  the empty-providers warn (126-129). A user with
  `TRAIDER_PROVIDERS=foo,bar` still gets log files created and the
  warning printed before the SystemExit fires. Move validation
  right after `load_settings()`.

- [ ] **#21 — `pyproject.toml` declares
  `requires-python = ">=3.11"` but DEVELOPING.md and the Dockerfile
  pin Python 3.13.** With no CI matrix, 3.11 / 3.12 are untested
  floors. Either tighten to `>=3.13` or add a test job.

## LOW

- [ ] **#22 — Stale logs in `logs/`.** `*-connector.log` files
  (root-owned, dated April 19) are from a previous logger-name
  scheme; commit f5ba823 ("Fix logger names") changed the
  convention. Files are gitignored but still on disk. Cleanup
  commit + a `.gitkeep` so the dir survives a `rm logs/*` would be
  tidier.

- [ ] **#23 — fed-calendar two-month meeting else-branch is
  identical to the if-branch.** `fed_calendar/fomc_scraper.py:188-197`.
  Works correctly; just confusing — the `else` was probably meant
  to anchor `start_dt` and `end_dt` both to `start_month` for
  single-month meetings. Either collapse the two branches or fix
  the `else` to match the comment.

- [ ] **#24 — fed-calendar `fetched_at` reflects response time, not
  scrape time.** `fed_calendar/tools.py:83,113`. Differs from
  `factor/tools.py` (which stamps the upstream fetch). Minor
  consistency bug.

- [ ] **#25 — ticker_map TTL uses `time.monotonic()` but
  `_fetched_at_iso` uses `datetime.now(...)`.**
  `sec_edgar/ticker_map.py`. Backward clock jump can put
  `fetched_at` *before* the previous fetch. Cosmetic.

- [ ] **#26 — Treasury logger name inconsistency.**
  `treasury_client.py:27` uses `"traider.treasury.fiscal"` while
  every other provider's client uses `"traider.<name>.client"` or
  just `"traider.<name>"`.

- [ ] **#27 — `__main__._run_auth` parses `sys.argv[2:]`
  positionally** — extra args are silently ignored
  (`traider auth schwab --foo` runs without complaint). Not a real
  risk; consider `argparse` for the auth subcommand for parity with
  the server one.

## Verified clean

- `.env` not tracked (gitignore + `git ls-files` checked); no
  secrets in repo.
- `compileall` on `src/` — no syntax errors.
- TokenBucket in `edgar_client.py` is correct (lock around refill,
  sleep when starved, no double-spend).
- `attach_provider_logger` is idempotent — safe to call repeatedly.
- Read-only invariant: no `POST` / `DELETE` / `PUT` to brokerage
  anywhere; auth flow only writes the local token file.
- API-key handling: keys live in env or headers, never echoed in
  responses or `logger.info(...)` calls reviewed.
- 429 propagation: every client raises rather than retry-looping.
- Schwab / Yahoo `attach_provider_logger` names match
  `settings.log_file("schwab")` / `"yahoo"`.
