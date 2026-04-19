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
│   └── fed_calendar_connector/   # FOMC meeting calendar (primary source)
└── logs/                     # per-server runtime logs (cwd-relative)
```

Each server under `mcp_servers/` is its own installable package with
its own `README.md`, `AGENTS.md`, and `pyproject.toml`.

## Available MCP servers

| Server                                                         | What it gives the model                                                          | Details                                                            |
|----------------------------------------------------------------|----------------------------------------------------------------------------------|--------------------------------------------------------------------|
| [`schwab_connector`](mcp_servers/schwab_connector)             | Quotes, OHLCV history, TA-Lib indicators, movers, instruments, hours, accounts, return/risk/correlation/regime/pair-spread analytics | [README](mcp_servers/schwab_connector/README.md) · [AGENTS](mcp_servers/schwab_connector/AGENTS.md) |
| [`yahoo_connector`](mcp_servers/yahoo_connector)               | Same tool surface as `schwab_connector`, backed by Yahoo Finance (no account). Accounts/market-hours tools raise — Yahoo has no brokerage or authoritative session data. | [README](mcp_servers/yahoo_connector/README.md) · [AGENTS](mcp_servers/yahoo_connector/AGENTS.md) |
| [`fred_connector`](mcp_servers/fred_connector)                 | Macro from FRED: economic-release calendar (CPI, NFP, GDP, PCE, retail sales, JOLTS, …), series metadata, and observation time-series. Additive — runs alongside either market-data backend on port 8766. | [README](mcp_servers/fred_connector/README.md) · [AGENTS](mcp_servers/fred_connector/AGENTS.md) |
| [`fed_calendar_connector`](mcp_servers/fed_calendar_connector) | FOMC meeting dates + SEP / press-conference flags, scraped directly from federalreserve.gov (primary source). Additive — port 8767. | [README](mcp_servers/fed_calendar_connector/README.md) · [AGENTS](mcp_servers/fed_calendar_connector/AGENTS.md) |

`schwab_connector` and `yahoo_connector` are **mutually exclusive
alternatives**. Pick one per hub install — see
[Choosing a market-data backend](#choosing-a-market-data-backend) below.

`fred_connector` and `fed_calendar_connector` are **additive** — they
expose different tool names and bind different ports, so they run
alongside whichever market-data backend you picked. Enable them via
`COMPOSE_PROFILES` (e.g. `COMPOSE_PROFILES=yahoo,fred,fed-calendar`)
for Docker, or just run the binaries for host mode.

More servers (other brokers, data vendors, news/sentiment, on-chain,
research tools) will be added over time. The pattern stays the same:
one subdirectory per server, independently installable.

## Choosing a market-data backend

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
| Intraday history depth       | Long (years of minute bars)                 | Short (~7d for 1m, ~60d for sub-hourly)      |
| Data freshness               | Real-time during RTH (Schwab entitlement)   | Typically delayed ~15 min                    |
| Unofficial endpoint?         | No — stable, paid, documented API           | Yes — `yfinance` scrapes; expect drift       |

**Only one backend runs at a time** — they both bind port 8765. The
selector is `COMPOSE_PROFILES` in `.env`:

```
COMPOSE_PROFILES=schwab    # or: yahoo
```

Each compose service is tagged with a matching profile, so
`docker compose up` only starts the one you picked. If no profile is
set, nothing starts (Compose prints the list of available profiles).

Copy `.env.dist` to `.env` and edit the value, then run compose with
`--env-file ../.env` so it picks up the profile:

```bash
cp .env.dist .env
# edit .env to set COMPOSE_PROFILES=schwab or yahoo
cd mcp_servers
docker compose --env-file ../.env up -d
```

For host-mode (non-Docker), the backend is simply whichever binary
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
`COMPOSE_PROFILES` from that file to decide which backend to start
(see [Choosing a market-data backend](#choosing-a-market-data-backend)).

Start from the template:

```bash
cp .env.dist .env
```

Then edit:

```
# Pick your backend — schwab or yahoo.
COMPOSE_PROFILES=schwab

# Schwab-only (ignored if COMPOSE_PROFILES=yahoo). See the Schwab
# connector's README for the app-registration walkthrough.
SCHWAB_APP_KEY=...
SCHWAB_APP_SECRET=...
SCHWAB_CALLBACK_URL=https://127.0.0.1
```

The Yahoo backend needs no credentials. Never commit `.env` or paste
its contents into logs or chat.

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

| Backend             | URL                     |
|---------------------|-------------------------|
| `schwab-connector`  | `http://localhost:8765` |
| `yahoo-connector`   | `http://localhost:8765` |

Both bind 8765 — that's why only one runs at a time. Wire the URL
into your AI CLI using the **HTTP** examples in
[Connect your AI CLI](#connect-your-ai-cli) below. Logs land in
`./logs/` on the host.

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
