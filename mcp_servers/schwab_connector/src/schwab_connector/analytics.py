"""Quant analytics over Schwab OHLCV candles.

Pure numpy. No scipy/pandas. All functions accept the candle list shape
returned by :meth:`SchwabClient.get_price_history`
(``[{open, high, low, close, volume, datetime}, ...]``) so they compose
cleanly with the existing fetch path.

Annualization factor is inferred from the median bar spacing unless
``annualization`` is passed explicitly. For irregular or intraday bars
where the inference is noisy, pass a value (e.g. daily=252, weekly=52,
monthly=12, 1-min RTH≈98280).
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np


_MS_PER_DAY = 86_400_000.0
_RTH_MIN_PER_DAY = 390.0
_TRADING_DAYS = 252.0


def _closes(candles: list[dict[str, Any]]) -> np.ndarray:
    return np.array([c["close"] for c in candles], dtype=float)


def _log_returns(closes: np.ndarray) -> np.ndarray:
    if closes.size < 2:
        return np.array([], dtype=float)
    return np.diff(np.log(closes))


def _infer_annualization(candles: list[dict[str, Any]]) -> float:
    """Best-effort periods-per-year from candle timestamps."""
    if len(candles) < 2:
        return _TRADING_DAYS
    dts = np.array([c["datetime"] for c in candles], dtype=float)
    median_dt = float(np.median(np.diff(dts)))
    if median_dt <= 0:
        return _TRADING_DAYS
    if median_dt >= 0.9 * _MS_PER_DAY:
        # Daily or slower — scale 252 by (1 day / bar).
        return _TRADING_DAYS * (_MS_PER_DAY / median_dt)
    # Intraday — assume RTH-only bars.
    minutes_per_bar = median_dt / 60_000.0
    return _TRADING_DAYS * (_RTH_MIN_PER_DAY / minutes_per_bar)


def _safe_std(x: np.ndarray, ddof: int = 1) -> float:
    if x.size <= ddof:
        return float("nan")
    return float(np.std(x, ddof=ddof))


def _moment(x: np.ndarray, order: int) -> float:
    if x.size == 0:
        return float("nan")
    mu = float(np.mean(x))
    sd = _safe_std(x, ddof=0)
    if not math.isfinite(sd) or sd == 0:
        return float("nan")
    return float(np.mean(((x - mu) / sd) ** order))


def _jsonify(x: Any) -> Any:
    """NaN/inf → None so responses stay JSON-safe."""
    if isinstance(x, (list, tuple)):
        return [_jsonify(v) for v in x]
    if isinstance(x, dict):
        return {k: _jsonify(v) for k, v in x.items()}
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, np.floating):
        v = float(x)
        return v if math.isfinite(v) else None
    if isinstance(x, np.ndarray):
        return _jsonify(x.tolist())
    return x


# ---------- returns / risk --------------------------------------------


def returns_metrics(
    candles: list[dict[str, Any]],
    risk_free_rate: float = 0.0,
    annualization: float | None = None,
) -> dict[str, Any]:
    """Summary performance/risk stats for one instrument.

    ``risk_free_rate`` is an annualized simple rate (e.g. 0.05 for 5%).
    """
    if len(candles) < 2:
        return {"error": "need at least 2 candles"}
    closes = _closes(candles)
    log_ret = _log_returns(closes)
    ann = annualization if annualization is not None else _infer_annualization(candles)
    rf_per_period = risk_free_rate / ann
    excess = log_ret - rf_per_period

    mean_r = float(np.mean(log_ret))
    std_r = _safe_std(log_ret, ddof=1)
    downside = log_ret[log_ret < rf_per_period]
    down_std = _safe_std(downside, ddof=1) if downside.size > 1 else float("nan")

    total_return = float(closes[-1] / closes[0] - 1.0)
    ann_return = math.expm1(mean_r * ann)
    ann_vol = std_r * math.sqrt(ann) if math.isfinite(std_r) else float("nan")
    sharpe = (float(np.mean(excess)) / std_r) * math.sqrt(ann) if std_r and math.isfinite(std_r) else float("nan")
    sortino = (float(np.mean(excess)) / down_std) * math.sqrt(ann) if math.isfinite(down_std) and down_std > 0 else float("nan")

    equity = np.concatenate(([1.0], np.exp(np.cumsum(log_ret))))
    peak = np.maximum.accumulate(equity)
    drawdown = equity / peak - 1.0
    max_dd = float(drawdown.min())
    calmar = ann_return / abs(max_dd) if max_dd < 0 else float("nan")

    return _jsonify({
        "n_bars": len(candles),
        "annualization": ann,
        "total_return": total_return,
        "ann_return": ann_return,
        "ann_volatility": ann_vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "calmar": calmar,
        "skew": _moment(log_ret, 3),
        "excess_kurtosis": _moment(log_ret, 4) - 3.0 if log_ret.size else float("nan"),
        "start_close": float(closes[0]),
        "end_close": float(closes[-1]),
    })


def realized_volatility(
    candles: list[dict[str, Any]],
    method: str = "close_to_close",
    annualization: float | None = None,
) -> dict[str, Any]:
    """Annualized realized volatility.

    ``method``: ``close_to_close`` (default), ``parkinson``,
    ``garman_klass``, or ``rogers_satchell``.
    """
    if len(candles) < 2:
        return {"error": "need at least 2 candles"}
    ann = annualization if annualization is not None else _infer_annualization(candles)
    method = method.lower()

    if method == "close_to_close":
        var = float(np.var(_log_returns(_closes(candles)), ddof=1))
    else:
        highs = np.array([c["high"] for c in candles], dtype=float)
        lows = np.array([c["low"] for c in candles], dtype=float)
        opens = np.array([c["open"] for c in candles], dtype=float)
        closes = np.array([c["close"] for c in candles], dtype=float)
        hl = np.log(highs / lows)
        co = np.log(closes / opens)
        hc = np.log(highs / closes)
        ho = np.log(highs / opens)
        lc = np.log(lows / closes)
        lo = np.log(lows / opens)
        if method == "parkinson":
            var = float(np.mean(hl ** 2) / (4.0 * math.log(2.0)))
        elif method == "garman_klass":
            var = float(np.mean(0.5 * hl ** 2 - (2.0 * math.log(2.0) - 1.0) * co ** 2))
        elif method == "rogers_satchell":
            var = float(np.mean(hc * ho + lc * lo))
        else:
            raise ValueError(f"unknown realized-vol method: {method!r}")

    vol = math.sqrt(max(var, 0.0)) * math.sqrt(ann)
    return _jsonify({
        "method": method,
        "annualization": ann,
        "volatility": vol,
        "n_bars": len(candles),
    })


# ---------- cross-asset -----------------------------------------------


def _align_closes(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
) -> tuple[list[str], list[int], np.ndarray]:
    """Inner-join candle closes by datetime. Returns
    (symbols, datetimes, closes_matrix[n_bars, n_symbols])."""
    symbols = list(candles_by_symbol.keys())
    if not symbols:
        return [], [], np.zeros((0, 0))
    common: set[int] | None = None
    for cs in candles_by_symbol.values():
        ts = {int(c["datetime"]) for c in cs}
        common = ts if common is None else (common & ts)
    shared = sorted(common or set())
    if not shared:
        return symbols, [], np.zeros((0, len(symbols)))
    by_sym: dict[str, dict[int, float]] = {
        s: {int(c["datetime"]): float(c["close"]) for c in cs}
        for s, cs in candles_by_symbol.items()
    }
    mat = np.array(
        [[by_sym[s][t] for s in symbols] for t in shared],
        dtype=float,
    )
    return symbols, shared, mat


def correlation_matrix(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Pearson correlation of log returns across symbols.

    Timestamps are inner-joined across inputs first."""
    symbols, shared, closes = _align_closes(candles_by_symbol)
    if closes.shape[0] < 3:
        return {"error": "need at least 3 overlapping bars across all symbols"}
    rets = np.diff(np.log(closes), axis=0)
    corr = np.corrcoef(rets, rowvar=False)
    if corr.ndim == 0:
        corr = corr.reshape(1, 1)
    return _jsonify({
        "symbols": symbols,
        "n_bars": int(rets.shape[0]),
        "first_datetime": shared[0],
        "last_datetime": shared[-1],
        "matrix": corr.tolist(),
    })


def rolling_correlation(
    candles_a: list[dict[str, Any]],
    candles_b: list[dict[str, Any]],
    window: int = 30,
) -> dict[str, Any]:
    """Rolling Pearson correlation of log returns, window in bars."""
    _, shared, closes = _align_closes({"a": candles_a, "b": candles_b})
    if closes.shape[0] < window + 1:
        return {"error": f"need at least {window + 1} overlapping bars"}
    rets = np.diff(np.log(closes), axis=0)
    n = rets.shape[0]
    out = [None] * n
    for i in range(window - 1, n):
        a = rets[i - window + 1 : i + 1, 0]
        b = rets[i - window + 1 : i + 1, 1]
        sa, sb = a.std(ddof=1), b.std(ddof=1)
        if sa == 0 or sb == 0:
            continue
        out[i] = float(np.corrcoef(a, b)[0, 1])
    return _jsonify({
        "window": window,
        "datetime": shared[1:],
        "correlation": out,
    })


def beta(
    asset_candles: list[dict[str, Any]],
    benchmark_candles: list[dict[str, Any]],
    annualization: float | None = None,
) -> dict[str, Any]:
    """Beta / alpha / R² of ``asset`` vs ``benchmark`` on log returns."""
    _, shared, closes = _align_closes({"a": asset_candles, "b": benchmark_candles})
    if closes.shape[0] < 3:
        return {"error": "need at least 3 overlapping bars"}
    rets = np.diff(np.log(closes), axis=0)
    ra, rb = rets[:, 0], rets[:, 1]
    var_b = float(np.var(rb, ddof=1))
    if var_b == 0:
        return {"error": "benchmark variance is zero"}
    cov_ab = float(np.cov(ra, rb, ddof=1)[0, 1])
    b = cov_ab / var_b
    alpha_per_period = float(np.mean(ra) - b * np.mean(rb))
    corr = float(np.corrcoef(ra, rb)[0, 1])
    ann = annualization if annualization is not None else _infer_annualization(asset_candles)
    return _jsonify({
        "beta": b,
        "alpha_annualized": math.expm1(alpha_per_period * ann),
        "r_squared": corr * corr,
        "correlation": corr,
        "n_bars": int(rets.shape[0]),
        "first_datetime": shared[0],
        "last_datetime": shared[-1],
    })


# ---------- vol regime / z-score --------------------------------------


def _rolling_std(x: np.ndarray, window: int, ddof: int = 1) -> np.ndarray:
    n = x.size
    out = np.full(n, np.nan)
    if n < window:
        return out
    for i in range(window - 1, n):
        out[i] = np.std(x[i - window + 1 : i + 1], ddof=ddof)
    return out


def volatility_regime(
    candles: list[dict[str, Any]],
    short_window: int = 20,
    lookback: int = 252,
    annualization: float | None = None,
) -> dict[str, Any]:
    """Classify current realized vol against its trailing distribution.

    Rolling ``short_window``-bar close-to-close vol is z-scored and
    percentile-ranked against the most recent ``lookback`` bars of that
    rolling series.
    """
    if len(candles) < short_window + 2:
        return {"error": f"need at least {short_window + 2} candles"}
    log_ret = _log_returns(_closes(candles))
    ann = annualization if annualization is not None else _infer_annualization(candles)
    roll_sd = _rolling_std(log_ret, short_window, ddof=1)
    roll_vol = roll_sd * math.sqrt(ann)
    valid = roll_vol[np.isfinite(roll_vol)]
    if valid.size < 2:
        return {"error": "not enough rolling windows"}
    tail = valid[-lookback:] if valid.size > lookback else valid
    current = float(valid[-1])
    mu, sd = float(np.mean(tail)), float(np.std(tail, ddof=1))
    z = (current - mu) / sd if sd > 0 else float("nan")
    pct = float((tail <= current).sum()) / tail.size

    if not math.isfinite(z):
        label = "unknown"
    elif z < -1.0:
        label = "low"
    elif z < 1.0:
        label = "normal"
    elif z < 2.0:
        label = "elevated"
    else:
        label = "extreme"

    return _jsonify({
        "current_volatility": current,
        "lookback_mean": mu,
        "lookback_std": sd,
        "z_score": z,
        "percentile": pct,
        "regime": label,
        "short_window": short_window,
        "lookback": int(tail.size),
        "annualization": ann,
    })


def rolling_zscore(
    candles: list[dict[str, Any]],
    window: int = 20,
    source: str = "close",
) -> dict[str, Any]:
    """Rolling z-score of ``source`` (``close`` or ``log_return``)."""
    closes = _closes(candles)
    if source == "close":
        x = closes
        dts = [c["datetime"] for c in candles]
    elif source == "log_return":
        x = _log_returns(closes)
        dts = [c["datetime"] for c in candles[1:]]
    else:
        raise ValueError(f"unknown source: {source!r}")
    if x.size < window + 1:
        return {"error": f"need at least {window + 1} points"}
    out = [None] * x.size
    for i in range(window - 1, x.size):
        w = x[i - window + 1 : i + 1]
        mu = float(np.mean(w))
        sd = float(np.std(w, ddof=1))
        if sd > 0:
            out[i] = float((x[i] - mu) / sd)
    return _jsonify({
        "window": window,
        "source": source,
        "datetime": dts,
        "zscore": out,
    })


# ---------- pairs -----------------------------------------------------


def pair_spread(
    candles_a: list[dict[str, Any]],
    candles_b: list[dict[str, Any]],
    hedge_ratio: float | None = None,
    zscore_window: int = 60,
) -> dict[str, Any]:
    """Log-price spread between two instruments with a z-score signal.

    If ``hedge_ratio`` is omitted, OLS regresses ``log(a)`` on ``log(b)``
    over the full overlap. Spread is ``log(a) - hedge_ratio * log(b)``.
    ``zscore_window`` is used for the rolling z-score and half-life.
    """
    _, shared, closes = _align_closes({"a": candles_a, "b": candles_b})
    if closes.shape[0] < max(zscore_window + 2, 10):
        return {"error": "not enough overlapping bars"}
    log_a = np.log(closes[:, 0])
    log_b = np.log(closes[:, 1])

    if hedge_ratio is None:
        var_b = float(np.var(log_b, ddof=1))
        if var_b == 0:
            return {"error": "benchmark log-price variance is zero"}
        cov = float(np.cov(log_a, log_b, ddof=1)[0, 1])
        hedge_ratio = cov / var_b

    spread = log_a - hedge_ratio * log_b

    n = spread.size
    z_series: list[float | None] = [None] * n
    for i in range(zscore_window - 1, n):
        w = spread[i - zscore_window + 1 : i + 1]
        mu, sd = float(np.mean(w)), float(np.std(w, ddof=1))
        if sd > 0:
            z_series[i] = float((spread[i] - mu) / sd)

    # AR(1) half-life: dS_t = λ * S_{t-1} + ε → HL = -ln(2) / ln(1+λ)
    ds = np.diff(spread)
    sp_lag = spread[:-1]
    if sp_lag.size > 2 and float(np.var(sp_lag, ddof=1)) > 0:
        lam = float(np.cov(ds, sp_lag, ddof=1)[0, 1] / np.var(sp_lag, ddof=1))
        one_plus = 1.0 + lam
        half_life = -math.log(2.0) / math.log(one_plus) if 0 < one_plus < 1 else None
    else:
        half_life = None

    return _jsonify({
        "symbols": ["a", "b"],
        "hedge_ratio": hedge_ratio,
        "spread_mean": float(np.mean(spread)),
        "spread_std": float(np.std(spread, ddof=1)) if n > 1 else None,
        "current_spread": float(spread[-1]),
        "current_zscore": z_series[-1],
        "half_life_bars": half_life,
        "zscore_window": zscore_window,
        "datetime": shared,
        "spread": spread.tolist(),
        "zscore": z_series,
    })


__all__: Iterable[str] = [
    "returns_metrics",
    "realized_volatility",
    "correlation_matrix",
    "rolling_correlation",
    "beta",
    "volatility_regime",
    "rolling_zscore",
    "pair_spread",
]
