"""Finnhub analyst-estimates tools registered on the shared FastMCP.

Surface is deliberately narrow — one endpoint, one tool. Finnhub's
free tier only exposes analyst *recommendation trends* (monthly
distribution of strong-buy / buy / hold / sell / strong-sell counts
per ticker). Price targets, upgrade/downgrade actions, and consensus
EPS / revenue estimates are premium-only and not wired here. See
``README.md`` for the gap list.

Recommendation trends answer a specific question: *is the Street
getting more bullish or more bearish on this name over the last few
months?* That is rating-revision breadth — a distinct signal from
EPS-revision breadth, which this provider does **not** cover.
"""
from __future__ import annotations

import atexit
import logging
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from ...logging_utils import attach_provider_logger
from ...settings import TraiderSettings
from .finnhub_client import FinnhubClient

logger = logging.getLogger("traider.estimates")
_client: FinnhubClient | None = None


def _get_client() -> FinnhubClient:
    global _client
    if _client is None:
        logger.info("initializing Finnhub estimates client")
        _client = FinnhubClient.from_env()
        atexit.register(_client.close)
        logger.info("Finnhub estimates client ready")
    return _client


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def register(mcp: FastMCP, settings: TraiderSettings) -> None:
    attach_provider_logger("traider.estimates", settings.log_file("estimates"))

    @mcp.tool()
    def get_recommendation_trends(symbol: str) -> dict[str, Any]:
        """Monthly analyst recommendation distribution for one ticker.

        Source: Finnhub ``/stock/recommendation``. Free tier; US
        issuers with sell-side coverage.

        Args:
            symbol: Ticker (e.g. ``AAPL``). Required. Case-sensitive
                upstream — pass the canonical exchange symbol.

        Returns:
            A dict with ``source``, ``fetched_at``, ``symbol``, and
            ``trends`` — a list of monthly snapshots newest-first.
            Each entry carries:

            - ``period`` — month-start date (``YYYY-MM-DD``) the
              snapshot covers.
            - ``strongBuy`` / ``buy`` / ``hold`` / ``sell`` /
              ``strongSell`` — number of sell-side analysts in each
              bucket for that month.
            - ``symbol`` — echoed from upstream.

            An unknown or unfollowed ticker returns ``trends: []``
            rather than raising. Surface that to the user — don't
            substitute a related name or a cached value.

        Use this to gauge whether the Street is shifting bullish or
        bearish month-over-month (rating-revision breadth). This is
        *not* EPS-revision breadth — Finnhub's ``eps-estimate`` and
        ``revenue-estimate`` endpoints are premium-only and not wired
        in this provider.

        These counts are Finnhub's aggregation of sell-side analyst
        ratings, not a primary source. Quote with attribution.
        """
        if not symbol:
            raise ValueError("symbol is required")

        logger.info("get_recommendation_trends symbol=%s", symbol)
        try:
            rows = _get_client().recommendation_trends(symbol=symbol)
        except Exception:
            logger.exception(
                "get_recommendation_trends failed symbol=%s", symbol,
            )
            raise

        source = (
            f"https://finnhub.io/api/v1/stock/recommendation?symbol={symbol}"
        )
        return {
            "source": source,
            "fetched_at": _now_iso(),
            "symbol": symbol,
            "trends": rows or [],
        }
