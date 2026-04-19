"""Thin HTTP client around the FRED API.

FRED (https://fred.stlouisfed.org) is the St. Louis Fed's public
economic-data service. It's free but keyed — register at
https://fredaccount.stlouisfed.org/apikeys and drop the key in ``.env``
as ``FRED_API_KEY``.

The client is deliberately thin: each method is one endpoint, returns
the provider's JSON essentially unchanged. MCP tools wrap these and
decide what to log. Rate-limit / auth errors propagate as
:class:`FredError` — no retries, no silent fallbacks (per hub
AGENTS.md).
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("fred_connector.fred")

_BASE_URL = "https://api.stlouisfed.org/fred"


class FredError(RuntimeError):
    """Raised when the FRED API returns a non-2xx response."""


class FredClient:
    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        if not api_key:
            raise FredError(
                "FRED_API_KEY is not set. Register at "
                "https://fredaccount.stlouisfed.org/apikeys and put the "
                "key in .env."
            )
        self._api_key = api_key
        self._http = httpx.Client(base_url=_BASE_URL, timeout=timeout)

    @classmethod
    def from_env(cls) -> "FredClient":
        return cls(api_key=os.environ.get("FRED_API_KEY", ""))

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        merged = {"api_key": self._api_key, "file_type": "json", **{
            k: v for k, v in params.items() if v is not None
        }}
        try:
            resp = self._http.get(path, params=merged)
        except httpx.HTTPError as exc:
            raise FredError(f"FRED request failed: {exc}") from exc
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise FredError(
                f"FRED {resp.status_code} on {path}: {body}"
            )
        return resp.json()

    def releases(self, limit: int | None = None) -> dict[str, Any]:
        """All FRED releases (CPI, PPI, Employment Situation, …)."""
        return self._get("/releases", {"limit": limit})

    def release(self, release_id: int) -> dict[str, Any]:
        """Metadata for a single release."""
        return self._get("/release", {"release_id": release_id})

    def release_dates(
        self,
        release_id: int,
        realtime_start: str | None = None,
        realtime_end: str | None = None,
        limit: int | None = None,
        include_release_dates_with_no_data: bool | None = None,
    ) -> dict[str, Any]:
        """Past/future publication dates for a single release."""
        params: dict[str, Any] = {
            "release_id": release_id,
            "realtime_start": realtime_start,
            "realtime_end": realtime_end,
            "limit": limit,
        }
        if include_release_dates_with_no_data is not None:
            params["include_release_dates_with_no_data"] = (
                "true" if include_release_dates_with_no_data else "false"
            )
        return self._get("/release/dates", params)

    def releases_dates(
        self,
        realtime_start: str | None = None,
        realtime_end: str | None = None,
        limit: int | None = None,
        include_release_dates_with_no_data: bool | None = None,
        order_by: str | None = None,
        sort_order: str | None = None,
    ) -> dict[str, Any]:
        """Release dates across *all* releases — the full economic calendar."""
        params: dict[str, Any] = {
            "realtime_start": realtime_start,
            "realtime_end": realtime_end,
            "limit": limit,
            "order_by": order_by,
            "sort_order": sort_order,
        }
        if include_release_dates_with_no_data is not None:
            params["include_release_dates_with_no_data"] = (
                "true" if include_release_dates_with_no_data else "false"
            )
        return self._get("/releases/dates", params)

    def release_series(
        self,
        release_id: int,
        limit: int | None = None,
        order_by: str | None = None,
    ) -> dict[str, Any]:
        """Series that belong to a release."""
        return self._get(
            "/release/series",
            {"release_id": release_id, "limit": limit, "order_by": order_by},
        )

    def series(self, series_id: str) -> dict[str, Any]:
        """Metadata for a single series (units, frequency, last updated)."""
        return self._get("/series", {"series_id": series_id})

    def series_observations(
        self,
        series_id: str,
        observation_start: str | None = None,
        observation_end: str | None = None,
        limit: int | None = None,
        sort_order: str | None = None,
        units: str | None = None,
        frequency: str | None = None,
        aggregation_method: str | None = None,
    ) -> dict[str, Any]:
        """Time-series observations."""
        return self._get(
            "/series/observations",
            {
                "series_id": series_id,
                "observation_start": observation_start,
                "observation_end": observation_end,
                "limit": limit,
                "sort_order": sort_order,
                "units": units,
                "frequency": frequency,
                "aggregation_method": aggregation_method,
            },
        )

    def series_search(
        self,
        search_text: str,
        limit: int | None = None,
        order_by: str | None = None,
        sort_order: str | None = None,
    ) -> dict[str, Any]:
        """Fuzzy search over series IDs/titles."""
        return self._get(
            "/series/search",
            {
                "search_text": search_text,
                "limit": limit,
                "order_by": order_by,
                "sort_order": sort_order,
            },
        )
