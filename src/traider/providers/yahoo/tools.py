"""Yahoo market-data tools registered on the shared FastMCP.

Tool surface mirrors the ``schwab`` provider so prompts are portable
between the two backends. See this provider's ``AGENTS.md`` for the
places Yahoo's data model forces a divergence (accounts, market
hours).
"""
from __future__ import annotations

import atexit
import datetime as _dt
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from ...logging_utils import attach_provider_logger
from ...settings import TraiderSettings
from . import analytics
from .options_summary import summarize_chain
from .ta import run_indicators
from .yahoo_client import YahooClient

logger = logging.getLogger("traider.yahoo")
_client: YahooClient | None = None


def _get_client() -> YahooClient:
    global _client
    if _client is None:
        logger.info("initializing Yahoo client")
        _client = YahooClient.from_env()
        atexit.register(_client.close)
        logger.info("Yahoo client ready")
    return _client


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


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
    attach_provider_logger("traider.yahoo", settings.log_file("yahoo"))

    @mcp.tool()
    def get_quote(symbol: str, field: str = "LAST") -> str:
        """Return a single field for one symbol.

        Args:
            symbol: Ticker (e.g. ``"SPY"``, ``"AAPL"``, ``"^GSPC"``).
                Schwab-style index aliases (``$SPX``, ``$DJI``, ``$COMPX``)
                are translated to Yahoo's ``^`` form.
            field: Either a friendly alias (``LAST``, ``BID``, ``ASK``,
                ``VOLUME``, ``MARK``, ``OPEN``, ``HIGH``, ``LOW``, ``CLOSE``,
                ``NET_CHANGE``, ``PERCENT_CHANGE``, ``BID_SIZE``,
                ``ASK_SIZE``) or a native quote key.
        """
        logger.info("get_quote symbol=%s field=%s", symbol, field)
        try:
            value = _get_client().get_quote(symbol, field)
        except Exception:
            logger.exception("get_quote failed symbol=%s field=%s", symbol, field)
            raise
        logger.info("get_quote result symbol=%s field=%s value=%r", symbol, field, value)
        return "" if value is None else str(value)

    @mcp.tool()
    def get_quotes(
        symbols: list[str],
        fields: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return many fields for many symbols.

        Returns a nested mapping ``{symbol: {field: value}}``. If ``fields``
        is omitted, each symbol's entry is the full quote payload.
        """
        logger.info("get_quotes symbols=%s fields=%s", symbols, fields)
        try:
            results = _get_client().get_quotes(symbols, fields)
        except Exception:
            logger.exception("get_quotes failed symbols=%s fields=%s", symbols, fields)
            raise
        return results

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

        Defaults give one year of daily bars. Response shape matches the
        Schwab provider's: ``{"symbol": ..., "empty": bool, "candles":
        [{open, high, low, close, volume, datetime}, ...]}`` with
        ``datetime`` in epoch ms UTC.

        Args:
            symbol: Ticker (equities, ETFs, Yahoo ``^`` indices).
            period_type: ``day``, ``month``, ``year``, or ``ytd``.
            period: Count of ``period_type`` units back from today.
            frequency_type: ``minute``, ``daily``, ``weekly``, ``monthly``.
            frequency: 1/5/15/30 for ``minute``; 1 otherwise. Note Schwab
                supports 10-minute bars — Yahoo does not, and the backend
                raises rather than substituting a different resolution.
            start_date: Optional epoch ms. Overrides ``period``.
            end_date: Optional epoch ms. Defaults to now.
            need_extended_hours_data: Include pre/post-market bars.
            need_previous_close: Include the prior session's close.
        """
        logger.info(
            "get_price_history symbol=%s period=%s%s frequency=%s%s",
            symbol, period, period_type, frequency, frequency_type,
        )
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
            logger.exception("get_price_history failed symbol=%s", symbol)
            raise
        candles = result.get("candles", [])
        logger.info(
            "get_price_history result symbol=%s candles=%d empty=%s",
            symbol, len(candles), result.get("empty"),
        )
        return result

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
        """Run TA-Lib indicators on historical candles.

        Fetches candles via :func:`get_price_history` then dispatches each
        indicator spec through TA-Lib's abstract API. See the Schwab
        provider's docs for the spec-dict grammar — it's identical here.
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
            logger.exception("run_technical_analysis failed symbol=%s", symbol)
            raise
        logger.info(
            "run_technical_analysis result symbol=%s candles=%d labels=%s",
            symbol, len(candles), list(result["indicators"].keys()),
        )
        return {"symbol": symbol, "fetched_at": fetched_at, **result}

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
        """Schwab-shaped option chain for an underlying.

        Response shape matches the Schwab provider's ``get_option_chain``:
        ``callExpDateMap`` / ``putExpDateMap`` keyed by
        ``"YYYY-MM-DD:dte"`` → strike → list of contract dicts. **Yahoo
        does not publish Greeks**, so ``delta``/``gamma``/``theta``/
        ``vega``/``rho`` are ``null``; quotes are **~15min delayed** and
        illiquid strikes may show stale or zero bid/ask. A
        ``dataQualityWarning`` key is included on every response. For
        real-time quotes and Greeks, switch to the Schwab backend.

        Only ``strategy="SINGLE"`` is supported. Multi-leg / analytical
        strategies and the ``ANALYTICAL``-only numeric overrides raise
        rather than silently returning single-leg data.
        """
        logger.info(
            "get_option_chain symbol=%s type=%s strategy=%s",
            symbol, contract_type, strategy,
        )
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
            logger.exception("get_option_chain failed symbol=%s", symbol)
            raise
        return result

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
        ATM strike, ATM call and put legs (mark/bid/ask/IV/OI/volume),
        straddle cost, implied one-day move as percent, implied range,
        IV skew across ±``wings`` strikes around ATM, and the top
        ``top_n`` strikes by open interest and volume on each side.

        Use this instead of ``get_option_chain`` when you need the
        digestible view — the raw chain for a single expiration at
        ``strike_count=20`` can exceed 70k chars, which busts LLM
        context. The summary is bounded by ``wings`` and ``top_n`` and
        typically lands under 5k chars per expiration.

        Greeks are not computed here; the passthrough ``delta`` on ATM
        legs is whatever the backend emitted (``null`` for Yahoo). The
        Yahoo ``dataQualityWarning`` is preserved in the summary.

        Only ``strategy="SINGLE"`` chains are summarized — multi-leg and
        ``ANALYTICAL`` strategies would need a different shape and are
        not covered here.
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
            logger.exception("analyze_option_chain failed symbol=%s", symbol)
            raise
        summary["fetched_at"] = fetched_at
        return summary

    @mcp.tool()
    def get_option_expirations(symbol: str) -> dict[str, Any]:
        """Expiration series list for an underlying.

        Schwab-shaped response: ``{"status", "expirationList":
        [{"expirationDate", "daysToExpiration", "expirationType",
        "settlementType", "optionRoots", "standard"}, ...]}``. Yahoo
        exposes only the date list, so the metadata fields are ``null``
        — switch to Schwab for standard-vs-weekly/AM-vs-PM tagging.
        """
        logger.info("get_option_expirations symbol=%s", symbol)
        try:
            result = _get_client().get_option_expirations(symbol)
        except Exception:
            logger.exception("get_option_expirations failed symbol=%s", symbol)
            raise
        return result

    @mcp.tool()
    def get_movers(
        index: str,
        sort: str | None = None,
        frequency: int | None = None,
    ) -> dict[str, Any]:
        """Top movers via a Yahoo predefined screener.

        Unlike Schwab, Yahoo screeners are US-market-wide rather than scoped
        to a single index. ``sort`` selects the screener
        (``PERCENT_CHANGE_UP`` → ``day_gainers``, ``PERCENT_CHANGE_DOWN`` →
        ``day_losers``, ``VOLUME``/``TRADES`` → ``most_actives``). Pass a
        raw Yahoo screener key as ``index`` to bypass the mapping.
        ``frequency`` is accepted for signature parity and ignored.
        """
        logger.info("get_movers index=%s sort=%s frequency=%s", index, sort, frequency)
        try:
            result = _get_client().get_movers(index, sort=sort, frequency=frequency)
        except Exception:
            logger.exception("get_movers failed index=%s", index)
            raise
        return result

    @mcp.tool()
    def search_instruments(
        symbol: str,
        projection: str = "symbol-search",
    ) -> dict[str, Any]:
        """Instrument lookup or fundamentals.

        Args:
            symbol: Ticker or search fragment.
            projection: ``symbol-search`` / ``desc-search`` / ``search``
                all delegate to Yahoo's fuzzy symbol search.
                ``fundamental`` hydrates the ``Ticker.info`` block (PE,
                EPS, dividend yield, 52-week range, market cap, …).
        """
        logger.info("search_instruments symbol=%s projection=%s", symbol, projection)
        try:
            result = _get_client().search_instruments(symbol, projection=projection)
        except Exception:
            logger.exception("search_instruments failed symbol=%s", symbol)
            raise
        return result

    @mcp.tool()
    def get_market_hours(
        markets: list[str],
        date: str | None = None,
    ) -> dict[str, Any]:
        """Not supported by the Yahoo backend.

        Yahoo Finance has no authoritative market-hours endpoint. This tool
        raises; switch to the ``schwab`` provider (or another broker
        backend) for session hours.
        """
        logger.info("get_market_hours markets=%s date=%s (unsupported)", markets, date)
        return _get_client().get_market_hours(markets, date=date)

    @mcp.tool()
    def get_accounts(include_positions: bool = False) -> list[dict[str, Any]]:
        """Not supported by the Yahoo backend.

        Yahoo is a market-data source, not a brokerage. This tool raises;
        use the ``schwab`` provider for account/position data.
        """
        logger.info("get_accounts include_positions=%s (unsupported)", include_positions)
        return _get_client().get_accounts(include_positions=include_positions)

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
    ) -> dict[str, Any]:
        """Return/risk summary: total/annual return, vol, Sharpe, Sortino,
        max drawdown, Calmar, skew, excess kurtosis."""
        logger.info("analyze_returns symbol=%s", symbol)
        fetched_at = _now_iso()
        try:
            candles = _fetch_candles(
                symbol, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            result = analytics.returns_metrics(
                candles, risk_free_rate=risk_free_rate, annualization=annualization,
            )
        except Exception:
            logger.exception("analyze_returns failed symbol=%s", symbol)
            raise
        return {"symbol": symbol, "fetched_at": fetched_at, **result}

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
        """Pearson correlation matrix of log returns across ``symbols``."""
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
            logger.exception("analyze_correlation failed symbols=%s", symbols)
            raise
        return {"fetched_at": fetched_at, **result}

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
        ``benchmark``."""
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
            logger.exception("analyze_beta failed symbol=%s vs %s", symbol, benchmark)
            raise
        return {
            "symbol": symbol,
            "benchmark": benchmark,
            "fetched_at": fetched_at,
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
        """Classify current realized vol against its trailing distribution."""
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
            logger.exception("analyze_volatility_regime failed symbol=%s", symbol)
            raise
        return {"symbol": symbol, "fetched_at": fetched_at, **result}

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
        """Rolling z-score of ``source`` (``close`` or ``log_return``)."""
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
            logger.exception("analyze_zscore failed symbol=%s", symbol)
            raise
        if tail is not None and tail > 0 and "zscore" in result:
            result["datetime"] = result["datetime"][-tail:]
            result["zscore"] = result["zscore"][-tail:]
        return {"symbol": symbol, "fetched_at": fetched_at, **result}

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
        """Log-price spread between two symbols with a rolling z-score."""
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
            logger.exception("analyze_pair_spread failed %s/%s", symbol_a, symbol_b)
            raise
        if tail is not None and tail > 0 and "spread" in result:
            result["datetime"] = result["datetime"][-tail:]
            result["spread"] = result["spread"][-tail:]
            result["zscore"] = result["zscore"][-tail:]
        result["symbols"] = [symbol_a, symbol_b]
        result["fetched_at"] = fetched_at
        return result

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
        Note the yfinance intraday caps: 1-minute history ~7 days,
        sub-hourly ~60 days; pick ``period`` / ``frequency`` so the
        request fits.

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
            symbol: Ticker (equities, ETFs, Yahoo ``^`` indices).
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
            logger.exception("analyze_session_ranges failed symbol=%s", symbol)
            raise
        if tail is not None and tail > 0 and "days" in result:
            result["days"] = result["days"][-tail:]
            result["n_days"] = len(result["days"])
        return {"symbol": symbol, "fetched_at": fetched_at, **result}
