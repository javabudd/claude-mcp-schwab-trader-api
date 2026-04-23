"""Thin HTTP client around the Massive news API.

Massive (https://massive.com) is a market-data vendor whose REST API
exposes, among other things, ticker-scoped news with publisher
metadata and per-article sentiment insights. This client only wraps
the news endpoint (``/v2/reference/news``) — the rest of Massive's
surface (quotes, aggregates, trades, …) is intentionally out of scope
because the hub already has dedicated market-data backends.

Auth is an ``apiKey`` query param — register at
https://massive.com and drop the key in ``.env`` as
``MASSIVE_API_KEY``.

The client is deliberately thin: one method, returns the provider's
JSON essentially unchanged. Rate-limit / auth errors propagate as
:class:`MassiveError` — no retries, no silent fallbacks (per hub
AGENTS.md).
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("traider.news.massive")

_BASE_URL = "https://api.massive.com"
_NEWS_PATH = "/v2/reference/news"


class MassiveError(RuntimeError):
    """Raised when the Massive API returns a non-2xx response."""


class MassiveClient:
    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        if not api_key:
            raise MassiveError(
                "MASSIVE_API_KEY is not set. Register at "
                "https://massive.com and put the key in .env."
            )
        self._api_key = api_key
        self._http = httpx.Client(base_url=_BASE_URL, timeout=timeout)

    @classmethod
    def from_env(cls) -> "MassiveClient":
        return cls(api_key=os.environ.get("MASSIVE_API_KEY", ""))

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {"apiKey": self._api_key}
        for k, v in params.items():
            if v is not None:
                merged[k] = v
        try:
            resp = self._http.get(path, params=merged)
        except httpx.HTTPError as exc:
            raise MassiveError(f"Massive request failed: {exc}") from exc
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise MassiveError(
                f"Massive {resp.status_code} on {path}: {body}"
            )
        return resp.json()

    def news(
        self,
        *,
        ticker: str | None = None,
        published_utc_gte: str | None = None,
        published_utc_lte: str | None = None,
        order: str | None = None,
        sort: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Fetch news articles.

        All parameters are optional; Massive defaults apply when omitted
        (``limit=10``, newest first). ``published_utc_gte`` /
        ``published_utc_lte`` take RFC3339 timestamps or ISO dates.
        """
        return self._get(
            _NEWS_PATH,
            {
                "ticker": ticker,
                "published_utc.gte": published_utc_gte,
                "published_utc.lte": published_utc_lte,
                "order": order,
                "sort": sort,
                "limit": limit,
            },
        )
