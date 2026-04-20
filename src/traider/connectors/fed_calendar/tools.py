"""FOMC calendar tools registered on the shared FastMCP.

Primary-source scrape of federalreserve.gov's FOMC calendar page.
Tool surface is intentionally narrow: dates and flags only. For
*data* driven by these meetings (rate decisions, SEP dot-plot
releases, minutes publication dates as tracked by FRED, ...) use
the ``fred`` profile.
"""
from __future__ import annotations

import atexit
import logging
from datetime import date, datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from ...logging_utils import attach_profile_logger
from ...settings import TraiderSettings
from .fomc_scraper import FomcScraper, utc_today

logger = logging.getLogger("traider.fed_calendar")
_scraper: FomcScraper | None = None


def _get_scraper() -> FomcScraper:
    global _scraper
    if _scraper is None:
        logger.info("initializing FOMC scraper")
        _scraper = FomcScraper()
        atexit.register(_scraper.close)
    return _scraper


def register(mcp: FastMCP, settings: TraiderSettings) -> None:
    attach_profile_logger("traider.fed_calendar", settings.log_file("fed-calendar"))

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
        month label as published, the SEP flag, the press-conference URL
        (populated once the Fed posts the permalink — typically days
        before the meeting; its absence on a future meeting does *not*
        mean no presser is scheduled, since every FOMC meeting since 2019
        has had one), and any parenthetical note (e.g. ``"notation vote"``,
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
