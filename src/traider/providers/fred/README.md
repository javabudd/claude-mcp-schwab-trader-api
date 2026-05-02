# fred provider

Read-only [FRED](https://fred.stlouisfed.org) bridge. One of the
provider modules bundled in the unified
[`traider`](../../../../README.md) MCP server. See the root
[AGENTS.md](../../../../AGENTS.md) for hub-wide analyst rules and
[DEVELOPING.md § fred](../../../../DEVELOPING.md#fred) for dev
internals.

This provider exposes macro / economic-release data — release
calendars, raw series, and a handful of derived "regime" snapshots
(yield curve, credit spreads, breakevens, financial conditions,
overall macro). Pair with `schwab` / `yahoo` to condition equity
decisions on the macro calendar or a regime read.

## Tools

All tools are **read-only**. Series-data and calendar tools return
FRED's JSON essentially unchanged inside a `source` / `fetched_at`
envelope; the `analyze_*` tools layer derived classifications on top
of raw observations.

### Calendar and metadata

#### `get_release_schedule(...)`

Economic-release calendar, **filtered server-side**. Defaults to a
forward-looking window (`realtime_start` = today UTC) so you're not
dragging down years of history to find next week's CPI print.

- `realtime_start` / `realtime_end` — ISO `YYYY-MM-DD`. Override the
  default (today → FRED's horizon) when you want history.
- `release_ids` — fan out to FRED's per-release endpoint once per
  id and merge. Cleanest way to cut noise when you already know the
  handful of releases you care about. See `list_releases` for ids.
- `name_contains` — list of substrings, OR'd together,
  case-insensitive match on `release_name`. Useful when you know
  the name but not the id (e.g. `["Consumer Price",
  "Personal Income", "Employment"]`).
- `include_empty=True` keeps scheduled future dates that don't yet
  carry values — that's how a forward-looking calendar finds
  upcoming prints.
- `dedupe=True` drops duplicate `(date, release_id)` rows (FRED
  sometimes emits near-duplicates).
- `limit`, `sort_order` — standard FRED knobs.

For **FOMC meeting dates** specifically, use the
[`fed-calendar` provider](../fed_calendar/README.md)'s
`get_fomc_meetings` — FRED's release 101 ("FOMC Press Release")
fires on every day of the meeting window, which is too noisy to be
useful.

#### `get_high_impact_calendar(...)`

Curated shortcut over `get_release_schedule` — pre-wired with the
release IDs a trader actually cares about (CPI, PCE, PPI, NFP,
JOLTS, GDP, Retail Sales) and a `category` annotation on each row.

- `categories` — subset of `inflation`, `labor`, `growth`,
  `consumer`; `None` = all.
- Other params match `get_release_schedule`.
- **Does not cover FOMC** — see `get_fomc_meetings` on the
  `fed-calendar` provider. For anything outside the curated list,
  fall back to `get_release_schedule` with your own `release_ids`
  or `name_contains`.

#### `get_release_dates(release_id, ...)`

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

#### `list_releases(limit=200)`

All FRED releases. Use this to discover `release_id` values.

#### `get_release_info(release_id)`

Metadata for one release — name, press-release URL, notes.

#### `get_release_series(release_id, ...)`

Series that live under a release (e.g. CPI headline, core CPI, and
every component).

#### `search_series(search_text, ...)`

Fuzzy search over series IDs and titles. Examples:

- `"core CPI"` → `CPILFESL`
- `"10-year treasury"` → `DGS10`
- `"fed funds"` → `FEDFUNDS` / `DFF`
- `"unemployment rate"` → `UNRATE`

#### `get_series_info(series_id)`

Metadata for one series: units, frequency, seasonal adjustment,
last-updated timestamp.

### Series data

#### `get_series(series_id, ...)`

The actual time-series observations. `units` can do server-side
transforms:

- `lin` (default) — levels
- `chg` — change, `ch1` — year-over-year change
- `pch` — % change, `pc1` — YoY % change, `pca` — % change
  annualized
- `log` — natural log

`frequency` + `aggregation_method` resample on the server (`m`,
`q`, `a` with `avg` / `sum` / `eop`).

### Derived "regime" snapshots

These tools pull a small basket of canonical FRED series, compute
deltas / z-scores / percentiles against a trailing window, and
attach a labelled regime tag. Useful as a one-call read on whether
a given dimension is in stress.

Per AGENTS.md, derived classifications are model output, not primary
data — quote them as such. Each response includes the raw component
series alongside the derived label so the user can see the inputs.
All interpretive labels (`regime`, `curve_shape`, `alignment`, etc.)
are nested under a `derived` key — top-level on single-classifier
tools, per-series on the multi-series ones — so callers can tell
provider-side classifications apart from raw / statistical fields.

#### `analyze_yield_curve(observation_start=None, zscore_window=504)`

Yield-curve regime snapshot from FRED H.15 (`DGS3MO`, `DGS2`,
`DGS10`, `DGS30`). Per tenor and per slope (2s10s, 3m10y, 2s30s):
latest value + date, 1m / 3m / 6m / 1y deltas, rolling z-score and
percentile vs the trailing `zscore_window` observations (default
504 ≈ 2y of daily). Slopes carry an `inverted` boolean.
`derived.curve_shape` labels the setup `normal` / `flat` /
`partially_inverted` / `inverted`.

#### `analyze_credit_spreads(observation_start=None, zscore_window=504)`

US corporate credit spreads — ICE BofA option-adjusted spread
indices `BAMLH0A0HYM2` (US High Yield) and `BAMLC0A0CM` (US
Corporate / IG). Per series: latest, 1m/3m/6m/1y deltas, z-score /
percentile vs `zscore_window`. `derived.regime` is derived from
the worse of the two z-scores (z<-1 `tight`, -1..1 `normal`, 1..2
`wide`, ≥2 `stressed`) — the provider deliberately over-flags
stress rather than under-flag it.

#### `analyze_breakevens(observation_start=None, zscore_window=504, target=2.0, target_band=0.25)`

Market-implied inflation expectations vs the Fed's 2% target.
Pulls `T5YIE`, `T10YIE`, `T5YIFR` and returns per tenor: latest,
1m/3m/6m/1y deltas, z-score, plus a per-tenor `derived` block with
an `alignment` label (`below_target` / `near_target` /
`above_target`) and `deviation_from_target` in percentage points. Note: the Fed's 2%
target is for PCE inflation, not breakevens — breakevens carry an
inflation risk premium typically 20-50bp above expected inflation,
which `target_band` absorbs.

#### `analyze_financial_conditions(observation_start=None, zscore_window=504)`

Chicago Fed financial-conditions indices: NFCI (raw read of
financial tightness vs the 1971-present average) and ANFCI
(cycle-adjusted — positive ANFCI flags stress beyond what the
cycle would justify). Per series: latest, deltas, z-score, and a
per-series `derived.regime` label (`loose` / `normal` / `tight` /
`stressed`). Both series are weekly, released Wednesdays.

#### `analyze_macro_regime(observation_start=None, zscore_window=504, breakeven_target=2.0, breakeven_band=0.25)`

One-call synthesis. Internally runs `analyze_yield_curve`,
`analyze_credit_spreads`, `analyze_breakevens`, and
`analyze_financial_conditions`, then rolls the components into a
single `derived.regime` label (`risk_on` / `neutral` / `risk_off`
/ `stressed`). The aggregate uses **NFCI** (absolute financial
tightness) for the risk-on/off read; ANFCI is surfaced as a
secondary component.

## Setup

1. Register a free FRED API key at
   <https://fredaccount.stlouisfed.org/apikeys>. There's only one
   tier — no rate-tier shopping.
2. In `.env`: `FRED_API_KEY=your-key-here`
3. Add `fred` to `TRAIDER_PROVIDERS`.
4. Start the hub as normal — no separate port. Tools are exposed on
   the shared endpoint at `http://localhost:8765/mcp`.

## Coverage and limits

- **Rate limit.** 120 requests per 60s per key. The tool surfaces
  FRED's 429 as a `FredError`; back off, don't retry-loop.
- **Realtime vs. observation.** The release calendar is *realtime*-
  dated (when was the value published); series observations are
  *observation*-dated (which period they describe). The tool
  parameters separate the two.
- **Empty release dates.** `include_empty=True` is what surfaces
  *future* scheduled dates that don't have values yet — that's the
  point. Filtering them out would hide the calendar.
- **Free-tier humility.** FRED is maintained by one Fed reserve
  bank on public funding. Uptime is very good but not nine-nines;
  if a tool call fails, the provider propagates the error rather
  than serving stale data.

## Prompts that put these tools to work

- **"What US macro releases are scheduled for the next 10 days?"** —
  `get_release_schedule(realtime_start=<today>, include_empty=True)`.
- **"When's the next CPI print?"** —
  `get_release_dates(release_id=10, realtime_start=<today>)`.
- **"Has core PCE been trending up or down YoY?"** —
  `get_series("PCEPILFE", units="pc1")`.
- **"Plot the unemployment rate for the last five years."** —
  `get_series("UNRATE", observation_start=<5y ago>)`.
- **"What's the 10Y–2Y yield spread doing?"** —
  `get_series("T10Y2Y")` (it's precomputed by FRED).
- **"Give me a one-call macro regime read."** —
  `analyze_macro_regime()` and weigh the component z-scores.
- **"Is the curve still inverted?"** — `analyze_yield_curve()`,
  read `derived.curve_shape` and the per-slope `inverted` flags.
- **"Are credit spreads stressed?"** — `analyze_credit_spreads()`,
  check `derived.regime` and the per-index z-scores.

Pair these with `schwab` / `yahoo` provider prompts to condition
equity decisions on the macro calendar — e.g. *"run
`analyze_returns` on SPY then show me what CPI prints overlap that
window."*
