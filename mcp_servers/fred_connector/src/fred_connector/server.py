"""MCP server exposing read-only FRED lookups to an AI CLI.

Tool surface focuses on what a trader actually reaches for: the
economic-release calendar (CPI, NFP, PCE, GDP, retail sales, …),
metadata about individual releases/series, and the observation
time-series themselves.

All responses are FRED's JSON essentially unchanged so the model can
introspect fields rather than second-guessing a translation layer.
"""
from __future__ import annotations

import argparse
import atexit
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .fred_client import FredClient


# Trading-relevant releases, grouped by what they move. Kept deliberately
# small — a curated list is only useful if the ceiling is low. Users who
# want more can call `get_release_schedule` with their own `release_ids`.
# Release 101 ("FOMC Press Release") is intentionally *not* here: FRED
# emits noisy every-day-of-the-meeting-window rows for it; for FOMC
# dates use `fed_calendar_connector`'s `get_fomc_meetings` instead.
_HIGH_IMPACT_RELEASES: dict[str, dict[int, str]] = {
    "inflation": {
        10: "Consumer Price Index",
        21: "Personal Income and Outlays",
        46: "Producer Price Index",
    },
    "labor": {
        50: "Employment Situation",
        192: "Job Openings and Labor Turnover Survey",
    },
    "growth": {
        53: "Gross Domestic Product",
    },
    "consumer": {
        32: "Advance Monthly Sales for Retail and Food Services",
    },
}

logger = logging.getLogger("fred_connector")

mcp = FastMCP(
    "fred-connector",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)
_client: FredClient | None = None


def _get_client() -> FredClient:
    global _client
    if _client is None:
        logger.info("initializing FRED client")
        _client = FredClient.from_env()
        atexit.register(_client.close)
        logger.info("FRED client ready")
    return _client


@mcp.tool()
def get_release_schedule(
    realtime_start: str | None = None,
    realtime_end: str | None = None,
    release_ids: list[int] | None = None,
    name_contains: list[str] | None = None,
    limit: int | None = 200,
    include_empty: bool = True,
    sort_order: str | None = "asc",
    dedupe: bool = True,
) -> dict[str, Any]:
    """Economic-release calendar, filtered server-side.

    Defaults to a **forward-looking** view (``realtime_start`` =
    today UTC) so the "schedule" actually means what's coming up.
    Pass an earlier ``realtime_start`` to see historical dates too.

    Filtering (apply as many as you want — they compose):

    - ``release_ids`` — fan out to FRED's per-release endpoint once
      per id and merge the rows. Cleanest way to cut noise: if you
      already know the releases you care about (CPI=10, NFP=50,
      GDP=53, …), this avoids dragging in hundreds of low-signal
      releases. See :func:`list_releases` to discover ids.
    - ``name_contains`` — case-insensitive substring match against
      ``release_name``. Multiple strings are OR'd. Useful when you
      know the name but not the id.
    - ``dedupe`` — drops duplicate ``(date, release_id)`` rows. FRED
      sometimes emits near-duplicates for the same release on the
      same day; ``True`` by default.

    ``include_empty=True`` keeps scheduled future dates that don't
    have data attached yet — that's how you *see* upcoming releases.

    For FOMC meeting dates specifically, reach for
    ``get_fomc_meetings`` on the ``fed_calendar_connector`` rather
    than this tool: FRED's release 101 ("FOMC Press Release") is
    noisy here (fires on every day of the meeting window).

    Returns a FRED-shaped payload with ``release_dates`` and a
    ``count`` that reflects the post-filter row count.
    """
    if realtime_start is None:
        realtime_start = datetime.now(timezone.utc).date().isoformat()

    logger.info(
        "get_release_schedule realtime=%s..%s release_ids=%s name_contains=%s "
        "limit=%s include_empty=%s dedupe=%s",
        realtime_start, realtime_end, release_ids, name_contains,
        limit, include_empty, dedupe,
    )
    client = _get_client()
    try:
        if release_ids:
            rows = _fan_out_release_dates(
                client,
                release_ids=release_ids,
                realtime_start=realtime_start,
                realtime_end=realtime_end,
                limit=limit,
                include_empty=include_empty,
            )
            base: dict[str, Any] = {
                "realtime_start": realtime_start,
                "realtime_end": realtime_end,
            }
        else:
            raw = client.releases_dates(
                realtime_start=realtime_start,
                realtime_end=realtime_end,
                limit=limit,
                include_release_dates_with_no_data=include_empty,
                order_by="release_date",
                sort_order=sort_order,
            )
            rows = list(raw.get("release_dates", []))
            base = {
                k: v for k, v in raw.items() if k != "release_dates" and k != "count"
            }
    except Exception:
        logger.exception("get_release_schedule failed")
        raise

    if name_contains:
        needles = [s.lower() for s in name_contains if s]
        rows = [
            r for r in rows
            if any(n in (r.get("release_name") or "").lower() for n in needles)
        ]

    if dedupe:
        rows = _dedupe_release_rows(rows)

    reverse = (sort_order or "asc").lower() == "desc"
    rows.sort(
        key=lambda r: (r.get("date") or "", r.get("release_id") or 0),
        reverse=reverse,
    )

    logger.info("get_release_schedule result count=%d", len(rows))
    return {**base, "count": len(rows), "release_dates": rows}


@mcp.tool()
def get_high_impact_calendar(
    realtime_start: str | None = None,
    realtime_end: str | None = None,
    categories: list[str] | None = None,
    include_empty: bool = True,
    limit_per_release: int = 50,
) -> dict[str, Any]:
    """Curated economic calendar for a trading analyst.

    Fans out to FRED per-release for a hand-picked list of
    market-moving releases (CPI, PCE, PPI, NFP, JOLTS, GDP, Retail
    Sales) and returns a single merged, deduped, category-annotated
    timeline. Defaults to a forward-looking view (today → FRED's
    horizon).

    Args:
        realtime_start: ISO date. Defaults to today UTC — i.e.
            upcoming only.
        realtime_end: ISO date. Defaults to FRED's horizon.
        categories: Subset of ``inflation`` / ``labor`` / ``growth``
            / ``consumer``. ``None`` = all.
        include_empty: Keep scheduled future dates that don't yet
            carry values (that's usually what you want for a
            calendar).
        limit_per_release: Per-release row cap (FRED max 10000).

    **Does not cover FOMC meeting dates** — FRED's release 101 is
    noisy. Use ``get_fomc_meetings`` on the ``fed_calendar_connector``
    for those.

    For anything outside this curated list, use
    :func:`get_release_schedule` with your own ``release_ids`` or
    ``name_contains``.

    Each row includes ``category``, ``release_id``, ``release_name``,
    and ``date``.
    """
    chosen = _resolve_categories(categories)
    flat: dict[int, tuple[str, str]] = {
        rid: (cat, name)
        for cat, releases in chosen.items()
        for rid, name in releases.items()
    }
    release_ids = sorted(flat.keys())

    if realtime_start is None:
        realtime_start = datetime.now(timezone.utc).date().isoformat()

    logger.info(
        "get_high_impact_calendar categories=%s ids=%s realtime=%s..%s",
        sorted(chosen.keys()), release_ids, realtime_start, realtime_end,
    )

    try:
        rows = _fan_out_release_dates(
            _get_client(),
            release_ids=release_ids,
            realtime_start=realtime_start,
            realtime_end=realtime_end,
            limit=limit_per_release,
            include_empty=include_empty,
        )
    except Exception:
        logger.exception("get_high_impact_calendar fan-out failed")
        raise

    for row in rows:
        rid = row.get("release_id")
        if rid in flat:
            row["category"] = flat[rid][0]

    rows = _dedupe_release_rows(rows)
    rows.sort(key=lambda r: (r.get("date") or "", r.get("release_id") or 0))

    logger.info(
        "get_high_impact_calendar result count=%d categories=%s",
        len(rows), sorted(chosen.keys()),
    )
    return {
        "realtime_start": realtime_start,
        "realtime_end": realtime_end,
        "categories": sorted(chosen.keys()),
        "release_ids": release_ids,
        "count": len(rows),
        "release_dates": rows,
        "note": (
            "FOMC meeting dates are not in this feed — use "
            "fed_calendar_connector.get_fomc_meetings for those."
        ),
    }


def _resolve_categories(
    categories: list[str] | None,
) -> dict[str, dict[int, str]]:
    if categories is None:
        return _HIGH_IMPACT_RELEASES
    wanted = {c.lower() for c in categories}
    unknown = wanted - set(_HIGH_IMPACT_RELEASES)
    if unknown:
        raise ValueError(
            f"unknown categories: {sorted(unknown)}; "
            f"valid: {sorted(_HIGH_IMPACT_RELEASES)}"
        )
    return {k: _HIGH_IMPACT_RELEASES[k] for k in wanted}


def _fan_out_release_dates(
    client: FredClient,
    *,
    release_ids: list[int],
    realtime_start: str | None,
    realtime_end: str | None,
    limit: int | None,
    include_empty: bool,
) -> list[dict[str, Any]]:
    """Per-release calls merged into a flat list of enriched rows.

    `/release/dates` doesn't echo the release name (you queried by id),
    so we stamp ``release_id`` and ``release_name`` onto each row to
    match the shape `/releases/dates` returns.
    """
    merged: list[dict[str, Any]] = []
    for rid in release_ids:
        payload = client.release_dates(
            rid,
            realtime_start=realtime_start,
            realtime_end=realtime_end,
            limit=limit,
            include_release_dates_with_no_data=include_empty,
        )
        name = _release_name_from_payload(payload)
        for row in payload.get("release_dates", []):
            merged.append({
                "release_id": rid,
                "release_name": name or row.get("release_name"),
                "date": row.get("date"),
            })
    return merged


def _release_name_from_payload(payload: dict[str, Any]) -> str | None:
    for row in payload.get("release_dates", []):
        name = row.get("release_name")
        if name:
            return name
    return None


def _dedupe_release_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, Any]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row.get("date"), row.get("release_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


@mcp.tool()
def get_release_dates(
    release_id: int,
    realtime_start: str | None = None,
    realtime_end: str | None = None,
    limit: int | None = 100,
    include_empty: bool = True,
) -> dict[str, Any]:
    """Past and scheduled publication dates for one release.

    Use :func:`list_releases` or the FRED website to find the
    ``release_id`` (CPI=10, Employment Situation=50, GDP=53, PCE=21,
    Retail Sales=30, JOLTS=192, FOMC Meeting=101, …).
    """
    logger.info(
        "get_release_dates release_id=%d realtime=%s..%s",
        release_id, realtime_start, realtime_end,
    )
    try:
        return _get_client().release_dates(
            release_id,
            realtime_start=realtime_start,
            realtime_end=realtime_end,
            limit=limit,
            include_release_dates_with_no_data=include_empty,
        )
    except Exception:
        logger.exception("get_release_dates failed release_id=%d", release_id)
        raise


@mcp.tool()
def list_releases(limit: int | None = 200) -> dict[str, Any]:
    """All FRED releases, for discovering ``release_id`` values."""
    logger.info("list_releases limit=%s", limit)
    try:
        return _get_client().releases(limit=limit)
    except Exception:
        logger.exception("list_releases failed")
        raise


@mcp.tool()
def get_release_info(release_id: int) -> dict[str, Any]:
    """Metadata for a single release (name, link, notes)."""
    logger.info("get_release_info release_id=%d", release_id)
    try:
        return _get_client().release(release_id)
    except Exception:
        logger.exception("get_release_info failed release_id=%d", release_id)
        raise


@mcp.tool()
def get_release_series(
    release_id: int,
    limit: int | None = 100,
    order_by: str | None = "popularity",
) -> dict[str, Any]:
    """Series published under a release (e.g. CPI headline + components)."""
    logger.info(
        "get_release_series release_id=%d limit=%s order_by=%s",
        release_id, limit, order_by,
    )
    try:
        return _get_client().release_series(
            release_id, limit=limit, order_by=order_by,
        )
    except Exception:
        logger.exception("get_release_series failed release_id=%d", release_id)
        raise


@mcp.tool()
def search_series(
    search_text: str,
    limit: int | None = 25,
    order_by: str | None = "popularity",
    sort_order: str | None = "desc",
) -> dict[str, Any]:
    """Fuzzy search for series IDs by title/notes.

    Examples: ``"core CPI"`` → ``CPILFESL``, ``"10-year treasury"`` →
    ``DGS10``, ``"fed funds"`` → ``DFF`` / ``FEDFUNDS``.
    """
    logger.info("search_series text=%r limit=%s", search_text, limit)
    try:
        return _get_client().series_search(
            search_text,
            limit=limit,
            order_by=order_by,
            sort_order=sort_order,
        )
    except Exception:
        logger.exception("search_series failed text=%r", search_text)
        raise


@mcp.tool()
def get_series_info(series_id: str) -> dict[str, Any]:
    """Series metadata (units, frequency, last-updated, seasonal adj)."""
    logger.info("get_series_info series_id=%s", series_id)
    try:
        return _get_client().series(series_id)
    except Exception:
        logger.exception("get_series_info failed series_id=%s", series_id)
        raise


@mcp.tool()
def get_series(
    series_id: str,
    observation_start: str | None = None,
    observation_end: str | None = None,
    limit: int | None = 500,
    sort_order: str | None = "desc",
    units: str | None = None,
    frequency: str | None = None,
    aggregation_method: str | None = None,
) -> dict[str, Any]:
    """Observations for one series.

    Args:
        series_id: FRED ID (e.g. ``CPIAUCSL``, ``UNRATE``, ``DGS10``).
        observation_start / observation_end: ISO dates.
        limit: Max observations (FRED caps at 100 000).
        sort_order: ``asc`` (oldest first) or ``desc`` (most recent first).
        units: ``lin`` | ``chg`` | ``ch1`` | ``pch`` | ``pc1`` | ``pca``
            | ``cch`` | ``cca`` | ``log``. Default is ``lin``.
        frequency: Resample on the server, e.g. ``m``, ``q``, ``a``.
        aggregation_method: ``avg`` (default), ``sum``, ``eop`` — only
            relevant with ``frequency``.
    """
    logger.info(
        "get_series series_id=%s start=%s end=%s limit=%s",
        series_id, observation_start, observation_end, limit,
    )
    try:
        result = _get_client().series_observations(
            series_id,
            observation_start=observation_start,
            observation_end=observation_end,
            limit=limit,
            sort_order=sort_order,
            units=units,
            frequency=frequency,
            aggregation_method=aggregation_method,
        )
    except Exception:
        logger.exception("get_series failed series_id=%s", series_id)
        raise
    obs = result.get("observations", [])
    logger.info("get_series result series_id=%s observations=%d", series_id, len(obs))
    return result


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
    for name in ("", "fred_connector", "mcp", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx"):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)


def main() -> None:
    parser = argparse.ArgumentParser(prog="fred-connector")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--log-file",
        default=os.environ.get("FRED_CONNECTOR_LOG", "logs/server.log"),
        help="Path to server log file (default: logs/server.log in cwd).",
    )
    args = parser.parse_args()

    _configure_logging(Path(args.log_file).resolve())
    logger.info(
        "fred-connector starting transport=%s host=%s port=%s log=%s",
        args.transport, args.host, args.port, args.log_file,
    )

    if args.transport in ("streamable-http", "sse"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
