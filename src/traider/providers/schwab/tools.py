"""Schwab market-data + account tools registered on the shared FastMCP.

Same tool surface as the ``yahoo`` provider; the two are mutually
exclusive. Schwab adds ``get_accounts`` (brokerage positions), which
Yahoo cannot serve.
"""
from __future__ import annotations

import atexit
import datetime as _dt
import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from ...logging_utils import attach_provider_logger
from ...ohlcv import analytics
from ...ohlcv.ta import run_indicators
from ...options.summary import summarize_chain
from ...settings import TraiderSettings
from .schwab_client import SCHWAB_API_BASE, SchwabAuthError, SchwabClient


def _src(path: str) -> str:
    return f"{SCHWAB_API_BASE}{path}"

logger = logging.getLogger("traider.schwab")
_client: SchwabClient | None = None


def _get_client() -> SchwabClient:
    global _client
    if _client is None:
        logger.info("initializing Schwab client")
        _client = SchwabClient.from_env()
        atexit.register(_client.close)
        logger.info("Schwab client ready")
    return _client


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _log_tool_error(msg: str, *args: Any) -> None:
    """Call from inside an ``except`` block to log the active exception.

    ``SchwabAuthError`` collapses to a one-line WARNING because the only
    fix is re-running ``traider auth schwab`` on a ~7-day cadence — the
    full traceback is just log noise. Anything else still gets the full
    ``logger.exception`` stack so real bugs stay debuggable.
    """
    exc = sys.exc_info()[1]
    if isinstance(exc, SchwabAuthError):
        logger.warning("%s: %s", msg % args if args else msg, exc)
    else:
        logger.exception(msg, *args)


def _fetch_candles(
    symbol: str,
    period_type: str,
    period: int,
    frequency_type: str,
    frequency: int,
    start_date: int | None,
    end_date: int | None,
    need_extended_hours_data: bool,
) -> list[dict[str, Any]]:
    history = _get_client().get_price_history(
        symbol,
        period_type=period_type,
        period=period,
        frequency_type=frequency_type,
        frequency=frequency,
        start_date=start_date,
        end_date=end_date,
        need_extended_hours_data=need_extended_hours_data,
    )
    return history.get("candles", [])


def register(mcp: FastMCP, settings: TraiderSettings) -> None:
    attach_provider_logger("traider.schwab", settings.log_file("schwab"))

    @mcp.tool()
    def get_quote(symbol: str, field: str = "LAST") -> dict[str, Any]:
        """Return a single field for one symbol.

        Args:
            symbol: Ticker. Equities/ETFs raw (``"SPY"``, ``"AAPL"``);
                futures ``/``-prefixed (``"/ES"``); indices and CBOE
                yield products ``$``-prefixed (``"$VIX"``, ``"$SPX"``).
                See **Symbology** below — the wrong convention silently
                returns an empty string, not an error.
            field: Either a friendly alias (``LAST``, ``BID``, ``ASK``,
                ``VOLUME``, ``MARK``, ``OPEN``, ``HIGH``, ``LOW``, ``CLOSE``,
                ``NET_CHANGE``, ``PERCENT_CHANGE``, ``BID_SIZE``,
                ``ASK_SIZE``) or a native Schwab quote key (e.g.
                ``lastPrice``, ``postMarketChange``, ``mark``).

        Returns ``{"source", "fetched_at", "symbol", "field", "value"}``.
        ``value`` is the field as a string, or ``""`` when the field
        isn't present on the quote.

        **Symbology — index prefix.** Schwab's ``/quotes`` endpoint
        wants indices and CBOE yield products with a ``$`` prefix
        and **no** ``.X`` suffix: ``$VIX``, ``$SPX``, ``$NDX``,
        ``$DJI``, ``$RUT``, ``$COMPX`` (Nasdaq Composite), ``$XSP``
        (mini-SPX), ``$VVIX``, ``$VXN``, and CBOE yield products
        ``$TNX`` / ``$TYX`` / ``$IRX`` / ``$FVX``. **CBOE yield
        products are quoted at 10× the percent yield** — ``$TNX =
        42.88`` means the 10Y is at 4.288%. The ToS / Yahoo /
        ThinkScript variants — bare ``VIX``, ``^VIX``, ``$VIX.X`` —
        all silently return ``""``, which is indistinguishable at
        the tool layer from a wrong symbol or a closed market.
        Equities reject the ``$`` prefix (``$AAPL`` → ``""``; use
        ``AAPL``). Non-CBOE indices (ICE Dollar Index ``$DXY``,
        index-options roots like ``$SPXW``) are not reachable via
        this endpoint — surface the gap to the user rather than
        faking a value from elsewhere.

        **Field semantics.** ``closePrice`` is the **prior** session's
        close, not today's 4PM. ``netChange`` / ``netPercentChange``
        are anchored on the regular session. ``postMarketChange`` /
        ``postMarketPercentChange`` are anchored on today's RTH close
        (Δ from today's 4PM to ``PostMarketLastPrice``). ``mark`` is
        the bid-ask midpoint.

        **REST /quotes is RTH-anchored BY DESIGN.** Schwab's Streamer
        docs expose two distinct last-price fields on
        ``LEVELONE_EQUITIES``: Field 3 ``Last Price`` (all trades)
        and Field 29 ``Regular Market Last Price`` ("Only records
        regular trade"). The REST ``/marketdata/v1/quotes`` response
        mirrors Field-29 semantics — ``lastPrice``, ``netChange``,
        ``tradeTime``, ``quoteTime`` all pin at the 4PM regular-
        session close and do not advance during pre-market, post-
        market, or overnight sessions. This is the endpoint's spec,
        not a cache or staleness bug. Don't retry a stale-looking
        AH quote — the next RTH open is when these fields move.

        **For live extended-hours data, use ``get_price_history``
        with ``need_extended_hours_data=True``** and read the last
        candle. That endpoint serves pre-market + regular + post-
        market minute bars (07:00–20:00 ET) with real volume and
        price discovery. For the 20:00–07:00 ET overnight 24/5
        session, no Schwab REST endpoint delivers ticks; the
        Streamer docs don't commit to overnight coverage either
        (and 24/5 is not referenced anywhere in the Streamer API
        spec). Overnight visibility currently requires ToS.
        """
        logger.info("get_quote symbol=%s field=%s", symbol, field)
        fetched_at = _now_iso()
        try:
            value = _get_client().get_quote(symbol, field)
        except Exception:
            _log_tool_error("get_quote failed symbol=%s field=%s", symbol, field)
            raise
        logger.info("get_quote result symbol=%s field=%s value=%r", symbol, field, value)
        return {
            "source": _src("/marketdata/v1/quotes"),
            "fetched_at": fetched_at,
            "symbol": symbol,
            "field": field,
            "value": "" if value is None else str(value),
        }

    @mcp.tool()
    def get_quotes(
        symbols: list[str],
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return many fields for many symbols in one call.

        Returns ``{"source", "fetched_at", "quotes": {symbol: {field:
        value}}}``. If ``fields`` is omitted, each symbol's entry is
        the full Schwab ``quote`` object.

        **Symbology.** Same rule as ``get_quote``: equities raw
        (``AAPL``), futures ``/``-prefixed (``/ES``), indices /
        CBOE yield products ``$``-prefixed with no ``.X`` suffix
        (``$VIX``, ``$SPX``, ``$NDX``, ``$DJI``, ``$RUT``,
        ``$COMPX``, ``$XSP``, ``$VVIX``, ``$VXN``, ``$TNX`` /
        ``$TYX`` / ``$IRX`` / ``$FVX`` — yield products are 10×
        the percent). Wrong-convention symbols are dropped from
        the result map silently rather than raising, so a missing
        key is indistinguishable from a wrong symbol. See
        ``get_quote`` for the full rule and gaps (non-CBOE indices
        like ``$DXY`` are not reachable).

        **``fields`` is a strict whitelist — keys you don't list are
        dropped from the response.** A narrow list like
        ``["lastPrice", "netChange"]`` will silently omit AH-delta
        keys (``postMarketChange`` et al.). Either omit ``fields``
        to get the full quote object, or include the AH keys
        explicitly: ``postMarketChange``, ``postMarketPercentChange``,
        ``mark``, ``markChange``, ``markPercentChange``.

        **REST /quotes is RTH-anchored BY DESIGN — it does not
        serve live extended-hours prices.** Per Schwab's Streamer
        docs, ``LEVELONE_EQUITIES`` Field 29 ``Regular Market Last
        Price`` ("Only records regular trade") is the semantic
        mirrored here. Every field — ``lastPrice``, ``mark``,
        ``postMarketChange``, ``quoteTime``, ``tradeTime`` — pins
        at the 4PM close until the next RTH session. For live AH
        data use ``get_price_history`` with
        ``need_extended_hours_data=True`` (covers 07:00–20:00 ET);
        no Schwab REST path covers 20:00–07:00 ET overnight.
        """
        logger.info("get_quotes symbols=%s fields=%s", symbols, fields)
        fetched_at = _now_iso()
        try:
            results = _get_client().get_quotes(symbols, fields)
        except Exception:
            _log_tool_error("get_quotes failed symbols=%s fields=%s", symbols, fields)
            raise
        logger.info("get_quotes result=%r", results)
        return {
            "source": _src("/marketdata/v1/quotes"),
            "fetched_at": fetched_at,
            "quotes": results,
        }

    @mcp.tool()
    def get_price_history(
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
        """Return OHLCV candles for one symbol.

        Defaults give one year of daily bars — the "daily bars on the
        yearly chart" case. Response is Schwab's native shape:
        ``{"symbol": ..., "empty": bool, "candles": [{open, high, low,
        close, volume, datetime}, ...]}``. ``datetime`` is epoch ms.

        Args:
            symbol: Ticker (e.g. ``"SPY"``, ``"/ES"``, or a 21-char OSI
                option symbol).
            period_type: ``day``, ``month``, ``year``, or ``ytd``.
            period: How many ``period_type`` units back from today. Valid
                values depend on ``period_type``:
                day=1/2/3/4/5/10, month=1/2/3/6,
                year=1/2/3/5/10/15/20, ytd=1. Ignored if
                ``start_date``/``end_date`` are set.
            frequency_type: ``minute``, ``daily``, ``weekly``, ``monthly``.
                Must be compatible with ``period_type``: day→minute,
                month→daily|weekly, year→daily|weekly|monthly,
                ytd→daily|weekly.
            frequency: Candle size within ``frequency_type``.
                minute=1/5/10/15/30, daily/weekly/monthly=1.
            start_date: Optional epoch ms. If set (with or without
                ``end_date``), overrides ``period``.
            end_date: Optional epoch ms. Defaults to now when only
                ``start_date`` is given.
            need_extended_hours_data: Include pre/post-market candles
                (07:00–09:30 ET + 16:00–20:00 ET). Does **not** cover
                the 20:00–07:00 ET overnight 24/5 session — Schwab
                clips `start_date` / `end_date` inside that window and
                returns the standard-session dataset. For QQQ / SPY /
                etc. in the 24/5 eligible set, overnight ticks are
                only on ToS.
            need_previous_close: Include the prior session's close in the
                response.

        **This is the reliable path for live post-market prices.**
        Unlike ``/marketdata/v1/quotes`` (which is RTH-anchored by
        design and pins at the 4PM close), ``/pricehistory`` with
        ``need_extended_hours_data=True`` returns real minute bars
        through the 20:00 ET post-market close with actual trade
        volume. Read the last candle's ``close`` for a live-ish AH
        quote (~30s behind wall clock).
        """
        logger.info(
            "get_price_history symbol=%s period=%s%s frequency=%s%s",
            symbol, period, period_type, frequency, frequency_type,
        )
        fetched_at = _now_iso()
        try:
            result = _get_client().get_price_history(
                symbol,
                period_type=period_type,
                period=period,
                frequency_type=frequency_type,
                frequency=frequency,
                start_date=start_date,
                end_date=end_date,
                need_extended_hours_data=need_extended_hours_data,
                need_previous_close=need_previous_close,
            )
        except Exception:
            _log_tool_error("get_price_history failed symbol=%s", symbol)
            raise
        candles = result.get("candles", [])
        logger.info(
            "get_price_history result symbol=%s candles=%d empty=%s",
            symbol, len(candles), result.get("empty"),
        )
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            **result,
        }

    @mcp.tool()
    def run_technical_analysis(
        symbol: str,
        indicators: list[dict[str, Any]],
        period_type: str = "year",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
        tail: int | None = None,
    ) -> dict[str, Any]:
        """Run TA-Lib indicators on historical candles for one symbol.

        Fetches OHLCV candles with the same parameters as
        ``get_price_history`` (see that tool for valid period/frequency
        combinations), then computes each requested TA-Lib indicator and
        returns the results aligned to the candle timestamps.

        Args:
            symbol: Ticker (equities, futures, or 21-char OSI option).
            indicators: List of indicator spec dicts. Each must include
                ``name`` (a TA-Lib function name, e.g. ``"SMA"``,
                ``"EMA"``, ``"RSI"``, ``"MACD"``, ``"BBANDS"``, ``"ATR"``,
                ``"STOCH"``, ``"ADX"``, ``"OBV"``). Any other keys are
                forwarded as kwargs to TA-Lib (e.g.
                ``{"name": "SMA", "timeperiod": 20}``,
                ``{"name": "MACD", "fastperiod": 12, "slowperiod": 26,
                "signalperiod": 9}``). Optional ``label`` renames the
                output key so the same indicator can be requested with
                different parameters (e.g. SMA_20 and SMA_50).
            period_type, period, frequency_type, frequency, start_date,
            end_date, need_extended_hours_data: forwarded to
                ``get_price_history``.
            tail: If set, return only the last N points of each series
                (and the matching ``datetime`` entries). Useful to keep
                responses small when you only need recent readings.

        Returns:
            ``{"symbol": ..., "datetime": [epoch_ms, ...],
            "indicators": {label: series}}``. ``series`` is either a list
            (single-output indicators) or a dict of named sub-series
            (multi-output indicators like MACD or BBANDS). Warm-up slots
            at the start of a series are ``null`` (TA-Lib NaN).
        """
        logger.info(
            "run_technical_analysis symbol=%s indicators=%s tail=%s",
            symbol, [i.get("name") for i in indicators], tail,
        )
        fetched_at = _now_iso()
        try:
            history = _get_client().get_price_history(
                symbol,
                period_type=period_type,
                period=period,
                frequency_type=frequency_type,
                frequency=frequency,
                start_date=start_date,
                end_date=end_date,
                need_extended_hours_data=need_extended_hours_data,
            )
            candles = history.get("candles", [])
            result = run_indicators(candles, indicators, tail=tail)
        except Exception:
            _log_tool_error("run_technical_analysis failed symbol=%s", symbol)
            raise
        logger.info(
            "run_technical_analysis result symbol=%s candles=%d labels=%s",
            symbol, len(candles), list(result["indicators"].keys()),
        )
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbol": symbol,
            **result,
        }

    @mcp.tool()
    def get_option_chain(
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
        """Option chain for an underlying.

        Native Schwab payload with ``callExpDateMap`` / ``putExpDateMap``
        keyed by ``"YYYY-MM-DD:dte"`` → strike → list of contracts. Each
        contract includes bid/ask/last/mark, volume, open interest, IV,
        Greeks (delta/gamma/theta/vega/rho), intrinsic/extrinsic value,
        and the 21-char OSI symbol.

        Args:
            symbol: Underlying ticker (e.g. ``"SPY"``, ``"AAPL"``).
            contract_type: ``CALL``, ``PUT``, or ``ALL``.
            strike_count: Strikes above and below the ATM strike.
            include_underlying_quote: Include the underlying's quote.
            strategy: ``SINGLE`` (default) or one of ``ANALYTICAL``,
                ``COVERED``, ``VERTICAL``, ``CALENDAR``, ``STRANGLE``,
                ``STRADDLE``, ``BUTTERFLY``, ``CONDOR``, ``DIAGONAL``,
                ``COLLAR``, ``ROLL``.
            interval: Strike spacing (``ANALYTICAL`` only).
            strike: Return only contracts at this exact strike.
            range_: ``ITM``, ``NTM``, ``OTM``, ``SAK``, ``SBK``,
                ``SNK``, or ``ALL``.
            from_date, to_date: ``YYYY-MM-DD`` expiration bounds.
            volatility, underlying_price, interest_rate,
            days_to_expiration: ``ANALYTICAL`` overrides.
            exp_month: ``JAN``..``DEC`` or ``ALL``.
            option_type: ``S``, ``NS``, or ``ALL``.
        """
        logger.info(
            "get_option_chain symbol=%s type=%s strategy=%s",
            symbol, contract_type, strategy,
        )
        fetched_at = _now_iso()
        try:
            result = _get_client().get_option_chain(
                symbol,
                contract_type=contract_type,
                strike_count=strike_count,
                include_underlying_quote=include_underlying_quote,
                strategy=strategy,
                interval=interval,
                strike=strike,
                range_=range_,
                from_date=from_date,
                to_date=to_date,
                volatility=volatility,
                underlying_price=underlying_price,
                interest_rate=interest_rate,
                days_to_expiration=days_to_expiration,
                exp_month=exp_month,
                option_type=option_type,
            )
        except Exception:
            _log_tool_error("get_option_chain failed symbol=%s", symbol)
            raise
        return {
            "source": _src("/marketdata/v1/chains"),
            "fetched_at": fetched_at,
            **result,
        }

    @mcp.tool()
    def analyze_option_chain(
        symbol: str,
        contract_type: str = "ALL",
        strike_count: int | None = 20,
        from_date: str | None = None,
        to_date: str | None = None,
        exp_month: str | None = None,
        option_type: str | None = None,
        range_: str | None = None,
        wings: int = 5,
        top_n: int = 5,
    ) -> dict[str, Any]:
        """Bounded-size analyst view of an option chain.

        Fetches via ``get_option_chain`` and returns, per expiration:
        ATM strike, ATM call and put legs (mark/bid/ask/IV/OI/volume
        plus Greeks when the backend ships them), straddle cost,
        implied one-day move as percent, implied range, IV skew across
        ±``wings`` strikes around ATM, and the top ``top_n`` strikes by
        open interest and volume on each side.

        Use this instead of ``get_option_chain`` when you need the
        digestible view — the raw chain for a single expiration at
        ``strike_count=20`` can exceed 70k chars, which busts LLM
        context. The summary is bounded by ``wings`` and ``top_n`` and
        typically lands under 5k chars per expiration.

        Only ``strategy="SINGLE"`` chains are summarized — multi-leg
        and ``ANALYTICAL`` strategies would need a different shape and
        are not covered here.
        """
        logger.info(
            "analyze_option_chain symbol=%s wings=%d top_n=%d",
            symbol, wings, top_n,
        )
        fetched_at = _now_iso()
        try:
            chain = _get_client().get_option_chain(
                symbol,
                contract_type=contract_type,
                strike_count=strike_count,
                include_underlying_quote=True,
                strategy="SINGLE",
                from_date=from_date,
                to_date=to_date,
                exp_month=exp_month,
                option_type=option_type,
                range_=range_,
            )
            summary = summarize_chain(chain, wings=wings, top_n=top_n)
        except Exception:
            _log_tool_error("analyze_option_chain failed symbol=%s", symbol)
            raise
        summary["source"] = _src("/marketdata/v1/chains")
        summary["fetched_at"] = fetched_at
        return summary

    @mcp.tool()
    def get_option_expirations(symbol: str) -> dict[str, Any]:
        """Expiration series list for an underlying.

        Returns ``{"status", "expirationList": [{"expirationDate",
        "daysToExpiration", "expirationType", "settlementType",
        "optionRoots", "standard"}, ...]}``. Use this to discover
        available expirations before pulling a full chain slice.
        """
        logger.info("get_option_expirations symbol=%s", symbol)
        fetched_at = _now_iso()
        try:
            result = _get_client().get_option_expirations(symbol)
        except Exception:
            _log_tool_error("get_option_expirations failed symbol=%s", symbol)
            raise
        return {
            "source": _src("/marketdata/v1/expirationchain"),
            "fetched_at": fetched_at,
            **result,
        }

    @mcp.tool()
    def get_movers(
        index: str,
        sort: str | None = None,
        frequency: int | None = None,
    ) -> dict[str, Any]:
        """Top movers for an index.

        Args:
            index: ``$DJI``, ``$COMPX``, ``$SPX``, ``NYSE``, ``NASDAQ``,
                ``OTCBB``, ``INDEX_ALL``, ``EQUITY_ALL``, ``OPTION_ALL``,
                ``OPTION_PUT``, ``OPTION_CALL``.
            sort: ``VOLUME``, ``TRADES``, ``PERCENT_CHANGE_UP``, or
                ``PERCENT_CHANGE_DOWN``. Defaults to Schwab's choice.
            frequency: Minutes of activity required. One of 0, 1, 5, 10,
                30, 60.
        """
        logger.info("get_movers index=%s sort=%s frequency=%s", index, sort, frequency)
        fetched_at = _now_iso()
        try:
            result = _get_client().get_movers(index, sort=sort, frequency=frequency)
        except Exception:
            _log_tool_error("get_movers failed index=%s", index)
            raise
        return {
            "source": _src(f"/marketdata/v1/movers/{index}"),
            "fetched_at": fetched_at,
            **result,
        }

    @mcp.tool()
    def search_instruments(
        symbol: str,
        projection: str = "symbol-search",
    ) -> dict[str, Any]:
        """Look up an instrument or pull fundamentals.

        Args:
            symbol: Ticker, CUSIP, regex, or description fragment depending
                on ``projection``.
            projection: ``symbol-search`` (exact), ``symbol-regex``,
                ``desc-search``, ``desc-regex``, ``search``, or
                ``fundamental`` (adds the fundamentals block: P/E, EPS,
                dividends, 52-week range, etc.).
        """
        logger.info("search_instruments symbol=%s projection=%s", symbol, projection)
        fetched_at = _now_iso()
        try:
            result = _get_client().search_instruments(symbol, projection=projection)
        except Exception:
            _log_tool_error("search_instruments failed symbol=%s", symbol)
            raise
        return {
            "source": _src("/marketdata/v1/instruments"),
            "fetched_at": fetched_at,
            **result,
        }

    @mcp.tool()
    def get_market_hours(
        markets: list[str],
        date: str | None = None,
    ) -> dict[str, Any]:
        """Session hours for one or more markets.

        Args:
            markets: Any of ``equity``, ``option``, ``bond``, ``future``,
                ``forex``.
            date: ``YYYY-MM-DD``. Defaults to today.
        """
        logger.info("get_market_hours markets=%s date=%s", markets, date)
        fetched_at = _now_iso()
        try:
            result = _get_client().get_market_hours(markets, date=date)
        except Exception:
            _log_tool_error("get_market_hours failed markets=%s", markets)
            raise
        return {
            "source": _src("/marketdata/v1/markets"),
            "fetched_at": fetched_at,
            **result,
        }

    @mcp.tool()
    def get_accounts(include_positions: bool = False) -> dict[str, Any]:
        """Authorized accounts (read-only).

        Returns ``{"source", "fetched_at", "count", "accounts": [...]}``
        where each entry is a raw Schwab ``securitiesAccount`` record —
        balances, buying power, and (optionally) positions with cost
        basis, market value, and open P&L.

        **Reading P&L fields correctly — this is a trap.** On a
        position opened *today*, Schwab reports ``currentDayProfitLoss``
        as the position's current ``marketValue``, NOT the intraday P&L.
        Quoting that field as "today's gain/loss" on a same-day open
        inflates the number by one to two orders of magnitude and is a
        trust-breaking error (a short call sold for $820 credit and now
        marked at $890 shows ``currentDayProfitLoss: -890`` — the real
        open P&L is -$70).

        Rules for any position field you cite:

        - **Did the position carry over from the prior session?** Check
          ``previousSessionLongQuantity`` / ``previousSessionShortQuantity``
          (both 0 → opened today) and ``currentDayCost`` (non-zero → traded
          today).
        - **For same-day opens, use the open-P&L field instead.**
          ``longOpenProfitLoss`` for longs, ``shortOpenProfitLoss`` for
          shorts. Both are relative to the trade's fill price.
        - **For carryover positions**, ``currentDayProfitLoss`` is the
          real intraday P&L and can be cited directly.

        The same trap applies to equities, ETFs, and options alike.

        **After-hours options: ``currentDayProfitLoss`` will NOT match
        the underlying's ``netChange``.** Once RTH closes, a stock's
        ``closePrice`` and ``netChange`` are pinned at the 4PM close,
        but an option position's ``currentDayProfitLoss`` keeps
        re-marking against the current mid-quote as the underlying
        drifts in AH. Since options don't actually trade after 4PM for
        most single-names, that AH mark is a bid-ask midpoint on a
        spread that has widened materially — it can diverge from what
        delta would predict on the spot move. The resulting apparent
        conflict (e.g. stock closed +8% on the day, but an ITM long
        call's ``currentDayProfitLoss`` is negative) is expected, not
        a data bug. Don't flag it, and don't pull a chain to
        "reconcile" an AH mark against an RTH close — real P&L prints
        at the next open.

        Args:
            include_positions: Include each account's ``positions`` array
                (quantity, cost basis, market value, unrealized P&L).
        """
        logger.info("get_accounts include_positions=%s", include_positions)
        fetched_at = _now_iso()
        try:
            accounts = _get_client().get_accounts(include_positions=include_positions)
        except Exception:
            _log_tool_error("get_accounts failed")
            raise
        logger.info("get_accounts result count=%d", len(accounts))
        return {
            "source": _src("/trader/v1/accounts"),
            "fetched_at": fetched_at,
            "count": len(accounts),
            "accounts": accounts,
        }

    @mcp.tool()
    def get_account_numbers() -> dict[str, Any]:
        """Plaintext account number → hashed account ID mapping.

        Every ``/trader/v1/accounts/{hash}/...`` endpoint in traider
        takes the hashed form (``hashValue``). Use this tool to discover
        which hash corresponds to a given plaintext account number on
        your Schwab login.

        Returns ``{"source", "fetched_at", "count", "accounts": [...]}``
        where each entry is ``{"accountNumber", "hashValue"}``.
        """
        logger.info("get_account_numbers")
        fetched_at = _now_iso()
        try:
            accounts = _get_client().get_account_numbers()
        except Exception:
            _log_tool_error("get_account_numbers failed")
            raise
        logger.info("get_account_numbers result count=%d", len(accounts))
        return {
            "source": _src("/trader/v1/accounts/accountNumbers"),
            "fetched_at": fetched_at,
            "count": len(accounts),
            "accounts": accounts,
        }

    def _resolve_account_hash(explicit: str | None) -> str:
        if explicit is not None:
            return explicit
        accounts = _get_client().get_account_numbers()
        if len(accounts) == 1:
            return accounts[0]["hashValue"]
        if not accounts:
            raise RuntimeError(
                "no authorized Schwab accounts on this token — "
                "re-run: traider auth schwab"
            )
        hashes = [a.get("hashValue") for a in accounts]
        raise RuntimeError(
            "multiple authorized accounts — pass account_hash explicitly; "
            f"available hashValues: {hashes}"
        )

    def _resolve_order_window(
        from_entered_time: str | None,
        to_entered_time: str | None,
    ) -> tuple[str, str]:
        now = _dt.datetime.now(_dt.UTC)
        resolved_from = from_entered_time or (
            now - _dt.timedelta(days=60)
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        resolved_to = to_entered_time or now.strftime(
            "%Y-%m-%dT%H:%M:%S.999Z"
        )
        return resolved_from, resolved_to

    @mcp.tool()
    def get_transactions(
        start_date: str,
        end_date: str,
        account_hash: str | None = None,
        symbol: str | None = None,
        types: str | list[str] | None = None,
    ) -> dict[str, Any]:
        """Transaction history for one account (read-only).

        Returns raw Schwab transaction records — trades (with fill
        prices, per-leg amounts, commissions, fees), dividends,
        interest, transfers, journal entries, etc. Use this to
        reconstruct realized P&L, check actual fill prices against
        marks, track cost basis for wash-sale windows, or audit
        closing-trade prices on options that printed far from mid.

        Args:
            start_date: Lower bound. ``YYYY-MM-DD`` (treated as start
                of day UTC) or full ISO-8601 UTC datetime like
                ``2026-04-01T14:30:00.000Z``. Required.
            end_date: Upper bound. ``YYYY-MM-DD`` (treated as end of
                day UTC) or full ISO-8601 UTC datetime. Required.
            account_hash: Hashed account ID from
                ``get_account_numbers``. If ``None`` and exactly one
                account is authorized, it is resolved automatically;
                otherwise the tool raises with the available hashes.
            symbol: Filter to one symbol. For options, use the 21-char
                OSI form (e.g. ``"SPY   260501P00705000"``).
            types: Filter by transaction type. Accepts a single type
                string, a list, or a comma-separated string. Common:
                ``TRADE`` (opens/closes), ``RECEIVE_AND_DELIVER``
                (option assignment / exercise),
                ``DIVIDEND_OR_INTEREST``, ``ACH_RECEIPT``,
                ``ACH_DISBURSEMENT``, ``CASH_RECEIPT``,
                ``CASH_DISBURSEMENT``, ``ELECTRONIC_FUND``,
                ``WIRE_IN``, ``WIRE_OUT``, ``JOURNAL``,
                ``MEMORANDUM``, ``MARGIN_CALL``, ``MONEY_MARKET``,
                ``SMA_ADJUSTMENT``.

        Each trade record's ``transferItems`` array holds per-leg
        fills with ``price``, ``amount`` (signed quantity), ``cost``,
        and the ``instrument`` block (symbol, option multiplier,
        underlying). Commissions and fees appear as separate
        ``transferItems`` entries with ``feeType`` populated. Schwab
        typically caps the lookback at ~1 year — narrow the window if
        the API errors on a long range.

        Returns ``{"source", "fetched_at", "count", "transactions":
        [...]}``.
        """
        logger.info(
            "get_transactions start=%s end=%s symbol=%s types=%s account=%s",
            start_date, end_date, symbol, types,
            "<auto>" if account_hash is None else "<explicit>",
        )
        fetched_at = _now_iso()
        try:
            resolved = _resolve_account_hash(account_hash)
            transactions = _get_client().get_transactions(
                account_hash=resolved,
                start_date=start_date,
                end_date=end_date,
                symbol=symbol,
                types=types,
            )
        except Exception:
            _log_tool_error("get_transactions failed")
            raise
        logger.info("get_transactions result count=%d", len(transactions))
        return {
            "source": _src(f"/trader/v1/accounts/{resolved}/transactions"),
            "fetched_at": fetched_at,
            "count": len(transactions),
            "transactions": transactions,
        }

    @mcp.tool()
    def get_transaction(
        transaction_id: str,
        account_hash: str | None = None,
    ) -> dict[str, Any]:
        """Single transaction by ID (read-only).

        Args:
            transaction_id: Schwab transaction / activity ID — the
                ``activityId`` field on a record returned by
                ``get_transactions``.
            account_hash: Same rules as ``get_transactions`` —
                optional when exactly one account is authorized.
        """
        logger.info(
            "get_transaction id=%s account=%s",
            transaction_id,
            "<auto>" if account_hash is None else "<explicit>",
        )
        fetched_at = _now_iso()
        try:
            resolved = _resolve_account_hash(account_hash)
            transaction = _get_client().get_transaction(resolved, transaction_id)
        except Exception:
            _log_tool_error("get_transaction failed id=%s", transaction_id)
            raise
        return {
            "source": _src(
                f"/trader/v1/accounts/{resolved}/transactions/{transaction_id}"
            ),
            "fetched_at": fetched_at,
            **transaction,
        }

    @mcp.tool()
    def get_orders(
        from_entered_time: str | None = None,
        to_entered_time: str | None = None,
        status: str | None = None,
        max_results: int | None = None,
        account_hash: str | None = None,
    ) -> dict[str, Any]:
        """Orders for one account, filterable by status (read-only).

        Returns raw Schwab order records — every order entered in the
        window, regardless of outcome (WORKING, FILLED, CANCELED,
        REPLACED, ...). Use ``status="WORKING"`` for resting /
        open orders; pair with ``get_transactions`` for post-fill
        detail (commissions, per-leg costs), since a WORKING order
        has no fill price yet.

        Args:
            from_entered_time: Lower bound on the order's
                ``enteredTime``. ``YYYY-MM-DD`` (start of day UTC) or
                full ISO-8601 UTC datetime. Defaults to 60 days ago —
                Schwab caps the lookback at ~60 days, so this covers
                the entire available window.
            to_entered_time: Upper bound on ``enteredTime``.
                ``YYYY-MM-DD`` (end of day UTC) or full ISO-8601 UTC
                datetime. Defaults to now.
            status: Filter to one status. Open/resting buckets:
                ``WORKING``, ``PENDING_ACTIVATION``, ``QUEUED``,
                ``ACCEPTED``, ``AWAITING_PARENT_ORDER``,
                ``AWAITING_CONDITION``, ``AWAITING_STOP_CONDITION``,
                ``AWAITING_MANUAL_REVIEW``, ``AWAITING_UR_OUT``,
                ``AWAITING_RELEASE_TIME``, ``PENDING_ACKNOWLEDGEMENT``.
                Terminal: ``FILLED``, ``CANCELED``, ``REJECTED``,
                ``EXPIRED``, ``REPLACED``. Transition:
                ``PENDING_CANCEL``, ``PENDING_REPLACE``,
                ``PENDING_RECALL``. Also ``NEW`` and ``UNKNOWN``.
                Omit to return all statuses in the window.
            max_results: Server-side row cap (Schwab default 3000).
            account_hash: Hashed account ID from
                ``get_account_numbers``. If ``None`` and exactly one
                account is authorized, it is resolved automatically;
                otherwise the tool raises with the available hashes.

        Each record carries ``orderId``, ``status``, ``enteredTime``,
        ``closeTime``, ``orderType`` (e.g. ``LIMIT`` / ``MARKET`` /
        ``STOP``), ``duration`` (``DAY`` / ``GOOD_TILL_CANCEL`` / ...),
        ``price`` / ``stopPrice``, ``quantity`` / ``filledQuantity`` /
        ``remainingQuantity``, ``cancelable`` / ``editable``, and
        ``orderLegCollection`` (per-leg ``instruction`` like
        ``BUY_TO_OPEN`` / ``SELL_TO_CLOSE``, ``positionEffect``, and
        the ``instrument`` block with symbol / OSI). Multi-leg and
        conditional (OCO / trigger) orders nest children under
        ``childOrderStrategies`` — walk that array to see every leg.

        Returns ``{"source", "fetched_at", "count", "orders": [...]}``.
        """
        logger.info(
            "get_orders from=%s to=%s status=%s max=%s account=%s",
            from_entered_time, to_entered_time, status, max_results,
            "<auto>" if account_hash is None else "<explicit>",
        )
        fetched_at = _now_iso()
        try:
            resolved_from, resolved_to = _resolve_order_window(
                from_entered_time, to_entered_time
            )
            resolved_hash = _resolve_account_hash(account_hash)
            orders = _get_client().get_orders(
                account_hash=resolved_hash,
                from_entered_time=resolved_from,
                to_entered_time=resolved_to,
                max_results=max_results,
                status=status,
            )
        except Exception:
            _log_tool_error("get_orders failed")
            raise
        logger.info("get_orders result count=%d", len(orders))
        return {
            "source": _src(f"/trader/v1/accounts/{resolved_hash}/orders"),
            "fetched_at": fetched_at,
            "count": len(orders),
            "orders": orders,
        }

    @mcp.tool()
    def get_order(
        order_id: str,
        account_hash: str | None = None,
    ) -> dict[str, Any]:
        """Single order by ID (read-only).

        Args:
            order_id: Schwab order ID — the ``orderId`` field on a
                record returned by ``get_orders``.
            account_hash: Same rules as ``get_orders`` — optional when
                exactly one account is authorized.
        """
        logger.info(
            "get_order id=%s account=%s",
            order_id,
            "<auto>" if account_hash is None else "<explicit>",
        )
        fetched_at = _now_iso()
        try:
            resolved = _resolve_account_hash(account_hash)
            order = _get_client().get_order(resolved, order_id)
        except Exception:
            _log_tool_error("get_order failed id=%s", order_id)
            raise
        return {
            "source": _src(f"/trader/v1/accounts/{resolved}/orders/{order_id}"),
            "fetched_at": fetched_at,
            **order,
        }

    @mcp.tool()
    def analyze_returns(
        symbol: str,
        period_type: str = "year",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
        risk_free_rate: float = 0.0,
        annualization: float | None = None,
        include_drawdown_series: bool = False,
    ) -> dict[str, Any]:
        """Return/risk summary: total/annual return, vol, Sharpe, Sortino,
        max drawdown (with peak/trough timestamps), Calmar, skew,
        excess kurtosis.

        Price-history params match ``get_price_history``. ``risk_free_rate``
        is annualized (e.g. ``0.05``). ``annualization`` overrides the
        periods-per-year inferred from bar spacing — set it for intraday
        bars if the inferred value looks wrong. Set
        ``include_drawdown_series=True`` to also return the per-bar
        equity curve and drawdown series; useful for charting or
        finding underwater periods, but pricier on the wire.
        """
        logger.info("analyze_returns symbol=%s", symbol)
        fetched_at = _now_iso()
        try:
            candles = _fetch_candles(
                symbol, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.returns_metrics(
                candles,
                risk_free_rate=risk_free_rate,
                annualization=annualization,
                include_drawdown_series=include_drawdown_series,
            )
        except Exception:
            _log_tool_error("analyze_returns failed symbol=%s", symbol)
            raise
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbol": symbol,
            **result,
        }

    @mcp.tool()
    def analyze_correlation(
        symbols: list[str],
        period_type: str = "year",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
    ) -> dict[str, Any]:
        """Pearson correlation matrix of log returns across ``symbols``.

        Fetches each symbol's candles then inner-joins on bar timestamps.
        """
        logger.info("analyze_correlation symbols=%s", symbols)
        fetched_at = _now_iso()
        try:
            candles_by_symbol = {
                sym: _fetch_candles(
                    sym, period_type, period, frequency_type, frequency,
                    start_date, end_date, need_extended_hours_data,
                )
                for sym in symbols
            }
            result = analytics.correlation_matrix(candles_by_symbol)
        except Exception:
            _log_tool_error("analyze_correlation failed symbols=%s", symbols)
            raise
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            **result,
        }

    @mcp.tool()
    def analyze_beta(
        symbol: str,
        benchmark: str = "SPY",
        period_type: str = "year",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
        annualization: float | None = None,
    ) -> dict[str, Any]:
        """Beta, annualized alpha, R², and correlation of ``symbol`` vs
        ``benchmark`` over the shared window."""
        logger.info("analyze_beta symbol=%s benchmark=%s", symbol, benchmark)
        fetched_at = _now_iso()
        try:
            a = _fetch_candles(
                symbol, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            b = _fetch_candles(
                benchmark, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.beta(a, b, annualization=annualization)
        except Exception:
            _log_tool_error("analyze_beta failed symbol=%s vs %s", symbol, benchmark)
            raise
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbol": symbol,
            "benchmark": benchmark,
            **result,
        }

    @mcp.tool()
    def analyze_volatility_regime(
        symbol: str,
        period_type: str = "year",
        period: int = 2,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
        short_window: int = 20,
        lookback: int = 252,
        annualization: float | None = None,
    ) -> dict[str, Any]:
        """Classify current realized vol against its trailing distribution.

        Returns current annualized vol, its z-score and percentile against
        the last ``lookback`` ``short_window``-bar readings, and a regime
        label: ``low`` / ``normal`` / ``elevated`` / ``extreme``.
        """
        logger.info(
            "analyze_volatility_regime symbol=%s short=%d lookback=%d",
            symbol, short_window, lookback,
        )
        fetched_at = _now_iso()
        try:
            candles = _fetch_candles(
                symbol, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.volatility_regime(
                candles,
                short_window=short_window,
                lookback=lookback,
                annualization=annualization,
            )
        except Exception:
            _log_tool_error("analyze_volatility_regime failed symbol=%s", symbol)
            raise
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbol": symbol,
            **result,
        }

    @mcp.tool()
    def analyze_zscore(
        symbol: str,
        window: int = 20,
        source: str = "close",
        period_type: str = "year",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
        tail: int | None = None,
    ) -> dict[str, Any]:
        """Rolling z-score of ``source`` (``close`` or ``log_return``).

        ``tail`` keeps only the last N points of the returned series.
        """
        logger.info(
            "analyze_zscore symbol=%s window=%d source=%s tail=%s",
            symbol, window, source, tail,
        )
        fetched_at = _now_iso()
        try:
            candles = _fetch_candles(
                symbol, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.rolling_zscore(candles, window=window, source=source)
        except Exception:
            _log_tool_error("analyze_zscore failed symbol=%s", symbol)
            raise
        if tail is not None and tail > 0 and "zscore" in result:
            result["datetime"] = result["datetime"][-tail:]
            result["zscore"] = result["zscore"][-tail:]
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbol": symbol,
            **result,
        }

    @mcp.tool()
    def analyze_pair_spread(
        symbol_a: str,
        symbol_b: str,
        hedge_ratio: float | None = None,
        zscore_window: int = 60,
        period_type: str = "year",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
        tail: int | None = None,
    ) -> dict[str, Any]:
        """Log-price spread between two symbols with a rolling z-score.

        ``hedge_ratio=None`` estimates it via OLS of ``log(A)`` on
        ``log(B)`` over the full overlap. Also reports an AR(1) half-life
        in bars (``null`` if the spread is not mean-reverting on this
        window). ``tail`` trims the returned series.
        """
        logger.info(
            "analyze_pair_spread a=%s b=%s window=%d hedge=%s",
            symbol_a, symbol_b, zscore_window, hedge_ratio,
        )
        fetched_at = _now_iso()
        try:
            a = _fetch_candles(
                symbol_a, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            b = _fetch_candles(
                symbol_b, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.pair_spread(
                a, b, hedge_ratio=hedge_ratio, zscore_window=zscore_window,
            )
        except Exception:
            _log_tool_error("analyze_pair_spread failed %s/%s", symbol_a, symbol_b)
            raise
        if tail is not None and tail > 0 and "spread" in result:
            result["datetime"] = result["datetime"][-tail:]
            result["spread"] = result["spread"][-tail:]
            result["zscore"] = result["zscore"][-tail:]
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbols": [symbol_a, symbol_b],
            **result,
        }

    @mcp.tool()
    def analyze_session_ranges(
        symbol: str,
        period_type: str = "day",
        period: int = 10,
        frequency_type: str = "minute",
        frequency: int = 30,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = True,
        asia_start: str = "18:00",
        asia_end: str = "03:00",
        london_start: str = "03:00",
        london_end: str = "08:00",
        ny_start: str = "08:00",
        ny_end: str = "17:00",
        timezone: str = "America/New_York",
        tight_lookback: int = 5,
        tight_multiplier: float = 0.7,
        tail: int | None = None,
    ) -> dict[str, Any]:
        """Per-day Asia / London / New York session ranges with a
        tight-Asia flag and a London-sweeps-Asia signal.

        Needs intraday bars with extended-hours coverage — the Asia
        session (default 18:00-03:00 ET) sits entirely outside US RTH,
        so ``need_extended_hours_data`` defaults to ``True`` here.

        Session windows are ``"HH:MM"`` strings interpreted in
        ``timezone`` (default ``America/New_York``). Each day's three
        sessions are keyed to the date the session *ends* on — so
        previous-evening Asia bars and early-morning Asia bars roll up
        into one Asia session for the following trading day, grouped
        with that day's London and New York sessions.

        For each day the response gives, per session, high / low /
        range / open / close / bar count / first and last bar
        timestamps. The Asia block adds:

        - ``tight_baseline``: rolling median of the prior
          ``tight_lookback`` Asia ranges (``null`` until filled).
        - ``tight``: ``True`` when
          ``range < tight_baseline * tight_multiplier``, else ``False``
          (``null`` until the baseline is available). Pragmatic
          default, not a canonical ICT definition — adjust
          ``tight_lookback`` / ``tight_multiplier`` or reinterpret
          client-side against ATR if you want a different convention.

        The London block adds (only when both Asia and London sessions
        have bars that day):

        - ``swept_asia_high``: ``True`` when London's high exceeded the
          Asia high AND London closed back below it.
        - ``swept_asia_low``: mirror for the low.
        - ``sweep``: list of ``"high"`` / ``"low"`` flags or ``null``.

        A pure breakout (London took the level and closed beyond it) is
        not flagged as a sweep.

        Args:
            symbol: Ticker (equities, futures, or 21-char OSI option).
            period_type, period, frequency_type, frequency, start_date,
            end_date, need_extended_hours_data: forwarded to
                ``get_price_history``. Defaults pull ~10 days of 30-min
                bars with extended hours so Asia sessions are covered.
            asia_start/asia_end, london_start/london_end,
            ny_start/ny_end: ``"HH:MM"`` session boundaries in
                ``timezone``. Asia's default wraps midnight.
            timezone: IANA zone used to bucket bars by session.
            tight_lookback: Number of prior Asia sessions for the
                tight-range baseline.
            tight_multiplier: Current Asia range is tight when below
                ``baseline * tight_multiplier``.
            tail: If set, return only the last N days.
        """
        logger.info(
            "analyze_session_ranges symbol=%s tz=%s tight_lookback=%d mult=%.2f tail=%s",
            symbol, timezone, tight_lookback, tight_multiplier, tail,
        )
        fetched_at = _now_iso()
        try:
            candles = _fetch_candles(
                symbol, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.session_ranges(
                candles,
                asia_start=asia_start,
                asia_end=asia_end,
                london_start=london_start,
                london_end=london_end,
                ny_start=ny_start,
                ny_end=ny_end,
                timezone=timezone,
                tight_lookback=tight_lookback,
                tight_multiplier=tight_multiplier,
            )
        except Exception:
            _log_tool_error("analyze_session_ranges failed symbol=%s", symbol)
            raise
        if tail is not None and tail > 0 and "days" in result:
            result["days"] = result["days"][-tail:]
            result["n_days"] = len(result["days"])
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbol": symbol,
            **result,
        }

    @mcp.tool()
    def analyze_support_resistance(
        symbol: str,
        period_type: str = "year",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
        swing_window: int = 5,
        max_swings: int = 10,
        prior_high: float | None = None,
        prior_low: float | None = None,
        prior_close: float | None = None,
    ) -> dict[str, Any]:
        """Recent swing highs / lows + classic / Fibonacci / Camarilla
        pivot points.

        *Swings* use a symmetric fractal: a bar is a swing high when its
        high is strictly greater than the highs of ``swing_window`` bars
        on each side. Returns up to ``max_swings`` of each, most recent
        first, with ``bars_ago`` so the model can weight by recency.

        *Pivot points* are computed from a prior session's high / low /
        close. Pass them explicitly via ``prior_high`` / ``prior_low`` /
        ``prior_close``. For daily candles the convention is yesterday's
        H/L/C; for intraday candles, pass the prior daily session's
        H/L/C — this tool does not infer the daily session boundary
        from intraday bars. Omit them and pivots are skipped from the
        response (swings still returned).
        """
        logger.info(
            "analyze_support_resistance symbol=%s swing_window=%d max_swings=%d",
            symbol, swing_window, max_swings,
        )
        fetched_at = _now_iso()
        try:
            candles = _fetch_candles(
                symbol, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.support_resistance(
                candles,
                swing_window=swing_window,
                max_swings=max_swings,
                prior_high=prior_high,
                prior_low=prior_low,
                prior_close=prior_close,
            )
        except Exception:
            _log_tool_error("analyze_support_resistance failed symbol=%s", symbol)
            raise
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbol": symbol,
            **result,
        }

    @mcp.tool()
    def analyze_anchored_vwap(
        symbol: str,
        anchor: int | str | None = None,
        period_type: str = "day",
        period: int = 10,
        frequency_type: str = "minute",
        frequency: int = 30,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = True,
        tail: int | None = None,
    ) -> dict[str, Any]:
        """Volume-weighted average price anchored to a point in the
        series.

        VWAP from ``anchor`` forward, where typical price is
        ``(H+L+C)/3``. Common institutional reference price; often acts
        as soft support/resistance because positions accumulated near
        the anchor (event date, gap day, earnings, key level) are flat
        at VWAP.

        ``anchor`` is ``None`` (anchor at first candle), an epoch-ms
        ``int``, or an ISO date / datetime string in UTC
        (``"2026-04-22"`` or ``"2026-04-22T13:30:00Z"``). Anchors
        earlier than the first candle pin to the first candle.

        Defaults pull ~10 days of 30-min bars with extended hours so
        the most common use (earnings/event-day VWAP) works without
        tuning. Pick frequency to match the anchor's granularity.
        ``tail`` trims the returned series.
        """
        logger.info(
            "analyze_anchored_vwap symbol=%s anchor=%s",
            symbol, anchor,
        )
        fetched_at = _now_iso()
        try:
            candles = _fetch_candles(
                symbol, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.anchored_vwap(candles, anchor=anchor)
        except Exception:
            _log_tool_error("analyze_anchored_vwap failed symbol=%s", symbol)
            raise
        if tail is not None and tail > 0 and "vwap" in result:
            result["datetime"] = result["datetime"][-tail:]
            result["vwap"] = result["vwap"][-tail:]
            result["n_bars"] = len(result["vwap"])
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbol": symbol,
            **result,
        }

    @mcp.tool()
    def analyze_donchian_channels(
        symbol: str,
        period_window: int = 20,
        period_type: str = "year",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
        tail: int | None = None,
    ) -> dict[str, Any]:
        """Donchian channels: rolling highest-high / lowest-low /
        midline over ``period_window`` bars.

        Classic breakout/range visualization. The current bar
        contributes to its own window — a fresh new high shows as
        ``current_close == current_upper``. The response's
        ``position`` field labels the current bar:
        ``"above_upper"`` / ``"at_upper"`` / ``"in_band"`` /
        ``"at_lower"`` / ``"below_lower"`` (the ``"at_*"`` band uses a
        0.05% tolerance).
        """
        logger.info(
            "analyze_donchian_channels symbol=%s window=%d",
            symbol, period_window,
        )
        fetched_at = _now_iso()
        try:
            candles = _fetch_candles(
                symbol, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.donchian_channels(candles, period=period_window)
        except Exception:
            _log_tool_error("analyze_donchian_channels failed symbol=%s", symbol)
            raise
        if tail is not None and tail > 0 and "upper" in result:
            for k in ("datetime", "upper", "lower", "middle"):
                result[k] = result[k][-tail:]
            result["n_bars"] = len(result["upper"])
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbol": symbol,
            **result,
        }

    @mcp.tool()
    def analyze_rolling_correlation(
        symbol_a: str,
        symbol_b: str,
        window: int = 30,
        period_type: str = "year",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
        tail: int | None = None,
    ) -> dict[str, Any]:
        """Rolling Pearson correlation of log returns over a moving
        ``window`` (in bars).

        Inner-joins the two symbols' bars on timestamp first. Use this
        to spot regime shifts in correlation (e.g. a defensive name
        decoupling from SPY ahead of an event). ``tail`` trims the
        returned series.
        """
        logger.info(
            "analyze_rolling_correlation a=%s b=%s window=%d",
            symbol_a, symbol_b, window,
        )
        fetched_at = _now_iso()
        try:
            a = _fetch_candles(
                symbol_a, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            b = _fetch_candles(
                symbol_b, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.rolling_correlation(a, b, window=window)
        except Exception:
            _log_tool_error(
                "analyze_rolling_correlation failed %s/%s", symbol_a, symbol_b,
            )
            raise
        if tail is not None and tail > 0 and "correlation" in result:
            result["datetime"] = result["datetime"][-tail:]
            result["correlation"] = result["correlation"][-tail:]
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbols": [symbol_a, symbol_b],
            **result,
        }

    @mcp.tool()
    def analyze_mean_reversion(
        symbol: str,
        period_type: str = "year",
        period: int = 2,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
        hurst_max_lag: int = 20,
        variance_ratio_lags: list[int] | None = None,
    ) -> dict[str, Any]:
        """Trend vs mean-reversion regime label from Hurst + variance
        ratio.

        Hurst exponent (``> 0.5`` trending, ``< 0.5`` mean reverting,
        ``≈ 0.5`` random walk) and Lo-MacKinlay variance ratios at
        several lags (``> 1`` trending, ``< 1`` mean reverting) are
        combined into a regime label: ``"trending"`` /
        ``"weak_trend"`` / ``"random_walk"`` / ``"weak_mean_revert"`` /
        ``"mean_reverting"``. Use this to *bias strategy choice*, not
        as a trade signal in isolation — these are estimators, not
        statistical tests.
        """
        lags = tuple(variance_ratio_lags) if variance_ratio_lags else (2, 5, 10, 20)
        logger.info(
            "analyze_mean_reversion symbol=%s hurst_lag=%d vr_lags=%s",
            symbol, hurst_max_lag, lags,
        )
        fetched_at = _now_iso()
        try:
            candles = _fetch_candles(
                symbol, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.mean_reversion_score(
                candles,
                hurst_max_lag=hurst_max_lag,
                variance_ratio_lags=lags,
            )
        except Exception:
            _log_tool_error("analyze_mean_reversion failed symbol=%s", symbol)
            raise
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbol": symbol,
            **result,
        }

    @mcp.tool()
    def analyze_atr_stop_levels(
        symbol: str,
        entry_price: float,
        side: str = "long",
        atr_period: int = 14,
        stop_atr_multiplier: float = 1.5,
        target_atr_multiplier: float = 3.0,
        period_type: str = "year",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date: int | None = None,
        end_date: int | None = None,
        need_extended_hours_data: bool = False,
    ) -> dict[str, Any]:
        """ATR-based stop loss and target for a hypothetical entry.

        Computes Wilder's ATR over the last ``atr_period`` bars then
        projects stop / target as multiples of ATR away from
        ``entry_price``. Bridge from raw TA into RISK.md-style sizing —
        pair ``risk_per_unit`` with the user's per-trade dollar risk
        budget to derive position size client-side.

        Args:
            symbol: Ticker.
            entry_price: Hypothetical entry. Use the user's intended
                limit price, not necessarily the current close.
            side: ``"long"`` or ``"short"``.
            atr_period: Wilder smoothing period. 14 conventional; 20
                also common on daily charts.
            stop_atr_multiplier: 1.5–2.0 typical for swing trades; 1.0
                for tighter intraday work.
            target_atr_multiplier: Projects the take-profit. R/R is
                ``target_atr_multiplier / stop_atr_multiplier``.
            period_type, period, frequency_type, frequency, start_date,
            end_date, need_extended_hours_data: forwarded to
                ``get_price_history``. Pick a frequency that matches
                the trade timeframe — ATR on 1-min bars is not
                comparable to ATR on daily bars.
        """
        logger.info(
            "analyze_atr_stop_levels symbol=%s entry=%.4f side=%s atr=%d sm=%.2f tm=%.2f",
            symbol, entry_price, side, atr_period,
            stop_atr_multiplier, target_atr_multiplier,
        )
        fetched_at = _now_iso()
        try:
            candles = _fetch_candles(
                symbol, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.atr_stop_levels(
                candles,
                entry_price=entry_price,
                side=side,
                atr_period=atr_period,
                stop_atr_multiplier=stop_atr_multiplier,
                target_atr_multiplier=target_atr_multiplier,
            )
        except Exception:
            _log_tool_error("analyze_atr_stop_levels failed symbol=%s", symbol)
            raise
        return {
            "source": _src("/marketdata/v1/pricehistory"),
            "fetched_at": fetched_at,
            "symbol": symbol,
            **result,
        }
