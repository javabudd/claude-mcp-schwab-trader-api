"""Thin HTTP client around Finnhub's analyst-recommendation endpoint.

Finnhub (https://finnhub.io) exposes a family of analyst / estimates
endpoints; on the free tier only ``/stock/recommendation`` is
reachable. Price targets, upgrade/downgrade actions, and consensus
EPS / revenue estimates sit behind the paid plan and return 403 with
the free key. This client only wraps the one free endpoint — adding
the paid ones is a one-method extension once a key upgrades.

Auth is an ``X-Finnhub-Token`` header. Reuses the same
``FINNHUB_API_KEY`` env var as the ``earnings`` provider; register at
https://finnhub.io and drop the key in ``.env``.

Rate-limit / auth errors propagate as :class:`FinnhubError` — no
retries, no silent fallbacks (per hub AGENTS.md). Free tier is
60 requests/minute; the tool surfaces 429s rather than looping. The
premium-only endpoints listed above surface as the more specific
:class:`FinnhubPremiumRequiredError` (a 403 subclass of
``FinnhubError``) so a future maintainer wiring a paid endpoint sees
the plan gap immediately rather than a generic 4xx — matches the
README's firm stance that those gaps are real and not to be papered
over.

The ``earnings`` provider has a near-identical client — they are
intentionally duplicated so the two providers stay self-contained
and can be loaded independently.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("traider.estimates.finnhub")

_BASE_URL = "https://finnhub.io/api/v1"
_RECOMMENDATION_PATH = "/stock/recommendation"


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

    def recommendation_trends(self, *, symbol: str) -> list[dict[str, Any]]:
        """Monthly analyst recommendation distribution for one ticker.

        Returns a list of monthly snapshots. Each entry carries
        ``symbol``, ``period`` (month-start ``YYYY-MM-DD``), and the
        five rating-bucket counts: ``strongBuy``, ``buy``, ``hold``,
        ``sell``, ``strongSell``. Unknown / unrated sell-side coverage
        is not reported.

        An unknown or unfollowed ticker returns an empty list rather
        than raising.
        """
        return self._get(_RECOMMENDATION_PATH, {"symbol": symbol})
