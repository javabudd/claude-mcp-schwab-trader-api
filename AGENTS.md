# AGENTS.md — ai-trader

**Read this first.** This is your north star when this repo is loaded
into an AI CLI (Claude Code, OpenCode, Cowork, Gemini CLI, Cursor,
Aider, …). It tells you what `ai-trader` is, what it is *not*, and
how to find the details for any individual capability without
re-deriving them.

## What this repo is

`ai-trader` is a **central hub for using an AI CLI to gain financial
insights and help make trading decisions**. It is not a bot, not a
broker, and not a standalone tool — it is a collection of **MCP
servers** that the user starts alongside an AI CLI so the model can:

- **Fetch** market data, account data, and fundamentals from brokerage
  and data-vendor APIs.
- **Compile** that data into the shapes analytics need (aligned candle
  series, joined time windows, portfolio-weighted aggregates).
- **Parse** and compute on it — technical-analysis indicators,
  return/risk metrics, correlation matrices, regime classifiers,
  pair-spread statistics, etc.

Everything the hub ships is **read-only**. No order entry, no alert
creation, no writes to external systems. The premise is that the user
stays in the loop for every decision — the model is here to fetch,
compute, and explain, not to trade.

## How the hub is organized

The repo is a collection of MCP servers under `mcp_servers/`. Each
server is independently installable and independently runnable:

```
ai-trader/
├── AGENTS.md                 # ← you are here (hub north star)
├── README.md                 # quick setup + list of servers
├── mcp_servers/
│   └── schwab_connector/     # Schwab Trader API (quotes, history,
│                             #   TA, movers, fundamentals, hours,
│                             #   accounts, analytics)
└── logs/                     # runtime logs (cwd-relative per server)
```

More servers will be added over time (other brokers, data vendors,
news/sentiment sources, on-chain feeds, research tools, etc.). The
pattern is always the same: one subdirectory per server, each with its
own `pyproject.toml`, `AGENTS.md`, `README.md`, and `src/<package>/`.

## Where to look when a user asks about a capability

1. **Check which server owns it.** Look at `mcp_servers/*/README.md` —
   each server's README begins with a "What this MCP server can do"
   section listing every tool it exposes.
2. **Then check that server's `AGENTS.md`** for the constraints,
   gotchas, and conventions specific to it (OAuth flows, symbology,
   rate limits, warmup behavior, C-library dependencies, …).
3. **Only drop into the code** for the specifics of an implementation
   you're about to change.

Do *not* generalize constraints from one server to another. A rule
that holds for the Schwab connector (e.g. "treat the refresh token as
sensitive") may not apply — or may apply differently — to a future
data-vendor server that uses a static API key.

## Hub-wide hard constraints

These apply to **every** MCP server in this repo. Per-server
`AGENTS.md` files add more on top, but never relax these.

- **Read-only.** No server in this hub performs writes to an external
  system (orders, alerts, account changes, posts, …). If a feature
  request implies writes, push back and discuss scope before
  implementing.
- **Secrets out of the repo and out of logs.** OAuth tokens, API keys,
  and brokerage credentials live in `.env` (gitignored) or the user's
  home dir. Never print them, never commit them, never include them
  in error messages or MCP tool responses.
- **Surface rate limits.** If a provider returns HTTP 429, the
  corresponding tool should raise — not silently retry in a loop. The
  user and the model need to see throttles immediately so they can
  back off intelligently.
- **No silent fallbacks that change the numbers.** If a C library,
  indicator, or data source is unavailable, error out. Do not
  substitute a pure-Python reimplementation or a cached/stale value
  and pretend it's equivalent — the downstream decisions depend on
  the numbers being what they claim to be.

## Don't start MCP servers yourself

The user runs each MCP server in a separate terminal and wires it
into their AI CLI themselves. As the model, you should assume the
servers are already running (or that the user will start them). If a
tool call fails because a server isn't up, say so and stop — do not
try to spawn, background, or restart the server from inside a tool
call. The same applies to any interactive OAuth flows a server
exposes (e.g. `schwab-connector auth`).

## Adding a new MCP server

When adding a new MCP server to the hub, mirror the `schwab_connector`
layout:

```
mcp_servers/<name>/
├── AGENTS.md          # per-server constraints and gotchas
├── README.md          # tool surface + setup
├── pyproject.toml     # independent deps and console script
└── src/<name>/        # package
```

Install independently (`pip install -e ./mcp_servers/<name>`) so
servers can have incompatible deps without blocking each other.

## Known MCP servers

| Server                                             | Purpose                                                            | Details                                                            |
|----------------------------------------------------|--------------------------------------------------------------------|--------------------------------------------------------------------|
| [`schwab_connector`](mcp_servers/schwab_connector) | Schwab Trader API: quotes, history, TA, movers, accounts, analytics | [README](mcp_servers/schwab_connector/README.md) · [AGENTS](mcp_servers/schwab_connector/AGENTS.md) |

Add new rows here as servers land.
