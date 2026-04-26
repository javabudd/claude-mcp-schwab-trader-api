"""Quant analytics over OHLCV candles.

Pure numpy. No scipy/pandas. All functions accept the candle list
shape every market-data backend in this repo emits
(``[{open, high, low, close, volume, datetime}, ...]`` with
``datetime`` in epoch ms UTC) so they compose cleanly with the
existing fetch path.

Annualization factor is inferred from the median bar spacing unless
``annualization`` is passed explicitly. For irregular or intraday bars
where the inference is noisy, pass a value (e.g. daily=252, weekly=52,
monthly=12, 1-min RTH≈98280).
"""
from __future__ import annotations

import math
from datetime import date as _date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

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
    include_drawdown_series: bool = False,
) -> dict[str, Any]:
    """Summary performance/risk stats for one instrument.

    ``risk_free_rate`` is an annualized simple rate (e.g. 0.05 for 5%).
    Set ``include_drawdown_series=True`` to also return the per-bar
    equity curve and drawdown series — useful for charting or finding
    the underwater periods, but pricier on the wire for long histories.
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
    max_dd_idx = int(np.argmin(drawdown))
    peak_idx = int(np.argmax(equity[: max_dd_idx + 1])) if max_dd_idx > 0 else 0
    calmar = ann_return / abs(max_dd) if max_dd < 0 else float("nan")

    out: dict[str, Any] = {
        "n_bars": len(candles),
        "annualization": ann,
        "total_return": total_return,
        "ann_return": ann_return,
        "ann_volatility": ann_vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "max_drawdown_peak_datetime": candles[peak_idx]["datetime"],
        "max_drawdown_trough_datetime": candles[max_dd_idx]["datetime"],
        "calmar": calmar,
        "skew": _moment(log_ret, 3),
        "excess_kurtosis": _moment(log_ret, 4) - 3.0 if log_ret.size else float("nan"),
        "start_close": float(closes[0]),
        "end_close": float(closes[-1]),
    }
    if include_drawdown_series:
        out["datetime"] = [c["datetime"] for c in candles]
        out["equity_curve"] = equity.tolist()
        out["drawdown_series"] = drawdown.tolist()
    return _jsonify(out)


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
        "series_source": source,
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


# ---------- session ranges (Asia / London / New York) -----------------


_UTC = ZoneInfo("UTC")


def _parse_hm(hm: str) -> time:
    h, m = hm.split(":")
    return time(int(h), int(m))


def _session_day(
    t_local: datetime,
    start: time,
    end: time,
) -> _date | None:
    """Trading date a bar contributes to for a given session window, or
    None if the bar falls outside the window.

    Sessions whose clock range wraps midnight (e.g. Asia 18:00-03:00) are
    keyed to the date on which the session *ends* — so 22:00 on 2026-04-17
    and 01:00 on 2026-04-18 both belong to the 2026-04-18 Asia session,
    grouped with that day's London and New York sessions.
    """
    tt = t_local.time()
    d = t_local.date()
    if start <= end:
        return d if start <= tt < end else None
    if tt >= start:
        return d + timedelta(days=1)
    if tt < end:
        return d
    return None


def _bucket_agg(bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not bars:
        return None
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    return {
        "high": max(highs),
        "low": min(lows),
        "range": max(highs) - min(lows),
        "open": float(bars[0]["open"]),
        "close": float(bars[-1]["close"]),
        "n_bars": len(bars),
        "start": int(bars[0]["datetime"]),
        "end": int(bars[-1]["datetime"]),
    }


def session_ranges(
    candles: list[dict[str, Any]],
    asia_start: str = "18:00",
    asia_end: str = "03:00",
    london_start: str = "03:00",
    london_end: str = "08:00",
    ny_start: str = "08:00",
    ny_end: str = "17:00",
    timezone: str = "America/New_York",
    tight_lookback: int = 5,
    tight_multiplier: float = 0.7,
) -> dict[str, Any]:
    """Per-day Asia / London / New York session ranges with a tight-Asia
    flag and a London-sweeps-Asia signal.

    Requires intraday candles with extended-hours coverage — the Asia
    session (defaults to 18:00-03:00 ET) lies entirely outside US RTH.

    Session assignment: London and New York are keyed by the bar's local
    date. Asia wraps midnight, so bars from the prior evening (>= the
    Asia start time) are grouped with bars from the following morning
    (< the Asia end time) under that following day's date.

    The *tight Asia* flag compares the current session's range to the
    rolling median of the prior ``tight_lookback`` Asia ranges. ``True``
    when ``range < baseline * tight_multiplier``; ``None`` until the
    lookback is filled. This is a pragmatic default, not a canonical
    ICT definition — override the parameters if you want a different
    convention (e.g. compare to ATR, or use a fixed dollar threshold
    client-side).

    *London sweep* flags use the strict liquidity-sweep definition: the
    London session traded past the Asia high/low AND closed back inside
    the Asia range. A pure breakout (took the level and closed beyond
    it) is NOT flagged as a sweep.
    """
    if not candles:
        return {"error": "no candles"}
    tz = ZoneInfo(timezone)
    as_s, as_e = _parse_hm(asia_start), _parse_hm(asia_end)
    lo_s, lo_e = _parse_hm(london_start), _parse_hm(london_end)
    ny_s, ny_e = _parse_hm(ny_start), _parse_hm(ny_end)

    buckets: dict[_date, dict[str, list[dict[str, Any]]]] = {}
    windows = (
        ("asia", as_s, as_e),
        ("london", lo_s, lo_e),
        ("new_york", ny_s, ny_e),
    )
    for c in candles:
        t_local = datetime.fromtimestamp(int(c["datetime"]) / 1000.0, tz=_UTC).astimezone(tz)
        for name, start, end in windows:
            d = _session_day(t_local, start, end)
            if d is None:
                continue
            day = buckets.setdefault(d, {"asia": [], "london": [], "new_york": []})
            day[name].append(c)

    prior_asia_ranges: list[float] = []
    days_out: list[dict[str, Any]] = []
    for d in sorted(buckets):
        day = buckets[d]
        asia = _bucket_agg(day["asia"])
        london = _bucket_agg(day["london"])
        new_york = _bucket_agg(day["new_york"])

        if asia is not None:
            if tight_lookback > 0 and len(prior_asia_ranges) >= tight_lookback:
                baseline = float(np.median(prior_asia_ranges[-tight_lookback:]))
                asia["tight_baseline"] = baseline
                asia["tight"] = asia["range"] < baseline * tight_multiplier
            else:
                asia["tight_baseline"] = None
                asia["tight"] = None
            prior_asia_ranges.append(asia["range"])

        if london is not None and asia is not None:
            swept_high = london["high"] > asia["high"] and london["close"] < asia["high"]
            swept_low = london["low"] < asia["low"] and london["close"] > asia["low"]
            london["swept_asia_high"] = swept_high
            london["swept_asia_low"] = swept_low
            sides = []
            if swept_high:
                sides.append("high")
            if swept_low:
                sides.append("low")
            london["sweep"] = sides or None

        days_out.append({
            "date": d.isoformat(),
            "asia": asia,
            "london": london,
            "new_york": new_york,
        })

    return _jsonify({
        "timezone": timezone,
        "sessions": {
            "asia": f"{asia_start}-{asia_end}",
            "london": f"{london_start}-{london_end}",
            "new_york": f"{ny_start}-{ny_end}",
        },
        "tight_params": {
            "lookback": tight_lookback,
            "multiplier": tight_multiplier,
        },
        "n_days": len(days_out),
        "days": days_out,
    })


# ---------- support / resistance --------------------------------------


def _swing_pivots(
    highs: np.ndarray,
    lows: np.ndarray,
    swing_window: int,
) -> tuple[list[int], list[int]]:
    """Fractal swing-high / swing-low indices.

    A bar at index i is a swing high when ``highs[i]`` is strictly
    greater than the highs of the ``swing_window`` bars on either side.
    Same idea for swing lows. The leading and trailing
    ``swing_window`` bars cannot be swings (no full window on one side)
    so they're skipped.
    """
    n = highs.size
    if n < 2 * swing_window + 1:
        return [], []
    sh: list[int] = []
    sl: list[int] = []
    for i in range(swing_window, n - swing_window):
        h = highs[i]
        l = lows[i]
        if h > highs[i - swing_window : i].max() and h > highs[i + 1 : i + swing_window + 1].max():
            sh.append(i)
        if l < lows[i - swing_window : i].min() and l < lows[i + 1 : i + swing_window + 1].min():
            sl.append(i)
    return sh, sl


def support_resistance(
    candles: list[dict[str, Any]],
    swing_window: int = 5,
    max_swings: int = 10,
    prior_high: float | None = None,
    prior_low: float | None = None,
    prior_close: float | None = None,
) -> dict[str, Any]:
    """Recent swing highs / lows plus pivot-point levels.

    *Swings* use a symmetric fractal: a bar is a swing high when its
    high is strictly above the highs of ``swing_window`` bars on each
    side. Returns up to ``max_swings`` of each, most recent first, with
    ``bars_ago`` so the model can weight them by recency.

    *Pivot points* (classic, Fibonacci, and Camarilla variants) are
    derived from a prior session's high / low / close. **Pass them
    explicitly via** ``prior_high`` / ``prior_low`` / ``prior_close``.
    For daily candles the standard convention is yesterday's H/L/C; for
    intraday candles, pass the prior daily session's H/L/C — the
    function does not infer the daily session boundary from intraday
    bars. If any of the three are omitted, pivots are skipped from the
    response (swings still returned).

    Pivot formulas:

    - Classic: ``P = (H+L+C)/3``; ``R1 = 2P - L``;
      ``S1 = 2P - H``; ``R2 = P + (H-L)``; ``S2 = P - (H-L)``;
      ``R3 = H + 2(P - L)``; ``S3 = L - 2(H - P)``.
    - Fibonacci: ``P`` as above; ``R1/S1 = P ± 0.382 * (H-L)``;
      ``R2/S2 = P ± 0.618 * (H-L)``; ``R3/S3 = P ± 1.000 * (H-L)``.
    - Camarilla: ``R1 = C + 1.1/12 * (H-L)``;
      ``R2 = C + 1.1/6 * (H-L)``; ``R3 = C + 1.1/4 * (H-L)``;
      ``R4 = C + 1.1/2 * (H-L)``; ``S{n}`` is the mirror with ``-``.
    """
    if not candles:
        return {"error": "no candles"}
    highs = np.array([c["high"] for c in candles], dtype=float)
    lows = np.array([c["low"] for c in candles], dtype=float)
    n = len(candles)

    sh_idx, sl_idx = _swing_pivots(highs, lows, swing_window)
    sh_idx = sh_idx[-max_swings:][::-1]
    sl_idx = sl_idx[-max_swings:][::-1]
    swing_highs = [
        {"datetime": candles[i]["datetime"], "price": float(highs[i]), "bars_ago": n - 1 - i}
        for i in sh_idx
    ]
    swing_lows = [
        {"datetime": candles[i]["datetime"], "price": float(lows[i]), "bars_ago": n - 1 - i}
        for i in sl_idx
    ]

    out: dict[str, Any] = {
        "n_bars": n,
        "swing_window": swing_window,
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
    }

    if prior_high is not None and prior_low is not None and prior_close is not None:
        h, l, c = float(prior_high), float(prior_low), float(prior_close)
        rng = h - l
        p = (h + l + c) / 3.0
        out["pivot_inputs"] = {"high": h, "low": l, "close": c, "range": rng}
        out["classic_pivots"] = {
            "P": p,
            "R1": 2.0 * p - l,
            "S1": 2.0 * p - h,
            "R2": p + rng,
            "S2": p - rng,
            "R3": h + 2.0 * (p - l),
            "S3": l - 2.0 * (h - p),
        }
        out["fibonacci_pivots"] = {
            "P": p,
            "R1": p + 0.382 * rng,
            "S1": p - 0.382 * rng,
            "R2": p + 0.618 * rng,
            "S2": p - 0.618 * rng,
            "R3": p + 1.000 * rng,
            "S3": p - 1.000 * rng,
        }
        out["camarilla_pivots"] = {
            "R1": c + 1.1 / 12.0 * rng,
            "S1": c - 1.1 / 12.0 * rng,
            "R2": c + 1.1 / 6.0 * rng,
            "S2": c - 1.1 / 6.0 * rng,
            "R3": c + 1.1 / 4.0 * rng,
            "S3": c - 1.1 / 4.0 * rng,
            "R4": c + 1.1 / 2.0 * rng,
            "S4": c - 1.1 / 2.0 * rng,
        }
    return _jsonify(out)


# ---------- anchored VWAP ---------------------------------------------


def _parse_anchor_to_ms(anchor: int | str) -> int:
    if isinstance(anchor, int):
        return anchor
    s = str(anchor)
    # Accept ``YYYY-MM-DD`` or ISO datetime; anchors earlier than the
    # first candle pin to the first candle.
    if "T" not in s and " " not in s and len(s) == 10:
        dt = datetime.fromisoformat(s).replace(tzinfo=_UTC)
    else:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
    return int(dt.timestamp() * 1000.0)


def anchored_vwap(
    candles: list[dict[str, Any]],
    anchor: int | str | None = None,
) -> dict[str, Any]:
    """Volume-weighted average price anchored to a point in the series.

    Cumulative ``sum(typical_price * volume) / sum(volume)`` from
    ``anchor`` forward, where typical price is ``(H+L+C)/3``. Common
    institutional reference price; often acts as soft S/R because
    large positions accumulated near an anchor (event date, gap day,
    earnings) are flat at VWAP.

    Args:
        candles: ``[{open, high, low, close, volume, datetime}, ...]``.
        anchor: ``None`` (anchor at first candle), an epoch-ms ``int``,
            or an ISO date / datetime string (UTC). Anchors earlier
            than the first candle pin to the first candle; anchors
            after the last candle return ``error``.

    Returns:
        ``{"anchor_datetime", "anchor_index", "n_bars", "datetime", "vwap",
        "current_vwap", "current_close", "deviation"}`` where
        ``deviation`` is ``(close - vwap) / vwap`` (signed).
    """
    if not candles:
        return {"error": "no candles"}
    n = len(candles)
    times = np.array([int(c["datetime"]) for c in candles], dtype=np.int64)

    if anchor is None:
        start = 0
    else:
        anchor_ms = _parse_anchor_to_ms(anchor)
        idx = int(np.searchsorted(times, anchor_ms, side="left"))
        if idx >= n:
            return {"error": "anchor is after the last candle"}
        start = idx

    sub = candles[start:]
    highs = np.array([c["high"] for c in sub], dtype=float)
    lows = np.array([c["low"] for c in sub], dtype=float)
    closes = np.array([c["close"] for c in sub], dtype=float)
    vols = np.array([c["volume"] for c in sub], dtype=float)
    typical = (highs + lows + closes) / 3.0

    cum_pv = np.cumsum(typical * vols)
    cum_v = np.cumsum(vols)
    vwap = np.where(cum_v > 0, cum_pv / np.maximum(cum_v, 1e-12), np.nan)

    current_vwap = float(vwap[-1]) if vwap.size and math.isfinite(vwap[-1]) else float("nan")
    current_close = float(closes[-1]) if closes.size else float("nan")
    deviation = (
        (current_close - current_vwap) / current_vwap
        if math.isfinite(current_vwap) and current_vwap != 0
        else float("nan")
    )

    return _jsonify({
        "anchor_datetime": int(times[start]),
        "anchor_index": start,
        "n_bars": int(vwap.size),
        "datetime": times[start:].tolist(),
        "vwap": vwap.tolist(),
        "current_vwap": current_vwap,
        "current_close": current_close,
        "deviation": deviation,
    })


# ---------- Donchian channels -----------------------------------------


def donchian_channels(
    candles: list[dict[str, Any]],
    period: int = 20,
) -> dict[str, Any]:
    """Donchian channel: rolling max-of-high / min-of-low / midline.

    Classic breakout/range visualization. Upper = highest high over the
    last ``period`` bars; lower = lowest low; middle = midpoint. The
    current bar contributes to its own window (so a fresh new high
    appears as ``close == upper``).

    Returns aligned series plus a ``position`` label for the last bar:
    ``"above_upper"`` when ``close > upper`` (rare; it usually equals),
    ``"at_upper"`` / ``"at_lower"`` when within 0.05% of the boundary,
    ``"in_band"`` otherwise.
    """
    if not candles:
        return {"error": "no candles"}
    n = len(candles)
    if n < period:
        return {"error": f"need at least {period} candles"}

    highs = np.array([c["high"] for c in candles], dtype=float)
    lows = np.array([c["low"] for c in candles], dtype=float)
    closes = np.array([c["close"] for c in candles], dtype=float)
    times = [c["datetime"] for c in candles]

    upper: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    middle: list[float | None] = [None] * n
    for i in range(period - 1, n):
        u = float(highs[i - period + 1 : i + 1].max())
        l = float(lows[i - period + 1 : i + 1].min())
        upper[i] = u
        lower[i] = l
        middle[i] = (u + l) / 2.0

    cu = upper[-1]
    cl = lower[-1]
    cm = middle[-1]
    cc = float(closes[-1])
    tol = 5e-4
    if cu is None or cl is None:
        position = "unknown"
    elif cc > cu:
        position = "above_upper"
    elif cc < cl:
        position = "below_lower"
    elif cu and abs(cc - cu) / cu < tol:
        position = "at_upper"
    elif cl and abs(cc - cl) / cl < tol:
        position = "at_lower"
    else:
        position = "in_band"

    return _jsonify({
        "period": period,
        "n_bars": n,
        "datetime": times,
        "upper": upper,
        "lower": lower,
        "middle": middle,
        "current_upper": cu,
        "current_lower": cl,
        "current_middle": cm,
        "current_close": cc,
        "position": position,
    })


# ---------- mean-reversion / trend regime -----------------------------


def _hurst_exponent(log_prices: np.ndarray, max_lag: int) -> float:
    """Hurst exponent via simple lag-of-std scaling.

    For lags k = 2..max_lag, compute std of (log_p[t] - log_p[t-k]).
    Slope of log(std) on log(k) is the Hurst exponent. ``H > 0.5``:
    persistent / trending. ``H < 0.5``: anti-persistent / mean-
    reverting. ``H = 0.5``: random walk. Crude estimator (R/S analysis
    is more rigorous) but well-known and good enough for a regime
    label.
    """
    if log_prices.size < max_lag + 2:
        return float("nan")
    lags = np.arange(2, max_lag + 1)
    stds = []
    for k in lags:
        diffs = log_prices[k:] - log_prices[:-k]
        s = float(np.std(diffs, ddof=1)) if diffs.size > 1 else float("nan")
        stds.append(s)
    arr = np.array(stds, dtype=float)
    valid = (arr > 0) & np.isfinite(arr)
    if valid.sum() < 3:
        return float("nan")
    x = np.log(lags[valid].astype(float))
    y = np.log(arr[valid])
    slope, _ = np.polyfit(x, y, 1)
    return float(slope)


def _variance_ratio(log_returns: np.ndarray, q: int) -> float:
    """Lo-MacKinlay variance ratio at lag q.

    VR(q) = Var(r_q) / (q * Var(r_1)). VR > 1 indicates positive serial
    correlation (trending), VR < 1 indicates negative (mean reverting),
    VR = 1 is random-walk consistent.
    """
    if log_returns.size < q + 2:
        return float("nan")
    var1 = float(np.var(log_returns, ddof=1))
    if var1 == 0:
        return float("nan")
    rq = np.array([log_returns[i : i + q].sum() for i in range(log_returns.size - q + 1)])
    if rq.size < 2:
        return float("nan")
    varq = float(np.var(rq, ddof=1))
    return varq / (q * var1)


def mean_reversion_score(
    candles: list[dict[str, Any]],
    hurst_max_lag: int = 20,
    variance_ratio_lags: tuple[int, ...] = (2, 5, 10, 20),
) -> dict[str, Any]:
    """Regime label: trending vs mean-reverting vs random walk.

    Combines two estimators on log prices/returns:

    - **Hurst exponent** (lag-of-std scaling). ``H ≈ 0.5`` is random
      walk, ``> 0.5`` trending, ``< 0.5`` mean reverting.
    - **Variance ratio** at several lags. ``VR > 1`` trending,
      ``VR < 1`` mean reverting, ``VR ≈ 1`` random walk.

    These are estimators, not tests — they don't reject random walk
    at any p-value. Use the regime label to *bias strategy choice*
    (trend-follow vs revert), not to decide a trade in isolation.

    Args:
        candles: ``[{open, high, low, close, volume, datetime}, ...]``.
        hurst_max_lag: max lag for the Hurst regression. Higher needs
            more data; default 20 wants ~22 bars minimum.
        variance_ratio_lags: lags to evaluate VR at. Default
            ``(2, 5, 10, 20)``.

    Returns:
        ``{"n_bars", "hurst_exponent", "variance_ratios": {q: vr},
        "mean_vr_excluding_1": ..., "regime": "trending" |
        "mean_reverting" | "random_walk", "interpretation": "..."}``.
    """
    if len(candles) < max(hurst_max_lag + 2, max(variance_ratio_lags) + 2):
        return {"error": "not enough candles for the requested lags"}
    closes = _closes(candles)
    log_prices = np.log(closes)
    log_ret = _log_returns(closes)

    h = _hurst_exponent(log_prices, hurst_max_lag)
    vrs = {int(q): _variance_ratio(log_ret, int(q)) for q in variance_ratio_lags}

    finite_vrs = [v for v in vrs.values() if math.isfinite(v)]
    mean_vr = float(np.mean(finite_vrs)) if finite_vrs else float("nan")

    # Combine: trending if both signals agree on trend; mean-reverting if
    # they agree on revert; otherwise random walk.
    trend_votes = 0
    revert_votes = 0
    if math.isfinite(h):
        if h > 0.55:
            trend_votes += 1
        elif h < 0.45:
            revert_votes += 1
    if math.isfinite(mean_vr):
        if mean_vr > 1.1:
            trend_votes += 1
        elif mean_vr < 0.9:
            revert_votes += 1

    if trend_votes >= 2:
        regime = "trending"
    elif revert_votes >= 2:
        regime = "mean_reverting"
    elif trend_votes > revert_votes:
        regime = "weak_trend"
    elif revert_votes > trend_votes:
        regime = "weak_mean_revert"
    else:
        regime = "random_walk"

    interp = {
        "trending": "Both Hurst and VR indicate persistence. Trend-following bias may pay; fading rallies/dips is risky.",
        "weak_trend": "One estimator suggests trend, the other is neutral. Soft trend bias.",
        "random_walk": "No clear regime. Don't lean on either trend or mean-revert assumptions.",
        "weak_mean_revert": "One estimator suggests mean reversion, the other is neutral. Soft revert bias.",
        "mean_reverting": "Both Hurst and VR indicate anti-persistence. Mean-reversion bias; trend-following likely whipsaws.",
    }[regime]

    return _jsonify({
        "n_bars": len(candles),
        "hurst_exponent": h,
        "variance_ratios": vrs,
        "mean_vr_excluding_1": mean_vr,
        "regime": regime,
        "interpretation": interp,
    })


# ---------- ATR-based stop / target ladder ----------------------------


def _wilder_atr(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int,
) -> float:
    """Wilder's ATR over the last ``period`` bars.

    Computed manually so this module stays pure-numpy. TR_t = max(H-L,
    |H-C_prev|, |L-C_prev|). ATR is Wilder's smoothed average:
    seed = mean of first ``period`` TRs, then
    ATR_t = (ATR_{t-1} * (period-1) + TR_t) / period.
    """
    n = highs.size
    if n < period + 1:
        return float("nan")
    prev_close = closes[:-1]
    h = highs[1:]
    l = lows[1:]
    tr = np.maximum.reduce([h - l, np.abs(h - prev_close), np.abs(l - prev_close)])
    if tr.size < period:
        return float("nan")
    atr = float(tr[:period].mean())
    for i in range(period, tr.size):
        atr = (atr * (period - 1) + float(tr[i])) / period
    return atr


def atr_stop_levels(
    candles: list[dict[str, Any]],
    entry_price: float,
    side: str = "long",
    atr_period: int = 14,
    stop_atr_multiplier: float = 1.5,
    target_atr_multiplier: float = 3.0,
) -> dict[str, Any]:
    """ATR-based stop loss and target for a hypothetical entry.

    Computes Wilder's ATR over the last ``atr_period`` bars in the
    candle history, then projects stop / target as multiples of ATR
    away from ``entry_price`` in the appropriate direction. Bridges
    raw TA into RISK.md sizing — given an account risk budget the
    caller can derive position size from ``risk_per_unit``.

    Args:
        candles: ``[{open, high, low, close, volume, datetime}, ...]``.
            Must contain at least ``atr_period + 1`` bars.
        entry_price: Hypothetical entry. Use the user's intended limit
            price, not necessarily the current close.
        side: ``"long"`` or ``"short"``.
        atr_period: Wilder smoothing period. 14 is the conventional
            default; 20 is also common for daily charts.
        stop_atr_multiplier: Stop is ``stop_atr_multiplier * ATR`` away
            from entry. 1.5–2.0 is typical for swing trades; 1.0 for
            tighter intraday work.
        target_atr_multiplier: Target is ``target_atr_multiplier * ATR``
            away from entry on the favorable side. R/R is
            ``target_atr_multiplier / stop_atr_multiplier``.

    Returns:
        ``{"side", "entry_price", "atr", "atr_period", "stop_loss",
        "target", "risk_per_unit", "reward_per_unit",
        "risk_reward_ratio", "current_close",
        "current_distance_to_entry_atr", ...}``. Distances are in ATRs
        so the model can sanity-check whether the entry is already
        near the stop or extended past the target.
    """
    if entry_price <= 0:
        return {"error": "entry_price must be positive"}
    side = side.lower()
    if side not in ("long", "short"):
        raise ValueError(f"unknown side: {side!r}")
    if len(candles) < atr_period + 1:
        return {"error": f"need at least {atr_period + 1} candles"}

    highs = np.array([c["high"] for c in candles], dtype=float)
    lows = np.array([c["low"] for c in candles], dtype=float)
    closes = np.array([c["close"] for c in candles], dtype=float)
    atr = _wilder_atr(highs, lows, closes, atr_period)
    if not math.isfinite(atr) or atr <= 0:
        return {"error": "ATR computation failed (zero or non-finite)"}

    stop_offset = stop_atr_multiplier * atr
    target_offset = target_atr_multiplier * atr
    if side == "long":
        stop = entry_price - stop_offset
        target = entry_price + target_offset
    else:
        stop = entry_price + stop_offset
        target = entry_price - target_offset

    risk = abs(entry_price - stop)
    reward = abs(target - entry_price)
    rr = reward / risk if risk > 0 else float("nan")
    current_close = float(closes[-1])
    distance_atr = (current_close - entry_price) / atr

    return _jsonify({
        "side": side,
        "entry_price": float(entry_price),
        "atr": atr,
        "atr_period": atr_period,
        "stop_atr_multiplier": stop_atr_multiplier,
        "target_atr_multiplier": target_atr_multiplier,
        "stop_loss": float(stop),
        "target": float(target),
        "risk_per_unit": float(risk),
        "reward_per_unit": float(reward),
        "risk_reward_ratio": rr,
        "current_close": current_close,
        "current_distance_to_entry_atr": float(distance_atr),
        "n_bars": len(candles),
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
    "session_ranges",
    "support_resistance",
    "anchored_vwap",
    "donchian_channels",
    "mean_reversion_score",
    "atr_stop_levels",
]
