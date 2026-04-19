# traider

A hub for using an AI CLI (Claude Code, OpenCode, Cowork, Gemini CLI,
Cursor, Aider, …) to gain financial insights and help make trading
decisions.

`traider` itself doesn't trade. It's a **collection of MCP servers**
that expose read-only market data, account data, and analytics as
tools the model can call. You keep every decision; the model fetches,
compiles, parses, and explains.

See [AGENTS.md](AGENTS.md) for the hub's north star — what belongs
here, what doesn't, and how to navigate the per-server docs.

## Layout

```
traider/
├── AGENTS.md                 # hub north star (load into your AI CLI)
├── README.md                 # this file
├── mcp_servers/
│   ├── docker-compose.yml        # one service per server (optional)
│   ├── schwab_connector/         # Schwab Trader API (incl. its Dockerfile)
│   ├── yahoo_connector/          # Yahoo Finance (no account required)
│   ├── fred_connector/           # FRED macro data / release calendar
│   ├── fed_calendar_connector/   # FOMC meeting calendar (primary source)
│   ├── sec_edgar_connector/      # SEC EDGAR filings, insiders, 13F, XBRL
│   └── factor_connector/         # Ken French data library (Fama-French, industry)
└── logs/                     # per-server runtime logs (cwd-relative)
```

Each server under `mcp_servers/` is its own installable package with
its own `README.md`, `AGENTS.md`, and `pyproject.toml`.

## Available MCP servers

| Server                                                         | What it gives the model                                                          | Details                                                            |
|----------------------------------------------------------------|----------------------------------------------------------------------------------|--------------------------------------------------------------------|
| [`schwab_connector`](mcp_servers/schwab_connector)             | Quotes, OHLCV history, TA-Lib indicators, option chains (with Greeks), movers, instruments, hours, accounts, return/risk/correlation/regime/pair-spread analytics | [README](mcp_servers/schwab_connector/README.md) · [AGENTS](mcp_servers/schwab_connector/AGENTS.md) |
| [`yahoo_connector`](mcp_servers/yahoo_connector)               | Same tool surface as `schwab_connector`, backed by Yahoo Finance (no account). Accounts/market-hours tools raise — Yahoo has no brokerage or authoritative session data. Option chains are delayed and omit Greeks. | [README](mcp_servers/yahoo_connector/README.md) · [AGENTS](mcp_servers/yahoo_connector/AGENTS.md) |
| [`fred_connector`](mcp_servers/fred_connector)                 | Macro from FRED: economic-release calendar (CPI, NFP, GDP, PCE, retail sales, JOLTS, …), series metadata, and observation time-series. Additive — runs alongside either market-data backend on port 8766. | [README](mcp_servers/fred_connector/README.md) · [AGENTS](mcp_servers/fred_connector/AGENTS.md) |
| [`fed_calendar_connector`](mcp_servers/fed_calendar_connector) | FOMC meeting dates + SEP / press-conference flags, scraped directly from federalreserve.gov (primary source). Additive — port 8767. | [README](mcp_servers/fed_calendar_connector/README.md) · [AGENTS](mcp_servers/fed_calendar_connector/AGENTS.md) |
| [`sec_edgar_connector`](mcp_servers/sec_edgar_connector)       | SEC EDGAR primary-source filings (10-K, 10-Q, 8-K, 20-F, …), Form 4 insider transactions, 13F institutional holdings, and XBRL company facts (per-company + cross-sectional frames). Additive — port 8768. | [README](mcp_servers/sec_edgar_connector/README.md) · [AGENTS](mcp_servers/sec_edgar_connector/AGENTS.md) |
| [`factor_connector`](mcp_servers/factor_connector)             | Ken French Data Library: Fama-French 3/5-factor, momentum, short/long-term reversal, and 5/10/12/17/30/38/48/49-industry portfolios at monthly / weekly / daily frequencies. Disk-cached with explicit TTL. Additive — port 8771. | [README](mcp_servers/factor_connector/README.md) · [AGENTS](mcp_servers/factor_connector/AGENTS.md) |

`schwab_connector` and `yahoo_connector` are **mutually exclusive
alternatives**. Every other server is **additive**. Which servers
actually start is controlled by Docker Compose profiles — see
[Selecting which servers to run](#selecting-which-servers-to-run)
below.

More servers (other brokers, data vendors, news/sentiment, on-chain,
research tools) will be added over time. The pattern stays the same:
one subdirectory per server, independently installable.

## Selecting which servers to run

Under Docker, **every MCP server in this hub is gated by its own
Docker Compose profile**, and you pick which ones start via the
comma-separated `COMPOSE_PROFILES` variable in `.env`. Services whose
profile isn't active simply don't launch. If no profile is set,
nothing starts (and Compose prints the list of available profiles).

| Profile        | Server                    | Port | Kind                | Creds required                  |
|----------------|---------------------------|------|---------------------|---------------------------------|
| `schwab`       | `schwab-connector`        | 8765 | Market-data backend | Schwab app key/secret + OAuth   |
| `yahoo`        | `yahoo-connector`         | 8765 | Market-data backend | None                            |
| `fred`         | `fred-connector`          | 8766 | Additive (macro)    | `FRED_API_KEY` (free)           |
| `fed-calendar` | `fed-calendar-connector`  | 8767 | Additive (macro)    | None                            |
| `sec-edgar`    | `sec-edgar-connector`     | 8768 | Additive (filings)  | `SEC_EDGAR_USER_AGENT` (yours)  |
| `factor`       | `factor-connector`        | 8771 | Additive (factors)  | None                            |

Rules of thumb:

- **Pick one market-data backend** (`schwab` *or* `yahoo`). They
  expose the same tool names and both bind 8765, so running both
  would make the second one fail to start.
- **Add any mix of additive profiles.** `fred`, `fed-calendar`,
  `sec-edgar`, and `factor` expose distinct tool names and distinct
  ports, so they compose freely with each other and with either
  market-data backend.

Examples:

```bash
COMPOSE_PROFILES=schwab                                    # just Schwab quotes/accounts
COMPOSE_PROFILES=yahoo                                     # just Yahoo quotes
COMPOSE_PROFILES=yahoo,fred,fed-calendar,sec-edgar,factor  # Yahoo + full macro + fundamentals + factors
COMPOSE_PROFILES=schwab,sec-edgar                          # Schwab + EDGAR filings/insiders
COMPOSE_PROFILES=yahoo,factor                              # Yahoo + Fama-French factors
```

Then from `mcp_servers/`:

```bash
docker compose --env-file ../.env up -d
```

**For host mode (no Docker), there is no profile system** — you just
`pip install` and launch the binaries you want. Profiles only matter
when you're running under Compose. See
[Run on the host](#run-on-the-host-alternative) for that path.

### Choosing a market-data backend

`schwab_connector` and `yahoo_connector` expose the **same tool names**
(`get_quote`, `get_price_history`, `run_technical_analysis`,
`analyze_*`, …) so prompts are portable. They differ only in where
the data comes from and what's not available:

|                              | `schwab_connector`                          | `yahoo_connector`                            |
|------------------------------|---------------------------------------------|----------------------------------------------|
| Account needed               | Schwab developer account (app approval)     | None                                         |
| Auth flow                    | One-time OAuth (browser)                    | None                                         |
| Brokerage (`get_accounts`)   | ✅ real positions, cost basis, P&L          | ❌ raises — no brokerage                      |
| Market hours (`get_market_hours`) | ✅ authoritative, holiday-aware        | ❌ raises — Yahoo has no such endpoint        |
| Movers                       | per-index (`$SPX`, `$DJI`, …)               | US-market-wide Yahoo screeners               |
| Option chains (`get_option_chain`) | ✅ Greeks, strategy previews, real-time | ⚠️ delayed ~15min, no Greeks, `SINGLE` only  |
| Intraday history depth       | Long (years of minute bars)                 | Short (~7d for 1m, ~60d for sub-hourly)      |
| Data freshness               | Real-time during RTH (Schwab entitlement)   | Typically delayed ~15 min                    |
| Unofficial endpoint?         | No — stable, paid, documented API           | Yes — `yfinance` scrapes; expect drift       |

**Only one backend runs at a time** — they both bind port 8765. Pick
it by putting exactly one of `schwab` or `yahoo` in
`COMPOSE_PROFILES` (see
[Selecting which servers to run](#selecting-which-servers-to-run)
for the full profile mechanics, including combining with additive
servers).

For host mode (non-Docker), the backend is simply whichever binary
you run — `schwab-connector` or `yahoo-connector`. Don't start both
(same port).

## Quickstart

You'll install or run one or more MCP servers, start each one, and
point your AI CLI at them. Each server's own `README.md` has the full
setup — the steps below are the short path for the Schwab connector.

The flow is:

1. **Configure credentials** (shared by both run modes).
2. **Run the server(s)** — either with [Docker](#run-with-docker-recommended)
   (recommended) or [directly on the host](#run-on-the-host-alternative).
3. **[Wire the server into your AI CLI](#connect-your-ai-cli).**

### Configure credentials

Both run modes read credentials from a `.env` at the repo root
(gitignored, loaded on startup). Compose also reads it via
`env_file: ../.env` in `mcp_servers/docker-compose.yml`, and uses
`COMPOSE_PROFILES` from that file to decide which servers to start
(see [Selecting which servers to run](#selecting-which-servers-to-run)).

Start from the template:

```bash
cp .env.dist .env
```

Then edit. The file has entries for every server the hub knows about;
each is only consulted if its profile is in `COMPOSE_PROFILES`:

```
# One market-data backend + any mix of additive servers.
COMPOSE_PROFILES=schwab,fred,fed-calendar,sec-edgar

# Schwab-only — ignored unless `schwab` is in COMPOSE_PROFILES.
SCHWAB_APP_KEY=...
SCHWAB_APP_SECRET=...
SCHWAB_CALLBACK_URL=https://127.0.0.1

# FRED-only — ignored unless `fred` is in COMPOSE_PROFILES.
FRED_API_KEY=...

# EDGAR-only — ignored unless `sec-edgar` is in COMPOSE_PROFILES.
SEC_EDGAR_USER_AGENT=your-name you@example.com
```

The Yahoo and FOMC-calendar servers need no credentials. Never commit
`.env` or paste its contents into logs or chat.

### Run with Docker (recommended)

Each MCP server ships a `Dockerfile` next to its code, and
`mcp_servers/docker-compose.yml` wires them all together. You skip
installing conda and the C deps (TA-Lib, …) on your host.

**1. Build the image(s)**

Only the backend your profile selects needs building — but building
all of them is cheap and lets you switch by flipping
`COMPOSE_PROFILES` without another `build`:

```bash
cd mcp_servers
docker compose --profile schwab --profile yahoo --env-file ../.env build
```

**2. One-time OAuth (Schwab only)**

Skip this step entirely on the Yahoo backend — yfinance is
unauthenticated.

For Schwab, run the auth subcommand interactively. The token file is
written to `~/.schwab-connector/` on the host (mounted into the
container), so a later `docker compose up` reuses it, and so does the
host `schwab-connector` CLI if you also use it outside Docker.

```bash
docker compose --env-file ../.env run --rm schwab-connector \
    schwab-connector auth
```

You'll paste the Schwab callback URL back into the terminal, same as
the non-Docker flow (the container never has to receive the callback
itself — it's a copy-paste from your browser).

**3. Start the servers**

```bash
docker compose --env-file ../.env up -d
```

Only the service whose profile matches `COMPOSE_PROFILES` starts. It
exposes its MCP endpoint on:

| Server                    | URL                     |
|---------------------------|-------------------------|
| `schwab-connector`        | `http://localhost:8765` |
| `yahoo-connector`         | `http://localhost:8765` |
| `fred-connector`          | `http://localhost:8766` |
| `fed-calendar-connector`  | `http://localhost:8767` |
| `sec-edgar-connector`     | `http://localhost:8768` |
| `factor-connector`        | `http://localhost:8771` |

`schwab-connector` and `yahoo-connector` both bind 8765 — that's why
only one runs at a time. `fred-connector` and `fed-calendar-connector`
are additive and come up alongside whichever market-data backend your
profile selected. Wire each URL into your AI CLI using the **HTTP**
examples in [Connect your AI CLI](#connect-your-ai-cli) below. Logs
land in `./logs/` on the host.

**4. Stop / switch / rebuild**

```bash
docker compose --env-file ../.env down       # stop whatever's running
# switch backends: edit COMPOSE_PROFILES in ../.env, then:
docker compose --env-file ../.env up -d
docker compose --env-file ../.env build --no-cache   # after Dockerfile changes
```

### Run on the host (alternative)

If you'd rather run servers directly on your machine — no Docker —
use a shared conda env.

**1. Create the conda env**

All Python in this repo uses a conda env named `traider`, pinned to
Python 3.13:

```bash
conda create -n traider python=3.13
conda activate traider
```

**2. Install the backend you want**

```bash
# Schwab (needs developer account + OAuth):
pip install -e ./mcp_servers/schwab_connector

# or — Yahoo (no account, no auth):
pip install -e ./mcp_servers/yahoo_connector
```

Install only one. They bind the same port and provide the same tool
names — having both on PATH is fine, but only run one at a time.

**3. Auth (Schwab only), then run the server**

Schwab:

```bash
schwab-connector auth    # one-time browser OAuth flow
schwab-connector         # starts the MCP server on stdio
```

Yahoo:

```bash
yahoo-connector          # starts the MCP server on stdio — no auth
```

Or over HTTP for remote MCP clients:

```bash
schwab-connector --transport streamable-http --port 8765
# or
yahoo-connector --transport streamable-http --port 8765
```

Then see [Connect your AI CLI](#connect-your-ai-cli) — use the
**stdio** form for a direct host run, or the **HTTP** form when you
start the server with `--transport streamable-http`.

## Connect your AI CLI

Once a server is running — either on the host (stdio) or in Docker
(HTTP on `localhost:8765/mcp`) — register it with your CLI using one
of the recipes below. Examples use the Schwab connector; swap the
name/URL for any other server in the hub.

### Claude Code

`claude mcp add` writes to your Claude config; no JSON editing. Add
`--scope user` to make it available across all projects, or
`--scope project` to check it into `.mcp.json` for teammates. Default
scope (`local`) is this project only.

**Stdio (host install):**

```bash
claude mcp add --transport stdio schwab-connector -- schwab-connector
```

The `--` separates `claude mcp add` flags from the command that
launches the server.

**HTTP (Docker, or any streamable-http server):**

```bash
claude mcp add --transport http schwab-connector http://localhost:8765/mcp
```

Use `--header "Authorization: Bearer …"` if the endpoint needs auth
(the servers in this hub don't).

Verify with `claude mcp list`, then restart the CLI session.

### OpenCode

Edit `opencode.json` in the repo root (project-local) or
`~/.config/opencode/opencode.json` (user-wide). MCP servers live
under the top-level `mcp` key.

**Stdio (host install):**

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "schwab-connector": {
      "type": "local",
      "command": ["schwab-connector"],
      "enabled": true
    }
  }
}
```

**HTTP (Docker, or any streamable-http server):**

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "schwab-connector": {
      "type": "remote",
      "url": "http://localhost:8765/mcp",
      "enabled": true
    }
  }
}
```

Use `{env:VAR_NAME}` inside `headers` for auth tokens when you need
them.

### Gemini CLI

Edit `.gemini/settings.json` in the repo root (project) or
`~/.gemini/settings.json` (user). MCP servers live under
`mcpServers`.

**Stdio (host install):**

```json
{
  "mcpServers": {
    "schwab-connector": {
      "command": "schwab-connector"
    }
  }
}
```

If the server needs env vars injected, use `"env": { "KEY": "$KEY" }`
— Gemini CLI does **not** auto-load `.env`, so either export the
vars in your shell first or put the literal values in `env`.

**HTTP (Docker, or any streamable-http server):**

```json
{
  "mcpServers": {
    "schwab-connector": {
      "httpUrl": "http://localhost:8765/mcp"
    }
  }
}
```

Add `"headers": { "Authorization": "Bearer $TOKEN" }` if the endpoint
requires auth.

## What this hub will and won't do

- **Will.** Fetch, align, and compute on market data. Explain what
  the numbers say. Flag regime shifts, correlations, mean-reversion
  setups, realized-vol outliers, fundamental outliers — all of it
  read-only, all of it for the user to act on.
- **Won't.** Place orders, create alerts, make writes to any
  brokerage or external service. Ship "auto-trader" features.
  Silently retry past a 429 or paper over a failing dependency.
  Store credentials in the repo or in logs.

See [AGENTS.md](AGENTS.md) for the full set of hub-wide constraints
(which every MCP server in this repo inherits).
