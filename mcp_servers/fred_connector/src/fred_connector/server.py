"""MCP server exposing read-only FRED lookups to an AI CLI.

Tool surface focuses on what a trader actually reaches for: the
economic-release calendar (CPI, NFP, PCE, GDP, retail sales, …),
metadata about individual releases/series, and the observation
time-series themselves.

All responses are FRED's JSON essentially unchanged so the model can
introspect fields rather than second-guessing a translation layer.
"""
from __future__ import annotations

import argparse
import atexit
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .fred_client import FredClient

logger = logging.getLogger("fred_connector")

mcp = FastMCP(
    "fred-connector",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)
_client: FredClient | None = None


def _get_client() -> FredClient:
    global _client
    if _client is None:
        logger.info("initializing FRED client")
        _client = FredClient.from_env()
        atexit.register(_client.close)
        logger.info("FRED client ready")
    return _client


@mcp.tool()
def get_release_schedule(
    realtime_start: str | None = None,
    realtime_end: str | None = None,
    limit: int | None = 200,
    include_empty: bool = True,
    order_by: str | None = "release_date",
    sort_order: str | None = "asc",
) -> dict[str, Any]:
    """Economic-release calendar across every FRED release.

    This is the "what's coming out this week" view: CPI, PPI,
    Employment Situation (NFP), GDP, PCE, retail sales, Jolts, and
    everything else FRED tracks.

    Args:
        realtime_start: ISO date (``YYYY-MM-DD``). Default is FRED's
            earliest realtime date. Use today's date to filter to
            upcoming releases.
        realtime_end: ISO date. Default is FRED's latest realtime date.
        limit: Max rows (FRED caps at 1000).
        include_empty: If True, list release dates even when no data was
            published that day (common for scheduled future dates).
        order_by: ``release_date`` | ``release_id`` | ``release_name``.
        sort_order: ``asc`` or ``desc``.

    Returns the raw FRED payload. Each entry in ``release_dates`` has
    ``release_id``, ``release_name``, and ``date``.
    """
    logger.info(
        "get_release_schedule realtime=%s..%s limit=%s include_empty=%s",
        realtime_start, realtime_end, limit, include_empty,
    )
    try:
        return _get_client().releases_dates(
            realtime_start=realtime_start,
            realtime_end=realtime_end,
            limit=limit,
            include_release_dates_with_no_data=include_empty,
            order_by=order_by,
            sort_order=sort_order,
        )
    except Exception:
        logger.exception("get_release_schedule failed")
        raise


@mcp.tool()
def get_release_dates(
    release_id: int,
    realtime_start: str | None = None,
    realtime_end: str | None = None,
    limit: int | None = 100,
    include_empty: bool = True,
) -> dict[str, Any]:
    """Past and scheduled publication dates for one release.

    Use :func:`list_releases` or the FRED website to find the
    ``release_id`` (CPI=10, Employment Situation=50, GDP=53, PCE=21,
    Retail Sales=30, JOLTS=192, FOMC Meeting=101, …).
    """
    logger.info(
        "get_release_dates release_id=%d realtime=%s..%s",
        release_id, realtime_start, realtime_end,
    )
    try:
        return _get_client().release_dates(
            release_id,
            realtime_start=realtime_start,
            realtime_end=realtime_end,
            limit=limit,
            include_release_dates_with_no_data=include_empty,
        )
    except Exception:
        logger.exception("get_release_dates failed release_id=%d", release_id)
        raise


@mcp.tool()
def list_releases(limit: int | None = 200) -> dict[str, Any]:
    """All FRED releases, for discovering ``release_id`` values."""
    logger.info("list_releases limit=%s", limit)
    try:
        return _get_client().releases(limit=limit)
    except Exception:
        logger.exception("list_releases failed")
        raise


@mcp.tool()
def get_release_info(release_id: int) -> dict[str, Any]:
    """Metadata for a single release (name, link, notes)."""
    logger.info("get_release_info release_id=%d", release_id)
    try:
        return _get_client().release(release_id)
    except Exception:
        logger.exception("get_release_info failed release_id=%d", release_id)
        raise


@mcp.tool()
def get_release_series(
    release_id: int,
    limit: int | None = 100,
    order_by: str | None = "popularity",
) -> dict[str, Any]:
    """Series published under a release (e.g. CPI headline + components)."""
    logger.info(
        "get_release_series release_id=%d limit=%s order_by=%s",
        release_id, limit, order_by,
    )
    try:
        return _get_client().release_series(
            release_id, limit=limit, order_by=order_by,
        )
    except Exception:
        logger.exception("get_release_series failed release_id=%d", release_id)
        raise


@mcp.tool()
def search_series(
    search_text: str,
    limit: int | None = 25,
    order_by: str | None = "popularity",
    sort_order: str | None = "desc",
) -> dict[str, Any]:
    """Fuzzy search for series IDs by title/notes.

    Examples: ``"core CPI"`` → ``CPILFESL``, ``"10-year treasury"`` →
    ``DGS10``, ``"fed funds"`` → ``DFF`` / ``FEDFUNDS``.
    """
    logger.info("search_series text=%r limit=%s", search_text, limit)
    try:
        return _get_client().series_search(
            search_text,
            limit=limit,
            order_by=order_by,
            sort_order=sort_order,
        )
    except Exception:
        logger.exception("search_series failed text=%r", search_text)
        raise


@mcp.tool()
def get_series_info(series_id: str) -> dict[str, Any]:
    """Series metadata (units, frequency, last-updated, seasonal adj)."""
    logger.info("get_series_info series_id=%s", series_id)
    try:
        return _get_client().series(series_id)
    except Exception:
        logger.exception("get_series_info failed series_id=%s", series_id)
        raise


@mcp.tool()
def get_series(
    series_id: str,
    observation_start: str | None = None,
    observation_end: str | None = None,
    limit: int | None = 500,
    sort_order: str | None = "desc",
    units: str | None = None,
    frequency: str | None = None,
    aggregation_method: str | None = None,
) -> dict[str, Any]:
    """Observations for one series.

    Args:
        series_id: FRED ID (e.g. ``CPIAUCSL``, ``UNRATE``, ``DGS10``).
        observation_start / observation_end: ISO dates.
        limit: Max observations (FRED caps at 100 000).
        sort_order: ``asc`` (oldest first) or ``desc`` (most recent first).
        units: ``lin`` | ``chg`` | ``ch1`` | ``pch`` | ``pc1`` | ``pca``
            | ``cch`` | ``cca`` | ``log``. Default is ``lin``.
        frequency: Resample on the server, e.g. ``m``, ``q``, ``a``.
        aggregation_method: ``avg`` (default), ``sum``, ``eop`` — only
            relevant with ``frequency``.
    """
    logger.info(
        "get_series series_id=%s start=%s end=%s limit=%s",
        series_id, observation_start, observation_end, limit,
    )
    try:
        result = _get_client().series_observations(
            series_id,
            observation_start=observation_start,
            observation_end=observation_end,
            limit=limit,
            sort_order=sort_order,
            units=units,
            frequency=frequency,
            aggregation_method=aggregation_method,
        )
    except Exception:
        logger.exception("get_series failed series_id=%s", series_id)
        raise
    obs = result.get("observations", [])
    logger.info("get_series result series_id=%s observations=%d", series_id, len(obs))
    return result


def _configure_logging(log_file: Path) -> None:
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
    for name in ("", "fred_connector", "mcp", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx"):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)


def main() -> None:
    parser = argparse.ArgumentParser(prog="fred-connector")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--log-file",
        default=os.environ.get("FRED_CONNECTOR_LOG", "logs/server.log"),
        help="Path to server log file (default: logs/server.log in cwd).",
    )
    args = parser.parse_args()

    _configure_logging(Path(args.log_file).resolve())
    logger.info(
        "fred-connector starting transport=%s host=%s port=%s log=%s",
        args.transport, args.host, args.port, args.log_file,
    )

    if args.transport in ("streamable-http", "sse"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
