"""FastMCP server that lazy-loads provider modules on startup.

One server, one port, one MCP surface. Which tools are exposed is
controlled at startup by ``TRAIDER_PROVIDERS`` in the environment:

    TRAIDER_PROVIDERS=schwab,fred,sec-edgar,factor,treasury,news

Each name maps to a provider module under ``traider.providers``.
The module exposes ``register(mcp, settings)`` which installs its
tools on the shared ``FastMCP`` instance. Modules for *disabled*
providers are never imported, so their third-party dependencies
(e.g. ``yfinance``, ``TA-Lib``, ``lxml``) stay off the hot path.

``schwab`` and ``yahoo`` both provide the "market-data backend"
surface and are mutually exclusive. Enabling both at once is a
configuration error and the server refuses to start.

The ``intent`` provider (local trade-intent journal + framework
rules) is core analyst infrastructure and is **always loaded**,
regardless of ``TRAIDER_PROVIDERS``. Listing it explicitly in the
env var is harmless (deduped) but unnecessary.
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

# Canonical provider→module map. New providers get one line here.
PROVIDERS: dict[str, str] = {
    "schwab":       "traider.providers.schwab.tools",
    "yahoo":        "traider.providers.yahoo.tools",
    "fred":         "traider.providers.fred.tools",
    "fed-calendar": "traider.providers.fed_calendar.tools",
    "sec-edgar":    "traider.providers.sec_edgar.tools",
    "factor":       "traider.providers.factor.tools",
    "treasury":     "traider.providers.treasury.tools",
    "news":         "traider.providers.news.tools",
    "earnings":     "traider.providers.earnings.tools",
    "estimates":    "traider.providers.estimates.tools",
    "eia":          "traider.providers.eia.tools",
    "cftc":         "traider.providers.cftc.tools",
    "intent":       "traider.providers.intent.tools",
}

# Backends that expose the same market-data surface; pick one.
MARKET_DATA_PROVIDERS: frozenset[str] = frozenset({"schwab", "yahoo"})

# Providers loaded unconditionally on every startup. The intent
# provider carries the local trade-intent journal and the framework
# rules surface (list_rules / get_rule / get_position_context /
# validate_intent_rule_refs) — it is core analyst infrastructure and
# would be silently broken if someone forgot to list it in
# TRAIDER_PROVIDERS.
ALWAYS_LOADED_PROVIDERS: frozenset[str] = frozenset({"intent"})

logger = logging.getLogger("traider")


def _build_mcp() -> FastMCP:
    return FastMCP(
        "traider",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )


def _validate_providers(providers: tuple[str, ...]) -> None:
    unknown = [p for p in providers if p not in PROVIDERS]
    if unknown:
        raise SystemExit(
            f"unknown TRAIDER_PROVIDERS entries: {unknown}. "
            f"valid names: {sorted(PROVIDERS)}"
        )
    backends = [p for p in providers if p in MARKET_DATA_PROVIDERS]
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


def load_providers(mcp: FastMCP, settings: TraiderSettings) -> None:
    """Import each enabled provider's module and call ``register``.

    Always-on providers (see ``ALWAYS_LOADED_PROVIDERS``) are loaded
    in addition to whatever the user listed in ``TRAIDER_PROVIDERS``,
    deduped so an explicit listing of an always-on provider is a
    no-op rather than a double-register.
    """
    seen: set[str] = set()
    for name in (*settings.providers, *ALWAYS_LOADED_PROVIDERS):
        if name in seen:
            continue
        seen.add(name)
        module_path = PROVIDERS[name]
        always_on = name in ALWAYS_LOADED_PROVIDERS and name not in settings.providers
        logger.info(
            "loading provider=%s module=%s%s",
            name, module_path, " (always-on)" if always_on else "",
        )
        module = importlib.import_module(module_path)
        register = getattr(module, "register", None)
        if register is None:
            raise SystemExit(
                f"provider {name!r}: module {module_path} has no register()"
            )
        register(mcp, settings)
        logger.info("provider=%s loaded", name)


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
        "traider starting transport=%s host=%s port=%s providers=%s log_dir=%s",
        args.transport, args.host, args.port, settings.providers, settings.log_dir,
    )

    if not settings.providers:
        logger.warning(
            "TRAIDER_PROVIDERS is empty — no providers will be loaded"
        )

    _validate_providers(settings.providers)

    mcp = _build_mcp()
    load_providers(mcp, settings)

    if args.transport in ("streamable-http", "sse"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
