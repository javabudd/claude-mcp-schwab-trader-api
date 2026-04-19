"""MCP server exposing read-only Yahoo Finance lookups to Claude.

Tool surface mirrors :mod:`schwab_connector.server` so prompts are
portable between the two backends. See ``AGENTS.md`` in this directory
for the places Yahoo's data model forces a divergence (accounts,
market hours).
"""
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
from .ta import run_indicators
from .yahoo_client import YahooClient

logger = logging.getLogger("yahoo_connector")

mcp = FastMCP(
    "yahoo-connector",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)
_client: YahooClient | None = None


def _get_client() -> YahooClient:
    global _client
    if _client is None:
        logger.info("initializing Yahoo client")
        _client = YahooClient.from_env()
        atexit.register(_client.close)
        logger.info("Yahoo client ready")
    return _client


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
    Schwab connector's: ``{"symbol": ..., "empty": bool, "candles":
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
    connector's docs for the spec-dict grammar — it's identical here.
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
    raises; switch to ``TRAIDER_BACKEND=schwab`` (or another broker
    backend) for session hours.
    """
    logger.info("get_market_hours markets=%s date=%s (unsupported)", markets, date)
    return _get_client().get_market_hours(markets, date=date)


@mcp.tool()
def get_accounts(include_positions: bool = False) -> list[dict[str, Any]]:
    """Not supported by the Yahoo backend.

    Yahoo is a market-data source, not a brokerage. This tool raises;
    use the Schwab backend for account/position data.
    """
    logger.info("get_accounts include_positions=%s (unsupported)", include_positions)
    return _get_client().get_accounts(include_positions=include_positions)


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
    max drawdown, Calmar, skew, excess kurtosis."""
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
    """Pearson correlation matrix of log returns across ``symbols``."""
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
    ``benchmark``."""
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
    """Classify current realized vol against its trailing distribution."""
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
    """Rolling z-score of ``source`` (``close`` or ``log_return``)."""
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
    """Log-price spread between two symbols with a rolling z-score."""
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
        "yahoo_connector",
        "mcp",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "yfinance",
    ):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)


def main() -> None:
    parser = argparse.ArgumentParser(prog="yahoo-connector")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--log-file",
        default=os.environ.get("YAHOO_CONNECTOR_LOG", "logs/server.log"),
        help="Path to server log file (default: logs/server.log in cwd).",
    )
    args = parser.parse_args()

    _configure_logging(Path(args.log_file).resolve())
    logger.info(
        "yahoo-connector starting transport=%s host=%s port=%s log=%s",
        args.transport, args.host, args.port, args.log_file,
    )

    if args.transport in ("streamable-http", "sse"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
