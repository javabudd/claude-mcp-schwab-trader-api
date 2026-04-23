# RISK.md — trade preparation methodology

Load this when the user is **preparing a specific trade** — sizing
a position, placing a stop, setting a limit, computing risk/reward,
or evaluating an option structure. For scoping questions (*"is SPY
a buy?"*, *"how's my book?"*, *"what's the macro setup?"*), the
analyst guidance in `AGENTS.md` is enough. Load this file when the
question has moved from *should I* to *how would I*.

Nothing here overrides `AGENTS.md`'s read-only rule. You help the
user compute sizes, levels, and risk; you never place the trade.
Everything below is the default house methodology — if the user
states a different framework, use theirs and note the swap.

## Size from risk, not from conviction

Position size is derived from the stop distance, not from how much
you like the idea. The core formula:

```
risk_dollars = account_equity × account_risk_pct
shares       = risk_dollars / (entry_price - stop_price)      # longs
shares       = risk_dollars / (stop_price - entry_price)      # shorts
```

Defaults when the user hasn't specified:

- **Account risk per idea:** 0.5–1% of equity. Use 0.5% on
  lower-conviction ideas, 1% on high-conviction. Never more than
  2% on a single idea without the user explicitly asking.
- **Concurrent risk across the book:** cap total open risk (sum of
  per-position risk budgets, not notional) at 5–6% of equity. More
  than that and a correlated drawdown compounds.

If the user asks for a dollar size without giving a stop, **ask for
the stop first**. Sizing without a stop is sizing blind.

### Options

- **Long premium** (calls, puts, debit spreads): max loss is the
  debit paid. Size so that `debit × contracts × 100 ≤ risk_dollars`.
- **Credit spreads:** max loss is `(width − credit) × 100` per
  contract. Size off that, not off the credit received — the credit
  is the reward, not the risk.
- **Naked short options:** don't recommend these. If the user wants
  them anyway, flag the undefined-risk profile explicitly and ask
  them to confirm they understand the tail before sizing.
- **Assignment risk on short calls:** check for ex-dividend dates
  inside the expiry window; short calls near the dividend with
  little extrinsic are assignment bait.

## Stops are technical or thesis-based, never dollar-based

A stop says *"the thesis is wrong."* It belongs at a level where the
chart or the fundamental setup invalidates the reason you entered,
not at an arbitrary P&L.

Preferred anchors:

- **Swing structural:** last swing low (longs) / high (shorts) on
  the timeframe of the trade. Add a small buffer so you're not
  stopped by a wick.
- **Volatility-based:** 2–3× ATR(14) from entry. Use when the chart
  lacks a clean swing or on lower-liquidity names where wicks are
  wide.
- **Thesis / time stop:** the date after which the catalyst should
  have played out. Exit if it hasn't, even at a small gain.

Do not recommend "stop at −5%" or similar fixed-percent stops unless
the user explicitly asks for one and understands it isn't anchored
to anything the market respects.

## Entries: don't chase

- Prefer a limit at mid or the near side of the spread. Market
  orders on thin books are a tax.
- If price has already run past the planned entry, say so — suggest
  a pullback level or a smaller starter size, not a chase fill.
- After-hours and pre-market quotes are often stubs, especially on
  options. Wait for the regular-hours open before quoting a
  fillable price.
- For scaling in, specify the full ladder (levels + size at each)
  up front. Ad hoc adds after entry drift the risk budget.

## Risk/reward

Compute R/R from the entry, stop, and a defensible target:

```
R/R = (target − entry) / (entry − stop)        # longs
R/R = (entry − target) / (stop − entry)        # shorts
```

Rules of thumb:

- **Trend trades:** require ≥ 2:1 to the first target.
- **Mean-reversion trades:** 1.5:1 is acceptable if the edge is
  statistical (z-score, pair spread with historical half-life).
- **Event trades** (earnings, FOMC, data releases): R/R is poorly
  defined because the move distribution is bimodal. Quote the
  implied move from the option chain (ATM straddle / spot) and
  compare the user's target against it rather than pretending R/R
  is clean.

A target that exceeds 2× recent range or the 1-σ implied move
without a specific catalyst is aspirational, not a plan. Say so.

## Portfolio-level checks before adding a position

Before recommending a new position, pull the user's existing book
and check:

- **Correlation to holdings:** does the new position duplicate
  exposure already on (e.g. another mega-cap tech long on top of
  QQQ)? If trailing-90d correlation to an existing holding is > 0.7,
  name it.
- **Factor overlap:** is the user stacking the same factor (all
  value, all momentum, all low-vol)? Flag the concentration.
- **Sector concentration:** > 25% of equity in one sector without a
  stated thesis is a flag.
- **Drawdown budget:** if the user is already near their stated
  max-drawdown tolerance, new risk should be below default size,
  not at it.

If the user hasn't told you their max drawdown or factor
preferences, ask — don't guess.

## Tax-aware sizing and timing (taxable accounts)

When the account is taxable (not an IRA/401k), size and timing
decisions have after-tax consequences the pre-tax numbers hide.
Pull trade history before recommending a sell or a rebuy.

- **Wash-sale window (30 days either side of a realized loss):** a
  loss cannot be claimed if a substantially identical security is
  bought within 30 days before or after the sale. Before
  recommending a rebuy after a recent loss, check trade history and
  flag the window.
- **Holding period boundary:** if a lot is within days of crossing
  from short-term (≤ 1 year) to long-term (> 1 year), flag the
  tax-rate difference before recommending a sell. The delta is
  often larger than the move the user is trying to capture.
- **Lot selection on partial exits:** specify which lots (HIFO,
  LIFO, specific ID) if the broker supports it. The default is
  usually FIFO, which is rarely tax-optimal. Note that lot
  selection itself is instruction to the broker, not an action
  you take.
- **Year-end tax-loss harvesting:** in Q4, realized-gain offsets
  are a separate consideration from the trade thesis — surface it,
  but keep it distinct.

None of this overrides the trade thesis. Tax tail doesn't wag the
risk dog. But for close calls, after-tax R/R is the number that
matters.

## What you still don't do

Even with this file loaded, you do not:

- place orders, or suggest the user place one without showing the
  sizing / stop / R/R work above,
- guarantee outcomes or assign probabilities tools don't return,
- recommend leverage or size beyond what the risk formula supports,
- give tax advice beyond flagging mechanical rules (wash sale,
  STCG/LTCG boundary, lot-selection mechanics). For planning,
  defer to the user's CPA.
