from __future__ import annotations

import sys

from dotenv import load_dotenv

from .auth import run_auth_flow
from .server import main as server_main


def main() -> None:
    load_dotenv()
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        # Pop the "auth" subcommand so any flags after it are parsed
        # (currently auth takes none, but keeps the pattern usable).
        sys.argv.pop(1)
        run_auth_flow()
        return
    server_main()


if __name__ == "__main__":
    main()
