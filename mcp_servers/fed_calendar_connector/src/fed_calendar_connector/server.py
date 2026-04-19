"""MCP server for Federal Reserve meeting calendar lookups.

Primary-source scrape of federalreserve.gov's FOMC calendar page.
Tool surface is intentionally narrow: dates and flags only. For
*data* driven by these meetings (rate decisions, SEP dot-plot
releases, minutes publication dates as tracked by FRED, …) use
`fred_connector`.
"""
from __future__ import annotations

import argparse
import atexit
import logging
import os
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .fomc_scraper import FomcScraper, utc_today

logger = logging.getLogger("fed_calendar_connector")

mcp = FastMCP(
    "fed-calendar-connector",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)
_scraper: FomcScraper | None = None


def _get_scraper() -> FomcScraper:
    global _scraper
    if _scraper is None:
        logger.info("initializing FOMC scraper")
        _scraper = FomcScraper()
        atexit.register(_scraper.close)
    return _scraper


@mcp.tool()
def get_fomc_meetings(
    year: int | None = None,
    upcoming_only: bool = False,
) -> dict[str, Any]:
    """FOMC meetings parsed from federalreserve.gov.

    Args:
        year: Filter to a specific year (e.g. 2026). ``None`` returns
            every year the calendar page currently lists (typically the
            prior year and one to two years forward).
        upcoming_only: If True, drop meetings whose ``end_date`` is
            before today (UTC).

    Each meeting includes ``start_date`` / ``end_date`` (ISO), the
    month label as published, the SEP flag, whether a press conference
    is scheduled, and any parenthetical note (e.g. ``"notation vote"``,
    ``"unscheduled"``).
    """
    logger.info("get_fomc_meetings year=%s upcoming_only=%s", year, upcoming_only)
    try:
        meetings = _get_scraper().scrape()
    except Exception:
        logger.exception("get_fomc_meetings scrape failed")
        raise

    if year is not None:
        meetings = [m for m in meetings if m.year == year]
    if upcoming_only:
        today = utc_today()
        meetings = [
            m for m in meetings
            if date.fromisoformat(m.end_date) >= today
        ]

    payload = [m.to_dict() for m in meetings]
    logger.info(
        "get_fomc_meetings result count=%d year=%s upcoming_only=%s",
        len(payload), year, upcoming_only,
    )
    return {
        "source": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(payload),
        "meetings": payload,
    }


@mcp.tool()
def get_next_fomc_meeting() -> dict[str, Any]:
    """The next scheduled FOMC meeting.

    Returns the first meeting whose ``start_date`` is on or after
    today (UTC), with ``days_until_start`` for convenience. If no
    future meeting is listed on federalreserve.gov, ``meeting`` is
    ``None``.
    """
    logger.info("get_next_fomc_meeting")
    try:
        meetings = _get_scraper().scrape()
    except Exception:
        logger.exception("get_next_fomc_meeting scrape failed")
        raise

    today = utc_today()
    upcoming = [
        m for m in meetings
        if date.fromisoformat(m.start_date) >= today
    ]
    upcoming.sort(key=lambda m: m.start_date)

    result: dict[str, Any] = {
        "source": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "today": today.isoformat(),
        "meeting": None,
    }
    if upcoming:
        nxt = upcoming[0]
        days_until = (date.fromisoformat(nxt.start_date) - today).days
        result["meeting"] = {**nxt.to_dict(), "days_until_start": days_until}
    logger.info(
        "get_next_fomc_meeting result %s",
        result["meeting"]["start_date"] if result["meeting"] else "(none)",
    )
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
    for name in (
        "",
        "fed_calendar_connector",
        "mcp",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "httpx",
    ):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)


def main() -> None:
    parser = argparse.ArgumentParser(prog="fed-calendar-connector")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument(
        "--log-file",
        default=os.environ.get("FED_CALENDAR_CONNECTOR_LOG", "logs/server.log"),
        help="Path to server log file (default: logs/server.log in cwd).",
    )
    args = parser.parse_args()

    _configure_logging(Path(args.log_file).resolve())
    logger.info(
        "fed-calendar-connector starting transport=%s host=%s port=%s log=%s",
        args.transport, args.host, args.port, args.log_file,
    )

    if args.transport in ("streamable-http", "sse"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
