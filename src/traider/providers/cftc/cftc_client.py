"""Thin HTTP client around the CFTC Public Reporting Socrata API.

The CFTC publishes its Commitments of Traders (COT) data on the
Socrata Open Data platform at
https://publicreporting.cftc.gov/. Three reports are wired here —
the ones a trader actually reaches for:

- **Disaggregated** (commodities) — splits commercials into
  Producer/Merchant/Processor/User and Swap Dealers, and speculators
  into Managed Money and Other Reportables. Started 2009. Used for
  oil, natural gas, gold, silver, copper, ag products, etc.
- **Traders in Financial Futures (TFF)** — the financial-markets
  equivalent. Splits into Dealer/Intermediary, Asset
  Manager/Institutional, Leveraged Funds, Other Reportables, and
  Non-Reportables. Used for S&P 500, Nasdaq, Treasuries, USD,
  currencies, VIX.
- **Legacy** — the original "Commercial vs. Non-Commercial" framing
  (commercial = hedger, non-commercial = speculator). Coverage goes
  back to the early 1980s. Most-cited in financial media.

Each report is published in two flavors: **futures-only** and
**combined** (futures plus the delta-equivalent of options). Combined
is the more common citation but futures-only is sometimes preferred
because options can be hedges. Both are exposed; toggle via
``combined`` (default ``True``).

Auth is optional — the Socrata endpoint is public and rate-limited
per-IP without a token, more generously with one. If
``CFTC_APP_TOKEN`` is set in the env, the client sends it on every
request as the ``X-App-Token`` header. Register at
https://evergreen.data.socrata.com/signup.

Like the other read-only hub clients, this one is deliberately thin:
each method maps to one Socrata dataset and returns the JSON
essentially unchanged. Rate-limit / HTTP errors propagate as
:class:`CftcError` — no retries, no silent fallbacks (per hub
AGENTS.md).
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("traider.cftc.client")

_BASE_URL = "https://publicreporting.cftc.gov"

# Socrata 4-by-4 dataset IDs. These are stable Socrata dataset
# identifiers; the CFTC has not changed them since the v2 portal
# launched. Verified against the dataset catalog at
# https://publicreporting.cftc.gov/browse?category=Commitments+of+Traders.
DATASETS: dict[str, str] = {
    "disaggregated_futures_only":   "72hh-3qpy",
    "disaggregated_combined":       "kh3c-gbw2",
    "tff_futures_only":             "gpe5-46if",
    "tff_combined":                 "yw9f-hn96",
    "legacy_futures_only":          "6dca-aqww",
    "legacy_combined":              "jun7-fc8e",
}


class CftcError(RuntimeError):
    """Raised when the CFTC Socrata API returns a non-2xx response."""


class CftcClient:
    """CFTC Public Reporting Socrata REST client.

    Socrata's SoQL query dialect is consistent across datasets:

    - ``$select`` — comma-separated projection (default: all columns).
    - ``$where`` — SoQL filter expression. Strings are single-quoted,
      ISO dates compared as floating timestamps:
      ``report_date_as_yyyy_mm_dd >= '2025-01-01'``. Substring match
      uses ``upper(...) like upper('%CRUDE%')``.
    - ``$order`` — column name with optional ``DESC``.
    - ``$limit`` — page size, max 50 000 per request.
    - ``$offset`` — 0-indexed paging offset.
    - ``$q`` — full-text search across all columns.

    Responses are a JSON array of records — the client returns it
    verbatim under a ``data`` key so callers see the raw Socrata
    shape without needing to know each dataset's column set.
    """

    def __init__(
        self,
        app_token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        headers: dict[str, str] = {"Accept": "application/json"}
        if app_token:
            headers["X-App-Token"] = app_token
        self._http = httpx.Client(
            base_url=_BASE_URL,
            timeout=timeout,
            headers=headers,
        )
        self._has_token = bool(app_token)

    @classmethod
    def from_env(cls) -> "CftcClient":
        return cls(app_token=os.environ.get("CFTC_APP_TOKEN") or None)

    def close(self) -> None:
        self._http.close()

    def _get(self, dataset_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        path = f"/resource/{dataset_id}.json"
        cleaned = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._http.get(path, params=cleaned)
        except httpx.HTTPError as exc:
            raise CftcError(f"CFTC request failed: {exc}") from exc
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise CftcError(
                f"CFTC {resp.status_code} on {path}: {body}"
            )
        payload = resp.json()
        if not isinstance(payload, list):
            raise CftcError(
                f"CFTC {path} returned non-list payload: {type(payload).__name__}"
            )
        return payload

    def query(
        self,
        dataset_id: str,
        *,
        select: str | None = None,
        where: str | None = None,
        order: str | None = "report_date_as_yyyy_mm_dd DESC",
        limit: int | None = 100,
        offset: int | None = 0,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        """Generic SoQL query against any CFTC Socrata dataset.

        ``dataset_id`` is the Socrata 4-by-4 (e.g. ``72hh-3qpy``).
        Listed in :data:`DATASETS` for the wired reports; for other
        CFTC datasets (Bank Participation, Supplemental CIT, Cotton
        On-Call, …) pass the ID directly.
        """
        params: dict[str, Any] = {
            "$select": select,
            "$where": where,
            "$order": order,
            "$limit": limit,
            "$offset": offset,
            "$q": q,
        }
        return self._get(dataset_id, params)

    def disaggregated(
        self,
        *,
        combined: bool = True,
        market_contains: str | None = None,
        contract_market_code: str | None = None,
        commodity_subgroup: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        select: str | None = None,
        order: str | None = "report_date_as_yyyy_mm_dd DESC",
        limit: int | None = 100,
        offset: int | None = 0,
    ) -> list[dict[str, Any]]:
        """Disaggregated COT report — commodities (PMPU / swap dealers / managed money / other reportables)."""
        dataset = DATASETS[
            "disaggregated_combined" if combined else "disaggregated_futures_only"
        ]
        return self.query(
            dataset,
            select=select,
            where=_build_where(
                market_contains=market_contains,
                contract_market_code=contract_market_code,
                commodity_subgroup=commodity_subgroup,
                start_date=start_date,
                end_date=end_date,
            ),
            order=order,
            limit=limit,
            offset=offset,
        )

    def traders_in_financial_futures(
        self,
        *,
        combined: bool = True,
        market_contains: str | None = None,
        contract_market_code: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        select: str | None = None,
        order: str | None = "report_date_as_yyyy_mm_dd DESC",
        limit: int | None = 100,
        offset: int | None = 0,
    ) -> list[dict[str, Any]]:
        """TFF report — financial markets (dealer / asset mgr / leveraged funds / other rept)."""
        dataset = DATASETS[
            "tff_combined" if combined else "tff_futures_only"
        ]
        return self.query(
            dataset,
            select=select,
            where=_build_where(
                market_contains=market_contains,
                contract_market_code=contract_market_code,
                start_date=start_date,
                end_date=end_date,
            ),
            order=order,
            limit=limit,
            offset=offset,
        )

    def legacy(
        self,
        *,
        combined: bool = True,
        market_contains: str | None = None,
        contract_market_code: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        select: str | None = None,
        order: str | None = "report_date_as_yyyy_mm_dd DESC",
        limit: int | None = 100,
        offset: int | None = 0,
    ) -> list[dict[str, Any]]:
        """Legacy COT report — Commercial vs. Non-Commercial (hedger vs speculator)."""
        dataset = DATASETS[
            "legacy_combined" if combined else "legacy_futures_only"
        ]
        return self.query(
            dataset,
            select=select,
            where=_build_where(
                market_contains=market_contains,
                contract_market_code=contract_market_code,
                start_date=start_date,
                end_date=end_date,
            ),
            order=order,
            limit=limit,
            offset=offset,
        )


def _build_where(
    *,
    market_contains: str | None = None,
    contract_market_code: str | None = None,
    commodity_subgroup: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str | None:
    """Compose a SoQL ``$where`` clause from the common filter knobs.

    All filters AND together. Returns ``None`` if no filter is set so
    Socrata sees no ``$where`` param at all.
    """
    clauses: list[str] = []
    if market_contains:
        # SoQL uppercase-folded LIKE so the match is case-insensitive.
        # Escape any single quotes by doubling them per SoQL rules.
        needle = market_contains.replace("'", "''").upper()
        clauses.append(
            f"upper(market_and_exchange_names) like '%{needle}%'"
        )
    if contract_market_code:
        code = contract_market_code.replace("'", "''")
        clauses.append(f"cftc_contract_market_code = '{code}'")
    if commodity_subgroup:
        sub = commodity_subgroup.replace("'", "''").upper()
        clauses.append(
            f"upper(commodity_subgroup_name) = '{sub}'"
        )
    if start_date:
        clauses.append(
            f"report_date_as_yyyy_mm_dd >= '{start_date}'"
        )
    if end_date:
        clauses.append(
            f"report_date_as_yyyy_mm_dd <= '{end_date}'"
        )
    return " AND ".join(clauses) if clauses else None
