"""Yahoo Finance client that emits Schwab-shaped payloads.

The tool surface mirrors :mod:`schwab_connector.schwab_client` so prompts
and analytics code are portable between backends. Where Yahoo's data
model doesn't cover a Schwab capability (brokerage accounts,
authoritative market hours) we raise :class:`YahooCapabilityError` —
no silent fallbacks, per the hub AGENTS.md.

Backed by `yfinance <https://pypi.org/project/yfinance/>`_. No API key
required, but Yahoo enforces unpublished rate limits and the library
is maintained on a best-effort basis against an unofficial endpoint.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import yfinance as yf

logger = logging.getLogger("yahoo_connector.yahoo")


# Same friendly aliases the Schwab connector accepts, mapped to the
# keys this module's ``_quote_payload`` emits. Unknown keys pass through
# so callers can also read native yfinance fields directly.
_FIELD_ALIASES = {
    "LAST": "lastPrice",
    "BID": "bidPrice",
    "ASK": "askPrice",
    "VOLUME": "totalVolume",
    "MARK": "mark",
    "OPEN": "openPrice",
    "HIGH": "highPrice",
    "LOW": "lowPrice",
    "CLOSE": "closePrice",
    "NET_CHANGE": "netChange",
    "PERCENT_CHANGE": "netPercentChange",
    "BID_SIZE": "bidSize",
    "ASK_SIZE": "askSize",
}

# Schwab's $-prefixed index tickers map to Yahoo's ^-prefixed symbols.
_INDEX_SYMBOL_ALIASES = {
    "$SPX": "^GSPC",
    "$DJI": "^DJI",
    "$COMPX": "^IXIC",
    "$RUT": "^RUT",
    "$VIX": "^VIX",
}

# Schwab's `get_movers(index=...)` values → Yahoo predefined screener
# keys. Yahoo's screeners are US-market-wide, not per-index, so this is
# a best-effort mapping; docstring on get_movers calls out the gap.
_MOVERS_SORT_DEFAULT = {
    "VOLUME": "most_actives",
    "TRADES": "most_actives",
    "PERCENT_CHANGE_UP": "day_gainers",
    "PERCENT_CHANGE_DOWN": "day_losers",
}


class YahooCapabilityError(RuntimeError):
    """Raised when a caller asks for a Schwab capability Yahoo can't provide.

    Currently: brokerage accounts/positions, authoritative market
    hours. Surface this to the user rather than synthesizing a
    plausible-looking response.
    """


class YahooClient:
    """Yahoo Finance client mirroring :class:`SchwabClient`'s tool surface."""

    def __init__(self) -> None:
        pass

    @classmethod
    def from_env(cls) -> "YahooClient":
        return cls()

    def close(self) -> None:
        # yfinance uses a module-level session; nothing to close here.
        return None

    # ----- quotes ------------------------------------------------------

    def get_quote(self, symbol: str, field: str = "LAST") -> Any:
        logger.info("get_quote symbol=%s field=%s", symbol, field)
        quote = self._quote_payload(symbol)
        return _extract_field(quote, field)

    def get_quotes(
        self,
        symbols: list[str],
        fields: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        logger.info("get_quotes symbols=%s fields=%s", symbols, fields)
        out: dict[str, dict[str, Any]] = {}
        for sym in symbols:
            quote = self._quote_payload(sym)
            if fields is None:
                out[sym] = quote
            else:
                out[sym] = {f: _extract_field(quote, f) for f in fields}
        return out

    # ----- price history ----------------------------------------------

    def get_price_history(
        self,
        symbol: str,
        period_type: str = "year",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
        need_previous_close: bool = False,
    ) -> dict[str, Any]:
        """Return Schwab-shaped OHLCV candles for one symbol.

        ``period_type`` / ``period`` / ``frequency_type`` / ``frequency``
        follow the Schwab grammar; this method translates to yfinance's
        ``period`` / ``interval`` (or ``start`` / ``end`` for explicit
        date ranges). Combinations Yahoo doesn't support (e.g. 10-minute
        bars, 15y/20y of daily) raise ``ValueError`` rather than silently
        subbing in a different resolution.
        """
        logger.info(
            "get_price_history symbol=%s period=%s%s frequency=%s%s",
            symbol, period, period_type, frequency, frequency_type,
        )
        interval = _map_frequency(frequency_type, frequency)
        hist_kwargs: dict[str, Any] = {
            "interval": interval,
            "prepost": need_extended_hours_data,
            "auto_adjust": False,
            "actions": False,
        }
        if start_date is not None or end_date is not None:
            if start_date is not None:
                hist_kwargs["start"] = datetime.fromtimestamp(
                    start_date / 1000.0, tz=timezone.utc
                )
            if end_date is not None:
                hist_kwargs["end"] = datetime.fromtimestamp(
                    end_date / 1000.0, tz=timezone.utc
                )
        else:
            start, end = _period_to_date_range(period_type, period)
            hist_kwargs["start"] = start
            if end is not None:
                hist_kwargs["end"] = end

        ticker = yf.Ticker(_yahoo_symbol(symbol))
        df = ticker.history(**hist_kwargs)

        candles: list[dict[str, Any]] = []
        for ts, row in df.iterrows():
            # pandas timestamps carry tz if yfinance set one; convert to
            # UTC epoch ms for parity with Schwab's candle schema.
            epoch_ms = int(ts.tz_convert("UTC").timestamp() * 1000) if ts.tzinfo else int(
                ts.replace(tzinfo=timezone.utc).timestamp() * 1000
            )
            o, h, l, c, v = (
                _safe_float(row.get("Open")),
                _safe_float(row.get("High")),
                _safe_float(row.get("Low")),
                _safe_float(row.get("Close")),
                _safe_int(row.get("Volume")),
            )
            if None in (o, h, l, c):
                # Skip rows yfinance flags as NaN (holidays inside a
                # date range, halted sessions).
                continue
            candles.append(
                {"open": o, "high": h, "low": l, "close": c,
                 "volume": v or 0, "datetime": epoch_ms}
            )

        result: dict[str, Any] = {
            "symbol": symbol,
            "empty": len(candles) == 0,
            "candles": candles,
        }
        if need_previous_close and candles:
            result["previousClose"] = candles[0]["close"]
        return result

    # ----- movers / search / hours ------------------------------------

    def get_movers(
        self,
        index: str,
        sort: str | None = None,
        frequency: int | None = None,
    ) -> dict[str, Any]:
        """Top movers via Yahoo predefined screeners.

        Yahoo screeners aren't scoped to a single index the way Schwab's
        are; ``index`` is accepted for signature parity but only the
        ``sort`` flag actually selects which screener runs. Pass a raw
        Yahoo screener key (``day_gainers``, ``day_losers``,
        ``most_actives``, …) as ``index`` to bypass the mapping.
        """
        screener = _pick_screener(index, sort)
        logger.info(
            "get_movers index=%s sort=%s screener=%s", index, sort, screener,
        )
        try:
            body = yf.screen(screener)
        except Exception:
            logger.exception("yahoo screener failed screener=%s", screener)
            raise
        quotes = body.get("quotes", []) if isinstance(body, dict) else []
        return {
            "screener": screener,
            "screenerRequested": {"index": index, "sort": sort},
            "screeners": quotes,
        }

    def search_instruments(
        self,
        symbol: str,
        projection: str = "symbol-search",
    ) -> dict[str, Any]:
        """Instrument lookup or fundamentals.

        ``projection="fundamental"`` hydrates the full ``Ticker.info``
        block (trailing/forward PE, EPS, dividend yield, 52-week range,
        market cap, …). Other projections delegate to ``yf.Search`` —
        Yahoo's symbol search is fuzzy by nature, so ``symbol-search``
        and ``desc-search`` return the same list.
        """
        logger.info("search_instruments symbol=%s projection=%s", symbol, projection)
        if projection == "fundamental":
            info = yf.Ticker(_yahoo_symbol(symbol)).info or {}
            return {"instruments": [_fundamental_payload(symbol, info)]}

        search = yf.Search(symbol)
        quotes = getattr(search, "quotes", []) or []
        return {"instruments": quotes}

    def get_market_hours(
        self,
        markets: list[str] | str,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Not available from Yahoo.

        Yahoo Finance does not publish an authoritative market-hours
        endpoint (exchange, session, holiday-aware). Rather than
        returning a hand-rolled schedule that could disagree with the
        actual session, this raises. Switch to the Schwab backend when
        authoritative hours matter.
        """
        raise YahooCapabilityError(
            "get_market_hours is not supported by the Yahoo backend. "
            "Use TRAIDER_BACKEND=schwab for authoritative session hours."
        )

    # ----- accounts ---------------------------------------------------

    def get_accounts(self, include_positions: bool = False) -> list[dict[str, Any]]:
        """Not available from Yahoo.

        Yahoo is a market-data source, not a brokerage — there are no
        accounts, positions, or cost basis here. Portfolio-aware
        prompts need the Schwab backend (or a future broker connector).
        """
        raise YahooCapabilityError(
            "get_accounts is not supported by the Yahoo backend. "
            "Yahoo Finance has no brokerage surface. Use "
            "TRAIDER_BACKEND=schwab (or another broker connector) for "
            "account data."
        )

    # ----- internals --------------------------------------------------

    def _quote_payload(self, symbol: str) -> dict[str, Any]:
        ticker = yf.Ticker(_yahoo_symbol(symbol))
        fast = getattr(ticker, "fast_info", {}) or {}
        info = {}
        # info is the expensive call — only touch it for bid/ask/sizes,
        # which fast_info doesn't carry.
        try:
            info = ticker.info or {}
        except Exception:
            logger.exception("yfinance .info failed symbol=%s", symbol)

        last = _safe_float(fast.get("last_price"))
        prev = _safe_float(fast.get("previous_close") or info.get("previousClose"))
        bid = _safe_float(info.get("bid"))
        ask = _safe_float(info.get("ask"))

        payload: dict[str, Any] = {
            "symbol": symbol,
            "lastPrice": last,
            "bidPrice": bid,
            "askPrice": ask,
            "bidSize": _safe_int(info.get("bidSize")),
            "askSize": _safe_int(info.get("askSize")),
            "openPrice": _safe_float(fast.get("open") or info.get("regularMarketOpen")),
            "highPrice": _safe_float(fast.get("day_high") or info.get("regularMarketDayHigh")),
            "lowPrice": _safe_float(fast.get("day_low") or info.get("regularMarketDayLow")),
            "closePrice": prev,
            "totalVolume": _safe_int(fast.get("last_volume") or info.get("regularMarketVolume")),
            "mark": (bid + ask) / 2.0 if bid and ask else last,
            "marketState": info.get("marketState"),
            "exchange": fast.get("exchange") or info.get("exchange"),
            "currency": fast.get("currency") or info.get("currency"),
        }
        if last is not None and prev:
            payload["netChange"] = last - prev
            payload["netPercentChange"] = (last - prev) / prev * 100.0
        else:
            payload["netChange"] = None
            payload["netPercentChange"] = None
        return payload


# ---- helpers --------------------------------------------------------


def _yahoo_symbol(symbol: str) -> str:
    """Map Schwab-style symbols to Yahoo's conventions where they differ."""
    return _INDEX_SYMBOL_ALIASES.get(symbol.upper(), symbol)


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _safe_int(v: Any) -> int | None:
    f = _safe_float(v)
    return int(f) if f is not None else None


def _extract_field(quote: dict[str, Any], field: str) -> Any:
    if field in quote:
        return quote[field]
    alias = _FIELD_ALIASES.get(field.upper())
    if alias is not None and alias in quote:
        return quote[alias]
    return None


def _map_frequency(frequency_type: str, frequency: int) -> str:
    ft = frequency_type.lower()
    if ft == "minute":
        mapping = {1: "1m", 5: "5m", 15: "15m", 30: "30m"}
        if frequency not in mapping:
            raise ValueError(
                f"Yahoo backend does not support {frequency}-minute bars. "
                f"Valid: 1, 5, 15, 30. (Schwab-only: 10.)"
            )
        return mapping[frequency]
    if ft == "daily":
        return "1d"
    if ft == "weekly":
        return "1wk"
    if ft == "monthly":
        return "1mo"
    raise ValueError(f"unknown frequency_type: {frequency_type!r}")


def _period_to_date_range(
    period_type: str, period: int,
) -> tuple[datetime, datetime | None]:
    """Translate Schwab's period grammar to a concrete UTC start date.

    Returning an explicit start (rather than a yfinance ``period``
    string) lets us support Schwab's 15/20-year windows, which
    yfinance's period vocabulary caps at 10y / max.
    """
    now = datetime.now(timezone.utc)
    pt = period_type.lower()
    if pt == "day":
        return now - timedelta(days=period), None
    if pt == "month":
        return now - timedelta(days=period * 31), None
    if pt == "year":
        return now - timedelta(days=period * 366), None
    if pt == "ytd":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc), None
    raise ValueError(f"unknown period_type: {period_type!r}")


def _pick_screener(index: str, sort: str | None) -> str:
    """Yahoo screener resolver.

    If ``index`` is already a Yahoo predefined screener key, use it.
    Otherwise default to ``most_actives`` and let ``sort`` refine.
    """
    raw = (index or "").strip()
    # Pass-through for raw Yahoo keys.
    if raw and "_" in raw and not raw.startswith("$"):
        return raw
    if sort:
        mapped = _MOVERS_SORT_DEFAULT.get(sort.upper())
        if mapped:
            return mapped
    return "most_actives"


def _fundamental_payload(symbol: str, info: dict[str, Any]) -> dict[str, Any]:
    """Shape yfinance's giant ``info`` dict into a fundamentals block
    roughly parallel to Schwab's ``projection=fundamental`` response."""
    return {
        "symbol": symbol,
        "exchange": info.get("exchange"),
        "description": info.get("longName") or info.get("shortName"),
        "assetType": info.get("quoteType"),
        "fundamental": {
            "peRatio": info.get("trailingPE"),
            "forwardPE": info.get("forwardPE"),
            "eps": info.get("trailingEps"),
            "forwardEps": info.get("forwardEps"),
            "dividendYield": info.get("dividendYield"),
            "dividendAmount": info.get("dividendRate"),
            "marketCap": info.get("marketCap"),
            "beta": info.get("beta"),
            "sharesOutstanding": info.get("sharesOutstanding"),
            "fiftyTwoWeekHigh": info.get("fiftyTwoWeekHigh"),
            "fiftyTwoWeekLow": info.get("fiftyTwoWeekLow"),
            "bookValue": info.get("bookValue"),
            "priceToBook": info.get("priceToBook"),
            "profitMargin": info.get("profitMargins"),
            "returnOnEquity": info.get("returnOnEquity"),
        },
    }
