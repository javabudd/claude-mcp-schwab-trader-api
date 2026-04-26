"""Shared OHLCV analytics + TA-Lib indicator runner.

Pure-numpy quant analytics and a thin TA-Lib wrapper that operate on
the candle-list shape every market-data backend in this repo emits:
``[{open, high, low, close, volume, datetime}, ...]`` with ``datetime``
as epoch ms UTC.

These utilities are stateless and provider-agnostic. Both ``schwab``
and ``yahoo`` providers (and any future market-data backend) consume
them the same way; nothing here imports from a specific provider.
"""
from . import analytics, ta

__all__ = ["analytics", "ta"]
