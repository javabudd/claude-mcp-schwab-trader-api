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

Register an app at <https://developer.schwab.com> and set:

```bash
export SCHWAB_APP_KEY=...
export SCHWAB_APP_SECRET=...
export SCHWAB_CALLBACK_URL=https://127.0.0.1   # must match the app reg
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

Tokens are persisted to `~/.tos-connector/schwab-token.json`
(overridable via `SCHWAB_TOKEN_FILE`). Access tokens auto-refresh;
refresh tokens expire ~7 days and require re-running
`tos-connector auth`.
