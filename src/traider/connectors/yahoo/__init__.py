"""Yahoo connector: free market data via yfinance, no account needed.

Mutually exclusive with the ``schwab`` profile — both bind the same
market-data tool surface and only one can be active at a time.
``get_accounts`` and ``get_market_hours`` raise here; switch to the
schwab profile for those.
"""
