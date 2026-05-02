"""Thin HTTP client around the Treasury Fiscal Data API.

Fiscal Data (https://fiscaldata.treasury.gov) is the US Treasury's
public dataset service. It's unauthenticated — no API key, no OAuth —
but the endpoints return a lot of fields, so the MCP tools on top of
this client focus on the columns a trader actually reaches for.

Like the other read-only hub clients, this one is deliberately thin:
each method maps to one Fiscal Data dataset and returns the provider's
JSON essentially unchanged. Rate-limit / HTTP errors propagate as
:class:`TreasuryError` — no retries, no silent fallbacks (per hub
AGENTS.md).

**Yield curve is NOT covered here.** FRED mirrors the H.15 Daily
Treasury Yield Curve in full (``DGS1MO`` … ``DGS30``, ``DFII*`` for
real yields). Use the ``fred`` provider's ``get_series`` for those. This server
is only for data FRED does not carry: auction mechanics, the Daily
Treasury Statement, and debt-to-the-penny.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("traider.treasury.fiscal")

_BASE_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"

# Dataset paths. These are stable — Fiscal Data versions endpoints in
# the URL (/v1, /v2) when the schema changes.
AUCTIONS_PATH = "/v1/accounting/od/auctions_query"
DEBT_TO_PENNY_PATH = "/v2/accounting/od/debt_to_penny"
DTS_PATHS: dict[str, str] = {
    "operating_cash_balance": "/v1/accounting/dts/operating_cash_balance",
    "deposits_withdrawals_operating_cash": (
        "/v1/accounting/dts/deposits_withdrawals_operating_cash"
    ),
    "public_debt_transactions": "/v1/accounting/dts/public_debt_transactions",
    "adjustment_public_debt_transactions_cash_basis": (
        "/v1/accounting/dts/adjustment_public_debt_transactions_cash_basis"
    ),
    "federal_tax_deposits": "/v1/accounting/dts/federal_tax_deposits",
    "short_term_cash_investments": (
        "/v1/accounting/dts/short_term_cash_investments"
    ),
    "income_tax_refunds_issued": (
        "/v1/accounting/dts/income_tax_refunds_issued"
    ),
    "inter_agency_tax_transfers": (
        "/v1/accounting/dts/inter_agency_tax_transfers"
    ),
}


class TreasuryError(RuntimeError):
    """Raised when the Fiscal Data API returns a non-2xx response."""


# Fields Fiscal Data returns as decimal strings to preserve precision —
# large public-debt totals (``tot_pub_debt_out_amt``) routinely exceed
# float64's 15-significant-digit safe range, and rates / ratios are
# published to more places than float-formatting round-trips cleanly.
# DEVELOPING.md treasury § "Amounts are strings" is the canonical
# statement of this contract; ``_assert_decimal_strings`` below is the
# tripwire that enforces it on every response.
#
# Any field name in this set OR ending with one of these suffixes must
# come back as ``str`` (or ``None``). If a future refactor adds
# ``float(...)`` somewhere "to tidy up" the response, the guard fires
# instead of letting the precision loss go silent.
_DECIMAL_STRING_SUFFIXES: tuple[str, ...] = ("_amt", "_bal")
_DECIMAL_STRING_FIELDS: frozenset[str] = frozenset({
    # auctions_query — dollar amounts without `_amt` suffix
    "total_accepted",
    "total_tendered",
    "primary_dealer_tendered",
    "primary_dealer_accepted",
    "direct_bidder_tendered",
    "direct_bidder_accepted",
    "indirect_bidder_tendered",
    "indirect_bidder_accepted",
    # auctions_query — rates / ratios published as decimal strings
    "bid_to_cover_ratio",
    "high_yield",
    "high_investment_rate",
    "high_discnt_rate",
    "high_price",
    "allocation_pctage",
})


def _assert_decimal_strings(payload: dict[str, Any], path: str) -> None:
    """Tripwire: refuse to silently downgrade Fiscal Data's
    precision-preserved decimal-string fields to a numeric type.

    Spot-checks the first row in ``payload["data"]`` — column types
    are consistent across rows in any tabular API, so one row is
    enough to catch a refactor that adds ``float(...)`` mid-pipeline
    or a Fiscal Data schema flip from string to number. Skips
    gracefully when the response has no ``data`` array, no rows, or
    a row that isn't a dict — those shapes can't violate the
    contract anyway.
    """
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        return
    first = rows[0]
    if not isinstance(first, dict):
        return
    for name, value in first.items():
        if value is None:
            continue
        if name not in _DECIMAL_STRING_FIELDS and not name.endswith(
            _DECIMAL_STRING_SUFFIXES
        ):
            continue
        if not isinstance(value, str):
            raise TreasuryError(
                f"Fiscal Data {path}: precision-preserving field "
                f"{name!r} returned as {type(value).__name__} "
                f"(value={value!r}) instead of decimal string. "
                f"DEVELOPING.md treasury § 'Amounts are strings' "
                f"requires these fields stay as str — refusing to "
                f"silently degrade precision."
            )


class TreasuryClient:
    """Fiscal Data REST client.

    Fiscal Data's query dialect is consistent across datasets:

    - ``filter`` — ``field:op:value`` joined with commas. Ops include
      ``eq``, ``gte``, ``gt``, ``lte``, ``lt``, ``in`` (CSV values).
    - ``sort`` — field name, prefix ``-`` for descending.
    - ``fields`` — comma-separated projection.
    - ``page[size]`` — max 10 000 per page; defaults vary.
    - ``page[number]`` — 1-indexed.
    - ``format=json`` — default; ``csv`` / ``xml`` also available.
    """

    def __init__(
        self,
        user_agent: str = "traider-treasury (contact: https://github.com)",
        timeout: float = 30.0,
    ) -> None:
        self._http = httpx.Client(
            base_url=_BASE_URL,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    @classmethod
    def from_env(cls) -> "TreasuryClient":
        return cls()

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        cleaned = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._http.get(path, params=cleaned)
        except httpx.HTTPError as exc:
            raise TreasuryError(f"Fiscal Data request failed: {exc}") from exc
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise TreasuryError(
                f"Fiscal Data {resp.status_code} on {path}: {body}"
            )
        payload = resp.json()
        _assert_decimal_strings(payload, path)
        return payload

    def query(
        self,
        path: str,
        *,
        filter_: str | None = None,
        fields: str | None = None,
        sort: str | None = None,
        page_size: int | None = None,
        page_number: int | None = None,
    ) -> dict[str, Any]:
        """Generic Fiscal Data query. All tool methods funnel through here."""
        params: dict[str, Any] = {
            "format": "json",
            "filter": filter_,
            "fields": fields,
            "sort": sort,
        }
        if page_size is not None:
            params["page[size]"] = page_size
        if page_number is not None:
            params["page[number]"] = page_number
        return self._get(path, params)

    def auctions(
        self,
        *,
        filter_: str | None = None,
        fields: str | None = None,
        sort: str | None = "-auction_date",
        page_size: int | None = 100,
        page_number: int | None = 1,
    ) -> dict[str, Any]:
        """Treasury securities auction results (bid-to-cover, bidder mix, rates)."""
        return self.query(
            AUCTIONS_PATH,
            filter_=filter_,
            fields=fields,
            sort=sort,
            page_size=page_size,
            page_number=page_number,
        )

    def debt_to_penny(
        self,
        *,
        filter_: str | None = None,
        fields: str | None = None,
        sort: str | None = "-record_date",
        page_size: int | None = 100,
        page_number: int | None = 1,
    ) -> dict[str, Any]:
        """Total public debt outstanding, daily."""
        return self.query(
            DEBT_TO_PENNY_PATH,
            filter_=filter_,
            fields=fields,
            sort=sort,
            page_size=page_size,
            page_number=page_number,
        )

    def dts(
        self,
        table: str,
        *,
        filter_: str | None = None,
        fields: str | None = None,
        sort: str | None = "-record_date",
        page_size: int | None = 100,
        page_number: int | None = 1,
    ) -> dict[str, Any]:
        """Daily Treasury Statement — one of the DTS_PATHS tables."""
        if table not in DTS_PATHS:
            raise TreasuryError(
                f"unknown DTS table {table!r}; "
                f"valid: {sorted(DTS_PATHS)}"
            )
        return self.query(
            DTS_PATHS[table],
            filter_=filter_,
            fields=fields,
            sort=sort,
            page_size=page_size,
            page_number=page_number,
        )
