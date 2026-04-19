# tos-connector

Read-only Schwab Trader API bridge exposed as an MCP server for Claude.
See [AGENTS.md](AGENTS.md) for how the code is organized and what to
watch out for.

## Setup

### 1. Install conda

If you don't already have it, install **Miniforge** (community conda
distribution, permissive license, fast solver):

- macOS / Linux / WSL:
  ```bash
  curl -L -O "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
  bash Miniforge3-$(uname)-$(uname -m).sh
  ```
- Windows: download the installer from
  <https://github.com/conda-forge/miniforge/releases/latest> and run it.

Miniconda works fine too if you already have it —
<https://docs.conda.io/en/latest/miniconda.html>.

Restart your shell (or `source ~/.bashrc` / `source ~/.zshrc`) so
`conda` is on your PATH.

### 2. Create the `tos` environment

The project always uses an env named `tos`, pinned to Python 3.13:

```bash
conda create -n tos python=3.13
conda activate tos
```

Every subsequent command in this repo (including `pip install`,
`tos-connector`, any test runner) assumes this env is active.

### 3. Install the package

```bash
conda activate tos
pip install -e .
```

### 4. Configure Schwab credentials

Register an app at <https://developer.schwab.com>. Either export the
vars directly:

```bash
export SCHWAB_APP_KEY=...
export SCHWAB_APP_SECRET=...
export SCHWAB_CALLBACK_URL=https://127.0.0.1   # must match the app reg
```

…or drop them in a `.env` file at the repo root (gitignored, loaded
automatically on startup):

```
SCHWAB_APP_KEY=...
SCHWAB_APP_SECRET=...
SCHWAB_CALLBACK_URL=https://127.0.0.1
```

### 5. Authorize once, then run the server

```bash
tos-connector auth             # browser flow, paste redirected URL
tos-connector                  # start the MCP server on stdio
```

Or expose it over HTTP for remote MCP clients:

```bash
tos-connector --transport streamable-http --port 8765
```

### `tos-connector auth` vs `tos-connector` — when to run which

- **`tos-connector auth`** is the interactive OAuth bootstrap. It
  opens your browser, you log into Schwab, and you paste the redirect
  URL back into the terminal. It writes `schwab-token.json` (access +
  refresh token) and exits. Run it:
  - the **first time** you set up the repo;
  - any time **`tos-connector` prints `SchwabAuthError`** (the
    refresh token is dead — happens after ~7 days of no use, or if
    you revoke the app);
  - after **rotating** `SCHWAB_APP_KEY` / `SCHWAB_APP_SECRET`, since
    tokens are bound to the app registration.
- **`tos-connector`** (no subcommand) starts the MCP server. It reuses
  the token file written by `auth` and refreshes the access token on
  its own as needed. This is the one Claude actually talks to — leave
  it running in a terminal while you use the connector. You do **not**
  need to re-run `auth` each session; only when the refresh token
  itself has expired.

Tokens are persisted to `~/.tos-connector/schwab-token.json`
(overridable via `SCHWAB_TOKEN_FILE`). Access tokens auto-refresh;
refresh tokens expire ~7 days and require re-running
`tos-connector auth`.
