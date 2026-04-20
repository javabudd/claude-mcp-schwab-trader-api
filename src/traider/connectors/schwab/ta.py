"""TA-Lib indicator runner over Schwab OHLCV candles.

Thin wrapper around :mod:`talib.abstract` so the MCP tool surface
doesn't have to know about each indicator individually. Callers pass a
list of ``{"name": "SMA", "timeperiod": 20, ...}`` dicts; we dispatch
by name, forward kwargs, and return JSON-safe aligned series.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np


_INPUT_KEYS = ("open", "high", "low", "close", "volume")


def _load_talib_abstract():
    """Import :mod:`talib.abstract` lazily so the MCP server still
    starts (for non-TA tools) when TA-Lib isn't installed."""
    try:
        from talib import abstract as talib_abstract
    except ImportError as exc:
        raise ImportError(
            "TA-Lib is not installed. Install the C library (e.g. "
            "`conda install -c conda-forge ta-lib`) and the Python "
            "wrapper (`pip install TA-Lib`)."
        ) from exc
    return talib_abstract


def _candles_to_inputs(candles: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    """Turn Schwab's candle list into the dict TA-Lib's abstract API wants."""
    if not candles:
        return {k: np.array([], dtype=float) for k in _INPUT_KEYS}
    return {
        "open": np.array([c["open"] for c in candles], dtype=float),
        "high": np.array([c["high"] for c in candles], dtype=float),
        "low": np.array([c["low"] for c in candles], dtype=float),
        "close": np.array([c["close"] for c in candles], dtype=float),
        "volume": np.array([c["volume"] for c in candles], dtype=float),
    }


def _nan_to_none(values: Iterable[float]) -> list[float | None]:
    """JSON has no NaN — TA-Lib warmup slots become ``null``."""
    return [None if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v) for v in values]


def _run_one(
    inputs: dict[str, np.ndarray],
    spec: dict[str, Any],
    talib_abstract,
) -> tuple[str, Any]:
    if "name" not in spec:
        raise ValueError(f"indicator spec missing 'name': {spec!r}")
    name = str(spec["name"]).upper()
    label = str(spec.get("label", name))
    kwargs = {k: v for k, v in spec.items() if k not in ("name", "label")}

    try:
        fn = talib_abstract.Function(name)
    except Exception as exc:
        raise ValueError(f"unknown TA-Lib indicator: {name!r}") from exc

    raw = fn(inputs, **kwargs)

    output_names = list(fn.output_names)
    if isinstance(raw, tuple) or len(output_names) > 1:
        arrays = raw if isinstance(raw, tuple) else (raw,)
        value: Any = {
            out_name: _nan_to_none(arr.tolist())
            for out_name, arr in zip(output_names, arrays)
        }
    else:
        value = _nan_to_none(raw.tolist())
    return label, value


def _tail(value: Any, n: int) -> Any:
    if isinstance(value, dict):
        return {k: v[-n:] for k, v in value.items()}
    return value[-n:]


def run_indicators(
    candles: list[dict[str, Any]],
    indicators: list[dict[str, Any]],
    tail: int | None = None,
) -> dict[str, Any]:
    """Compute TA-Lib indicators on a Schwab candle list.

    Args:
        candles: ``[{open, high, low, close, volume, datetime}, ...]``
            as returned by :func:`SchwabClient.get_price_history`.
        indicators: list of indicator spec dicts. Each must have
            ``name`` (TA-Lib function name, case-insensitive); other
            keys are forwarded as kwargs to that function. Optional
            ``label`` renames the output entry so callers can request
            the same indicator with different parameters.
        tail: if set, trim each returned series (and ``datetime``) to
            the last N points.

    Returns:
        ``{"datetime": [...epoch ms...], "indicators": {label: series}}``
        where ``series`` is either a ``list[float | None]`` for
        single-output indicators or a ``dict[output_name, list]`` for
        multi-output ones (MACD, BBANDS, STOCH, ...).
    """
    talib_abstract = _load_talib_abstract()
    inputs = _candles_to_inputs(candles)
    datetimes = [c.get("datetime") for c in candles]

    results: dict[str, Any] = {}
    for spec in indicators:
        label, value = _run_one(inputs, spec, talib_abstract)
        results[label] = value

    if tail is not None and tail > 0:
        datetimes = datetimes[-tail:]
        results = {k: _tail(v, tail) for k, v in results.items()}

    return {"datetime": datetimes, "indicators": results}
