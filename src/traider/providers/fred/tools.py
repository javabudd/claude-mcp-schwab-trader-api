"""FRED tools registered on the shared FastMCP.

Tool surface focuses on what a trader actually reaches for: the
economic-release calendar (CPI, NFP, PCE, GDP, retail sales, ...),
metadata about individual releases/series, and the observation
time-series themselves.

All responses are FRED's JSON essentially unchanged so the model can
introspect fields rather than second-guessing a translation layer.
"""
from __future__ import annotations

import atexit
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from ...logging_utils import attach_provider_logger
from ...settings import TraiderSettings
from . import analytics
from .fred_client import FredClient

_FRED_BASE = "https://api.stlouisfed.org/fred"


def _src(path: str) -> str:
    return f"{_FRED_BASE}{path}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# Trading-relevant releases, grouped by what they move. Kept deliberately
# small — a curated list is only useful if the ceiling is low. Users who
# want more can call `get_release_schedule` with their own `release_ids`.
# Release 101 ("FOMC Press Release") is intentionally *not* here: FRED
# emits noisy every-day-of-the-meeting-window rows for it; for FOMC
# dates use the `fed-calendar` provider's `get_fomc_meetings` instead.
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

logger = logging.getLogger("traider.fred")
_client: FredClient | None = None


def _get_client() -> FredClient:
    global _client
    if _client is None:
        logger.info("initializing FRED client")
        _client = FredClient.from_env()
        atexit.register(_client.close)
        logger.info("FRED client ready")
    return _client


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


# Three-year default for macro tools: a 504-observation z-score window
# on a daily series needs ~2 calendar years; 3 gives headroom and keeps
# the deltas (1m/3m/6m/1y) inside the fetched window.
_MACRO_DEFAULT_LOOKBACK_DAYS = 3 * 365 + 60


def _default_macro_start() -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=_MACRO_DEFAULT_LOOKBACK_DAYS)).isoformat()


def _fetch_series(
    client: FredClient,
    series_id: str,
    observation_start: str,
) -> list[tuple[str, float]]:
    payload = client.series_observations(
        series_id,
        observation_start=observation_start,
        limit=100_000,
        sort_order="asc",
    )
    return analytics.parse_observations(payload)


def _latest_as_of(series_map: dict[str, list[tuple[str, float]]]) -> str | None:
    dates = [s[-1][0] for s in series_map.values() if s]
    return max(dates) if dates else None


def _yield_curve_payload(
    client: FredClient,
    observation_start: str,
    zscore_window: int,
) -> dict[str, Any]:
    series_ids = {"3m": "DGS3MO", "2y": "DGS2", "10y": "DGS10", "30y": "DGS30"}
    fetched = {
        label: _fetch_series(client, sid, observation_start)
        for label, sid in series_ids.items()
    }
    levels = {
        label: analytics.summarize_series(s, zscore_window)
        for label, s in fetched.items()
    }
    slope_2s10s = analytics.difference_series(fetched["10y"], fetched["2y"])
    slope_3m10y = analytics.difference_series(fetched["10y"], fetched["3m"])
    slope_5s30s = analytics.difference_series(fetched["30y"], fetched["2y"])

    def slope_summary(spread: list[tuple[str, float]]) -> dict[str, Any]:
        summary = analytics.summarize_series(spread, zscore_window)
        summary["inverted"] = (spread[-1][1] < 0) if spread else None
        return summary

    slopes = {
        "2s10s": slope_summary(slope_2s10s),
        "3m10y": slope_summary(slope_3m10y),
        "2s30s": slope_summary(slope_5s30s),
    }
    latest_2s10s = slope_2s10s[-1][1] if slope_2s10s else None
    latest_3m10y = slope_3m10y[-1][1] if slope_3m10y else None
    shape = analytics.curve_shape(latest_2s10s, latest_3m10y)
    return {
        "as_of": _latest_as_of(fetched),
        "levels": levels,
        "slopes": slopes,
        "series_ids": series_ids,
        "zscore_window": zscore_window,
        "derived": {"curve_shape": shape},
        "units_note": "Yields are in percent; slopes in percentage points.",
    }


def _credit_spreads_payload(
    client: FredClient,
    observation_start: str,
    zscore_window: int,
) -> dict[str, Any]:
    series_ids = {"hy_oas": "BAMLH0A0HYM2", "ig_oas": "BAMLC0A0CM"}
    fetched = {
        label: _fetch_series(client, sid, observation_start)
        for label, sid in series_ids.items()
    }
    summaries = {
        label: analytics.summarize_series(s, zscore_window)
        for label, s in fetched.items()
    }
    hy_z = (summaries["hy_oas"].get("zscore") or {}).get("z_score")
    ig_z = (summaries["ig_oas"].get("zscore") or {}).get("z_score")
    return {
        "as_of": _latest_as_of(fetched),
        "series": summaries,
        "series_ids": series_ids,
        "zscore_window": zscore_window,
        "derived": {"regime": analytics.credit_regime(hy_z, ig_z)},
        "units_note": "OAS values are in percent (decimal percent, not bps).",
    }


# Ordered highest-to-lowest quality so the diagnostic's "lowest-quality
# bucket holds the max z" check (low_end_stress) is well-defined.
_IG_RATING_SERIES: dict[str, str] = {
    "AAA": "BAMLC0A1CAAA",
    "AA":  "BAMLC0A2CAA",
    "A":   "BAMLC0A3CA",
    "BBB": "BAMLC0A4CBBB",
}
_HY_RATING_SERIES: dict[str, str] = {
    "BB":  "BAMLH0A1HYBB",
    "B":   "BAMLH0A2HYB",
    "CCC": "BAMLH0A3HYC",
}

# IG corporate OAS by maturity bucket. ICE BofA does not publish HY
# term-bucket OAS on FRED — see the tool docstring.
_IG_TERM_SERIES: dict[str, str] = {
    "1_3y":   "BAMLC1A0C13Y",
    "3_5y":   "BAMLC2A0C35Y",
    "5_7y":   "BAMLC3A0C57Y",
    "7_10y":  "BAMLC4A0C710Y",
    "10_15y": "BAMLC7A0C1015Y",
    "15p_y":  "BAMLC8A0C15PY",
}


def _summarize_basket(
    client: FredClient,
    series_ids: dict[str, str],
    observation_start: str,
    zscore_window: int,
) -> tuple[dict[str, list[tuple[str, float]]], dict[str, Any]]:
    fetched = {
        label: _fetch_series(client, sid, observation_start)
        for label, sid in series_ids.items()
    }
    summaries = {
        label: analytics.summarize_series(s, zscore_window)
        for label, s in fetched.items()
    }
    return fetched, summaries


def _quality_curve_segment(
    client: FredClient,
    series_ids: dict[str, str],
    observation_start: str,
    zscore_window: int,
) -> dict[str, Any]:
    fetched, summaries = _summarize_basket(
        client, series_ids, observation_start, zscore_window,
    )
    z_by_rating = [
        (summaries[label].get("zscore") or {}).get("z_score")
        for label in series_ids
    ]
    latest_by_rating = [
        (summaries[label].get("latest") or {}).get("value")
        for label in series_ids
    ]
    return {
        "as_of": _latest_as_of(fetched),
        "series": summaries,
        "series_ids": series_ids,
        "derived": analytics.quality_curve_diagnostic(z_by_rating, latest_by_rating),
    }


def _credit_quality_curve_payload(
    client: FredClient,
    observation_start: str,
    zscore_window: int,
    segment: str,
) -> dict[str, Any]:
    segments: dict[str, Any] = {}
    if segment in ("ig", "both"):
        segments["ig"] = _quality_curve_segment(
            client, _IG_RATING_SERIES, observation_start, zscore_window,
        )
    if segment in ("hy", "both"):
        segments["hy"] = _quality_curve_segment(
            client, _HY_RATING_SERIES, observation_start, zscore_window,
        )
    as_ofs = [v.get("as_of") for v in segments.values() if v.get("as_of")]
    return {
        "as_of": max(as_ofs) if as_ofs else None,
        "segment": segment,
        "segments": segments,
        "zscore_window": zscore_window,
        "units_note": (
            "OAS values are in percent (decimal percent, not bps). "
            "Per-segment derived.regime: 'compressed' (all z<-0.5; "
            "late-cycle reach for yield), 'broad_widening' (all z>1 "
            "with low dispersion; uniform stress), 'low_end_stress' "
            "(z dispersion >=1.0 with the worst-rated bucket on top; "
            "classic flight to quality), 'mixed' otherwise."
        ),
    }


def _credit_term_structure_payload(
    client: FredClient,
    observation_start: str,
    zscore_window: int,
) -> dict[str, Any]:
    fetched, summaries = _summarize_basket(
        client, _IG_TERM_SERIES, observation_start, zscore_window,
    )

    def _latest(label: str) -> float | None:
        return (summaries[label].get("latest") or {}).get("value")

    return {
        "as_of": _latest_as_of(fetched),
        "series": summaries,
        "series_ids": _IG_TERM_SERIES,
        "zscore_window": zscore_window,
        "derived": {
            "front_to_belly": analytics.credit_term_slope(_latest("1_3y"), _latest("7_10y")),
            "full_curve": analytics.credit_term_slope(_latest("1_3y"), _latest("15p_y")),
        },
        "units_note": (
            "OAS values in percent (decimal percent, not bps); slopes "
            "in percentage points (long minus short). For IG the OAS "
            "term structure is almost always upward-sloping — an "
            "'inverted' label is rare and flags acute short-end stress."
        ),
    }


def _breakevens_payload(
    client: FredClient,
    observation_start: str,
    zscore_window: int,
    target: float,
    target_band: float,
) -> dict[str, Any]:
    series_ids = {"5y": "T5YIE", "10y": "T10YIE", "5y5y_forward": "T5YIFR"}
    fetched = {
        label: _fetch_series(client, sid, observation_start)
        for label, sid in series_ids.items()
    }
    summaries: dict[str, Any] = {}
    for label, s in fetched.items():
        summary = analytics.summarize_series(s, zscore_window)
        latest = (summary.get("latest") or {}).get("value")
        summary["derived"] = {
            "alignment": analytics.breakeven_alignment(
                latest, target=target, band=target_band,
            ),
            "deviation_from_target": (
                latest - target if latest is not None else None
            ),
        }
        summaries[label] = summary
    return {
        "as_of": _latest_as_of(fetched),
        "target": target,
        "target_band": target_band,
        "series": summaries,
        "series_ids": series_ids,
        "zscore_window": zscore_window,
        "units_note": (
            "Breakeven rates are in percent. The Fed's 2% target is for "
            "PCE, not breakevens — breakevens include an inflation risk "
            "premium (typically 20-50bp above expected inflation)."
        ),
    }


def _financial_conditions_payload(
    client: FredClient,
    observation_start: str,
    zscore_window: int,
) -> dict[str, Any]:
    series_ids = {"nfci": "NFCI", "anfci": "ANFCI"}
    fetched = {
        label: _fetch_series(client, sid, observation_start)
        for label, sid in series_ids.items()
    }
    summaries: dict[str, Any] = {}
    for label, s in fetched.items():
        summary = analytics.summarize_series(s, zscore_window)
        latest = (summary.get("latest") or {}).get("value")
        summary["derived"] = {"regime": analytics.nfci_regime(latest)}
        summaries[label] = summary
    return {
        "as_of": _latest_as_of(fetched),
        "series": summaries,
        "series_ids": series_ids,
        "zscore_window": zscore_window,
        "units_note": (
            "Both indices are centered at 0. NFCI = absolute level of "
            "financial tightness vs the 1971-present average; positive = "
            "tighter. ANFCI = financial conditions *adjusted for* the "
            "current economic cycle (regressed out of credit / inflation / "
            "activity), so ANFCI ≈ 0 means conditions are in line with "
            "what macro would normally produce. ANFCI >> 0 flags stress "
            "beyond what the cycle justifies. Weekly, Wed release."
        ),
    }


def register(mcp: FastMCP, settings: TraiderSettings) -> None:
    attach_provider_logger("traider.fred", settings.log_file("fred"))

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
          GDP=53, ...), this avoids dragging in hundreds of low-signal
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
        ``get_fomc_meetings`` on the ``fed-calendar`` provider rather
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
        source = (
            _src("/release/dates") if release_ids else _src("/releases/dates")
        )
        return {
            "source": source,
            "fetched_at": _now_iso(),
            **base,
            "count": len(rows),
            "release_dates": rows,
        }

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
        noisy. Use ``get_fomc_meetings`` on the ``fed-calendar`` provider
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
            "source": _src("/release/dates"),
            "fetched_at": _now_iso(),
            "realtime_start": realtime_start,
            "realtime_end": realtime_end,
            "categories": sorted(chosen.keys()),
            "release_ids": release_ids,
            "count": len(rows),
            "release_dates": rows,
            "note": (
                "FOMC meeting dates are not in this feed — use "
                "the fed-calendar provider's get_fomc_meetings for those."
            ),
        }

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
        Retail Sales=30, JOLTS=192, FOMC Meeting=101, ...).
        """
        logger.info(
            "get_release_dates release_id=%d realtime=%s..%s",
            release_id, realtime_start, realtime_end,
        )
        fetched_at = _now_iso()
        try:
            payload = _get_client().release_dates(
                release_id,
                realtime_start=realtime_start,
                realtime_end=realtime_end,
                limit=limit,
                include_release_dates_with_no_data=include_empty,
            )
        except Exception:
            logger.exception("get_release_dates failed release_id=%d", release_id)
            raise
        return {
            "source": _src("/release/dates"),
            "fetched_at": fetched_at,
            **payload,
        }

    @mcp.tool()
    def list_releases(limit: int | None = 200) -> dict[str, Any]:
        """All FRED releases, for discovering ``release_id`` values."""
        logger.info("list_releases limit=%s", limit)
        fetched_at = _now_iso()
        try:
            payload = _get_client().releases(limit=limit)
        except Exception:
            logger.exception("list_releases failed")
            raise
        return {
            "source": _src("/releases"),
            "fetched_at": fetched_at,
            **payload,
        }

    @mcp.tool()
    def get_release_info(release_id: int) -> dict[str, Any]:
        """Metadata for a single release (name, link, notes)."""
        logger.info("get_release_info release_id=%d", release_id)
        fetched_at = _now_iso()
        try:
            payload = _get_client().release(release_id)
        except Exception:
            logger.exception("get_release_info failed release_id=%d", release_id)
            raise
        return {
            "source": _src("/release"),
            "fetched_at": fetched_at,
            **payload,
        }

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
        fetched_at = _now_iso()
        try:
            payload = _get_client().release_series(
                release_id, limit=limit, order_by=order_by,
            )
        except Exception:
            logger.exception("get_release_series failed release_id=%d", release_id)
            raise
        return {
            "source": _src("/release/series"),
            "fetched_at": fetched_at,
            **payload,
        }

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
        fetched_at = _now_iso()
        try:
            payload = _get_client().series_search(
                search_text,
                limit=limit,
                order_by=order_by,
                sort_order=sort_order,
            )
        except Exception:
            logger.exception("search_series failed text=%r", search_text)
            raise
        return {
            "source": _src("/series/search"),
            "fetched_at": fetched_at,
            **payload,
        }

    @mcp.tool()
    def get_series_info(series_id: str) -> dict[str, Any]:
        """Series metadata (units, frequency, last-updated, seasonal adj)."""
        logger.info("get_series_info series_id=%s", series_id)
        fetched_at = _now_iso()
        try:
            payload = _get_client().series(series_id)
        except Exception:
            logger.exception("get_series_info failed series_id=%s", series_id)
            raise
        return {
            "source": _src("/series"),
            "fetched_at": fetched_at,
            **payload,
        }

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
        fetched_at = _now_iso()
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
        return {
            "source": _src("/series/observations"),
            "fetched_at": fetched_at,
            **result,
        }

    @mcp.tool()
    def analyze_yield_curve(
        observation_start: str | None = None,
        zscore_window: int = 504,
    ) -> dict[str, Any]:
        """Yield-curve regime snapshot from FRED H.15 daily series.

        Pulls ``DGS3MO``, ``DGS2``, ``DGS10``, ``DGS30`` and returns,
        per tenor and per slope (2s10s, 3m10y, 2s30s):

        - ``latest`` value and date,
        - 1m / 3m / 6m / 1y absolute change,
        - rolling z-score and percentile vs the trailing
          ``zscore_window`` observations (default 504 ≈ 2y of daily),
        - for slopes: an ``inverted`` boolean (current value < 0).

        ``derived.curve_shape`` labels the current setup as ``normal``
        / ``flat`` / ``partially_inverted`` / ``inverted``. All
        interpretive labels are nested under ``derived`` so callers can
        tell provider-side classifications apart from raw / statistical
        fields.

        Args:
            observation_start: ISO date. Defaults to ~3 years back —
                enough history for a 504-day z-score plus the 1y delta.
            zscore_window: Number of observations in the rolling z-score
                baseline. Daily series: 504 = ~2y, 252 = ~1y.
        """
        if observation_start is None:
            observation_start = _default_macro_start()
        logger.info(
            "analyze_yield_curve observation_start=%s zscore_window=%d",
            observation_start, zscore_window,
        )
        fetched_at = _now_iso()
        try:
            payload = _yield_curve_payload(
                _get_client(), observation_start, zscore_window,
            )
        except Exception:
            logger.exception("analyze_yield_curve failed")
            raise
        return {
            "source": _src("/series/observations"),
            "fetched_at": fetched_at,
            **payload,
        }

    @mcp.tool()
    def analyze_credit_spreads(
        observation_start: str | None = None,
        zscore_window: int = 504,
    ) -> dict[str, Any]:
        """US corporate credit spreads (HY + IG) with regime label.

        Pulls ICE BofA option-adjusted spread indices — ``BAMLH0A0HYM2``
        (US High Yield) and ``BAMLC0A0CM`` (US Corporate / IG) — and
        returns per-series latest, 1m/3m/6m/1y deltas, and z-score /
        percentile vs the trailing ``zscore_window`` observations.

        ``derived.regime`` is derived from the worse of the two
        z-scores (z < -1 = ``tight``, -1..1 = ``normal``, 1..2 =
        ``wide``, >= 2 = ``stressed``) — we'd rather over-flag credit
        stress than under-flag it. Nested under ``derived`` so callers
        can tell the interpretive label apart from the raw / statistical
        fields.

        Args:
            observation_start: ISO date. Defaults to ~3 years back.
            zscore_window: Observations in the z-score baseline.
        """
        if observation_start is None:
            observation_start = _default_macro_start()
        logger.info(
            "analyze_credit_spreads observation_start=%s zscore_window=%d",
            observation_start, zscore_window,
        )
        fetched_at = _now_iso()
        try:
            payload = _credit_spreads_payload(
                _get_client(), observation_start, zscore_window,
            )
        except Exception:
            logger.exception("analyze_credit_spreads failed")
            raise
        return {
            "source": _src("/series/observations"),
            "fetched_at": fetched_at,
            **payload,
        }

    @mcp.tool()
    def analyze_credit_quality_curve(
        observation_start: str | None = None,
        zscore_window: int = 504,
        segment: str = "both",
    ) -> dict[str, Any]:
        """US corporate OAS by credit rating — IG, HY, or both.

        Pulls ICE BofA option-adjusted spread sub-indices keyed by
        rating: ``AAA / AA / A / BBB`` for investment grade
        (``BAMLC0A1CAAA``…``BAMLC0A4CBBB``) and ``BB / B / CCC`` for
        high yield (``BAMLH0A1HYBB`` / ``BAMLH0A2HYB`` /
        ``BAMLH0A3HYC``). Per rating: latest value + date,
        1m / 3m / 6m / 1y deltas, and rolling z-score / percentile
        vs the trailing ``zscore_window`` observations.

        The point of this tool over :func:`analyze_credit_spreads` is
        to show whether stress (or compression) is broad-based or
        concentrated at the low end of the quality stack — the
        headline IG / HY aggregates can hide that distinction. CCC
        blowing out while BB stays calm is a classic late-cycle
        quality-flight signal that the headline HY OAS softens.

        Per segment ``derived`` carries:

        - ``regime``: ``compressed`` (all z<-0.5; reach-for-yield),
          ``broad_widening`` (all z>1 with tight dispersion;
          uniform stress), ``low_end_stress`` (dispersion ≥1.0 with
          the worst-rated bucket on top), ``mixed`` otherwise.
        - ``zscore_dispersion``: range across rating-bucket z-scores
          in z-units. High = stress concentrated; low = uniform.
        - ``low_end_premium_pp``: lowest-quality OAS minus
          highest-quality OAS in percentage points — the absolute
          cost of credit risk at the bottom of the segment.

        Interpretive labels are nested under ``derived`` so callers
        can tell them apart from raw / statistical fields.

        Args:
            observation_start: ISO date. Defaults to ~3 years back.
            zscore_window: Observations in the z-score baseline.
            segment: ``ig`` / ``hy`` / ``both``.
        """
        if segment not in ("ig", "hy", "both"):
            raise ValueError(
                f"unknown segment={segment!r}; valid: 'ig', 'hy', 'both'"
            )
        if observation_start is None:
            observation_start = _default_macro_start()
        logger.info(
            "analyze_credit_quality_curve segment=%s observation_start=%s zscore_window=%d",
            segment, observation_start, zscore_window,
        )
        fetched_at = _now_iso()
        try:
            payload = _credit_quality_curve_payload(
                _get_client(), observation_start, zscore_window, segment,
            )
        except Exception:
            logger.exception("analyze_credit_quality_curve failed")
            raise
        return {
            "source": _src("/series/observations"),
            "fetched_at": fetched_at,
            **payload,
        }

    @mcp.tool()
    def analyze_credit_term_structure(
        observation_start: str | None = None,
        zscore_window: int = 504,
    ) -> dict[str, Any]:
        """IG corporate OAS by maturity bucket (1-3y … 15+y).

        Pulls ICE BofA option-adjusted spread term-bucket sub-indices:
        ``BAMLC1A0C13Y`` (1-3y), ``BAMLC2A0C35Y`` (3-5y),
        ``BAMLC3A0C57Y`` (5-7y), ``BAMLC4A0C710Y`` (7-10y),
        ``BAMLC7A0C1015Y`` (10-15y), ``BAMLC8A0C15PY`` (15+y). Per
        bucket: latest value + date, 1m / 3m / 6m / 1y deltas, and
        rolling z-score / percentile vs ``zscore_window`` observations.

        For IG the OAS term structure is almost always upward-sloping —
        longer-duration bonds carry more credit risk. An ``inverted``
        slope (front-end OAS > long-end OAS) is rare and signal-rich,
        flagging acute short-end stress (think 2008-Q4, March 2020).

        ``derived`` carries two slope reads:

        - ``front_to_belly``: slope from 1-3y to 7-10y in pp + label.
        - ``full_curve``: slope from 1-3y to 15+y in pp + label.

        Slope labels: ``inverted`` (long < short), ``flat`` (slope
        < 0.25 pp), ``normal`` otherwise. Interpretive labels are
        nested under ``derived``.

        **HY term-bucket OAS** is not published by ICE BofA on FRED.
        For HY by maturity you'd need a paid index-holdings feed
        (S&P Global / IHS Markit) — out of scope here.

        Args:
            observation_start: ISO date. Defaults to ~3 years back.
            zscore_window: Observations in the z-score baseline.
        """
        if observation_start is None:
            observation_start = _default_macro_start()
        logger.info(
            "analyze_credit_term_structure observation_start=%s zscore_window=%d",
            observation_start, zscore_window,
        )
        fetched_at = _now_iso()
        try:
            payload = _credit_term_structure_payload(
                _get_client(), observation_start, zscore_window,
            )
        except Exception:
            logger.exception("analyze_credit_term_structure failed")
            raise
        return {
            "source": _src("/series/observations"),
            "fetched_at": fetched_at,
            **payload,
        }

    @mcp.tool()
    def analyze_breakevens(
        observation_start: str | None = None,
        zscore_window: int = 504,
        target: float = 2.0,
        target_band: float = 0.25,
    ) -> dict[str, Any]:
        """Market-implied inflation expectations vs the Fed's 2% target.

        Pulls ``T5YIE`` (5y breakeven), ``T10YIE`` (10y), and
        ``T5YIFR`` (5y5y forward) and returns, per tenor: latest,
        1m/3m/6m/1y deltas, z-score vs ``zscore_window``, plus a
        per-tenor ``derived`` block with an ``alignment`` label
        (``below_target`` / ``near_target`` / ``above_target``) and
        ``deviation_from_target`` in percentage points. Interpretive
        labels are nested under ``derived`` so callers can tell them
        apart from the raw / statistical fields.

        Note: the Fed's 2% target is for PCE inflation, not breakevens.
        Breakevens include an inflation risk premium and typically run
        20-50bp above expected inflation. ``target_band`` widens the
        ``near_target`` zone to absorb that premium.

        Args:
            observation_start: ISO date. Defaults to ~3 years back.
            zscore_window: Observations in the z-score baseline.
            target: Center of the "near target" band, in percent.
            target_band: Half-width of the band around ``target``.
        """
        if observation_start is None:
            observation_start = _default_macro_start()
        logger.info(
            "analyze_breakevens observation_start=%s target=%.2f±%.2f zscore_window=%d",
            observation_start, target, target_band, zscore_window,
        )
        fetched_at = _now_iso()
        try:
            payload = _breakevens_payload(
                _get_client(), observation_start, zscore_window,
                target, target_band,
            )
        except Exception:
            logger.exception("analyze_breakevens failed")
            raise
        return {
            "source": _src("/series/observations"),
            "fetched_at": fetched_at,
            **payload,
        }

    @mcp.tool()
    def analyze_financial_conditions(
        observation_start: str | None = None,
        zscore_window: int = 504,
    ) -> dict[str, Any]:
        """Chicago Fed financial-conditions indices: NFCI and ANFCI.

        Pulls both the National Financial Conditions Index (``NFCI``)
        and the Adjusted NFCI (``ANFCI``) and returns per-series
        summary (latest, 1m/3m/6m/1y deltas, z-score vs
        ``zscore_window``) plus a per-series ``derived.regime`` label
        (``loose`` / ``normal`` / ``tight`` / ``stressed``).
        Interpretive labels are nested under ``derived`` so callers
        can tell them apart from the raw / statistical fields.

        **What each tells you:**

        - ``NFCI`` is the raw read: positive = financial conditions
          tighter than the 1971-present average; negative = looser.
          Moves with the cycle.
        - ``ANFCI`` removes the cyclical component (regresses out
          credit, inflation, activity), so ~0 means conditions are in
          line with what macro would normally produce. Positive ANFCI
          flags financial stress *beyond* what the cycle justifies —
          a cleaner read on "are markets stressed independent of where
          we are in the cycle?"

        Read both: a positive NFCI with a near-zero ANFCI is
        cycle-explained tightening; a positive ANFCI regardless of
        NFCI is the interesting signal.

        Both series are weekly, released Wednesdays.

        Args:
            observation_start: ISO date. Defaults to ~3 years back.
            zscore_window: Observations in the z-score baseline (504
                weekly observations ≈ 10y).
        """
        if observation_start is None:
            observation_start = _default_macro_start()
        logger.info(
            "analyze_financial_conditions observation_start=%s zscore_window=%d",
            observation_start, zscore_window,
        )
        fetched_at = _now_iso()
        try:
            payload = _financial_conditions_payload(
                _get_client(), observation_start, zscore_window,
            )
        except Exception:
            logger.exception("analyze_financial_conditions failed")
            raise
        return {
            "source": _src("/series/observations"),
            "fetched_at": fetched_at,
            **payload,
        }

    @mcp.tool()
    def analyze_macro_regime(
        observation_start: str | None = None,
        zscore_window: int = 504,
        breakeven_target: float = 2.0,
        breakeven_band: float = 0.25,
    ) -> dict[str, Any]:
        """One-call synthesis of curve / credit / inflation / financial
        conditions.

        Internally runs :func:`analyze_yield_curve`,
        :func:`analyze_credit_spreads`, :func:`analyze_breakevens`, and
        :func:`analyze_financial_conditions` (NFCI + ANFCI), then rolls
        the components into a single ``derived.regime`` label
        (``risk_on`` / ``neutral`` / ``risk_off`` / ``stressed``).
        Interpretive labels are nested under ``derived`` (top-level and
        per-component) so callers can tell them apart from the raw /
        statistical fields.

        The aggregate uses **NFCI** (absolute financial tightness), not
        ANFCI (cycle-adjusted), because NFCI's sign carries the "are
        we in stress mode right now" read that a risk-on/off tag should
        reflect. ANFCI is surfaced as a secondary component so you can
        see whether observed tightness is cycle-explained or not.

        A ``stressed`` reading in credit or NFCI forces the aggregate
        to ``stressed``. Otherwise components accrue ±1/±2 to a score
        that buckets into the other labels. This is deliberately
        coarse — read the per-component labels
        (``curve.derived.curve_shape``, ``credit.derived.regime``,
        ``breakevens.series[*].derived.alignment``,
        ``financial_conditions.series.{nfci,anfci}.derived.regime``)
        for the real signal.

        Args:
            observation_start: ISO date. Defaults to ~3 years back.
            zscore_window: Observations in the per-series z-score
                baseline. Daily: 504 ≈ 2y; applied to NFCI/ANFCI too
                even though they're weekly (504 weeks ≈ 10y), which is
                consistent with how those indices are themselves
                normalised.
            breakeven_target, breakeven_band: Forwarded to the
                breakevens component.
        """
        if observation_start is None:
            observation_start = _default_macro_start()
        logger.info(
            "analyze_macro_regime observation_start=%s zscore_window=%d",
            observation_start, zscore_window,
        )
        client = _get_client()
        try:
            curve = _yield_curve_payload(client, observation_start, zscore_window)
            credit = _credit_spreads_payload(client, observation_start, zscore_window)
            breakevens = _breakevens_payload(
                client, observation_start, zscore_window,
                breakeven_target, breakeven_band,
            )
            fin_cond = _financial_conditions_payload(client, observation_start, zscore_window)
        except Exception:
            logger.exception("analyze_macro_regime failed")
            raise

        nfci_label = (
            (fin_cond["series"]["nfci"].get("derived") or {}).get("regime") or "unknown"
        )
        anfci_label = (
            (fin_cond["series"]["anfci"].get("derived") or {}).get("regime") or "unknown"
        )
        component_labels = {
            "curve": curve["derived"]["curve_shape"],
            "credit": credit["derived"]["regime"],
            "breakevens_10y": (
                ((breakevens["series"].get("10y") or {}).get("derived") or {}).get(
                    "alignment"
                )
                or "unknown"
            ),
            "nfci": nfci_label,
            "anfci": anfci_label,
        }
        regime = analytics.aggregate_regime(
            curve=component_labels["curve"],
            credit=component_labels["credit"],
            breakevens=component_labels["breakevens_10y"],
            nfci=component_labels["nfci"],
        )
        component_as_ofs = [
            d for d in (
                curve.get("as_of"),
                credit.get("as_of"),
                breakevens.get("as_of"),
                fin_cond.get("as_of"),
            ) if d
        ]
        as_of = max(component_as_ofs) if component_as_ofs else None
        return {
            "source": _src("/series/observations"),
            "fetched_at": _now_iso(),
            "as_of": as_of,
            "curve": curve,
            "credit": credit,
            "breakevens": breakevens,
            "financial_conditions": fin_cond,
            "derived": {
                "regime": regime,
                "component_labels": component_labels,
            },
            "note": (
                "Aggregate label uses NFCI (absolute tightness) not "
                "ANFCI (cycle-adjusted) — ANFCI is surfaced as a "
                "secondary signal. Stressed in credit or NFCI forces "
                "the aggregate to stressed."
            ),
        }
