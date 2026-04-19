"""MCP server exposing read-only Schwab quote lookups to Claude."""
from __future__ import annotations

import argparse
import atexit
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import analytics
from .schwab_client import SchwabClient
from .ta import run_indicators

logger = logging.getLogger("schwab_connector")

mcp = FastMCP(
    "schwab-connector",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)
_client: SchwabClient | None = None


def _get_client() -> SchwabClient:
    global _client
    if _client is None:
        logger.info("initializing Schwab client")
        _client = SchwabClient.from_env()
        atexit.register(_client.close)
        logger.info("Schwab client ready")
    return _client


@mcp.tool()
def get_quote(symbol: str, field: str = "LAST") -> str:
    """Return a single field for one symbol.

    Args:
        symbol: Ticker (e.g. ``"SPY"``, ``"AAPL"``, ``"/ES"``).
        field: Either a friendly alias (``LAST``, ``BID``, ``ASK``,
            ``VOLUME``, ``MARK``, ``OPEN``, ``HIGH``, ``LOW``, ``CLOSE``,
            ``NET_CHANGE``, ``PERCENT_CHANGE``, ``BID_SIZE``,
            ``ASK_SIZE``) or a native Schwab quote key (e.g.
            ``lastPrice``).
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
    """Return many fields for many symbols in one call.

    Returns a nested mapping ``{symbol: {field: value}}``. If ``fields``
    is omitted, each symbol's entry is the full Schwab ``quote`` object.
    """
    logger.info("get_quotes symbols=%s fields=%s", symbols, fields)
    try:
        results = _get_client().get_quotes(symbols, fields)
    except Exception:
        logger.exception("get_quotes failed symbols=%s fields=%s", symbols, fields)
        raise
    logger.info("get_quotes result=%r", results)
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
        need_extended_hours_data: Include pre/post-market candles.
        need_previous_close: Include the prior session's close in the
            response.
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
    return {"symbol": symbol, **result}


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
def get_option_expirations(symbol: str) -> dict[str, Any]:
    """Expiration series list for an underlying.

    Returns ``{"status", "expirationList": [{"expirationDate",
    "daysToExpiration", "expirationType", "settlementType",
    "optionRoots", "standard"}, ...]}``. Use this to discover
    available expirations before pulling a full chain slice.
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
    """Session hours for one or more markets.

    Args:
        markets: Any of ``equity``, ``option``, ``bond``, ``future``,
            ``forex``.
        date: ``YYYY-MM-DD``. Defaults to today.
    """
    logger.info("get_market_hours markets=%s date=%s", markets, date)
    try:
        result = _get_client().get_market_hours(markets, date=date)
    except Exception:
        logger.exception("get_market_hours failed markets=%s", markets)
        raise
    return result


@mcp.tool()
def get_accounts(include_positions: bool = False) -> list[dict[str, Any]]:
    """Authorized accounts (read-only).

    Args:
        include_positions: Include each account's ``positions`` array
            (quantity, cost basis, market value, unrealized P&L).
    """
    logger.info("get_accounts include_positions=%s", include_positions)
    try:
        result = _get_client().get_accounts(include_positions=include_positions)
    except Exception:
        logger.exception("get_accounts failed")
        raise
    logger.info("get_accounts result count=%d", len(result))
    return result


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
    max drawdown, Calmar, skew, excess kurtosis.

    Price-history params match ``get_price_history``. ``risk_free_rate``
    is annualized (e.g. ``0.05``). ``annualization`` overrides the
    periods-per-year inferred from bar spacing — set it for intraday
    bars if the inferred value looks wrong.
    """
    logger.info("analyze_returns symbol=%s", symbol)
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
    return {"symbol": symbol, **result}


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
    try:
        candles_by_symbol = {
            sym: _fetch_candles(
                sym, period_type, period, frequency_type, frequency,
                start_date, end_date, need_extended_hours_data,
            )
            for sym in symbols
        }
        return analytics.correlation_matrix(candles_by_symbol)
    except Exception:
        logger.exception("analyze_correlation failed symbols=%s", symbols)
        raise


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
    return {"symbol": symbol, "benchmark": benchmark, **result}


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
    return {"symbol": symbol, **result}


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
    return {"symbol": symbol, **result}


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
    for name in (
        "",
        "schwab_connector",
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
    parser = argparse.ArgumentParser(prog="schwab-connector")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--log-file",
        default=os.environ.get("SCHWAB_CONNECTOR_LOG", "logs/server.log"),
        help="Path to server log file (default: logs/server.log in cwd).",
    )
    args = parser.parse_args()

    _configure_logging(Path(args.log_file).resolve())
    logger.info(
        "schwab-connector starting transport=%s host=%s port=%s log=%s",
        args.transport, args.host, args.port, args.log_file,
    )

    if args.transport in ("streamable-http", "sse"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
