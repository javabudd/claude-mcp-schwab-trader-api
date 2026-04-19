from __future__ import annotations

from dotenv import load_dotenv

from .server import main as server_main


def main() -> None:
    load_dotenv()
    server_main()


if __name__ == "__main__":
    main()
