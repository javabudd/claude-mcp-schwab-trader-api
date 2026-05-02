"""Shared option-chain analytics.

Provider-agnostic helpers that operate on the generic option-chain
shape every market-data backend in this repo emits — top-level
``underlyingPrice`` / ``symbol`` / ``isDelayed`` plus ``callExpDateMap``
and ``putExpDateMap`` keyed by ``"YYYY-MM-DD:dte"`` → strike (string)
→ list of contract dicts. Both ``schwab`` and ``yahoo`` providers
consume them; nothing here imports from a specific provider.
"""
from . import summary

__all__ = ["summary"]
