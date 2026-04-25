"""CFTC Commitments of Traders tools registered on the shared FastMCP.

Tool surface is deliberately narrow — three curated reports plus a
generic SoQL escape hatch:

- **Disaggregated** (commodities) — PMPU / swap dealer / managed
  money / other reportables. The most-cited COT framing for crude,
  natural gas, gold, silver, copper, ag products. Started 2009.
- **Traders in Financial Futures (TFF)** — dealer/intermediary /
  asset manager / leveraged funds / other reportables. The
  financial-markets analogue: S&P 500, Nasdaq, Treasuries, USD,
  currencies, VIX.
- **Legacy** — Commercial (hedger) vs. Non-Commercial (speculator).
  Simpler framing, history back to the early 1980s; what financial
  media usually means by "the latest COT report."
- **Generic dataset query** — any other CFTC Socrata dataset
  (Bank Participation, Supplemental CIT, Cotton On-Call, …) via
  ``get_cftc_dataset``.

Release cadence is **weekly, Friday 3:30 PM ET**, reflecting
positions held the prior **Tuesday close**. That ~3-day reporting
lag is structural — when a user asks "what's positioning right now?",
the COT print is always one Tuesday old. The release schedule slides
to Monday on a holiday week.

All responses include CFTC's JSON essentially unchanged inside a
``source`` / ``fetched_at`` / ``dataset_id`` envelope.
"""
from __future__ import annotations

import atexit
import datetime as _dt
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from ...logging_utils import attach_provider_logger
from ...settings import TraiderSettings
from .cftc_client import DATASETS, CftcClient

_PORTAL_BASE = "https://publicreporting.cftc.gov/resource"

logger = logging.getLogger("traider.cftc")
_client: CftcClient | None = None


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _src(dataset_id: str) -> str:
    return f"{_PORTAL_BASE}/{dataset_id}.json"


def _envelope(
    dataset_id: str,
    fetched_at: str,
    rows: list[dict[str, Any]],
    *,
    flavor: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": _src(dataset_id),
        "dataset_id": dataset_id,
        "fetched_at": fetched_at,
        "row_count": len(rows),
        "data": rows,
    }
    if flavor is not None:
        payload["flavor"] = flavor
    return payload


def _get_client() -> CftcClient:
    global _client
    if _client is None:
        logger.info("initializing CFTC client")
        _client = CftcClient.from_env()
        atexit.register(_client.close)
        logger.info("CFTC client ready (app_token=%s)", _client._has_token)
    return _client


def _validate_limit(limit: int) -> None:
    if limit < 1 or limit > 50_000:
        raise ValueError(f"limit must be 1..50000; got {limit}")


def register(mcp: FastMCP, settings: TraiderSettings) -> None:
    attach_provider_logger("traider.cftc", settings.log_file("cftc"))

    @mcp.tool()
    def get_cot_disaggregated(
        market_contains: str | None = None,
        contract_market_code: str | None = None,
        commodity_subgroup: str | None = None,
        combined: bool = True,
        start_date: str | None = None,
        end_date: str | None = None,
        select: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Disaggregated Commitments of Traders — commodities positioning.

        The disaggregated report is the most-cited COT framing for
        physical commodities. It splits the market into five trader
        classes:

        - **Producer/Merchant/Processor/User (PMPU)** — physical
          hedgers (oil majors, refiners, miners, ag producers).
        - **Swap Dealers** — banks running commodity swap books;
          their futures positions hedge OTC client exposure.
        - **Managed Money** — hedge funds and CTAs. The "fast
          money" speculative crowd; the read most analysts watch.
        - **Other Reportables** — large traders that don't fit the
          first three (corporates, family offices).
        - **Non-Reportable** — small traders below CFTC reporting
          thresholds.

        Each class has long, short, and (where applicable) spread
        positions, plus week-over-week changes, percent of open
        interest, and trader counts.

        **Release cadence.** Weekly, Friday 3:30 PM ET, for positions
        held the prior Tuesday close. ~3-day reporting lag. Slides
        to Monday on a holiday week.

        **Combined vs futures-only.** ``combined=True`` (default)
        includes the delta-equivalent of options exposure; this is
        the more common citation. ``combined=False`` returns
        futures-only — sometimes preferred because options can be
        hedges of a non-COT-reportable position.

        Args:
            market_contains: Substring match on
                ``market_and_exchange_names`` (case-insensitive).
                E.g. ``"CRUDE OIL"``, ``"GOLD"``,
                ``"NATURAL GAS"``, ``"COPPER"``, ``"WHEAT"``.
            contract_market_code: Exact CFTC contract code
                (``cftc_contract_market_code``) when you know it.
                Use ``market_contains`` for discovery.
            commodity_subgroup: Filter by ``commodity_subgroup_name``
                (case-insensitive exact match). E.g. ``"GRAINS"``,
                ``"PETROLEUM AND PRODUCTS"``, ``"NATURAL GAS AND
                PRODUCTS"``, ``"PRECIOUS METALS"``,
                ``"BASE METALS"``, ``"LIVESTOCK"``, ``"SOFTS"``.
            combined: ``True`` (default) for futures-and-options
                combined; ``False`` for futures-only.
            start_date: ISO ``YYYY-MM-DD`` lower bound on
                ``report_date_as_yyyy_mm_dd``.
            end_date: ISO ``YYYY-MM-DD`` upper bound.
            select: Comma-separated SoQL projection. Omit for all
                columns (note: ~150 fields per row including old/
                other contract splits — narrow with ``select`` for
                long windows).
            limit: Page size, max 50000. Default 100.
            offset: 0-indexed paging offset.

        Returns:
            ``{"source", "dataset_id", "fetched_at", "flavor",
            "row_count", "data"}`` envelope. ``data`` is the Socrata
            JSON array of records, sorted newest-first. The
            ``flavor`` field reads ``"combined"`` or
            ``"futures_only"``.

            Field naming convention (long form): trader class
            prefix (``prod_merc_``, ``swap_``, ``m_money_``,
            ``other_rept_``, ``nonrept_``) + ``positions_long_all`` /
            ``positions_short_all`` for current contract; trailing
            ``_old`` / ``_other`` columns split out the front-month
            / back-month decomposition. ``change_in_*`` mirrors the
            same shape for week-over-week deltas. ``pct_of_oi_*``
            for percent of open interest. ``traders_*`` for trader
            counts. CFTC has historic field-name typos
            (``swap__positions_spread_all`` with double underscore)
            preserved verbatim — don't reshape.
        """
        _validate_limit(limit)
        flavor = "combined" if combined else "futures_only"
        dataset = DATASETS[f"disaggregated_{flavor}"]
        logger.info(
            "get_cot_disaggregated flavor=%s market=%r code=%r subgroup=%r "
            "range=%s..%s offset=%d limit=%d",
            flavor, market_contains, contract_market_code, commodity_subgroup,
            start_date, end_date, offset, limit,
        )
        fetched_at = _now_iso()
        try:
            rows = _get_client().disaggregated(
                combined=combined,
                market_contains=market_contains,
                contract_market_code=contract_market_code,
                commodity_subgroup=commodity_subgroup,
                start_date=start_date,
                end_date=end_date,
                select=select,
                limit=limit,
                offset=offset,
            )
        except Exception:
            logger.exception("get_cot_disaggregated failed")
            raise
        return _envelope(dataset, fetched_at, rows, flavor=flavor)

    @mcp.tool()
    def get_cot_financial_futures(
        market_contains: str | None = None,
        contract_market_code: str | None = None,
        combined: bool = True,
        start_date: str | None = None,
        end_date: str | None = None,
        select: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Traders in Financial Futures (TFF) — financial-markets positioning.

        The TFF report is the financial-markets analogue of the
        disaggregated commodities report. It splits the market into
        five trader classes more meaningful for futures on rates,
        currencies, and equity indices:

        - **Dealer/Intermediary** — sell-side banks, futures
          commission merchants, the "market maker" side.
        - **Asset Manager/Institutional** — long-only institutional
          (pension funds, mutual funds, insurance, endowments).
          Positioning here is slow-moving structural exposure.
        - **Leveraged Funds** — hedge funds, CTAs, prop traders.
          The "fast money" speculative read; most-watched bucket
          for sentiment-shift signals (e.g. record-short SPX
          futures by leveraged funds is the textbook
          contrarian-bullish setup).
        - **Other Reportables** — large traders not fitting the
          above (corporates, central banks, real-money outside the
          asset-manager category).
        - **Non-Reportable** — small traders below the CFTC
          reporting threshold.

        Standard TFF coverage includes:

        - **Equity indices** — E-mini S&P 500, E-mini Nasdaq-100,
          E-mini Russell 2000, Dow.
        - **Treasuries** — 2Y / 5Y / 10Y / Ultra 10Y / 30Y T-Bond /
          Ultra T-Bond, Fed Funds, Eurodollar / SOFR.
        - **Currencies** — EUR/USD, JPY, GBP, AUD, CAD, CHF, MXN,
          BRL, dollar index futures.
        - **Vol** — VIX futures.

        **Release cadence.** Weekly, Friday 3:30 PM ET, for positions
        held the prior Tuesday close. Same ~3-day lag and holiday
        slip as the disaggregated report.

        Args:
            market_contains: Substring match on
                ``market_and_exchange_names`` (case-insensitive).
                E.g. ``"E-MINI S&P 500"``, ``"10-YEAR U.S. TREASURY
                NOTES"``, ``"VIX FUTURES"``, ``"EURO FX"``.
            contract_market_code: Exact CFTC contract code when
                you know it.
            combined: ``True`` (default) for futures-and-options
                combined; ``False`` for futures-only.
            start_date: ISO ``YYYY-MM-DD`` lower bound on
                ``report_date_as_yyyy_mm_dd``.
            end_date: ISO ``YYYY-MM-DD`` upper bound.
            select: Comma-separated SoQL projection.
            limit: Page size, max 50000. Default 100.
            offset: 0-indexed paging offset.

        Returns:
            ``{"source", "dataset_id", "fetched_at", "flavor",
            "row_count", "data"}`` envelope. ``data`` is the Socrata
            JSON array of records, sorted newest-first.

            Field naming convention: trader class prefix
            (``dealer_``, ``asset_mgr_``, ``lev_money_``,
            ``other_rept_``, ``nonrept_``) + ``positions_long`` /
            ``positions_short`` / ``positions_spread`` (where
            applicable). Plus ``change_in_*`` for week-over-week
            deltas, ``pct_of_oi_*`` for percent of OI, ``traders_*``
            for trader counts. The TFF dataset is flatter than the
            commodities disaggregated report — no ``_old`` / ``_other``
            front/back contract splits.
        """
        _validate_limit(limit)
        flavor = "combined" if combined else "futures_only"
        dataset = DATASETS[f"tff_{flavor}"]
        logger.info(
            "get_cot_financial_futures flavor=%s market=%r code=%r "
            "range=%s..%s offset=%d limit=%d",
            flavor, market_contains, contract_market_code,
            start_date, end_date, offset, limit,
        )
        fetched_at = _now_iso()
        try:
            rows = _get_client().traders_in_financial_futures(
                combined=combined,
                market_contains=market_contains,
                contract_market_code=contract_market_code,
                start_date=start_date,
                end_date=end_date,
                select=select,
                limit=limit,
                offset=offset,
            )
        except Exception:
            logger.exception("get_cot_financial_futures failed")
            raise
        return _envelope(dataset, fetched_at, rows, flavor=flavor)

    @mcp.tool()
    def get_cot_legacy(
        market_contains: str | None = None,
        contract_market_code: str | None = None,
        combined: bool = True,
        start_date: str | None = None,
        end_date: str | None = None,
        select: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Legacy Commitments of Traders — Commercial vs. Non-Commercial.

        The legacy report is the simplest COT framing and the one
        financial media most often cites by default. It splits the
        market into three buckets:

        - **Commercial** — physical hedgers (the "smart money"
          framing in trader folklore: producers and end-users
          hedging physical exposure).
        - **Non-Commercial** — speculators (large traders that
          aren't hedging — funds, prop traders).
        - **Non-Reportable** — small traders below CFTC reporting
          thresholds; assumed retail.

        Coverage goes back to the early 1980s, which is why this
        report is preferred for very-long-horizon positioning
        studies. For modern analysis, prefer:

        - ``get_cot_disaggregated`` — sharper trader-class splits
          for commodities (started 2009).
        - ``get_cot_financial_futures`` — for any financial-market
          futures (TFF replaces the legacy splits with classes
          that map onto how financials actually trade).

        **Release cadence.** Weekly, Friday 3:30 PM ET, for positions
        held the prior Tuesday close. Same ~3-day lag and holiday
        slip as the other COT reports.

        Args:
            market_contains: Substring match on
                ``market_and_exchange_names`` (case-insensitive).
            contract_market_code: Exact CFTC contract code when
                you know it.
            combined: ``True`` (default) for futures-and-options
                combined; ``False`` for futures-only.
            start_date: ISO ``YYYY-MM-DD`` lower bound on
                ``report_date_as_yyyy_mm_dd``.
            end_date: ISO ``YYYY-MM-DD`` upper bound.
            select: Comma-separated SoQL projection.
            limit: Page size, max 50000. Default 100.
            offset: 0-indexed paging offset.

        Returns:
            ``{"source", "dataset_id", "fetched_at", "flavor",
            "row_count", "data"}`` envelope. ``data`` is the Socrata
            JSON array of records, sorted newest-first.

            Field naming convention: trader class prefix
            (``comm_``, ``noncomm_``, ``nonrept_``, ``tot_rept_``)
            + ``positions_long_all`` / ``positions_short_all`` /
            ``positions_spread_all`` (non-commercial only — the
            other classes don't carry spread positions). Plus
            ``change_in_*`` for week-over-week deltas. ``_old`` /
            ``_other`` columns split front/back contract months.
            CFTC has a historic field-name typo
            (``noncomm_postions_spread_all`` — note "postions") that
            is preserved verbatim — don't reshape.
        """
        _validate_limit(limit)
        flavor = "combined" if combined else "futures_only"
        dataset = DATASETS[f"legacy_{flavor}"]
        logger.info(
            "get_cot_legacy flavor=%s market=%r code=%r "
            "range=%s..%s offset=%d limit=%d",
            flavor, market_contains, contract_market_code,
            start_date, end_date, offset, limit,
        )
        fetched_at = _now_iso()
        try:
            rows = _get_client().legacy(
                combined=combined,
                market_contains=market_contains,
                contract_market_code=contract_market_code,
                start_date=start_date,
                end_date=end_date,
                select=select,
                limit=limit,
                offset=offset,
            )
        except Exception:
            logger.exception("get_cot_legacy failed")
            raise
        return _envelope(dataset, fetched_at, rows, flavor=flavor)

    @mcp.tool()
    def get_cftc_dataset(
        dataset_id: str,
        select: str | None = None,
        where: str | None = None,
        order: str | None = "report_date_as_yyyy_mm_dd DESC",
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Generic SoQL query against any CFTC Socrata dataset.

        Use the curated tools first (``get_cot_disaggregated``,
        ``get_cot_financial_futures``, ``get_cot_legacy``). Reach for
        this when the question needs a CFTC dataset they don't cover
        — Bank Participation Report, Supplemental Commodity Index
        Traders, Cotton On-Call, or any future CFTC dataset.

        Browse the dataset catalog at
        https://publicreporting.cftc.gov/browse to find the 4-by-4
        dataset ID. Common datasets:

        - ``72hh-3qpy`` — Disaggregated futures-only (curated
          via ``get_cot_disaggregated``).
        - ``kh3c-gbw2`` — Disaggregated combined (curated).
        - ``gpe5-46if`` — TFF futures-only (curated via
          ``get_cot_financial_futures``).
        - ``yw9f-hn96`` — TFF combined (curated).
        - ``6dca-aqww`` — Legacy futures-only (curated via
          ``get_cot_legacy``).
        - ``jun7-fc8e`` — Legacy combined (curated).
        - ``4zgm-a668`` — Supplemental Commodity Index Traders
          (13 ag commodities; not curated).

        The Bank Participation Report is published as a separate
        report category; look up its current dataset ID on the
        portal before passing it here.

        Args:
            dataset_id: Socrata 4-by-4 (e.g. ``"72hh-3qpy"``).
            select: SoQL ``$select`` projection (comma-separated).
                Omit for all columns.
            where: SoQL ``$where`` filter expression. Strings are
                single-quoted; double internal single quotes per
                SoQL escaping. Substring match uses
                ``upper(field) like upper('%PATTERN%')``.
                Date compare: ``report_date_as_yyyy_mm_dd >=
                '2025-01-01'``.
            order: SoQL ``$order``, default
                ``"report_date_as_yyyy_mm_dd DESC"``. Pass
                explicitly for datasets that don't have that
                column.
            q: Full-text search across all columns (Socrata ``$q``).
            limit: Page size, max 50000. Default 100.
            offset: 0-indexed paging offset.

        Returns:
            ``{"source", "dataset_id", "fetched_at", "row_count",
            "data"}`` envelope.

        Notes:
            CFTC Socrata datasets occasionally carry **historic
            field-name typos** (``swap__positions_spread_all``,
            ``noncomm_postions_spread_all``). They are preserved as
            published — quote field names verbatim from a sample row
            rather than fixing the spelling.
        """
        _validate_limit(limit)
        if not dataset_id or "-" not in dataset_id:
            raise ValueError(
                f"dataset_id must be a Socrata 4-by-4 (e.g. '72hh-3qpy'); "
                f"got {dataset_id!r}"
            )
        logger.info(
            "get_cftc_dataset id=%s where=%r order=%r q=%r offset=%d limit=%d",
            dataset_id, where, order, q, offset, limit,
        )
        fetched_at = _now_iso()
        try:
            rows = _get_client().query(
                dataset_id,
                select=select,
                where=where,
                order=order,
                limit=limit,
                offset=offset,
                q=q,
            )
        except Exception:
            logger.exception("get_cftc_dataset failed dataset_id=%s", dataset_id)
            raise
        return _envelope(dataset_id, fetched_at, rows)
