"""Thin HTTP client for SEC EDGAR.

SEC EDGAR (https://www.sec.gov/edgar) is the primary source for every
US public company's filings: 10-K (annual), 10-Q (quarterly), 8-K
(material events), Form 4 (insider transactions), 13F (institutional
holdings), XBRL company facts, and the full-text filing search.

The client is deliberately thin: each method is one endpoint, returns
the provider's JSON / text essentially unchanged. MCP tools wrap these
and decide what to log. Errors propagate as :class:`SecEdgarError`
subclasses — no retries, no silent fallbacks (per hub AGENTS.md).

## Access rules

SEC Fair Access requires every request to carry a descriptive
``User-Agent`` with a contact email — unauthenticated, but not
anonymous. The client raises :class:`SecEdgarUserAgentError` at
construction if ``SEC_EDGAR_USER_AGENT`` is unset, so a misconfigured
server fails loudly rather than getting silently IP-blocked.

The rate limit is 10 requests/second per IP. We enforce it
client-side with a token bucket so a burst of MCP tool calls doesn't
trip SEC's block list. On 429/403 we raise
:class:`SecEdgarRateLimitError` and stop — the user and the model
need to see throttles immediately so they can back off intelligently.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger("sec_edgar_connector.edgar")

_DATA_BASE = "https://data.sec.gov"
_WWW_BASE = "https://www.sec.gov"
_EFTS_BASE = "https://efts.sec.gov"

_RATE_LIMIT_PER_SEC = 10


class SecEdgarError(RuntimeError):
    """Base class for EDGAR client errors."""


class SecEdgarUserAgentError(SecEdgarError):
    """SEC_EDGAR_USER_AGENT is unset or malformed."""


class SecEdgarRateLimitError(SecEdgarError):
    """SEC returned 429 or 403 — back off, do not retry in a loop."""


class _TokenBucket:
    """Simple token bucket: up to N tokens, refilled to full every second.

    We don't need anything fancier — SEC's limit is per-IP and a
    single-process MCP server is the only caller. The bucket protects
    against tight loops in the model or repeated fan-outs.
    """

    def __init__(self, rate_per_sec: int) -> None:
        self._rate = rate_per_sec
        self._tokens = float(rate_per_sec)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            if elapsed > 0:
                self._tokens = min(
                    float(self._rate),
                    self._tokens + elapsed * self._rate,
                )
                self._last_refill = now
            if self._tokens < 1.0:
                needed = 1.0 - self._tokens
                wait = needed / self._rate
                time.sleep(wait)
                self._tokens = 0.0
                self._last_refill = time.monotonic()
            else:
                self._tokens -= 1.0


class SecEdgarClient:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 30.0,
        rate_per_sec: int = _RATE_LIMIT_PER_SEC,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise SecEdgarUserAgentError(
                "SEC_EDGAR_USER_AGENT must be set to a descriptive string "
                "containing a contact email, e.g. "
                "'traider-hub you@example.com'. SEC Fair Access requires "
                "an identifying User-Agent or requests are blocked."
            )
        self._user_agent = user_agent
        self._bucket = _TokenBucket(rate_per_sec)
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            follow_redirects=True,
        )

    @classmethod
    def from_env(cls) -> "SecEdgarClient":
        return cls(user_agent=os.environ.get("SEC_EDGAR_USER_AGENT", ""))

    def close(self) -> None:
        self._http.close()

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        self._bucket.take()
        try:
            resp = self._http.request(method, url, params=params or None)
        except httpx.HTTPError as exc:
            raise SecEdgarError(f"EDGAR request failed: {exc}") from exc
        if resp.status_code in (403, 429):
            body = resp.text[:500]
            raise SecEdgarRateLimitError(
                f"EDGAR {resp.status_code} on {url}: likely rate-limited or "
                f"blocked User-Agent. Body: {body}"
            )
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise SecEdgarError(
                f"EDGAR {resp.status_code} on {url}: {body}"
            )
        return resp

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request("GET", url, params=params).json()

    def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> str:
        return self._request("GET", url, params=params).text

    def get_bytes(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> bytes:
        return self._request("GET", url, params=params).content

    def company_tickers(self) -> dict[str, Any]:
        """Full ticker -> CIK mapping. Refreshed by SEC daily."""
        return self.get_json(f"{_WWW_BASE}/files/company_tickers.json")

    def submissions(self, cik: str) -> dict[str, Any]:
        """All filings for one CIK, most recent first.

        ``cik`` must be zero-padded to 10 digits (EDGAR convention).
        Response contains recent filings inline plus references to
        overflow files for older history.
        """
        return self.get_json(f"{_DATA_BASE}/submissions/CIK{cik}.json")

    def submissions_overflow(self, filename: str) -> dict[str, Any]:
        """An older-history overflow referenced by ``submissions``.

        ``filename`` is the ``name`` field from the submissions payload's
        ``filings.files`` entries (e.g. ``CIK0000320193-submissions-001.json``).
        """
        return self.get_json(f"{_DATA_BASE}/submissions/{filename}")

    def company_facts(self, cik: str) -> dict[str, Any]:
        """Full XBRL ``companyfacts`` blob for one CIK.

        Large payload — some mega-caps exceed a few MB. Tool layer
        should warn the caller before dumping it into a model's
        context.
        """
        return self.get_json(f"{_DATA_BASE}/api/xbrl/companyfacts/CIK{cik}.json")

    def company_concept(
        self,
        cik: str,
        concept: str,
        taxonomy: str = "us-gaap",
    ) -> dict[str, Any]:
        """One XBRL concept's reported values over time for one CIK.

        Example: ``taxonomy="us-gaap", concept="Revenues"``. Concepts
        are not uniform across filers — see AGENTS.md for the common
        aliases gotcha.
        """
        return self.get_json(
            f"{_DATA_BASE}/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{concept}.json"
        )

    def frame(
        self,
        concept: str,
        period: str,
        taxonomy: str = "us-gaap",
        unit: str = "USD",
    ) -> dict[str, Any]:
        """Cross-sectional snapshot: all filers' values for one concept.

        ``period`` uses EDGAR frame notation:

        - instantaneous: ``CY2024Q4I`` (trailing ``I`` for as-of values
          like ``Assets``).
        - duration: ``CY2024Q4`` (no ``I`` for period values like
          ``Revenues`` covering Q4 '24), or ``CY2024`` for annual.
        """
        return self.get_json(
            f"{_DATA_BASE}/api/xbrl/frames/{taxonomy}/{concept}/{unit}/{period}.json"
        )

    def full_text_search(
        self,
        query: str,
        *,
        forms: list[str] | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        from_offset: int = 0,
    ) -> dict[str, Any]:
        """EDGAR full-text filing search (``efts.sec.gov``).

        Returns Elasticsearch-shaped JSON. ``forms`` is a list like
        ``["10-K", "8-K"]``; the API joins with commas. Dates are
        ``YYYY-MM-DD``. ``from_offset`` paginates by Elasticsearch
        offset.
        """
        params: dict[str, Any] = {"q": query, "from": from_offset}
        if forms:
            params["forms"] = ",".join(forms)
        if date_start:
            params["dateRange"] = "custom"
            params["startdt"] = date_start
        if date_end:
            params["dateRange"] = "custom"
            params["enddt"] = date_end
        return self.get_json(
            f"{_EFTS_BASE}/LATEST/search-index", params=params,
        )

    def filing_index(self, cik: str, accession_nodash: str) -> dict[str, Any]:
        """``index.json`` for a single filing (document listing).

        ``accession_nodash`` is the 18-char accession with dashes
        stripped (e.g. ``0000320193-24-000123`` -> ``000032019324000123``).
        """
        return self.get_json(
            f"{_WWW_BASE}/Archives/edgar/data/{int(cik)}/{accession_nodash}/index.json"
        )

    def archive_document(
        self,
        cik: str,
        accession_nodash: str,
        filename: str,
    ) -> bytes:
        """Raw bytes of one document inside a filing (e.g. the Form 4 XML)."""
        return self.get_bytes(
            f"{_WWW_BASE}/Archives/edgar/data/{int(cik)}/{accession_nodash}/{filename}"
        )
