"""Ticker ↔ CIK mapping cached with an explicit TTL.

SEC's canonical mapping lives at
``https://www.sec.gov/files/company_tickers.json`` and is refreshed
daily. It's ~400 KB and needed for almost every tool (users think in
tickers, EDGAR works in CIKs), so we cache it per-process.

Per the hub AGENTS.md rule on silent fallbacks, the cache has a
*visible* TTL: every response that consults the map includes a
``ticker_map_fetched_at`` timestamp so the caller can see how fresh
the lookup was.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .edgar_client import SecEdgarClient, SecEdgarError

logger = logging.getLogger("traider.sec_edgar.ticker_map")

_DEFAULT_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Company:
    cik: str
    ticker: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        return {"cik": self.cik, "ticker": self.ticker, "name": self.name}


class TickerMap:
    def __init__(
        self,
        client: SecEdgarClient,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._by_ticker: dict[str, Company] = {}
        self._by_cik: dict[str, Company] = {}
        self._fetched_at_monotonic: float = 0.0
        self._fetched_at_iso: str | None = None

    @property
    def fetched_at(self) -> str | None:
        return self._fetched_at_iso

    def _ensure_loaded(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._by_ticker and (now - self._fetched_at_monotonic) < self._ttl:
                return
            logger.info("refreshing SEC ticker map (TTL expired or first load)")
            raw = self._client.company_tickers()
            by_ticker: dict[str, Company] = {}
            by_cik: dict[str, Company] = {}
            for entry in raw.values():
                cik_int = entry.get("cik_str")
                ticker = entry.get("ticker")
                name = entry.get("title")
                if cik_int is None or not ticker or not name:
                    continue
                cik = f"{int(cik_int):010d}"
                company = Company(cik=cik, ticker=ticker.upper(), name=name)
                by_ticker[company.ticker] = company
                # One CIK can map to multiple tickers (class shares). Keep
                # the first one encountered — callers who need every ticker
                # for a CIK should iterate the raw map instead.
                by_cik.setdefault(cik, company)
            self._by_ticker = by_ticker
            self._by_cik = by_cik
            self._fetched_at_monotonic = now
            self._fetched_at_iso = (
                datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            )
            logger.info(
                "SEC ticker map loaded: %d tickers, %d unique CIKs",
                len(by_ticker), len(by_cik),
            )

    def lookup(self, ticker_or_cik: str) -> Company:
        """Resolve a ticker or CIK string to a :class:`Company`.

        Accepts:
        - Ticker: ``"AAPL"`` (case-insensitive).
        - CIK: ``"320193"``, ``"0000320193"``, or ``"CIK0000320193"``.
        """
        self._ensure_loaded()
        normalized = ticker_or_cik.strip()
        if not normalized:
            raise SecEdgarError("empty ticker/cik")

        cik_candidate = normalized.upper()
        if cik_candidate.startswith("CIK"):
            cik_candidate = cik_candidate[3:]
        if cik_candidate.isdigit():
            cik = f"{int(cik_candidate):010d}"
            company = self._by_cik.get(cik)
            if company is not None:
                return company
            # CIK not in the ticker map — still a valid EDGAR entity
            # (private reporters, FPI funds, etc.). Return a bare record
            # so the submissions endpoint can still be hit.
            return Company(cik=cik, ticker="", name="")

        company = self._by_ticker.get(normalized.upper())
        if company is None:
            raise SecEdgarError(
                f"no CIK found for ticker {normalized!r}; try the "
                f"numeric CIK directly or call search_companies"
            )
        return company

    def search(self, query: str, limit: int = 20) -> list[Company]:
        """Substring match against ticker or name (case-insensitive)."""
        self._ensure_loaded()
        needle = query.strip().lower()
        if not needle:
            return []
        hits: list[Company] = []
        for company in self._by_ticker.values():
            if (
                needle in company.ticker.lower()
                or needle in company.name.lower()
            ):
                hits.append(company)
                if len(hits) >= limit:
                    break
        return hits
