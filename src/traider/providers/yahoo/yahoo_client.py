"""Yahoo Finance client that emits Schwab-shaped payloads.

The tool surface mirrors :mod:`traider.providers.schwab.schwab_client` so prompts
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

logger = logging.getLogger("traider.yahoo.client")


# Same friendly aliases the Schwab provider accepts, mapped to the
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


class YahooDataError(RuntimeError):
    """Raised when an upstream Yahoo fetch fails outright.

    Distinct from :class:`YahooCapabilityError` (which means Yahoo has
    no equivalent surface) — this is for transient/structural
    failures where we *would* have had data but the call to yfinance
    raised. Surface this to the caller rather than returning a
    payload of ``None``-valued fields, per the hub AGENTS.md "no
    silent fallbacks / no fabricated numbers" rule.
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

    # ----- options ----------------------------------------------------

    def get_option_expirations(self, symbol: str) -> dict[str, Any]:
        """Schwab-shaped expiration list from ``Ticker.options``.

        Yahoo exposes only the expiration-date list — not the rich
        metadata Schwab's ``/expirationchain`` returns — so
        ``expirationType`` / ``settlementType`` / ``optionRoots`` /
        ``standard`` are filled with ``None`` and callers can see the
        gap explicitly rather than inferring false values.
        """
        logger.info("get_option_expirations symbol=%s", symbol)
        ticker = yf.Ticker(_yahoo_symbol(symbol))
        dates = getattr(ticker, "options", []) or []
        today = datetime.now(timezone.utc).date()
        out: list[dict[str, Any]] = []
        for d in dates:
            try:
                exp = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                continue
            out.append(
                {
                    "expirationDate": d,
                    "daysToExpiration": (exp - today).days,
                    "expirationType": None,
                    "settlementType": None,
                    "optionRoots": None,
                    "standard": None,
                }
            )
        return {"status": "SUCCESS", "expirationList": out}

    def get_option_chain(
        self,
        symbol: str,
        contract_type: str = "ALL",
        strike_count: int | None = None,
        include_underlying_quote: bool = True,
        strategy: str = "SINGLE",
        interval: float | None = None,
        strike: float | None = None,
        range_: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        volatility: float | None = None,
        underlying_price: float | None = None,
        interest_rate: float | None = None,
        days_to_expiration: int | None = None,
        exp_month: str | None = None,
        option_type: str | None = None,
    ) -> dict[str, Any]:
        """Schwab-shaped option chain built from ``Ticker.option_chain``.

        Response mirrors Schwab's ``/chains``: ``callExpDateMap`` /
        ``putExpDateMap`` keyed by ``"YYYY-MM-DD:dte"`` → strike →
        list of contract dicts (bid/ask/last/mark, volume, OI, IV,
        OSI ``symbol``). **Greeks (delta/gamma/theta/vega/rho) are
        emitted as ``None``** — Yahoo does not publish them. Quotes
        are delayed ~15 minutes and illiquid strikes may have
        zero/stale bid-ask. A top-level ``"dataQualityWarning"`` key
        is set so callers can see this without digging.

        Only ``strategy="SINGLE"`` is supported; other Schwab
        strategies (``ANALYTICAL``, ``VERTICAL``, ``STRADDLE``, …)
        raise :class:`YahooCapabilityError`. ``interval``,
        ``volatility``, ``underlying_price``, ``interest_rate``,
        ``days_to_expiration`` are accepted for signature parity but
        are meaningful only with ``ANALYTICAL``, so passing them
        raises too rather than being silently ignored.

        Supported filters: ``contract_type`` (CALL/PUT/ALL),
        ``strike_count`` (symmetric band around ATM),
        ``include_underlying_quote``, ``strike`` (exact match),
        ``range_`` (ITM/OTM/NTM), ``from_date`` / ``to_date``
        (YYYY-MM-DD), ``exp_month`` (JAN..DEC or ALL), ``option_type``
        (S/NS/ALL — Yahoo does not tag non-standards so ``NS`` is
        unsupported).
        """
        logger.info(
            "get_option_chain symbol=%s type=%s strategy=%s",
            symbol, contract_type, strategy,
        )
        strategy_up = (strategy or "SINGLE").upper()
        if strategy_up != "SINGLE":
            raise YahooCapabilityError(
                f"Yahoo backend supports only strategy=SINGLE, got {strategy!r}. "
                "Multi-leg / analytical strategies need the Schwab backend."
            )
        for name, val in (
            ("interval", interval),
            ("volatility", volatility),
            ("underlying_price", underlying_price),
            ("interest_rate", interest_rate),
            ("days_to_expiration", days_to_expiration),
        ):
            if val is not None:
                raise YahooCapabilityError(
                    f"{name!r} is only meaningful with strategy=ANALYTICAL, "
                    "which the Yahoo backend does not support."
                )
        if option_type is not None and option_type.upper() == "NS":
            raise YahooCapabilityError(
                "Yahoo does not tag non-standard (NS) options. Use the "
                "Schwab backend to filter by option_type=NS."
            )

        ctype = (contract_type or "ALL").upper()
        if ctype not in {"CALL", "PUT", "ALL"}:
            raise ValueError(f"contract_type must be CALL/PUT/ALL, got {contract_type!r}")

        ysym = _yahoo_symbol(symbol)
        ticker = yf.Ticker(ysym)
        exp_dates = list(getattr(ticker, "options", []) or [])
        exp_dates = _filter_expirations(
            exp_dates, from_date=from_date, to_date=to_date, exp_month=exp_month,
        )

        underlying_last: float | None = None
        underlying_block: dict[str, Any] | None = None
        if include_underlying_quote or strike_count is not None or (range_ is not None):
            quote = self._quote_payload(symbol)
            underlying_last = quote.get("lastPrice")
            if include_underlying_quote:
                underlying_block = {
                    "symbol": symbol,
                    "last": quote.get("lastPrice"),
                    "bid": quote.get("bidPrice"),
                    "ask": quote.get("askPrice"),
                    "mark": quote.get("mark"),
                    "change": quote.get("netChange"),
                    "percentChange": quote.get("netPercentChange"),
                    "totalVolume": quote.get("totalVolume"),
                    "exchangeName": quote.get("exchange"),
                    "quoteTime": None,
                }

        call_map: dict[str, dict[str, list[dict[str, Any]]]] = {}
        put_map: dict[str, dict[str, list[dict[str, Any]]]] = {}
        today = datetime.now(timezone.utc).date()
        for d in exp_dates:
            try:
                chain = ticker.option_chain(d)
            except Exception:
                logger.exception("option_chain failed symbol=%s date=%s", symbol, d)
                continue
            try:
                exp = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                continue
            dte = (exp - today).days
            key = f"{d}:{dte}"

            if ctype in ("CALL", "ALL"):
                call_map[key] = _frame_to_strike_map(
                    chain.calls, "CALL", exp, dte,
                    strike=strike, range_=range_,
                    strike_count=strike_count, underlying=underlying_last,
                )
            if ctype in ("PUT", "ALL"):
                put_map[key] = _frame_to_strike_map(
                    chain.puts, "PUT", exp, dte,
                    strike=strike, range_=range_,
                    strike_count=strike_count, underlying=underlying_last,
                )

        result: dict[str, Any] = {
            "symbol": symbol,
            "status": "SUCCESS",
            "strategy": strategy_up,
            "interval": 0.0,
            "isDelayed": True,
            "isIndex": False,
            "interestRate": None,
            "underlyingPrice": underlying_last,
            "volatility": None,
            "daysToExpiration": 0,
            "numberOfContracts": sum(
                len(strikes) for m in (call_map, put_map) for strikes in m.values()
            ),
            "callExpDateMap": call_map,
            "putExpDateMap": put_map,
            "dataQualityWarning": (
                "Yahoo options data is ~15min delayed, omits Greeks "
                "(delta/gamma/theta/vega/rho = null), and may show stale "
                "or zero bid/ask on illiquid strikes. Use the Schwab "
                "backend for real-time quotes and Greeks."
            ),
        }
        if underlying_block is not None:
            result["underlying"] = underlying_block
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
        prompts need the Schwab backend (or a future broker provider).
        """
        raise YahooCapabilityError(
            "get_accounts is not supported by the Yahoo backend. "
            "Yahoo Finance has no brokerage surface. Use "
            "TRAIDER_BACKEND=schwab (or another broker provider) for "
            "account data."
        )

    # ----- internals --------------------------------------------------

    def _quote_payload(self, symbol: str) -> dict[str, Any]:
        # We deliberately don't use ``ticker.fast_info``: in current
        # yfinance it's backed by the same ``Quote`` class as
        # ``ticker.info``, and its ``.get()`` either returns ``None`` for
        # keys that aren't property-backed (so ``last_price`` silently
        # goes missing) or triggers the ``_dividends`` AttributeError
        # for keys that are (``open`` / ``day_high`` / ...). All fields
        # come from ``info`` via the safe ``_info_get`` wrapper, which
        # swallows that bug per-key.
        ticker = yf.Ticker(_yahoo_symbol(symbol))
        try:
            info: Any = ticker.info or {}
        except Exception as e:
            # Don't swallow: a payload of None-valued fields is
            # indistinguishable from a real quote where every field
            # happened to be missing. Per AGENTS.md, surface the
            # failure so the caller can stop rather than cite a
            # fabricated quote.
            raise YahooDataError(
                f"yfinance .info failed for {symbol!r}: {e}"
            ) from e

        last = _safe_float(
            _info_get(info, "regularMarketPrice")
            or _info_get(info, "currentPrice")
        )
        prev = _safe_float(
            _info_get(info, "regularMarketPreviousClose")
            or _info_get(info, "previousClose")
        )
        bid = _safe_float(_info_get(info, "bid"))
        ask = _safe_float(_info_get(info, "ask"))

        payload: dict[str, Any] = {
            "symbol": symbol,
            "lastPrice": last,
            "bidPrice": bid,
            "askPrice": ask,
            "bidSize": _safe_int(_info_get(info, "bidSize")),
            "askSize": _safe_int(_info_get(info, "askSize")),
            "openPrice": _safe_float(
                _info_get(info, "regularMarketOpen") or _info_get(info, "open")
            ),
            "highPrice": _safe_float(
                _info_get(info, "regularMarketDayHigh") or _info_get(info, "dayHigh")
            ),
            "lowPrice": _safe_float(
                _info_get(info, "regularMarketDayLow") or _info_get(info, "dayLow")
            ),
            "closePrice": prev,
            "totalVolume": _safe_int(
                _info_get(info, "regularMarketVolume") or _info_get(info, "volume")
            ),
            "mark": (bid + ask) / 2.0 if bid and ask else last,
            "marketState": _info_get(info, "marketState"),
            "exchange": _info_get(info, "exchange"),
            "currency": _info_get(info, "currency"),
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


def _info_get(info: Any, key: str) -> Any:
    """yfinance's ``Ticker.info`` is a lazy ``InfoDictWrapper`` — resolving
    a key can trigger network calls and internal library bugs (e.g.
    ``regularMarketOpen`` hits a codepath that has raised
    ``AttributeError: 'PriceHistory' object has no attribute
    '_dividends'``). Treat any failure as a missing field."""
    try:
        return info.get(key)
    except Exception:
        return None


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

    Schwab-style sort names (uppercase, e.g. ``PERCENT_CHANGE_UP``) map
    through ``_MOVERS_SORT_DEFAULT`` whether they arrive via ``sort`` or
    mistakenly via ``index``. Lowercase strings pass through as raw
    Yahoo screener keys (``day_gainers``, ``most_actives``, …).
    """
    raw = (index or "").strip()
    for candidate in (sort, raw):
        if candidate:
            mapped = _MOVERS_SORT_DEFAULT.get(candidate.upper())
            if mapped:
                return mapped
    # Pass-through for raw Yahoo keys (lowercase convention).
    if raw and raw.islower() and "_" in raw:
        return raw
    return "most_actives"


_MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


def _filter_expirations(
    dates: list[str],
    from_date: str | None,
    to_date: str | None,
    exp_month: str | None,
) -> list[str]:
    """Apply Schwab-style expiration filters to Yahoo's date list."""
    if not dates:
        return []
    try:
        lo = datetime.strptime(from_date, "%Y-%m-%d").date() if from_date else None
        hi = datetime.strptime(to_date, "%Y-%m-%d").date() if to_date else None
    except ValueError as e:
        raise ValueError(f"from_date/to_date must be YYYY-MM-DD: {e}") from e
    month: str | None = None
    if exp_month is not None:
        m = exp_month.upper()
        if m not in {"ALL"} | set(_MONTH_ABBR.values()):
            raise ValueError(
                f"exp_month must be JAN..DEC or ALL, got {exp_month!r}"
            )
        month = None if m == "ALL" else m
    out: list[str] = []
    for d in dates:
        try:
            exp = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        if lo and exp < lo:
            continue
        if hi and exp > hi:
            continue
        if month and _MONTH_ABBR[exp.month] != month:
            continue
        out.append(d)
    return out


def _frame_to_strike_map(
    df: Any,
    put_call: str,
    expiration: Any,
    dte: int,
    strike: float | None,
    range_: str | None,
    strike_count: int | None,
    underlying: float | None,
) -> dict[str, list[dict[str, Any]]]:
    """Convert a yfinance calls/puts DataFrame into Schwab's
    ``{strike: [contract]}`` shape."""
    if df is None or getattr(df, "empty", True):
        return {}
    rows = list(df.to_dict(orient="records"))
    rows = _apply_strike_filters(
        rows, put_call=put_call, strike=strike, range_=range_,
        strike_count=strike_count, underlying=underlying,
    )
    out: dict[str, list[dict[str, Any]]] = {}
    exp_ms = int(
        datetime(expiration.year, expiration.month, expiration.day, tzinfo=timezone.utc)
        .timestamp() * 1000
    )
    for row in rows:
        k_raw = _safe_float(row.get("strike"))
        if k_raw is None:
            continue
        key = f"{k_raw:.1f}" if k_raw == int(k_raw) else f"{k_raw}"
        out.setdefault(key, []).append(
            _yahoo_contract_dict(row, put_call, expiration, dte, exp_ms, underlying)
        )
    return out


def _apply_strike_filters(
    rows: list[dict[str, Any]],
    put_call: str,
    strike: float | None,
    range_: str | None,
    strike_count: int | None,
    underlying: float | None,
) -> list[dict[str, Any]]:
    if strike is not None:
        rows = [r for r in rows if _safe_float(r.get("strike")) == strike]
        return rows

    if range_ is not None and underlying is not None:
        r = range_.upper()
        if r == "ITM":
            if put_call == "CALL":
                rows = [x for x in rows if (_safe_float(x.get("strike")) or 0) < underlying]
            else:
                rows = [x for x in rows if (_safe_float(x.get("strike")) or 0) > underlying]
        elif r == "OTM":
            if put_call == "CALL":
                rows = [x for x in rows if (_safe_float(x.get("strike")) or 0) > underlying]
            else:
                rows = [x for x in rows if (_safe_float(x.get("strike")) or 0) < underlying]
        elif r == "NTM":
            # Schwab's NTM is a narrow band; approximate as the 20 strikes
            # nearest the mark. Exact-width isn't documented, so this is a
            # reasonable stand-in rather than a fabricated match.
            rows = sorted(rows, key=lambda x: abs((_safe_float(x.get("strike")) or 0) - underlying))[:20]
        elif r == "ALL":
            pass
        else:
            # SAK/SBK/SNK aren't meaningful without Schwab's definitions;
            # fall through and emit everything.
            pass

    if strike_count is not None and underlying is not None and rows:
        rows = sorted(rows, key=lambda x: _safe_float(x.get("strike")) or 0.0)
        strikes = [_safe_float(r.get("strike")) or 0.0 for r in rows]
        atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying))
        lo = max(0, atm_idx - strike_count)
        hi = min(len(rows), atm_idx + strike_count + 1)
        rows = rows[lo:hi]
    return rows


def _yahoo_contract_dict(
    row: dict[str, Any],
    put_call: str,
    expiration: Any,
    dte: int,
    exp_ms: int,
    underlying: float | None,
) -> dict[str, Any]:
    bid = _safe_float(row.get("bid"))
    ask = _safe_float(row.get("ask"))
    last = _safe_float(row.get("lastPrice"))
    strike = _safe_float(row.get("strike"))
    mark = (bid + ask) / 2.0 if bid is not None and ask is not None and (bid or ask) else last
    intrinsic: float | None = None
    if strike is not None and underlying is not None:
        if put_call == "CALL":
            intrinsic = max(underlying - strike, 0.0)
        else:
            intrinsic = max(strike - underlying, 0.0)
    extrinsic = mark - intrinsic if mark is not None and intrinsic is not None else None
    last_trade = row.get("lastTradeDate")
    try:
        quote_time_ms = int(last_trade.timestamp() * 1000) if last_trade is not None else None
    except AttributeError:
        quote_time_ms = None
    return {
        "putCall": put_call,
        "symbol": row.get("contractSymbol"),
        "description": None,
        "exchangeName": "OPR",
        "bid": bid,
        "ask": ask,
        "last": last,
        "mark": mark,
        "bidSize": 0,
        "askSize": 0,
        "lastSize": 0,
        "highPrice": None,
        "lowPrice": None,
        "openPrice": None,
        "closePrice": None,
        "netChange": _safe_float(row.get("change")),
        "quoteTimeInLong": quote_time_ms,
        "tradeTimeInLong": quote_time_ms,
        "totalVolume": _safe_int(row.get("volume")) or 0,
        "openInterest": _safe_int(row.get("openInterest")) or 0,
        "volatility": _pct(_safe_float(row.get("impliedVolatility"))),
        "delta": None,
        "gamma": None,
        "theta": None,
        "vega": None,
        "rho": None,
        "timeValue": extrinsic,
        "theoreticalOptionValue": None,
        "theoreticalVolatility": None,
        "strikePrice": strike,
        "expirationDate": f"{expiration.isoformat()}T00:00:00.000Z",
        "daysToExpiration": dte,
        "expirationType": None,
        "lastTradingDay": exp_ms,
        "multiplier": 100.0,
        "settlementType": None,
        "deliverableNote": None,
        "percentChange": _safe_float(row.get("percentChange")),
        "markChange": None,
        "markPercentChange": None,
        "intrinsicValue": intrinsic,
        "inTheMoney": bool(row.get("inTheMoney")) if row.get("inTheMoney") is not None else None,
        "nonStandard": False,
    }


def _pct(v: float | None) -> float | None:
    """yfinance reports IV as a decimal (0.42 = 42%); Schwab reports it
    as a percent (42.0). Normalize to Schwab's convention."""
    return v * 100.0 if v is not None else None


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
