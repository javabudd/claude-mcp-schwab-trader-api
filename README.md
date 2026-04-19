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
│   ├── docker-compose.yml    # one service per server (optional)
│   └── schwab_connector/     # Schwab Trader API (incl. its Dockerfile)
└── logs/                     # per-server runtime logs (cwd-relative)
```

Each server under `mcp_servers/` is its own installable package with
its own `README.md`, `AGENTS.md`, and `pyproject.toml`.

## Available MCP servers

| Server                                             | What it gives the model                                                          | Details                                                            |
|----------------------------------------------------|----------------------------------------------------------------------------------|--------------------------------------------------------------------|
| [`schwab_connector`](mcp_servers/schwab_connector) | Quotes, OHLCV history, TA-Lib indicators, movers, instruments, hours, accounts, return/risk/correlation/regime/pair-spread analytics | [README](mcp_servers/schwab_connector/README.md) · [AGENTS](mcp_servers/schwab_connector/AGENTS.md) |

More servers (other brokers, data vendors, news/sentiment, on-chain,
research tools) will be added over time. The pattern stays the same:
one subdirectory per server, independently installable.

## Quickstart

You'll install one or more MCP servers, start each in its own
terminal, and point your AI CLI at them. Each server's own `README.md`
has the full setup — the steps below are the short path for the
Schwab connector.

### 1. Conda env (shared across all servers)

All Python in this repo uses a conda env named `traider`, pinned to
Python 3.13:

```bash
conda create -n traider python=3.13
conda activate traider
```

### 2. Install the server(s) you want

```bash
conda activate traider
pip install -e ./mcp_servers/schwab_connector
```

### 3. Configure credentials

Drop them in a `.env` at the repo root (gitignored, loaded on
startup). For the Schwab connector, see its
[README](mcp_servers/schwab_connector/README.md#5-configure-schwab-credentials)
for the app-registration walkthrough.

```
SCHWAB_APP_KEY=...
SCHWAB_APP_SECRET=...
SCHWAB_CALLBACK_URL=https://127.0.0.1
```

### 4. One-time auth, then run the server

```bash
schwab-connector auth    # one-time browser OAuth flow
schwab-connector         # starts the MCP server on stdio
```

Or over HTTP for remote MCP clients:

```bash
schwab-connector --transport streamable-http --port 8765
```

### 5. Wire it into your AI CLI

Point your MCP client at the running server (stdio or HTTP). The
model will then see every tool the server exposes. From there, ask
questions and let the model chain tools — the per-server README has
worked examples.

## Alternative: run with Docker

If you'd rather not install conda and the C deps (TA-Lib, …) on your
host, each MCP server ships a `Dockerfile` next to its code, and
`mcp_servers/docker-compose.yml` wires them all together. The images
use the same `traider` conda env internally, so install paths match
the non-Docker quickstart.

### 1. Configure credentials

Same as step 3 above — drop a `.env` at the repo root. Compose reads
it from `mcp_servers/docker-compose.yml` via `env_file: ../.env`.

### 2. Build the images

```bash
cd mcp_servers
docker compose build
```

### 3. One-time OAuth (per server that needs it)

Run the server's auth subcommand interactively. The token file is
written to `~/.schwab-connector/` on the host (mounted into the
container), so a later `docker compose up` reuses it, and so does the
host `schwab-connector` CLI if you also use it outside Docker.

```bash
docker compose run --rm schwab-connector schwab-connector auth
```

You'll paste the Schwab callback URL back into the terminal, same as
the non-Docker flow (the container never has to receive the callback
itself — it's a copy-paste from your browser).

### 4. Start the servers

```bash
docker compose up -d
```

Each server exposes its MCP endpoint on a fixed port:

| Server              | URL                     |
|---------------------|-------------------------|
| `schwab-connector`  | `http://localhost:8765` |

Point your AI CLI's MCP client at those URLs (streamable-http
transport). Logs land in `./logs/` on the host.

### 5. Stop / rebuild

```bash
docker compose down                   # stop everything
docker compose up -d schwab-connector # start just one server
docker compose build --no-cache       # after changing a Dockerfile
```

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
