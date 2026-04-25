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

## Layout

```
traider/
├── AGENTS.md                 # analyst guidance (auto-loaded into your AI CLI)
├── CLAUDE.md                 # Claude Code entry point — re-exports AGENTS.md
├── DEVELOPING.md             # dev overlay (not auto-loaded)
├── OPTIONS.md                # options-analysis methodology (loaded when options are in scope)
├── RISK.md                   # trade-preparation methodology (loaded when sizing / stops are in scope)
├── todo/PROVIDERS.md         # punch list of planned provider additions
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
│       └── cftc/             # CFTC Commitments of Traders (weekly positioning)
└── logs/                     # per-provider runtime logs
```

Each provider under `src/traider/providers/` is a module with its
own `AGENTS.md` and `README.md`. Provider modules are loaded
**lazily** — only the ones listed in `TRAIDER_PROVIDERS` are imported,
so you don't pay the dep-load or warmup cost for providers you aren't
using.

## Selecting which providers to load

One env var, `TRAIDER_PROVIDERS`, controls the exposed tool surface:

```
TRAIDER_PROVIDERS=schwab,fred,fed-calendar,sec-edgar,factor,treasury,news
```

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

Rules:

- **Pick at most one market-data backend** (`schwab` *or* `yahoo`).
  They expose the same tool names and are mutually exclusive; enabling
  both at once is a startup error.
- **Add any mix of the other providers.** They expose distinct tool
  names, so they compose freely with each other and with whichever
  market-data backend you chose.

If `TRAIDER_PROVIDERS` is empty, the server starts with no tools
registered — useful for smoke-testing the transport but not for
actual work.

### Choosing a market-data backend

`schwab` and `yahoo` expose the **same tool names** (`get_quote`,
`get_price_history`, `run_technical_analysis`, `analyze_*`, …) so
prompts are portable. They differ only in where the data comes from
and what's not available:

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

1. **Configure credentials** in `.env`.
2. **Run the server** — with [Docker](#run-with-docker-recommended)
   (recommended) or [directly on the host](#run-on-the-host-alternative).
3. **[Wire the server into your AI CLI](#connect-your-ai-cli).**

### Configure credentials

Copy the template and edit:

```bash
cp .env.dist .env
```

Set `TRAIDER_PROVIDERS` to the providers you want, plus credentials
for the ones that need them:

```
TRAIDER_PROVIDERS=schwab,fred,fed-calendar,sec-edgar

# schwab provider only.
SCHWAB_APP_KEY=...
SCHWAB_APP_SECRET=...
SCHWAB_CALLBACK_URL=https://127.0.0.1

# fred provider only.
FRED_API_KEY=...

# sec-edgar provider only.
SEC_EDGAR_USER_AGENT=your-name you@example.com
```

The `yahoo`, `fed-calendar`, `factor`, and `treasury` providers need no
credentials. Never commit `.env` or paste its contents into logs or
chat.

### Run with Docker (recommended)

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

### Run on the host (alternative)

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

## Connect your AI CLI

The server exposes a single MCP endpoint. Register it once; the tools
available are whatever providers you enabled in `TRAIDER_PROVIDERS`.

### Claude Code

```bash
# HTTP (Docker, or any streamable-http run):
claude mcp add --transport http traider http://localhost:8765/mcp

# Stdio (host install):
claude mcp add --transport stdio traider -- traider --transport stdio
```

Add `--scope user` for cross-project or `--scope project` to check it
into `.mcp.json`. Verify with `claude mcp list`.

### OpenCode

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

### Gemini CLI

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
