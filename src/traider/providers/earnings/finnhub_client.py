"""Thin HTTP client around the Finnhub API.

Finnhub (https://finnhub.io) exposes earnings calendars, consensus
estimates, and historical surprises on its free tier. This client
only wraps the two endpoints the hub needs:

- ``/calendar/earnings`` — forward- and backward-looking earnings
  calendar with consensus EPS / revenue estimates and (for past
  reports) actuals.
- ``/stock/earnings`` — per-ticker history of quarterly EPS actual
  vs. estimate and the resulting surprise.

Everything else on Finnhub's surface (quotes, fundamentals, sentiment,
recommendation trends, ...) is intentionally out of scope — quotes
stay on the dedicated market-data backend, filings stay on
``sec-edgar``, news stays on the ``news`` provider.

Auth is an ``X-Finnhub-Token`` header — register at
https://finnhub.io and drop the key in ``.env`` as
``FINNHUB_API_KEY``.

Rate-limit / auth errors propagate as :class:`FinnhubError` — no
retries, no silent fallbacks (per hub AGENTS.md). Free tier is
60 requests/minute; the tool surfaces 429s rather than looping. A
403 from a premium-only endpoint surfaces as the more specific
:class:`FinnhubPremiumRequiredError` so a future maintainer wiring
a paid endpoint sees the plan gap immediately rather than a
generic 4xx.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("traider.earnings.finnhub")

_BASE_URL = "https://finnhub.io/api/v1"
_CALENDAR_PATH = "/calendar/earnings"
_EARNINGS_PATH = "/stock/earnings"


class FinnhubError(RuntimeError):
    """Raised when the Finnhub API returns a non-2xx response."""


class FinnhubPremiumRequiredError(FinnhubError):
    """Raised on 403 — endpoint requires a paid Finnhub plan."""


class FinnhubClient:
    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        if not api_key:
            raise FinnhubError(
                "FINNHUB_API_KEY is not set. Register at "
                "https://finnhub.io and put the key in .env."
            )
        self._api_key = api_key
        self._http = httpx.Client(
            base_url=_BASE_URL,
            timeout=timeout,
            headers={"X-Finnhub-Token": api_key},
        )

    @classmethod
    def from_env(cls) -> "FinnhubClient":
        return cls(api_key=os.environ.get("FINNHUB_API_KEY", ""))

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        cleaned = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._http.get(path, params=cleaned)
        except httpx.HTTPError as exc:
            raise FinnhubError(f"Finnhub request failed: {exc}") from exc
        if resp.status_code == 403:
            body = resp.text[:500]
            raise FinnhubPremiumRequiredError(
                f"Finnhub 403 on {path}: premium plan required for this "
                f"endpoint. Upstream body: {body}"
            )
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise FinnhubError(
                f"Finnhub {resp.status_code} on {path}: {body}"
            )
        return resp.json()

    def calendar_earnings(
        self,
        *,
        from_date: str,
        to_date: str,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Earnings calendar for a date window.

        ``from_date`` and ``to_date`` are ISO ``YYYY-MM-DD`` and the
        upstream requires both. ``symbol`` narrows the result to one
        ticker; omit for the cross-market calendar.

        Returns Finnhub's payload: ``{"earningsCalendar": [...]}``.
        Each entry carries ``symbol``, ``date`` (announcement date in
        the issuer's local exchange timezone), ``hour`` (``"bmo"`` =
        before market open, ``"amc"`` = after market close, ``"dmh"`` =
        during market hours, ``""`` = not specified), ``year``,
        ``quarter``, ``epsEstimate``, ``epsActual`` (``null`` for
        future prints), ``revenueEstimate``, ``revenueActual``.
        """
        return self._get(
            _CALENDAR_PATH,
            {"from": from_date, "to": to_date, "symbol": symbol},
        )

    def stock_earnings(
        self,
        *,
        symbol: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Historical EPS actual vs. estimate surprises for one ticker.

        Returns a list of quarters, newest first. Each entry has
        ``actual``, ``estimate``, ``surprise``, ``surprisePercent``,
        ``period`` (report date), ``quarter``, ``year``, ``symbol``.
        Free tier typically exposes the last 4 quarters; ``limit``
        caps the response length client-side via the upstream param.
        """
        return self._get(
            _EARNINGS_PATH,
            {"symbol": symbol, "limit": limit},
        )
