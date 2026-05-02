"""HTTP client for the Schwab Trader API (read-only market data)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("traider.schwab.client")

SCHWAB_API_BASE = "https://api.schwabapi.com"
SCHWAB_TOKEN_URL = f"{SCHWAB_API_BASE}/v1/oauth/token"
SCHWAB_AUTHORIZE_URL = f"{SCHWAB_API_BASE}/v1/oauth/authorize"

DEFAULT_TOKEN_FILE = Path.home() / ".schwab-connector" / "schwab-token.json"

# Access tokens live ~30min; refresh a bit before expiry so in-flight
# calls don't race the boundary.
_TOKEN_REFRESH_SLACK = 60.0

# Map the RTD-flavored field names the MCP tools historically accepted
# to the JSON keys Schwab actually returns. Unknown keys pass through,
# so callers can also request native Schwab keys directly.
_FIELD_ALIASES = {
    "LAST": "lastPrice",
    "BID": "bidPrice",
    "ASK": "askPrice",
    "VOLUME": "totalVolume",
    "MARK": "mark",
    "OPEN": "openPrice",
    "HIGH": "highPrice",
    "LOW": "lowPrice",
    "CLOSE": "closePrice",
    "NET_CHANGE": "netChange",
    "PERCENT_CHANGE": "netPercentChange",
    "BID_SIZE": "bidSize",
    "ASK_SIZE": "askSize",
}


class SchwabAuthError(RuntimeError):
    """Raised when the user needs to re-run the interactive OAuth flow."""


class SchwabClient:
    """Schwab Trader API client.

    Tokens are loaded from ``token_file`` and auto-refreshed on expiry.
    If the refresh token is itself invalid (expired or revoked),
    ``SchwabAuthError`` is raised — re-run ``traider auth schwab``.
    """

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        token_file: Path = DEFAULT_TOKEN_FILE,
        base_url: str = SCHWAB_API_BASE,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._token_file = token_file
        self._base_url = base_url.rstrip("/")
        self._http = http_client or httpx.Client(timeout=30.0)
        self._lock = threading.Lock()
        self._tokens: dict[str, Any] | None = None

    @classmethod
    def from_env(cls) -> "SchwabClient":
        app_key = os.environ.get("SCHWAB_APP_KEY")
        app_secret = os.environ.get("SCHWAB_APP_SECRET")
        if not app_key or not app_secret:
            raise RuntimeError(
                "Missing SCHWAB_APP_KEY / SCHWAB_APP_SECRET env vars."
            )
        base = os.environ.get("SCHWAB_BASE_URL", SCHWAB_API_BASE)
        token_file = Path(
            os.environ.get("SCHWAB_TOKEN_FILE", str(DEFAULT_TOKEN_FILE))
        )
        return cls(app_key, app_secret, token_file=token_file, base_url=base)

    # ----- public API --------------------------------------------------

    def get_quote(self, symbol: str, field: str = "LAST") -> Any:
        """Return a single field for one symbol.

        Returns the value (typically a number) or None if the field is
        not present on the quote.
        """
        logger.info("get_quote symbol=%s field=%s", symbol, field)
        quote = self._fetch_quotes([symbol]).get(symbol, {}).get("quote", {})
        return _extract_field(quote, field)

    def get_quotes(
        self,
        symbols: list[str],
        fields: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return ``{symbol: {field: value}}`` for many symbols in one call.

        If ``fields`` is None, the full Schwab quote payload is returned
        per symbol (the ``quote`` sub-object from the API response).
        """
        logger.info("get_quotes symbols=%s fields=%s", symbols, fields)
        body = self._fetch_quotes(symbols)
        if fields is None:
            return {sym: entry.get("quote", {}) for sym, entry in body.items()}
        out: dict[str, dict[str, Any]] = {}
        for sym, entry in body.items():
            quote = entry.get("quote", {})
            out[sym] = {f: _extract_field(quote, f) for f in fields}
        return out

    def get_price_history(
        self,
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
        """Return OHLCV candles for ``symbol``.

        Thin wrapper over ``/marketdata/v1/pricehistory``. Defaults give
        one year of daily bars. Dates, if provided, are epoch
        milliseconds and take precedence over ``period``.

        Valid combinations (per Schwab):
            period_type=day    period=1,2,3,4,5,10   frequency_type=minute  frequency=1,5,10,15,30
            period_type=month  period=1,2,3,6        frequency_type=daily|weekly
            period_type=year   period=1,2,3,5,10,15,20  frequency_type=daily|weekly|monthly
            period_type=ytd    period=1              frequency_type=daily|weekly
        """
        logger.info(
            "get_price_history symbol=%s period=%s%s frequency=%s%s",
            symbol, period, period_type, frequency, frequency_type,
        )
        params: dict[str, Any] = {
            "symbol": symbol,
            "periodType": period_type,
            "frequencyType": frequency_type,
            "frequency": frequency,
            "needExtendedHoursData": str(need_extended_hours_data).lower(),
            "needPreviousClose": str(need_previous_close).lower(),
        }
        if start_date is not None or end_date is not None:
            if start_date is None:
                raise ValueError(
                    "get_price_history: end_date set without start_date. "
                    "Schwab's /pricehistory requires startDate whenever "
                    "endDate is provided; pass both, or pass period."
                )
            params["startDate"] = start_date
            params["endDate"] = (
                end_date if end_date is not None else int(time.time() * 1000)
            )
        else:
            params["period"] = period
        return self._get_json("/marketdata/v1/pricehistory", params=params)

    def get_option_chain(
        self,
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
        """Option chain from ``/marketdata/v1/chains``.

        Native Schwab response: ``{"symbol", "status",
        "underlying", "strategy", "callExpDateMap",
        "putExpDateMap", ...}``. Each exp-date map is keyed by
        ``"YYYY-MM-DD:dte"`` then by strike, and each strike holds
        a list of contracts with bid/ask/last/mark, volume, OI, IV,
        delta/gamma/theta/vega/rho, intrinsic/extrinsic value, and
        the 21-char OSI symbol.

        Args:
            symbol: Underlying ticker (e.g. ``"SPY"``).
            contract_type: ``CALL``, ``PUT``, or ``ALL``.
            strike_count: Number of strikes to return above and below
                the at-the-money strike.
            include_underlying_quote: Include the underlying's quote
                block alongside the chain.
            strategy: ``SINGLE`` (default) or one of ``ANALYTICAL``,
                ``COVERED``, ``VERTICAL``, ``CALENDAR``, ``STRANGLE``,
                ``STRADDLE``, ``BUTTERFLY``, ``CONDOR``, ``DIAGONAL``,
                ``COLLAR``, ``ROLL``. ``ANALYTICAL`` enables the
                ``volatility``/``underlying_price``/``interest_rate``/
                ``days_to_expiration`` overrides.
            interval: Strike spacing for ``ANALYTICAL``.
            strike: Return only contracts at this exact strike.
            range_: ``ITM``, ``NTM``, ``OTM``, ``SAK`` (strikes above
                mkt), ``SBK`` (below), ``SNK`` (near), or ``ALL``.
            from_date, to_date: ``YYYY-MM-DD`` bounds on expiration.
            volatility, underlying_price, interest_rate,
            days_to_expiration: overrides for ``ANALYTICAL``.
            exp_month: ``JAN``..``DEC`` or ``ALL``.
            option_type: ``S`` (standard), ``NS`` (non-standard),
                or ``ALL``.
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "contractType": contract_type,
            "strategy": strategy,
            "includeUnderlyingQuote": str(include_underlying_quote).lower(),
        }
        if strike_count is not None:
            params["strikeCount"] = strike_count
        if interval is not None:
            params["interval"] = interval
        if strike is not None:
            params["strike"] = strike
        if range_ is not None:
            params["range"] = range_
        if from_date is not None:
            params["fromDate"] = from_date
        if to_date is not None:
            params["toDate"] = to_date
        if volatility is not None:
            params["volatility"] = volatility
        if underlying_price is not None:
            params["underlyingPrice"] = underlying_price
        if interest_rate is not None:
            params["interestRate"] = interest_rate
        if days_to_expiration is not None:
            params["daysToExpiration"] = days_to_expiration
        if exp_month is not None:
            params["expMonth"] = exp_month
        if option_type is not None:
            params["optionType"] = option_type
        return self._get_json("/marketdata/v1/chains", params=params)

    def get_option_expirations(self, symbol: str) -> dict[str, Any]:
        """Expiration-series list from ``/marketdata/v1/expirationchain``.

        Returns ``{"status", "expirationList": [{"expirationDate",
        "daysToExpiration", "expirationType", "settlementType",
        "optionRoots", "standard"}, ...]}``. Use this to discover
        available expirations before pulling a full chain slice.
        """
        return self._get_json(
            "/marketdata/v1/expirationchain", params={"symbol": symbol}
        )

    def get_movers(
        self,
        index: str,
        sort: str | None = None,
        frequency: int | None = None,
    ) -> dict[str, Any]:
        """Top movers for an index from ``/marketdata/v1/movers/{index}``.

        Args:
            index: ``$DJI``, ``$COMPX``, ``$SPX``, ``NYSE``, ``NASDAQ``,
                ``OTCBB``, ``INDEX_ALL``, ``EQUITY_ALL``, ``OPTION_ALL``,
                ``OPTION_PUT``, ``OPTION_CALL``.
            sort: ``VOLUME``, ``TRADES``, ``PERCENT_CHANGE_UP``, or
                ``PERCENT_CHANGE_DOWN``.
            frequency: Only report movers with at least this many minutes
                of activity (``0``, ``1``, ``5``, ``10``, ``30``, ``60``).
        """
        params: dict[str, Any] = {}
        if sort is not None:
            params["sort"] = sort
        if frequency is not None:
            params["frequency"] = frequency
        return self._get_json(f"/marketdata/v1/movers/{index}", params=params)

    def search_instruments(
        self,
        symbol: str,
        projection: str = "symbol-search",
    ) -> dict[str, Any]:
        """Instrument search / fundamentals via ``/marketdata/v1/instruments``.

        Args:
            symbol: Symbol, CUSIP, or regex/description depending on
                ``projection``.
            projection: ``symbol-search`` (exact), ``symbol-regex``,
                ``desc-search`` (description contains), ``desc-regex``,
                ``search``, or ``fundamental`` (adds fundamentals block).
        """
        return self._get_json(
            "/marketdata/v1/instruments",
            params={"symbol": symbol, "projection": projection},
        )

    def get_market_hours(
        self,
        markets: list[str] | str,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Market hours from ``/marketdata/v1/markets``.

        Args:
            markets: One or more of ``equity``, ``option``, ``bond``,
                ``future``, ``forex``. A list is joined with commas.
            date: ``YYYY-MM-DD``. Defaults to today server-side.
        """
        params: dict[str, Any] = {
            "markets": ",".join(markets) if isinstance(markets, list) else markets
        }
        if date is not None:
            params["date"] = date
        return self._get_json("/marketdata/v1/markets", params=params)

    def get_account_numbers(self) -> list[dict[str, Any]]:
        """Map of plaintext account numbers to the hashed IDs used by
        every other ``/trader/v1/accounts/*`` endpoint."""
        data = self._get_json("/trader/v1/accounts/accountNumbers")
        return data if isinstance(data, list) else []

    def get_accounts(self, include_positions: bool = False) -> list[dict[str, Any]]:
        """All accounts for the authorized user.

        Args:
            include_positions: If ``True``, each account includes its
                ``positions`` array (cost basis, quantities, market
                value, unrealized P&L).
        """
        params: dict[str, Any] = {}
        if include_positions:
            params["fields"] = "positions"
        data = self._get_json("/trader/v1/accounts", params=params)
        return data if isinstance(data, list) else []

    def get_account(
        self,
        account_hash: str,
        include_positions: bool = False,
    ) -> dict[str, Any]:
        """Single account by hashed ID (from :meth:`get_account_numbers`)."""
        params: dict[str, Any] = {}
        if include_positions:
            params["fields"] = "positions"
        return self._get_json(
            f"/trader/v1/accounts/{account_hash}", params=params
        )

    def get_transactions(
        self,
        account_hash: str,
        start_date: str,
        end_date: str,
        symbol: str | None = None,
        types: str | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Transaction history from
        ``/trader/v1/accounts/{hash}/transactions``.

        Args:
            account_hash: Hashed account ID (from
                :meth:`get_account_numbers`).
            start_date: Lower bound. ``YYYY-MM-DD`` (expanded to
                ``00:00:00.000Z``) or full ISO-8601 UTC datetime.
            end_date: Upper bound. ``YYYY-MM-DD`` (expanded to
                ``23:59:59.999Z``) or full ISO-8601 UTC datetime.
            symbol: Filter by symbol. Options accept the 21-char OSI
                form.
            types: Filter by transaction type(s). Accepts a single type
                name, a list, or a comma-separated string. See Schwab
                docs for the full vocabulary — common values are
                ``TRADE``, ``RECEIVE_AND_DELIVER``,
                ``DIVIDEND_OR_INTEREST``, ``ACH_RECEIPT``,
                ``ACH_DISBURSEMENT``, ``CASH_RECEIPT``,
                ``CASH_DISBURSEMENT``, ``ELECTRONIC_FUND``,
                ``WIRE_IN``, ``WIRE_OUT``, ``JOURNAL``,
                ``MEMORANDUM``, ``MARGIN_CALL``, ``MONEY_MARKET``,
                ``SMA_ADJUSTMENT``.
        """
        start_iso = _normalize_iso_datetime(start_date, end_of_day=False)
        end_iso = _normalize_iso_datetime(end_date, end_of_day=True)
        span = datetime.fromisoformat(end_iso.replace("Z", "+00:00")) - datetime.fromisoformat(
            start_iso.replace("Z", "+00:00")
        )
        if span > timedelta(days=365):
            raise ValueError(
                f"Schwab transactions window is capped at 365 days; got "
                f"{span.days}d between start_date={start_date!r} and "
                f"end_date={end_date!r}. Narrow the range and retry."
            )
        params: dict[str, Any] = {"startDate": start_iso, "endDate": end_iso}
        if symbol is not None:
            params["symbol"] = symbol
        if types is not None:
            params["types"] = (
                ",".join(types) if isinstance(types, list) else types
            )
        data = self._get_json(
            f"/trader/v1/accounts/{account_hash}/transactions", params=params
        )
        return data if isinstance(data, list) else []

    def get_transaction(
        self,
        account_hash: str,
        transaction_id: str | int,
    ) -> dict[str, Any]:
        """Single transaction by ID from
        ``/trader/v1/accounts/{hash}/transactions/{id}``."""
        return self._get_json(
            f"/trader/v1/accounts/{account_hash}/transactions/{transaction_id}"
        )

    def get_orders(
        self,
        account_hash: str,
        from_entered_time: str,
        to_entered_time: str,
        max_results: int | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Orders for one account from
        ``/trader/v1/accounts/{hash}/orders``.

        Returns every order entered in the window regardless of final
        state (WORKING, FILLED, CANCELED, REPLACED, ...). Filter with
        ``status`` to narrow (e.g. ``"WORKING"`` for open/resting
        orders). Schwab caps ``from_entered_time`` at ~60 days ago.

        Args:
            account_hash: Hashed account ID (from
                :meth:`get_account_numbers`).
            from_entered_time: Lower bound on ``enteredTime``.
                ``YYYY-MM-DD`` (expanded to ``00:00:00.000Z``) or full
                ISO-8601 UTC datetime.
            to_entered_time: Upper bound on ``enteredTime``.
                ``YYYY-MM-DD`` (expanded to ``23:59:59.999Z``) or full
                ISO-8601 UTC datetime.
            max_results: Server-side cap on rows returned (Schwab
                default 3000).
            status: Optional status filter. Schwab's vocabulary:
                ``AWAITING_PARENT_ORDER``, ``AWAITING_CONDITION``,
                ``AWAITING_STOP_CONDITION``, ``AWAITING_MANUAL_REVIEW``,
                ``ACCEPTED``, ``AWAITING_UR_OUT``,
                ``PENDING_ACTIVATION``, ``QUEUED``, ``WORKING``,
                ``REJECTED``, ``PENDING_CANCEL``, ``CANCELED``,
                ``PENDING_REPLACE``, ``REPLACED``, ``FILLED``,
                ``EXPIRED``, ``NEW``, ``AWAITING_RELEASE_TIME``,
                ``PENDING_ACKNOWLEDGEMENT``, ``PENDING_RECALL``,
                ``UNKNOWN``.
        """
        from_iso = _normalize_iso_datetime(from_entered_time, end_of_day=False)
        to_iso = _normalize_iso_datetime(to_entered_time, end_of_day=True)
        from_dt = datetime.fromisoformat(from_iso.replace("Z", "+00:00"))
        lookback = datetime.now(timezone.utc) - from_dt
        if lookback > timedelta(days=60):
            raise ValueError(
                f"Schwab orders lookback is capped at 60 days; "
                f"from_entered_time={from_entered_time!r} is {lookback.days}d "
                f"before now. Move it forward and retry."
            )
        params: dict[str, Any] = {
            "fromEnteredTime": from_iso,
            "toEnteredTime": to_iso,
        }
        if max_results is not None:
            params["maxResults"] = max_results
        if status is not None:
            params["status"] = status
        data = self._get_json(
            f"/trader/v1/accounts/{account_hash}/orders", params=params
        )
        return data if isinstance(data, list) else []

    def get_order(
        self,
        account_hash: str,
        order_id: str | int,
    ) -> dict[str, Any]:
        """Single order by ID from
        ``/trader/v1/accounts/{hash}/orders/{orderId}``."""
        return self._get_json(
            f"/trader/v1/accounts/{account_hash}/orders/{order_id}"
        )

    def close(self) -> None:
        self._http.close()

    # ----- token / auth internals --------------------------------------

    def _fetch_quotes(self, symbols: list[str]) -> dict[str, Any]:
        return self._get_json(
            "/marketdata/v1/quotes", params={"symbols": ",".join(symbols)}
        )

    def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET ``path`` with bearer auth; on 401, drop the cached token and
        retry once before raising."""
        url = f"{self._base_url}{path}"
        r = self._http.get(url, params=params, headers=self._auth_headers())
        if r.status_code == 401:
            logger.info("401 on %s; forcing token refresh", path)
            with self._lock:
                self._tokens = None
            r = self._http.get(url, params=params, headers=self._auth_headers())
        r.raise_for_status()
        return r.json()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def _access_token(self) -> str:
        with self._lock:
            tokens = self._load_tokens()
            if time.time() >= tokens.get("expires_at", 0) - _TOKEN_REFRESH_SLACK:
                tokens = self._refresh(tokens["refresh_token"])
            return tokens["access_token"]

    def _load_tokens(self) -> dict[str, Any]:
        if self._tokens is None:
            if not self._token_file.exists():
                raise SchwabAuthError(
                    f"No tokens at {self._token_file}. "
                    "Run: traider auth schwab"
                )
            with self._token_file.open("r", encoding="utf-8") as f:
                self._tokens = json.load(f)
        return self._tokens

    def _save_tokens(self, tokens: dict[str, Any]) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._token_file.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2)
        os.replace(tmp, self._token_file)
        try:
            os.chmod(self._token_file, 0o600)
        except OSError:
            # Best-effort; Windows filesystems may reject chmod.
            pass
        self._tokens = tokens

    def _refresh(self, refresh_token: str) -> dict[str, Any]:
        logger.info("refreshing access token")
        r = self._http.post(
            SCHWAB_TOKEN_URL,
            auth=(self._app_key, self._app_secret),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            logger.error(
                "token refresh failed status=%s body=%s",
                r.status_code, r.text[:500],
            )
            raise SchwabAuthError(
                "Token refresh failed. Re-run: traider auth schwab"
            )
        body = r.json()
        tokens = {
            "access_token": body["access_token"],
            # Schwab rotates the refresh token on most refreshes; fall
            # back to the old one if the response omits it.
            "refresh_token": body.get("refresh_token", refresh_token),
            "expires_at": time.time() + int(body.get("expires_in", 1800)),
            "token_type": body.get("token_type", "Bearer"),
        }
        self._save_tokens(tokens)
        return tokens


def _normalize_iso_datetime(value: str, end_of_day: bool = False) -> str:
    """Accept ``YYYY-MM-DD`` or full ISO-8601; return full ISO-8601 UTC.

    Schwab's transaction endpoints require ``startDate`` / ``endDate`` in
    ISO-8601 form with milliseconds and a trailing ``Z``, e.g.
    ``2024-01-01T00:00:00.000Z``. Pure-date inputs expand to the start of
    day (or end of day when ``end_of_day=True``) to preserve the
    intuitive "April 1 through April 21 inclusive" semantics most
    callers expect.
    """
    if "T" in value:
        return value
    suffix = "T23:59:59.999Z" if end_of_day else "T00:00:00.000Z"
    return f"{value}{suffix}"


def _extract_field(quote: dict[str, Any], field: str) -> Any:
    if field in quote:
        return quote[field]
    alias = _FIELD_ALIASES.get(field.upper())
    if alias is not None and alias in quote:
        return quote[alias]
    return None
