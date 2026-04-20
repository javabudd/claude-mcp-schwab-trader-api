"""FastMCP server that lazy-loads connector modules by profile.

One server, one port, one MCP surface. Which tools are exposed is
controlled at startup by ``TRAIDER_PROFILES`` in the environment:

    TRAIDER_PROFILES=schwab,fred,sec-edgar,factor,treasury,news

Each name maps to a connector module under ``traider.connectors``.
The module exposes ``register(mcp, settings)`` which installs its
tools on the shared ``FastMCP`` instance. Modules for *disabled*
profiles are never imported, so their third-party dependencies
(e.g. ``yfinance``, ``TA-Lib``, ``lxml``) stay off the hot path.

``schwab`` and ``yahoo`` both provide the "market-data backend"
surface and are mutually exclusive. Enabling both at once is a
configuration error and the server refuses to start.
"""
from __future__ import annotations

import argparse
import importlib
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .settings import TraiderSettings, load_settings

# Canonical profile→module map. New connectors get one line here.
PROFILES: dict[str, str] = {
    "schwab":       "traider.connectors.schwab.tools",
    "yahoo":        "traider.connectors.yahoo.tools",
    "fred":         "traider.connectors.fred.tools",
    "fed-calendar": "traider.connectors.fed_calendar.tools",
    "sec-edgar":    "traider.connectors.sec_edgar.tools",
    "factor":       "traider.connectors.factor.tools",
    "treasury":     "traider.connectors.treasury.tools",
    "news":         "traider.connectors.news.tools",
}

# Backends that expose the same market-data surface; pick one.
MARKET_DATA_PROFILES: frozenset[str] = frozenset({"schwab", "yahoo"})

logger = logging.getLogger("traider")


def _build_mcp() -> FastMCP:
    return FastMCP(
        "traider",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )


def _validate_profiles(profiles: tuple[str, ...]) -> None:
    unknown = [p for p in profiles if p not in PROFILES]
    if unknown:
        raise SystemExit(
            f"unknown TRAIDER_PROFILES entries: {unknown}. "
            f"valid names: {sorted(PROFILES)}"
        )
    backends = [p for p in profiles if p in MARKET_DATA_PROFILES]
    if len(backends) > 1:
        raise SystemExit(
            "schwab and yahoo are mutually exclusive market-data backends "
            f"— enable at most one, got: {backends}"
        )


def _configure_root_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    for name in ("", "traider", "mcp", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx"):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)


def load_profiles(mcp: FastMCP, settings: TraiderSettings) -> None:
    """Import each enabled profile's module and call ``register``."""
    for name in settings.profiles:
        module_path = PROFILES[name]
        logger.info("loading profile=%s module=%s", name, module_path)
        module = importlib.import_module(module_path)
        register = getattr(module, "register", None)
        if register is None:
            raise SystemExit(
                f"profile {name!r}: module {module_path} has no register()"
            )
        register(mcp, settings)
        logger.info("profile=%s loaded", name)


def main() -> None:
    parser = argparse.ArgumentParser(prog="traider")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="streamable-http",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    settings = load_settings()
    _configure_root_logging(settings.log_dir / "traider.log")
    logger.info(
        "traider starting transport=%s host=%s port=%s profiles=%s log_dir=%s",
        args.transport, args.host, args.port, settings.profiles, settings.log_dir,
    )

    if not settings.profiles:
        logger.warning(
            "TRAIDER_PROFILES is empty — no connectors will be loaded"
        )

    _validate_profiles(settings.profiles)

    mcp = _build_mcp()
    load_profiles(mcp, settings)

    if args.transport in ("streamable-http", "sse"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
