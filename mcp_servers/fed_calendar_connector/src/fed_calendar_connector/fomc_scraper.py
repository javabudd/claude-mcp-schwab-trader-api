"""Scraper for the FOMC meeting calendar at federalreserve.gov.

Primary source, read-only. Parses the HTML at
https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm into
structured meeting records.

Schema of each returned meeting:

    {
        "year": 2026,
        "month": "January",            # or "April/May" for 2-month meetings
        "day_range": "27-28",          # trailing "*" stripped; SEP in flag
        "start_date": "2026-01-27",    # ISO; first day of the range
        "end_date":   "2026-01-28",    # ISO; last day (equal to start for 1-day)
        "is_sep": True,                # "* Meeting associated with SEP"
        "has_press_conference": True,  # <a href fomc[pr]es{s}conf...> present
        "note": "notation vote",       # parenthetical on the date, if any
        "statement_url": "...",        # if published
        "minutes_url":   "...",        # if published
    }

The scraper never substitutes a stale snapshot if the fetch fails —
per hub AGENTS.md, no silent fallbacks. A cache can be added later if
rate-limiting becomes a concern; for now each call refetches.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger("fed_calendar_connector.fomc")

_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

_MONTH_NUMBER = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    # Fed mixes full and abbreviated spellings; the abbrev form shows
    # up most often in two-month labels like "Jan/Feb" or "Oct/Nov".
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Matches date cells like "27-28", "8-9*", "22", "(unscheduled)",
# "22 (notation vote)", "29-30*".
_DATE_RE = re.compile(
    r"""
    ^\s*
    (?P<days>\d{1,2}(?:-\d{1,2})?)?      # optional "27-28" or "22"
    \s*
    (?P<sep>\*)?                         # optional SEP flag
    \s*
    (?:\((?P<note>[^)]+)\))?             # optional "(notation vote)"
    \s*$
    """,
    re.VERBOSE,
)

# Press-conference links: href contains fomcpressconf OR fomcpresconf
# (Fed has historically used both spellings).
_PRESS_CONF_RE = re.compile(r"fomcpres{1,2}conf", re.IGNORECASE)


class FomcScrapeError(RuntimeError):
    """Raised when federalreserve.gov can't be fetched or parsed."""


@dataclass
class Meeting:
    year: int
    month: str
    day_range: str
    start_date: str
    end_date: str
    is_sep: bool = False
    has_press_conference: bool = False
    note: str | None = None
    statement_url: str | None = None
    minutes_url: str | None = None
    press_conference_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "month": self.month,
            "day_range": self.day_range,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "is_sep": self.is_sep,
            "has_press_conference": self.has_press_conference,
            "note": self.note,
            "statement_url": self.statement_url,
            "minutes_url": self.minutes_url,
            "press_conference_url": self.press_conference_url,
        }


@dataclass
class FomcScraper:
    timeout: float = 30.0
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                headers={"User-Agent": "traider-fed-calendar-connector/0.1"},
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def fetch(self) -> str:
        try:
            resp = self._http().get(_URL)
        except httpx.HTTPError as exc:
            raise FomcScrapeError(f"could not fetch {_URL}: {exc}") from exc
        if resp.status_code >= 400:
            raise FomcScrapeError(
                f"federalreserve.gov returned {resp.status_code} for {_URL}"
            )
        return resp.text

    def parse(self, html: str) -> list[Meeting]:
        soup = BeautifulSoup(html, "html.parser")
        panels = soup.select("div.panel.panel-default")
        if not panels:
            raise FomcScrapeError(
                "no FOMC year panels found — federalreserve.gov layout may "
                "have changed; update the scraper"
            )
        meetings: list[Meeting] = []
        for panel in panels:
            year = _year_from_panel(panel)
            if year is None:
                continue
            for row in panel.select("div.row.fomc-meeting"):
                meeting = _parse_row(row, year)
                if meeting is not None:
                    meetings.append(meeting)
        return meetings

    def scrape(self) -> list[Meeting]:
        return self.parse(self.fetch())


def _year_from_panel(panel: Tag) -> int | None:
    heading = panel.select_one("div.panel-heading h4 a, div.panel-heading h4")
    if heading is None:
        return None
    text = heading.get_text(" ", strip=True)
    match = re.search(r"(19|20)\d{2}", text)
    return int(match.group(0)) if match else None


def _parse_row(row: Tag, year: int) -> Meeting | None:
    month_el = row.select_one("div.fomc-meeting__month")
    date_el = row.select_one("div.fomc-meeting__date")
    if month_el is None or date_el is None:
        return None
    month_text = month_el.get_text(" ", strip=True)
    date_text = date_el.get_text(" ", strip=True)
    m = _DATE_RE.match(date_text)
    if not m or not m.group("days"):
        # purely parenthetical rows (e.g. "(unscheduled)") with no days:
        # skip — nothing actionable to anchor a date on.
        return None
    days = m.group("days")
    is_sep = bool(m.group("sep"))
    note = m.group("note")

    start_day, end_day = _parse_day_range(days)
    start_month, end_month = _parse_month_range(month_text)

    try:
        start_dt = date(year, start_month, start_day)
        # For "April/May 6-7" FOMC puts the lower day under the first
        # month when the meeting actually spans the boundary (rare but
        # happens — last Tuesday/Wednesday of April into Wednesday of
        # May). The HTML isn't unambiguous about which day belongs to
        # which month; when we detect a two-month label we anchor
        # start_day to the first month and end_day to the second.
        if end_month != start_month:
            end_dt = date(year, end_month, end_day)
        else:
            end_dt = date(year, end_month, end_day)
    except ValueError:
        # Malformed day/month combo (e.g. Feb 30). Surface it instead of
        # silently dropping — helps catch upstream markup drift.
        logger.warning(
            "FOMC row has invalid date: year=%d month=%r days=%r",
            year, month_text, days,
        )
        return None

    statement_url = None
    minutes_url = None
    press_conference_url = None
    has_press_conference = False

    for anchor in row.find_all("a"):
        href = anchor.get("href") or ""
        if not href:
            continue
        absolute = _absolute(href)
        label = anchor.get_text(" ", strip=True).lower()
        if _PRESS_CONF_RE.search(href):
            has_press_conference = True
            press_conference_url = absolute
        elif "minutes" in label or "fomcminutes" in href.lower():
            minutes_url = absolute
        elif "statement" in label or "monetary" in href.lower():
            statement_url = absolute

    return Meeting(
        year=year,
        month=month_text,
        day_range=days + ("*" if is_sep else ""),
        start_date=start_dt.isoformat(),
        end_date=end_dt.isoformat(),
        is_sep=is_sep,
        has_press_conference=has_press_conference,
        note=note,
        statement_url=statement_url,
        minutes_url=minutes_url,
        press_conference_url=press_conference_url,
    )


def _parse_day_range(days: str) -> tuple[int, int]:
    if "-" in days:
        a, b = days.split("-", 1)
        return int(a), int(b)
    d = int(days)
    return d, d


def _parse_month_range(month_text: str) -> tuple[int, int]:
    parts = re.split(r"[\s/]+", month_text.strip())
    months = [
        _MONTH_NUMBER[p.lower()]
        for p in parts
        if p.lower() in _MONTH_NUMBER
    ]
    if not months:
        raise FomcScrapeError(f"unrecognized month label: {month_text!r}")
    return months[0], months[-1]


def _absolute(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://www.federalreserve.gov" + href
    return "https://www.federalreserve.gov/monetarypolicy/" + href


def utc_today() -> date:
    return datetime.now(timezone.utc).date()
