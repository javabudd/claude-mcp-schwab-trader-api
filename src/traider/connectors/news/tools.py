"""Massive news tools registered on the shared FastMCP instance."""
from __future__ import annotations

import atexit
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from ...logging_utils import attach_profile_logger
from ...settings import TraiderSettings
from .massive_client import MassiveClient

_VALID_ORDER = frozenset({"asc", "desc"})

logger = logging.getLogger("traider.news")
_client: MassiveClient | None = None


def _get_client() -> MassiveClient:
    global _client
    if _client is None:
        logger.info("initializing Massive news client")
        _client = MassiveClient.from_env()
        atexit.register(_client.close)
        logger.info("Massive news client ready")
    return _client


def register(mcp: FastMCP, settings: TraiderSettings) -> None:
    attach_profile_logger("traider.news", settings.log_file("news"))

    @mcp.tool()
    def get_news(
        ticker: str | None = None,
        published_after: str | None = None,
        published_before: str | None = None,
        limit: int = 10,
        order: str = "desc",
        sort: str = "published_utc",
    ) -> dict[str, Any]:
        """Recent news articles from Massive (``/v2/reference/news``).

        Each article carries title, description, publisher, article URL,
        the tickers it references, and a per-ticker ``insights`` array with
        Massive's sentiment label and reasoning. Use this for catalyst
        tracking and to explain intraday moves.

        Pair with other hub tools for context — recent 8-K filings
        (``sec-edgar`` profile), upcoming macro releases (``fred``), price
        action around the article timestamp (market-data backend).

        Args:
            ticker: Case-sensitive ticker to filter by (e.g. ``AAPL``).
                Omit to pull the cross-market feed.
            published_after: ISO date or RFC3339 timestamp; only articles
                with ``published_utc`` at or after this are returned. Maps
                to Massive's ``published_utc.gte``.
            published_before: ISO date or RFC3339 timestamp; maps to
                Massive's ``published_utc.lte``.
            limit: Page size. Massive allows 1–1000; default 10.
            order: ``asc`` or ``desc``. Defaults to ``desc`` (newest
                first).
            sort: Field to sort by. Defaults to ``published_utc``.

        Returns:
            Massive's response JSON unchanged — ``status``, ``count``,
            ``results`` (article list), ``next_url`` (pagination cursor
            when more rows are available), ``request_id``.
        """
        if order not in _VALID_ORDER:
            raise ValueError(
                f"order must be one of {sorted(_VALID_ORDER)}; got {order!r}"
            )
        if limit < 1 or limit > 1000:
            raise ValueError(f"limit must be 1..1000; got {limit}")

        logger.info(
            "get_news ticker=%s after=%s before=%s limit=%d order=%s sort=%s",
            ticker, published_after, published_before, limit, order, sort,
        )
        try:
            return _get_client().news(
                ticker=ticker,
                published_utc_gte=published_after,
                published_utc_lte=published_before,
                order=order,
                sort=sort,
                limit=limit,
            )
        except Exception:
            logger.exception("get_news failed")
            raise
