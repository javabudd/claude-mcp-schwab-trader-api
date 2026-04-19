# AGENTS.md

Guidance for AI coding agents working in this repo.

## What this is

`tos-connector` (name kept for continuity — it no longer talks to TOS)
is a read-only bridge between Claude (via MCP) and the **Schwab Trader
API**. It exposes quote lookups as MCP tools so Claude can call them
like any other tool. Pure Python over HTTP — cross-platform, no COM,
no desktop app required.

### Why not the TOS RTD path anymore

The repo originally tried to reach TOS Desktop's RTD COM server
(`Tos.RTD`, the interface behind Excel's `=RTD("tos.rtd",…)` formulas).
That path is abandoned. The blocking issue: `IRTDUpdateEvent` is a
dual COM interface, and pywin32 can only synthesize a real vtable for
a dual interface from a registered type library (Office's `MSO.DLL`
ships it; plain Windows does not). Without that TLB, every call path
into our Python callback eventually hits undefined vtable memory and
`Py_FatalError`s the process. Investigating that took a lot of time —
don't re-open it without (a) Office installed and (b) a plan to use
`win32com.universal.RegisterInterfaces`.

## Hard constraints

- **Read-only scope.** No order entry, no alert creation, no writes.
  The Trader API can do writes — we deliberately don't. If a feature
  request implies writes, push back.
- **OAuth required.** Schwab's API is OAuth 2.0 (authorization code
  flow). Tokens are refreshed, not re-issued; treat the refresh token
  as sensitive and keep it out of logs.
- **Rate limits apply.** Schwab publishes per-endpoint quotas (see the
  developer portal). Don't add retry-storm fallbacks that mask a
  throttle — surface 429s.

## Layout

```
src/tos_connector/
  __init__.py       # re-exports SchwabClient / SchwabAuthError
  __main__.py       # dispatches "auth" subcommand vs. server
  schwab_client.py  # OAuth-authenticated HTTP client
  auth.py           # interactive authorization-code flow
  server.py         # FastMCP server exposing get_quote / get_quotes
pyproject.toml      # deps: mcp, httpx, python-dotenv
```

## Don't start the MCP server yourself

The user runs `tos-connector` in a separate terminal. You do **not**
need to spawn the server, background it, or restart it — assume it is
already running (or that the user will start it). If a tool call fails
because the server isn't up, tell the user; don't try to launch it.
The same applies to `tos-connector auth` — that's an interactive
browser flow the user runs themselves.

## Running / developing

**All Python commands in this repo run inside the `tos` conda
environment.** Activate it before running anything — `pip`,
`python`, `tos-connector`, test runners, one-off REPLs, everything.
The env is always named `tos` (Python 3.13); see `README.md` for
creation instructions. If you see an `ImportError` or
`command not found`, the first thing to check is whether the env is
active.

```bash
conda activate tos

pip install -e .

export SCHWAB_APP_KEY=...
export SCHWAB_APP_SECRET=...
export SCHWAB_CALLBACK_URL=https://127.0.0.1   # must match the app reg

tos-connector auth                              # one-time browser flow
tos-connector                                   # MCP server on stdio
tos-connector --transport streamable-http --port 8765   # or over HTTP
```

Tokens are persisted to the file referenced by `SCHWAB_TOKEN_FILE`
(default `~/.tos-connector/schwab-token.json`, mode 0600).
`SchwabClient` auto-refreshes the access token on expiry; if the
refresh token itself is dead, it raises `SchwabAuthError` — the user
must re-run `tos-connector auth`.

## Server logs

The server writes a rotating log to `logs/server.log` (relative to
cwd). Override with `--log-file PATH` or `TOS_CONNECTOR_LOG`.

MCP servers are typically spawned as subprocesses (stdio transport) or
run detached (HTTP), so stdout/stderr often aren't visible to the
agent calling tools. The log file is the reliable place to read what
the server did. **When a tool call fails, read `logs/server.log`
before asking the user for the traceback** — tool handlers wrap their
bodies in `logger.exception(...)`, so the full traceback lands in the
file.

Captured log sources: `tos_connector`, `mcp`, `uvicorn`, and `httpx`
(so you can see the outbound API calls and response statuses).
Rotation: 5 MB × 3 backups.

## Things that will bite you

- **Token expiry.** Schwab access tokens expire in ~30 minutes and
  refresh tokens in ~7 days. After a lapse, the user has to re-run
  `tos-connector auth`. Don't silently swallow "invalid refresh
  token" — surface it.
- **Options symbology.** Schwab expects the 21-character OSI format
  (e.g. `SPY   250321C00500000`), not dotted TOS notation. Equities
  and futures (`/ES`) work as-is.
- **Market hours.** Outside RTH, `lastPrice` may be stale; pre/post
  session fields live under different keys in the quote JSON.
- **Sandbox vs production.** The developer portal offers a sandbox
  environment. If you set `SCHWAB_BASE_URL`, make sure it points where
  you intend.

## What not to do

- Don't store OAuth tokens in the repo, in env files committed to git,
  or in log output.
- Don't introduce an ORM, a database, or a queue. This is a thin HTTP
  client plus an MCP surface — keep it thin.
- Don't add write operations. The Trader API supports them; this
  connector does not, by policy.
- Don't paper over rate limits with exponential-retry loops. One retry
  for a transient 5xx is fine; 429s should propagate.
- Don't re-attempt the RTD COM path without Office's MSO TLB and a
  concrete plan for `win32com.universal`. See the "Why not the TOS
  RTD path" section above.
