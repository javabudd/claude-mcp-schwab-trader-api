"""MCP server exposing read-only Treasury Fiscal Data lookups.

Tool surface is deliberately narrow — only the things FRED does *not*
already mirror:

- **Auction results** (bid-to-cover, high yield/rate, primary dealer
  takedown, indirect/direct bidder share).
- **Daily Treasury Statement** (operating cash balance / TGA,
  deposits + withdrawals, public debt transactions).
- **Debt to the penny** (daily total public debt outstanding).

Yield-curve queries (DGS1MO … DGS30, DFII* for real yields) should go
to ``fred_connector.get_series`` — FRED carries H.15 in full and is
the common entry point for that data.
"""
from __future__ import annotations

import argparse
import atexit
import logging
import os
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .treasury_client import DTS_PATHS, TreasuryClient

# Curated auction-results projection. Fiscal Data's auctions_query has
# 90+ columns; this is the subset a trader actually reads when sizing
# demand for a given refunding. Callers can pass their own `fields=`
# to override.
AUCTION_DEFAULT_FIELDS = [
    "record_date",
    "security_type",
    "security_term",
    "cusip",
    "auction_date",
    "issue_date",
    "maturity_date",
    "offering_amt",
    "total_accepted",
    "total_tendered",
    "bid_to_cover_ratio",
    "high_yield",
    "high_investment_rate",
    "high_discnt_rate",
    "high_price",
    "allocation_pctage",
    "primary_dealer_tendered",
    "primary_dealer_accepted",
    "direct_bidder_tendered",
    "direct_bidder_accepted",
    "indirect_bidder_tendered",
    "indirect_bidder_accepted",
]

# Security types used in the `security_type` filter column. Keeping
# this as an explicit list lets the tool error with a useful message
# rather than passing garbage through to the API.
AUCTION_SECURITY_TYPES = frozenset({"Bill", "Note", "Bond", "CMB", "TIPS", "FRN"})

logger = logging.getLogger("treasury_connector")

mcp = FastMCP(
    "treasury-connector",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)
_client: TreasuryClient | None = None


def _get_client() -> TreasuryClient:
    global _client
    if _client is None:
        logger.info("initializing Treasury Fiscal Data client")
        _client = TreasuryClient.from_env()
        atexit.register(_client.close)
        logger.info("Treasury Fiscal Data client ready")
    return _client


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _default_start(days_back: int) -> str:
    return (_today_utc() - timedelta(days=days_back)).isoformat()


def _build_filter(parts: list[str]) -> str | None:
    cleaned = [p for p in parts if p]
    return ",".join(cleaned) if cleaned else None


def _fields_csv(fields: list[str] | None, default: list[str]) -> str:
    chosen = fields if fields else default
    return ",".join(chosen)


@mcp.tool()
def get_auction_results(
    security_type: str | None = None,
    security_term: str | None = None,
    cusip: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    fields: list[str] | None = None,
    limit: int = 100,
    page: int = 1,
    sort: str = "-auction_date",
) -> dict[str, Any]:
    """Treasury securities auction results.

    Primary demand signals for a refunding: bid-to-cover, stop-out
    yield/rate, primary-dealer takedown, and the direct vs indirect
    bidder split. The projection returned by default covers those
    columns plus basic identifiers. Pass your own ``fields=[...]`` to
    pull any other column from Fiscal Data's auctions_query dataset
    (90+ columns available — see
    https://fiscaldata.treasury.gov/datasets/securities-auctions-data).

    Args:
        security_type: ``Bill`` / ``Note`` / ``Bond`` / ``CMB`` /
            ``TIPS`` / ``FRN``. Omit for all types.
        security_term: e.g. ``4-Week``, ``13-Week``, ``2-Year``,
            ``10-Year``, ``30-Year``. Matches exactly — use the
            ``security_term`` values Fiscal Data publishes.
        cusip: Match a single auctioned security by CUSIP.
        start_date / end_date: ISO ``YYYY-MM-DD`` filters on
            ``auction_date``. Default start is 90 days ago.
        fields: Override the projection. Omit to get the curated
            default list (bid-to-cover, dealer takedown, etc.).
        limit: Page size (Fiscal Data allows up to 10 000).
        page: 1-indexed page number.
        sort: Fiscal Data sort string (``-`` prefix for desc).
            Defaults to most-recent first.
    """
    if security_type and security_type not in AUCTION_SECURITY_TYPES:
        raise ValueError(
            f"unknown security_type={security_type!r}; "
            f"valid: {sorted(AUCTION_SECURITY_TYPES)}"
        )

    if start_date is None:
        start_date = _default_start(90)

    filter_ = _build_filter([
        f"auction_date:gte:{start_date}",
        f"auction_date:lte:{end_date}" if end_date else "",
        f"security_type:eq:{security_type}" if security_type else "",
        f"security_term:eq:{security_term}" if security_term else "",
        f"cusip:eq:{cusip}" if cusip else "",
    ])

    logger.info(
        "get_auction_results type=%s term=%s cusip=%s range=%s..%s page=%d limit=%d",
        security_type, security_term, cusip, start_date, end_date, page, limit,
    )
    try:
        return _get_client().auctions(
            filter_=filter_,
            fields=_fields_csv(fields, AUCTION_DEFAULT_FIELDS),
            sort=sort,
            page_size=limit,
            page_number=page,
        )
    except Exception:
        logger.exception("get_auction_results failed")
        raise


@mcp.tool()
def get_daily_treasury_statement(
    table: str = "operating_cash_balance",
    start_date: str | None = None,
    end_date: str | None = None,
    fields: list[str] | None = None,
    limit: int = 100,
    page: int = 1,
    sort: str = "-record_date",
) -> dict[str, Any]:
    """Daily Treasury Statement — cash flows in and out of the TGA.

    The DTS is broken into eight tables; pick one via ``table``:

    - ``operating_cash_balance`` *(default)* — TGA opening/closing
      balances and running totals. This is the big one — when people
      say "the TGA drained" or "the TGA is rebuilding," this is the
      series they mean.
    - ``deposits_withdrawals_operating_cash`` — line-item daily flows
      (tax receipts, outlays, debt issuance), detailed component view.
    - ``public_debt_transactions`` — gross issuance / redemption by
      security class.
    - ``adjustment_public_debt_transactions_cash_basis`` — cash-basis
      adjustments to the public-debt transactions table.
    - ``federal_tax_deposits`` — withheld taxes by category.
    - ``short_term_cash_investments`` — Treasury's short-term
      investments (usually zeros since program suspension).
    - ``income_tax_refunds_issued`` — daily refund totals by type.
    - ``inter_agency_tax_transfers`` — trust-fund tax transfers.

    Args:
        table: One of the keys above.
        start_date / end_date: ISO ``YYYY-MM-DD`` on ``record_date``.
            Default start is 30 days ago.
        fields: Column projection. Omit to get everything in the
            table — the DTS tables are narrow enough that's fine.
        limit: Page size (Fiscal Data allows up to 10 000).
        page: 1-indexed page number.
        sort: Fiscal Data sort string.
    """
    if table not in DTS_PATHS:
        raise ValueError(
            f"unknown DTS table {table!r}; valid: {sorted(DTS_PATHS)}"
        )

    if start_date is None:
        start_date = _default_start(30)

    filter_ = _build_filter([
        f"record_date:gte:{start_date}",
        f"record_date:lte:{end_date}" if end_date else "",
    ])

    logger.info(
        "get_daily_treasury_statement table=%s range=%s..%s page=%d limit=%d",
        table, start_date, end_date, page, limit,
    )
    try:
        return _get_client().dts(
            table,
            filter_=filter_,
            fields=",".join(fields) if fields else None,
            sort=sort,
            page_size=limit,
            page_number=page,
        )
    except Exception:
        logger.exception("get_daily_treasury_statement failed table=%s", table)
        raise


@mcp.tool()
def get_debt_to_the_penny(
    start_date: str | None = None,
    end_date: str | None = None,
    fields: list[str] | None = None,
    limit: int = 60,
    page: int = 1,
    sort: str = "-record_date",
) -> dict[str, Any]:
    """Total public debt outstanding, daily.

    Returns ``debt_held_public_amt`` (market-held), ``intragov_hold_amt``
    (Social Security, Medicare trust funds, etc.), and
    ``tot_pub_debt_out_amt`` (the headline total).

    Args:
        start_date / end_date: ISO ``YYYY-MM-DD`` on ``record_date``.
            Default start is 60 days ago.
        fields: Column projection. Omit for all columns.
        limit: Page size (Fiscal Data allows up to 10 000).
        page: 1-indexed page number.
        sort: Fiscal Data sort string.
    """
    if start_date is None:
        start_date = _default_start(60)

    filter_ = _build_filter([
        f"record_date:gte:{start_date}",
        f"record_date:lte:{end_date}" if end_date else "",
    ])

    logger.info(
        "get_debt_to_the_penny range=%s..%s page=%d limit=%d",
        start_date, end_date, page, limit,
    )
    try:
        return _get_client().debt_to_penny(
            filter_=filter_,
            fields=",".join(fields) if fields else None,
            sort=sort,
            page_size=limit,
            page_number=page,
        )
    except Exception:
        logger.exception("get_debt_to_the_penny failed")
        raise


def _configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    for name in (
        "",
        "treasury_connector",
        "mcp",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "httpx",
    ):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)


def main() -> None:
    parser = argparse.ArgumentParser(prog="treasury-connector")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument(
        "--log-file",
        default=os.environ.get("TREASURY_CONNECTOR_LOG", "logs/server.log"),
        help="Path to server log file (default: logs/server.log in cwd).",
    )
    args = parser.parse_args()

    _configure_logging(Path(args.log_file).resolve())
    logger.info(
        "treasury-connector starting transport=%s host=%s port=%s log=%s",
        args.transport, args.host, args.port, args.log_file,
    )

    if args.transport in ("streamable-http", "sse"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
