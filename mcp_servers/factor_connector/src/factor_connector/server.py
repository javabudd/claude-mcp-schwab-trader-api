"""MCP server exposing the Ken French Data Library.

Tool surface is intentionally narrow:

- ``list_datasets`` — catalog of the datasets this server knows about
- ``get_factors`` — canonical Fama-French factor series (3/5 factor,
  momentum, reversal)
- ``get_industry_portfolios`` — N-industry portfolio returns
- ``get_dataset`` — escape hatch for any Ken French filename the
  catalog doesn't cover

All responses include the source URL, fetched-at timestamp, and
cache-hit flag so the model can audit freshness (see
``french_client.py`` for the caching contract).
"""
from __future__ import annotations

import argparse
import atexit
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .french_client import (
    DEFAULT_TTL_SECONDS,
    FrenchClient,
    ParsedFile,
    filter_rows_by_date,
)


logger = logging.getLogger("factor_connector")

mcp = FastMCP(
    "factor-connector",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)
_client: FrenchClient | None = None


def _get_client() -> FrenchClient:
    global _client
    if _client is None:
        logger.info("initializing Ken French client")
        _client = FrenchClient()
        atexit.register(_client.close)
    return _client


# -----------------------------------------------------------------------
# Catalog. Kept in-code (static) because Ken French's filename list
# changes on the order of years, not weeks. If a user needs something
# outside this list, `get_dataset` takes the raw filename.
# -----------------------------------------------------------------------

_FACTOR_FILES: dict[tuple[str, str], str] = {
    # (model, frequency) → dataset filename stem (sans "_CSV.zip")
    ("3factor", "monthly"):  "F-F_Research_Data_Factors",
    ("3factor", "weekly"):   "F-F_Research_Data_Factors_weekly",
    ("3factor", "daily"):    "F-F_Research_Data_Factors_daily",
    ("5factor", "monthly"):  "F-F_Research_Data_5_Factors_2x3",
    ("5factor", "daily"):    "F-F_Research_Data_5_Factors_2x3_daily",
    ("momentum", "monthly"): "F-F_Momentum_Factor",
    ("momentum", "daily"):   "F-F_Momentum_Factor_daily",
    ("st_reversal", "monthly"): "F-F_ST_Reversal_Factor",
    ("st_reversal", "daily"):   "F-F_ST_Reversal_Factor_daily",
    ("lt_reversal", "monthly"): "F-F_LT_Reversal_Factor",
    ("lt_reversal", "daily"):   "F-F_LT_Reversal_Factor_daily",
}

_INDUSTRY_COUNTS = (5, 10, 12, 17, 30, 38, 48, 49)
_INDUSTRY_DAILY_COUNTS = (5, 10, 12, 17, 30, 48)  # 38 / 49 are monthly-only

_WEIGHTING_TO_SECTION: dict[str, str] = {
    "value":          "Average Value Weighted Returns -- Monthly",
    "equal":          "Average Equal Weighted Returns -- Monthly",
    "value_annual":   "Average Value Weighted Returns -- Annual",
    "equal_annual":   "Average Equal Weighted Returns -- Annual",
    "num_firms":      "Number of Firms in Portfolios",
    "avg_firm_size":  "Average Firm Size",
}
_WEIGHTING_TO_SECTION_DAILY: dict[str, str] = {
    "value": "Average Value Weighted Returns -- Daily",
    "equal": "Average Equal Weighted Returns -- Daily",
}


@mcp.tool()
def list_datasets() -> dict[str, Any]:
    """Catalog of Ken French datasets this server knows about.

    Returns the curated list that ``get_factors`` and
    ``get_industry_portfolios`` cover. For anything outside this list,
    use ``get_dataset`` with the raw Ken French filename (see
    https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html).
    """
    factors = [
        {
            "model": model,
            "frequency": freq,
            "dataset_filename": fname,
            "source_url": _get_client().zip_url(fname),
        }
        for (model, freq), fname in sorted(_FACTOR_FILES.items())
    ]
    industries: list[dict[str, Any]] = []
    for n in _INDUSTRY_COUNTS:
        industries.append({
            "n_industries": n,
            "frequency": "monthly",
            "dataset_filename": f"{n}_Industry_Portfolios",
            "source_url": _get_client().zip_url(f"{n}_Industry_Portfolios"),
        })
    for n in _INDUSTRY_DAILY_COUNTS:
        industries.append({
            "n_industries": n,
            "frequency": "daily",
            "dataset_filename": f"{n}_Industry_Portfolios_daily",
            "source_url": _get_client().zip_url(f"{n}_Industry_Portfolios_daily"),
        })
    return {
        "source": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html",
        "factor_datasets": factors,
        "industry_datasets": industries,
        "note": (
            "This catalog covers the factor series and industry portfolios. "
            "Ken French publishes ~300 datasets — use get_dataset(<filename>) "
            "for sort-based portfolios (BE/ME, size, OP, INV), international "
            "regional factors, or any other file on the data library page."
        ),
    }


@mcp.tool()
def get_factors(
    model: Literal["3factor", "5factor", "momentum", "st_reversal", "lt_reversal"] = "3factor",
    frequency: Literal["monthly", "weekly", "daily"] = "monthly",
    start_date: str | None = None,
    end_date: str | None = None,
    refresh: bool = False,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    annual: bool = False,
) -> dict[str, Any]:
    """Fama-French factor series.

    Args:
        model: Which factor set.
            - ``3factor``: Mkt-RF, SMB, HML, RF.
            - ``5factor``: Mkt-RF, SMB, HML, RMW, CMA, RF.
            - ``momentum``: Mom (UMD). Pair with 3factor or 5factor for
              the Carhart model.
            - ``st_reversal``: Short-term reversal factor.
            - ``lt_reversal``: Long-term reversal factor.
        frequency: ``monthly``, ``weekly`` (3factor only), or ``daily``.
        start_date: ISO bound (``YYYY-MM`` for monthly, ``YYYY-MM-DD``
            for daily/weekly, ``YYYY`` for annual).
        end_date: ISO bound, same format rules as ``start_date``.
        refresh: Bypass the on-disk cache and re-fetch.
        ttl_seconds: Cache TTL for this call (default 24 h). The
            upstream file updates monthly, so a day-old cache is fine.
        annual: If True, return the annual-returns block appended to
            most factor files instead of the periodic block.

    Values are **percent returns** (not decimals) — Ken French's
    convention. RF is a one-month T-bill rate.
    """
    key = (model, frequency)
    if key not in _FACTOR_FILES:
        valid = sorted(_FACTOR_FILES.keys())
        raise ValueError(
            f"unsupported (model, frequency) = {key!r}; valid: {valid}"
        )
    dataset = _FACTOR_FILES[key]
    logger.info(
        "get_factors model=%s frequency=%s dataset=%s annual=%s start=%s end=%s",
        model, frequency, dataset, annual, start_date, end_date,
    )
    try:
        parsed, meta = _get_client().load(
            dataset, ttl_seconds=ttl_seconds, refresh=refresh,
        )
    except Exception:
        logger.exception("get_factors load failed dataset=%s", dataset)
        raise

    section = _pick_factor_section(parsed, annual=annual)
    rows = filter_rows_by_date(section.rows, start_date, end_date)
    logger.info(
        "get_factors result dataset=%s section=%r rows=%d from_cache=%s",
        dataset, section.title, len(rows), meta["from_cache"],
    )
    return {
        **meta,
        "model": model,
        "frequency": frequency,
        "annual": annual,
        "section_title": section.title,
        "columns": section.columns,
        "count": len(rows),
        "rows": rows,
    }


@mcp.tool()
def get_industry_portfolios(
    n_industries: int = 12,
    frequency: Literal["monthly", "daily"] = "monthly",
    weighting: Literal[
        "value", "equal", "value_annual", "equal_annual",
        "num_firms", "avg_firm_size",
    ] = "value",
    start_date: str | None = None,
    end_date: str | None = None,
    refresh: bool = False,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """N-industry portfolio returns (Ken French classification).

    Args:
        n_industries: 5, 10, 12, 17, 30, 38, 48, or 49. Daily files
            exist only for 5/10/12/17/30/48 — pick monthly for 38/49.
        frequency: ``monthly`` or ``daily``.
        weighting: Which block inside the file to return:
            - ``value`` — value-weighted returns (the default).
            - ``equal`` — equal-weighted returns.
            - ``value_annual`` / ``equal_annual`` — January-December
              annual returns (monthly files only).
            - ``num_firms`` — firm count per portfolio (monthly only).
            - ``avg_firm_size`` — mean market cap per portfolio
              (monthly only).
        start_date: ISO bound (``YYYY-MM`` monthly, ``YYYY-MM-DD``
            daily, ``YYYY`` annual).
        end_date: ISO bound, same format rules as ``start_date``.
        refresh: Bypass the on-disk cache.
        ttl_seconds: Cache TTL for this call (default 24 h).

    Return values are **percent returns** for the returns blocks;
    ``num_firms`` is a count and ``avg_firm_size`` is market cap in
    millions USD.
    """
    if n_industries not in _INDUSTRY_COUNTS:
        raise ValueError(
            f"n_industries must be one of {_INDUSTRY_COUNTS}; got {n_industries}"
        )
    if frequency == "daily" and n_industries not in _INDUSTRY_DAILY_COUNTS:
        raise ValueError(
            f"{n_industries}-industry portfolios have no daily file — "
            f"pick frequency='monthly' or choose from {_INDUSTRY_DAILY_COUNTS}"
        )
    if frequency == "daily" and weighting not in _WEIGHTING_TO_SECTION_DAILY:
        raise ValueError(
            f"daily files only expose {list(_WEIGHTING_TO_SECTION_DAILY)} — "
            f"use a monthly file for '{weighting}'"
        )

    suffix = "_daily" if frequency == "daily" else ""
    dataset = f"{n_industries}_Industry_Portfolios{suffix}"
    section_title = (
        _WEIGHTING_TO_SECTION_DAILY[weighting]
        if frequency == "daily"
        else _WEIGHTING_TO_SECTION[weighting]
    )
    logger.info(
        "get_industry_portfolios n=%d freq=%s weighting=%s dataset=%s section=%r "
        "start=%s end=%s",
        n_industries, frequency, weighting, dataset, section_title,
        start_date, end_date,
    )
    try:
        parsed, meta = _get_client().load(
            dataset, ttl_seconds=ttl_seconds, refresh=refresh,
        )
    except Exception:
        logger.exception(
            "get_industry_portfolios load failed dataset=%s", dataset,
        )
        raise

    section = parsed.find_section(section_title)
    if section is None:
        available = [s.title for s in parsed.sections]
        raise ValueError(
            f"section {section_title!r} not found in {dataset}; "
            f"available sections: {available}"
        )

    rows = filter_rows_by_date(section.rows, start_date, end_date)
    logger.info(
        "get_industry_portfolios result dataset=%s rows=%d from_cache=%s",
        dataset, len(rows), meta["from_cache"],
    )
    return {
        **meta,
        "n_industries": n_industries,
        "frequency": frequency,
        "weighting": weighting,
        "section_title": section.title,
        "columns": section.columns,
        "count": len(rows),
        "rows": rows,
    }


@mcp.tool()
def get_dataset(
    dataset_filename: str,
    table: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    refresh: bool = False,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Escape hatch for any Ken French dataset.

    Args:
        dataset_filename: Filename stem as it appears at
            ``https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/``
            *without* the trailing ``_CSV.zip``. Examples:
            ``Portfolios_Formed_on_BE-ME``,
            ``25_Portfolios_5x5``,
            ``F-F_Research_Data_Factors``,
            ``Developed_3_Factors``.
        table: Optional substring to match against section titles
            (case-insensitive). If omitted, every section is returned
            without rows (just columns and ``row_count``) so you can
            pick one in a follow-up call. If supplied, the first
            matching section's rows are returned in full.
        start_date / end_date: ISO-prefix date filters, applied only
            when ``table`` is set. Format must match the data
            ("YYYY-MM" monthly, "YYYY-MM-DD" daily, "YYYY" annual).
        refresh: Bypass the on-disk cache.
        ttl_seconds: Cache TTL for this call (default 24 h).
    """
    logger.info(
        "get_dataset dataset=%s table=%r start=%s end=%s",
        dataset_filename, table, start_date, end_date,
    )
    try:
        parsed, meta = _get_client().load(
            dataset_filename, ttl_seconds=ttl_seconds, refresh=refresh,
        )
    except Exception:
        logger.exception("get_dataset load failed dataset=%s", dataset_filename)
        raise

    if table is None:
        return {
            **meta,
            "header_notes": parsed.header_notes,
            "sections": [
                {
                    "title": s.title,
                    "columns": s.columns,
                    "row_count": len(s.rows),
                }
                for s in parsed.sections
            ],
        }

    section = parsed.find_section(table)
    if section is None:
        available = [s.title for s in parsed.sections]
        raise ValueError(
            f"no section matched {table!r} in {dataset_filename}; "
            f"available: {available}"
        )
    rows = filter_rows_by_date(section.rows, start_date, end_date)
    logger.info(
        "get_dataset result dataset=%s section=%r rows=%d from_cache=%s",
        dataset_filename, section.title, len(rows), meta["from_cache"],
    )
    return {
        **meta,
        "header_notes": parsed.header_notes,
        "section_title": section.title,
        "columns": section.columns,
        "count": len(rows),
        "rows": rows,
    }


def _pick_factor_section(parsed: ParsedFile, *, annual: bool):
    """Factor files have an unlabeled periodic section + a labeled annual section.

    The periodic section comes first and has ``title=None``; the annual
    one (if present) is titled something like "Annual Factors:
    January-December".
    """
    if annual:
        for s in parsed.sections:
            if s.title and "annual" in s.title.lower():
                return s
        raise ValueError(
            "annual=True but no annual-returns section in this file"
        )
    for s in parsed.sections:
        if s.title is None or "annual" not in (s.title or "").lower():
            return s
    raise ValueError("no periodic factor section found in this file")


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
        "factor_connector",
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
    parser = argparse.ArgumentParser(prog="factor-connector")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument(
        "--log-file",
        default=os.environ.get("FACTOR_CONNECTOR_LOG", "logs/server.log"),
        help="Path to server log file (default: logs/server.log in cwd).",
    )
    args = parser.parse_args()

    _configure_logging(Path(args.log_file).resolve())
    logger.info(
        "factor-connector starting transport=%s host=%s port=%s log=%s",
        args.transport, args.host, args.port, args.log_file,
    )

    if args.transport in ("streamable-http", "sse"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
