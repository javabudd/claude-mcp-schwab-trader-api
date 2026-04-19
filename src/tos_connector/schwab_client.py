"""HTTP client for the Schwab Trader API (read-only market data)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("tos_connector.schwab")

SCHWAB_API_BASE = "https://api.schwabapi.com"
SCHWAB_TOKEN_URL = f"{SCHWAB_API_BASE}/v1/oauth/token"
SCHWAB_AUTHORIZE_URL = f"{SCHWAB_API_BASE}/v1/oauth/authorize"

DEFAULT_TOKEN_FILE = Path.home() / ".tos-connector" / "schwab-token.json"

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
    ``SchwabAuthError`` is raised — re-run ``tos-connector auth``.
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
        self._http = http_client or httpx.Client(timeout=10.0)
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

    def close(self) -> None:
        self._http.close()

    # ----- token / auth internals --------------------------------------

    def _fetch_quotes(self, symbols: list[str]) -> dict[str, Any]:
        url = f"{self._base_url}/marketdata/v1/quotes"
        params = {"symbols": ",".join(symbols)}
        r = self._http.get(url, params=params, headers=self._auth_headers())
        if r.status_code == 401:
            # Access token rejected despite local expiry heuristic. Drop
            # the cached token and try once more.
            logger.info("401 on quote fetch; forcing token refresh")
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
                    "Run: tos-connector auth"
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
                "Token refresh failed. Re-run: tos-connector auth"
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


def _extract_field(quote: dict[str, Any], field: str) -> Any:
    if field in quote:
        return quote[field]
    alias = _FIELD_ALIASES.get(field.upper())
    if alias is not None and alias in quote:
        return quote[alias]
    return None
