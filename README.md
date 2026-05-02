# traider

`traider` is two things that only work together:

1. **This repo's `AGENTS.md`.** When loaded into an AI CLI (Claude
   Code, OpenCode, Cowork, Gemini CLI, Cursor, Aider, …), it reframes
   the assistant as a **senior trading analyst** for you — how to
   scope a question, what context to reach for, how to cite numbers,
   what never to fabricate.
2. **A single MCP server** you run yourself that exposes read-only
   market data, account data, macro, fundamentals, filings, factor
   returns, and news as tools the model can call. Without this, the
   analyst framing has nothing to pull from and falls back on stale
   training-data recall.

`traider` itself doesn't trade. You keep every decision; the model
fetches, compiles, parses, and explains.

See [AGENTS.md](AGENTS.md) for the runtime analyst guidance that gets
loaded into your AI CLI's context. Internals for modifying the code
(how providers load, how to add a provider) live in
[DEVELOPING.md](DEVELOPING.md) and are intentionally not auto-loaded.

## Available providers

One env var, `TRAIDER_PROVIDERS`, controls the data-provider tool
surface. The `intent` provider — local trade-intent journal,
framework rules, and account profile — is **always loaded** as core
analyst infrastructure and is *not* toggled by this env var (listing
it explicitly is harmless but unnecessary).

Out of the box, traider ships with the no-credentials data set
enabled — useful market data, macro, factors, Treasury, and CFTC
positioning without any signups:

```
TRAIDER_PROVIDERS=yahoo,fed-calendar,factor,treasury,cftc
```

Add API-key providers (`fred`, `sec-edgar`, `news`, `earnings`,
`estimates`, `eia`) as you obtain credentials, or swap `yahoo` for
`schwab` if you want real brokerage data and Schwab's market-data
entitlement.

| Provider       | Tool group                        | Creds required                  |
|----------------|-----------------------------------|---------------------------------|
| `schwab`       | Schwab market data + accounts + trade history | Schwab app key/secret + OAuth |
| `yahoo`        | Yahoo Finance market data         | None                            |
| `fred`         | FRED macro / release calendar     | `FRED_API_KEY` (free)           |
| `fed-calendar` | FOMC meeting calendar             | None                            |
| `sec-edgar`    | SEC filings, insiders, 13F, XBRL  | `SEC_EDGAR_USER_AGENT` (yours)  |
| `factor`       | Ken French factors + industries   | None                            |
| `treasury`     | Treasury auctions, DTS, debt      | None                            |
| `news`         | Massive news + sentiment          | `MASSIVE_API_KEY` (free tier)   |
| `earnings`     | Finnhub earnings calendar + surprises | `FINNHUB_API_KEY` (free tier) |
| `estimates`    | Finnhub analyst recommendation trends | `FINNHUB_API_KEY` (free tier, shared with earnings) |
| `eia`          | EIA energy data (petroleum, natgas, electricity) | `EIA_API_KEY` (free) |
| `cftc`         | CFTC Commitments of Traders (weekly positioning) | None (optional `CFTC_APP_TOKEN`) |

Plus, always loaded regardless of `TRAIDER_PROVIDERS`:

| Provider | Tool group | Creds required |
|----------|------------|----------------|
| `intent` | Local trade-intent journal + framework rules + account profile (why each share/contract exists, what discipline governs it, what role the account plays in your total wealth) | None (local SQLite, optional `TRAIDER_INTENT_DB`, `TRAIDER_ACCOUNT_PROFILES`) |

Rules:

- **Pick at most one market-data backend** (`schwab` *or* `yahoo`).
  They expose the same tool names and are mutually exclusive; enabling
  both at once is a startup error.
- **Add any mix of the other providers.** They expose distinct tool
  names, so they compose freely with each other and with whichever
  market-data backend you chose.

If `TRAIDER_PROVIDERS` is empty, only the always-on `intent` provider
is registered — useful for smoke-testing the transport or for working
purely against the local intent journal, but you'll get no live
market data.

### Choosing a market-data backend

`schwab` and `yahoo` expose the **same tool names** (`get_quote`,
`get_price_history`, `run_technical_analysis`, `analyze_*`, …) so
prompts are portable. `yahoo` is the default because it works with
no signup; `schwab` is the upgrade path for real brokerage data and
real-time market-data entitlement. They differ only in where the
data comes from and what's not available:

|                              | `schwab`                                    | `yahoo`                                      |
|------------------------------|---------------------------------------------|----------------------------------------------|
| Account needed               | Schwab developer account (app approval)     | None                                         |
| Auth flow                    | One-time OAuth (browser)                    | None                                         |
| Brokerage (`get_accounts`)   | ✅ real positions, cost basis, P&L          | ❌ raises — no brokerage                      |
| Market hours (`get_market_hours`) | ✅ authoritative, holiday-aware        | ❌ raises — Yahoo has no such endpoint        |
| Movers                       | per-index (`$SPX`, `$DJI`, …)               | US-market-wide Yahoo screeners               |
| Option chains                | ✅ Greeks, strategy previews, real-time     | ⚠️ delayed ~15min, no Greeks, `SINGLE` only  |
| Intraday history depth       | Long (years of minute bars)                 | Short (~7d for 1m, ~60d for sub-hourly)      |
| Data freshness               | Real-time during RTH (Schwab entitlement)   | Typically delayed ~15 min                    |
| Unofficial endpoint?         | No — stable, paid, documented API           | Yes — `yfinance` scrapes; expect drift       |

## Quickstart

1. **[Configure credentials](#1-configure-credentials)** in `.env`.
2. **[Run the server](#2-run-the-server)** — with Docker
   (recommended) or directly on the host.
3. **[Connect your AI CLI](#3-connect-your-ai-cli)** to the server.

### 1. Configure credentials

Copy the template and edit:

```bash
cp .env.dist .env
```

`.env.dist` ships with the no-credentials default, so an unedited
`.env` will start a working server (Yahoo quotes, FOMC calendar, Ken
French factors, Treasury auctions, CFTC positioning, plus the
always-on `intent` journal / rules / account profile):

```
TRAIDER_PROVIDERS=yahoo,fed-calendar,factor,treasury,cftc
```

To add API-key providers, append their names to `TRAIDER_PROVIDERS`
and fill in the matching credentials. For example, to add FRED macro
data, SEC EDGAR filings, and switch the market-data backend to
Schwab:

```
TRAIDER_PROVIDERS=schwab,fred,fed-calendar,sec-edgar,factor,treasury,cftc

# schwab provider only.
SCHWAB_APP_KEY=...
SCHWAB_APP_SECRET=...
SCHWAB_CALLBACK_URL=https://127.0.0.1

# fred provider only.
FRED_API_KEY=...

# sec-edgar provider only.
SEC_EDGAR_USER_AGENT=your-name you@example.com
```

See [Available providers](#available-providers) above for the full
list and the env var each one needs. Never commit `.env` or paste
its contents into logs or chat.

The `intent` provider is always on, but its account-profile surface
needs a file you populate. While you're configuring, copy the
template:

```bash
cp account-profiles.example.yaml ~/.traider/account-profiles.yaml
```

This captures framing the brokerage API can't supply — your age,
the role each account plays in your total wealth (trading sleeve vs.
primary wealth vs. retirement), and risk capacity — so the analyst
can tell whether a given allocation is "appropriately tactical" or
"wildly off-frame" for the account in question. Without it,
`get_account_profile` returns `_has_file: false` and the model has
to ask you the framing questions every session. Override the path
with `TRAIDER_ACCOUNT_PROFILES` if you'd rather keep it elsewhere.

### 2. Run the server

#### With Docker (recommended)

One image, one service, one port. You skip installing conda + the
TA-Lib C library on your host.

```bash
# Build once (or after a Dockerfile / pyproject.toml change):
docker compose build

# (Schwab provider only) one-time interactive OAuth:
docker compose run --rm traider auth schwab

# Start the server:
docker compose up -d
```

The MCP endpoint is exposed at `http://localhost:8765/mcp`. Per-
provider log files land in `./logs/` on the host
(`schwab.log`, `fred.log`, …) plus an aggregated `traider.log`.

Switch provider mix: edit `TRAIDER_PROVIDERS` in `.env`, then
`docker compose restart`. No rebuild needed unless deps changed.

#### On the host (alternative)

No Docker. Use a conda env because TA-Lib needs the native C library.

```bash
conda create -n traider -c conda-forge -y python=3.13 ta-lib
conda activate traider
pip install -e .

# Schwab-only: one-time browser OAuth.
traider auth schwab

# Start the server (providers come from $TRAIDER_PROVIDERS).
traider --transport streamable-http --port 8765
# or stdio:
traider --transport stdio
```

### 3. Connect your AI CLI

The server exposes a single MCP endpoint. Register it once; the tools
available are whatever providers you enabled in `TRAIDER_PROVIDERS`.

#### Claude Code

```bash
# HTTP (Docker, or any streamable-http run):
claude mcp add --transport http traider http://localhost:8765/mcp

# Stdio (host install):
claude mcp add --transport stdio traider -- traider --transport stdio
```

Add `--scope user` for cross-project or `--scope project` to check it
into `.mcp.json`. Verify with `claude mcp list`.

#### OpenCode

`opencode.json` (project) or `~/.config/opencode/opencode.json`
(user):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "traider": {
      "type": "remote",
      "url": "http://localhost:8765/mcp",
      "enabled": true
    }
  }
}
```

Stdio variant: `"type": "local", "command": ["traider", "--transport", "stdio"]`.

#### Gemini CLI

`.gemini/settings.json` (project) or `~/.gemini/settings.json` (user):

```json
{
  "mcpServers": {
    "traider": {
      "httpUrl": "http://localhost:8765/mcp"
    }
  }
}
```

Stdio variant: `"command": "traider", "args": ["--transport", "stdio"]`.
Gemini CLI does not auto-load `.env` — export vars in your shell or
list them under `"env"` in the server entry.

## Example questions

Once the server is wired in and `AGENTS.md` is loaded, simple
trading prompts fan out into multi-tool analyses instead of
collapsing to a single quote call. A few representative shapes:

### *"What's my portfolio look like today?"*

Pulls your accounts via the brokerage tool, then weighs the
dimensions an analyst would: per-position day P&L (with the
open-vs-carryover field check from `AGENTS.md`, so a same-day open
isn't mis-cited as a P&L swing), concentration, correlation
structure across holdings, factor / sector exposure of the book,
and any catalysts (earnings, FOMC, macro releases) hitting your
names this week. Numbers come back with tool + timestamp; nothing
is recalled from training data.

### *"I'm bearish on SPY — what trade?"*

Asks the framing inputs the tools can't supply first: how bearish
(mild pullback vs. crash hedge), over what horizon, and whether
this is a hedge against existing longs or a standalone directional
bet. Then pulls SPY's price action, IV regime and term structure,
and the week's macro / FOMC catalysts; sketches candidate
structures (short delta, put debit spread, put calendar, collar
against a long book…) with their R/R; and once specific levels and
size are on the table, consults the trade-prep `rules/` (via the
intent provider's `list_rules` / `get_rule` tools) and `OPTIONS.md`
for the sizing math and chain-quality checks before naming a strike.

### *"Should I buy NVDA here?"*

Decomposes instead of one-shotting a quote: price action and
volatility regime, technicals, fundamentals and valuation vs.
peers, recent filings and insider activity, factor exposure, news
flow, upcoming catalysts. If you already hold NVDA in a taxable
account and the question is really *trim or add*, holding period
and recent trade history get pulled too — wash-sale windows and
STCG/LTCG boundaries are surfaced before any sell recommendation.
Conflicts (TA bullish, fundamentals stretched; or vice versa) are
named, not silently resolved.

### *"What's the macro setup this week?"*

Pulls the FRED release calendar and FOMC calendar for the window,
the current yield-curve level and shape, recent Treasury auction
demand and TGA cash, and the cross-asset regime (equity / bond /
FX / commodity) via factor returns. Names what's high-impact,
what's already priced in, and what would mark a regime shift —
without making calls on releases that haven't happened.

A literal one-tool-call answer to any of these is a failure mode,
not the goal — the analyst framing in `AGENTS.md` is what turns
*"is SPY a buy?"* into the multi-dimension read above.

## What traider will and won't do

- **Will.** Fetch, align, and compute on market data. Explain what
  the numbers say. Flag regime shifts, correlations, mean-reversion
  setups, realized-vol outliers, fundamental outliers — all of it
  read-only, all of it for the user to act on.
- **Won't.** Place orders, create alerts, make writes to any
  brokerage or external service. Ship "auto-trader" features.
  Silently retry past a 429 or paper over a failing dependency.
  Store credentials in the repo or in logs.

See [AGENTS.md](AGENTS.md) for the full set of traider-wide
constraints (which every provider module inherits).

## Development

Internals for modifying the code — how the unified server loads
providers, how to add a new provider, how to run the test suite —
live in [DEVELOPING.md](DEVELOPING.md). The notes below are just
enough to navigate the repo.

### Repo layout

```
traider/
├── AGENTS.md                 # analyst guidance (auto-loaded into your AI CLI)
├── CLAUDE.md                 # Claude Code entry point — re-exports AGENTS.md
├── DEVELOPING.md             # dev overlay (not auto-loaded)
├── OPTIONS.md                # options-analysis methodology (loaded when options are in scope)
├── RISK.md                   # thin orientation pointing at rules/
├── rules/                    # trade-preparation framework (one YAML per rule; surfaced via the intent provider's list_rules / get_rule tools)
├── todo/PROVIDERS.md         # punch list of planned provider additions
├── account-profiles.example.yaml   # template — copy to ~/.traider/account-profiles.yaml to capture account framing (age, role in total wealth, risk capacity)
├── README.md                 # this file
├── Dockerfile                # single image for the unified server
├── docker-compose.yml        # one service, one port
├── pyproject.toml            # installable package
├── src/traider/
│   └── providers/
│       ├── schwab/           # Schwab Trader API + auth
│       ├── yahoo/            # Yahoo Finance (via yfinance)
│       ├── fred/             # FRED macro data / release calendar
│       ├── fed_calendar/     # FOMC meeting calendar (primary source)
│       ├── sec_edgar/        # SEC EDGAR filings, insiders, 13F, XBRL
│       ├── factor/           # Ken French data library
│       ├── treasury/         # US Treasury Fiscal Data
│       ├── news/             # Massive news API
│       ├── earnings/         # Finnhub earnings calendar + surprises
│       ├── estimates/        # Finnhub analyst recommendation trends
│       ├── eia/              # EIA energy data (petroleum, natgas, electricity)
│       ├── cftc/             # CFTC Commitments of Traders (weekly positioning)
│       └── intent/           # Local trade-intent journal (SQLite, no external writes)
└── logs/                     # per-provider runtime logs
```

Each provider under `src/traider/providers/` is a module with its
own `AGENTS.md` and `README.md`. Provider modules are loaded
**lazily** — only the ones listed in `TRAIDER_PROVIDERS` are imported,
so you don't pay the dep-load or warmup cost for providers you aren't
using.
