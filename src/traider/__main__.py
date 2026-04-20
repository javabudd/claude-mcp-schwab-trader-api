"""Entry point: ``python -m traider`` / ``traider`` console script.

Subcommands:

- ``traider`` (default) — run the MCP server over the transport / host /
  port given on the CLI. Profiles to load are read from the
  ``TRAIDER_PROFILES`` env var.
- ``traider auth schwab`` — one-shot interactive Schwab OAuth flow.
  Writes the token file consumed at server startup when the ``schwab``
  profile is active.
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from .server import main as server_main


def _run_auth(args: list[str]) -> None:
    if not args or args[0] != "schwab":
        raise SystemExit(
            "usage: traider auth schwab\n"
            "(only 'schwab' has an interactive auth flow today)"
        )
    from .connectors.schwab.auth import run_auth_flow
    run_auth_flow()


def main() -> None:
    load_dotenv()
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        _run_auth(sys.argv[2:])
        return
    server_main()


if __name__ == "__main__":
    main()
